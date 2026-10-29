import asyncio
from datetime import datetime
import hashlib
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, types
from recovery import Reconciler
from storage import Store
from transport import MediaQueue, Network, checksum, media_identity


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.log = Mock()
        self.config = dict(recheck_hours=24, recheck_batch_size=50, recheck_interval_seconds=60,
                           max_media_mb=1, min_free_mb=1)
        self.photo = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=3))

    def save(self, chat=7, mid=1, payload=None):
        self.store.save(dict(chat_id=chat, message_id=mid, user_id=10, username='test',
            first_name='Test', last_name='', text='original', file_path=None,
            date_sent=datetime.now().isoformat()), media_payload=payload)

    def row(self, chat=7, mid=1):
        with self.store.connection() as db:
            return dict(db.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?', (chat, mid)).fetchone())


class StorageRecoveryTests(Fixture, unittest.TestCase):
    def test_media_job_and_message_survive_restart_together(self):
        self.save(payload=bytes(self.photo))
        reopened = Store(self.store.path)
        self.assertEqual(reopened.pending_media()[0]['payload'], bytes(self.photo))
        self.assertEqual(self.row()['text'], 'original')

    def test_new_story_metadata_does_not_clear_retry_schedule(self):
        now = time.time()
        self.store.story(7, 1, '', now, now + 3600, media_payload=bytes(self.photo))
        key = self.store.pending_media()[0]['job_key']
        self.store.retry_media(key, 3600, 'TimeoutError')
        self.store.story(7, 1, 'new caption', now, now + 3600, media_payload=bytes(self.photo))
        self.assertEqual(self.store.pending_media(), [])
        self.assertEqual(self.store.status()['media_pending'], 1)

    def test_expired_story_never_notifies_including_late_media(self):
        now = time.time()
        for story_id, explicit in enumerate((False, True), start=1):
            self.store.story(7, story_id, '', now - 86400, now)
            self.assertFalse(self.store.story_removed(7, story_id, explicit=explicit, now=now))
            self.store.attach_story(7, story_id, str(self.root / 'late.jpg'))
        self.store.cleanup(14, self.root / 'media')
        self.assertEqual(self.store.pending(), [])

    def test_early_deletion_still_notifies(self):
        now = time.time()
        self.store.story(7, 1, '', now, now + 3600)
        self.assertTrue(self.store.story_removed(7, 1, explicit=True, now=now))
        self.assertIn('удалена раньше срока', self.store.pending()[0]['text'])

    def test_late_message_matches_persisted_deletion_trigger(self):
        reconciler = Reconciler(None, None, self.store, self.log, self.config)
        reconciler.deletion(7, 1)
        self.assertEqual(len(self.store.pending_signals()), 1)
        self.save()
        reopened = Store(self.store.path)
        Reconciler(None, None, reopened, self.log, self.config).signals()
        self.assertEqual(self.row()['deleted'], 1)
        self.assertEqual(reopened.pending_signals(), [])

    def test_common_deletion_never_matches_a_channel(self):
        self.save(chat=-1000000000123)
        reconciler = Reconciler(None, None, self.store, self.log, self.config)
        reconciler.deletion(None, 1)
        self.assertEqual(self.row(chat=-1000000000123)['deleted'], 0)
        self.assertFalse(self.store.deleted(None, 1))
        self.assertEqual(self.store.pending(), [])

    def test_channel_deletion_with_explicit_chat_is_confirmed(self):
        self.save(chat=-1000000000123)
        Reconciler(None, None, self.store, self.log, self.config).deletion(-1000000000123, 1)
        self.assertEqual(self.row(chat=-1000000000123)['deletion_source'], 'update')

    def test_ambiguous_common_trigger_keeps_both_messages_intact(self):
        self.save(chat=7)
        self.save(chat=8)
        reconciler = Reconciler(None, None, self.store, self.log, self.config)
        reconciler.deletion(None, 1)
        reconciler.signals()
        self.assertEqual(self.row(7)['deleted'], 0)
        self.assertEqual(self.row(8)['deleted'], 0)
        self.assertEqual(self.store.pending(), [])

    def test_repeated_trigger_does_not_duplicate_notifications(self):
        self.save()
        reconciler = Reconciler(None, None, self.store, self.log, self.config)
        reconciler.deletion(7, 1)
        count = len(self.store.pending())
        reconciler.deletion(7, 1)
        self.assertEqual(len(self.store.pending()), count)

    def test_missing_requires_two_observations_at_least_30_seconds_apart(self):
        self.save()
        self.assertFalse(self.store.record_check(7, 1, 'missing', now=100))
        self.assertFalse(self.store.record_check(7, 1, 'missing', now=120))
        self.assertTrue(self.store.record_check(7, 1, 'missing', now=130))

    def test_present_or_unknown_breaks_missing_observations(self):
        self.save()
        for result in ('present', 'unknown'):
            self.store.record_check(7, 1, 'missing', now=100)
            self.store.record_check(7, 1, result, now=140)
            self.assertFalse(self.store.record_check(7, 1, 'missing', now=200))
            self.store.record_check(7, 1, 'present', now=201)


class AsyncRecoveryTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def queue(self, client):
        return MediaQueue(client, Network(), self.root / 'media', self.config, self.log, self.store)

    def reconciler(self, client):
        return Reconciler(client, Network(), self.store, self.log, self.config)

    async def download(self, media, file, progress_callback):
        progress_callback(4, 4)
        Path(file).write_bytes(b'data')
        return str(file)

    async def test_download_success_commits_path_and_removes_job(self):
        self.save(payload=bytes(self.photo))
        client = NS(download_media=AsyncMock(side_effect=self.download))
        await self.queue(client).once()
        self.assertEqual(self.store.status()['media_pending'], 0)
        self.assertEqual(Path(self.row()['file_path']).read_bytes(), b'data')

    async def test_late_download_after_deletion_queues_recovered_file(self):
        self.save(payload=bytes(self.photo))
        self.store.deleted(7, 1)
        await self.queue(NS(download_media=AsyncMock(side_effect=self.download))).once()
        self.assertEqual(len([j for j in self.store.pending() if j['kind'] == 'media']), 1)

    async def test_cancelled_download_remains_pending_after_restart(self):
        self.save(payload=bytes(self.photo))
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        worker = asyncio.create_task(self.queue(NS(download_media=blocked)).once())
        await started.wait()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(len(Store(self.store.path).pending_media()), 1)

    async def test_staged_file_is_verified_and_reused_after_crash(self):
        for mid, suffix in [(1, ''), (2, '.part')]:
            with self.subTest(suffix=suffix):
                self.save(mid=mid, payload=bytes(self.photo))
                path = self.root / 'media' / 'messages' / f'7_{mid}.jpg'
                path.parent.mkdir(parents=True, exist_ok=True)
                staged = Path(str(path) + suffix)
                staged.write_bytes(b'complete')
                self.store.stage_media(f'messages/7_{mid}', 8, checksum(staged), media_identity(self.photo))
                client = NS(download_media=AsyncMock(side_effect=AssertionError('Must reuse artifact')))
                await self.queue(client).once()
                client.download_media.assert_not_called()
                self.assertEqual(Path(self.row(mid=mid)['file_path']).read_bytes(), b'complete')

    async def test_corrupted_staged_file_is_not_trusted(self):
        self.save(payload=bytes(self.photo))
        path = self.root / 'media' / 'messages' / '7_1.jpg'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'bad!')
        self.store.stage_media('messages/7_1', 4, hashlib.sha256(b'good').hexdigest(), media_identity(self.photo))
        client = NS(download_media=AsyncMock(side_effect=self.download))
        await self.queue(client).once()
        client.download_media.assert_awaited_once()
        self.assertEqual(path.read_bytes(), b'data')

    async def test_expired_file_reference_is_refreshed_without_false_deletion(self):
        self.save(payload=bytes(self.photo))
        client = NS(download_media=AsyncMock(side_effect=errors.FileReferenceExpiredError(request=None)),
                    get_messages=AsyncMock(return_value=NS(id=1, chat_id=7, media=self.photo)))
        await self.queue(client).once()
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM media_jobs').fetchone()
            self.assertEqual(row['last_error'], 'FileReferenceRefreshed')
        self.assertEqual(self.row()['deleted'], 0)

    async def test_two_workers_do_not_download_the_same_job(self):
        self.save(payload=bytes(self.photo))
        started, finish = asyncio.Event(), asyncio.Event()
        async def held_download(*args, **kwargs):
            started.set()
            await finish.wait()
            return await self.download(*args, **kwargs)
        client = NS(download_media=AsyncMock(side_effect=held_download))
        queue = self.queue(client)
        first = asyncio.create_task(queue.once())
        await started.wait()
        self.assertFalse(await queue.once())
        finish.set()
        await first
        self.assertEqual(client.download_media.await_count, 1)

    async def test_missed_delete_event_is_labeled_unavailable_after_two_checks(self):
        self.save()
        client = NS(get_entity=AsyncMock(return_value=NS()), get_messages=AsyncMock(return_value=[None]))
        reconciler = self.reconciler(client)
        await reconciler.recent()
        self.assertEqual(self.store.pending(), [])
        with patch('storage.time.time', return_value=time.time() + 61):
            await reconciler.recent()
        self.assertEqual(self.row()['deletion_source'], 'recheck')
        self.assertIn('событие удаления не получено', self.store.pending()[0]['text'])

    async def test_network_error_resets_missing_evidence_without_alert(self):
        self.save()
        self.store.record_check(7, 1, 'missing', now=time.time() - 100)
        client = NS(get_entity=AsyncMock(side_effect=TimeoutError()), get_messages=AsyncMock())
        await self.reconciler(client).recent()
        self.assertIsNone(self.row()['missing_since'])
        self.assertEqual(self.store.pending(), [])

    async def test_lost_chat_access_never_becomes_deletion(self):
        self.save()
        client = NS(get_entity=AsyncMock(return_value=NS(left=True)), get_messages=AsyncMock())
        await self.reconciler(client).recent()
        client.get_messages.assert_not_called()
        self.assertEqual(self.row()['deleted'], 0)

    async def test_incomplete_or_wrong_peer_response_is_not_deletion(self):
        self.save()
        for response in ([], [NS(id=1, chat_id=99)]):
            client = NS(get_entity=AsyncMock(return_value=NS()), get_messages=AsyncMock(return_value=response))
            with patch('storage.time.time', return_value=time.time() + 61):
                await self.reconciler(client).recent()
            self.assertEqual(self.row()['deleted'], 0)
        self.assertEqual(self.store.pending(), [])

    async def test_recheck_backfills_media_job_for_existing_metadata(self):
        self.save()
        client = NS(get_entity=AsyncMock(return_value=NS()),
                    get_messages=AsyncMock(return_value=[NS(id=1, chat_id=7, media=self.photo)]))
        await self.reconciler(client).recent()
        self.assertEqual(self.store.pending_media()[0]['job_key'], 'messages/7_1')


if __name__ == '__main__':
    unittest.main()
