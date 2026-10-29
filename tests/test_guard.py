import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, functions, types
from runtime import atomic_config, authorised, read_config
from storage import Store, chunks
from stories import StoryMonitor
from transport import Delivery, MediaQueue, Network
import userbot


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'messages.db')

    def save(self, chat=10, mid=1, text='hello', **extra):
        record = dict(chat_id=chat, message_id=mid, user_id=123, username='alice',
                      first_name='Alice', last_name='Smith', text=text, file_path=None,
                      date_sent=datetime.now().isoformat())
        return self.store.save({**record, **extra})

    def test_legacy_database_migration_preserves_records(self):
        path = self.root / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE messages(chat_id INTEGER,message_id INTEGER,user_id INTEGER,
                username TEXT,first_name TEXT,last_name TEXT,text TEXT,file_path TEXT,date_sent TEXT,
                deleted INTEGER DEFAULT 0,PRIMARY KEY(chat_id,message_id))''')
            db.execute("INSERT INTO messages(chat_id,message_id,text) VALUES (1,2,'legacy')")
        db.close()
        migrated = Store(path)
        with migrated.connection() as db:
            row = db.execute('SELECT * FROM messages').fetchone()
            self.assertEqual((row['text'], row['revision']), ('legacy', 0))

    def test_deletion_has_correct_author_and_is_idempotent(self):
        self.save()
        self.assertTrue(self.store.deleted(10, 1))
        before = self.store.pending()
        self.assertFalse(self.store.deleted(10, 1))
        self.assertEqual(len(self.store.pending()), len(before))
        self.assertIn('Alice Smith @alice', before[0]['text'])

    def test_ambiguous_chat_is_not_guessed(self):
        self.save(chat=10)
        self.save(chat=-100123)
        self.assertFalse(self.store.deleted(None, 1))
        self.assertEqual(self.store.pending(), [])

    def test_batch_rows_keep_their_own_author(self):
        self.save(mid=1)
        self.save(mid=2, username='bob', first_name='Bob')
        self.store.deleted(10, 1)
        self.store.deleted(10, 2)
        texts = [j['text'] for j in self.store.pending() if j['kind'] == 'archive']
        self.assertIn('@alice', texts[0])
        self.assertIn('@bob', texts[1])

    def test_late_media_is_recovered_after_deletion(self):
        self.save()
        self.store.deleted(10, 1)
        self.store.attach_media(10, 1, self.root / 'photo.jpg')
        self.assertEqual(len([j for j in self.store.pending() if j['kind'] == 'media']), 1)

    def test_edit_history_and_repeated_events(self):
        self.save()
        self.assertTrue(self.store.edit(10, 1, 'caption 2'))
        self.assertFalse(self.store.edit(10, 1, 'caption 2'))
        self.assertTrue(self.store.edit(10, 1, 'caption 3'))
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM edits ORDER BY revision').fetchall()
            self.assertEqual([(r['old_text'], r['new_text']) for r in rows],
                             [('hello', 'caption 2'), ('caption 2', 'caption 3')])

    def test_duplicate_incoming_does_not_overwrite_edit_or_resurrect(self):
        self.save()
        self.store.edit(10, 1, 'edited')
        self.store.deleted(10, 1)
        self.assertFalse(self.save())
        with self.store.connection() as db:
            row = db.execute('SELECT text,deleted FROM messages').fetchone()
            self.assertEqual(tuple(row), ('edited', 1))

    def test_outbox_survives_reopening_database_and_retry(self):
        self.save()
        self.store.deleted(10, 1)
        job = self.store.pending()[0]
        self.store.retry(job['id'], 3600)
        reopened = Store(self.store.path)
        self.assertNotIn(job['id'], [j['id'] for j in reopened.pending()])
        with reopened.connection() as db:
            self.assertEqual(db.execute('SELECT attempts FROM outbox WHERE id=?', (job['id'],)).fetchone()[0], 1)

    def test_concurrent_capture_and_cleanup_no_shared_cursor(self):
        def work(i):
            self.save(mid=i)
            if i % 3 == 0:
                self.store.deleted(10, i)
            if i % 5 == 0:
                self.store.cleanup(14, self.root / 'media')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(200)))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 200)

    def test_cleanup_preserves_pending_and_never_deletes_outside_root(self):
        media = self.root / 'media'
        media.mkdir()
        old = time.time() - 20 * 86400
        orphan = media / 'orphan.jpg'
        pending = media / 'pending.jpg'
        outside = self.root / 'external.jpg'
        for p in (orphan, pending, outside):
            p.write_bytes(b'x')
            os.utime(p, (old, old))
        self.save(file_path=str(pending), date_sent='2000-01-01T00:00:00')
        self.store.deleted(10, 1)
        self.store.cleanup(14, media)
        self.assertFalse(orphan.exists())
        self.assertTrue(pending.exists())
        self.assertTrue(outside.exists())

    def test_chunk_limits_include_surrogate_pairs(self):
        value = '🧪' * 5000
        parts = chunks(value)
        self.assertEqual(''.join(parts), value)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 4096 for p in parts))

    def test_story_early_deletion_once_and_late_download(self):
        now = time.time()
        self.store.story(1, 2, 'a story', now - 60, now + 3600)
        self.assertTrue(self.store.story_removed(1, 2, explicit=True, now=now))
        self.assertFalse(self.store.story_removed(1, 2, explicit=True, now=now))
        self.store.attach_story(1, 2, str(self.root / 'story.mp4'))
        self.assertEqual(len([j for j in self.store.pending() if j['kind'] == 'media']), 1)

    def test_expired_story_and_unknown_story_do_not_notify(self):
        now = time.time()
        self.store.story(1, 2, '', now - 3600, now + 30)
        self.assertFalse(self.store.story_removed(1, 2, explicit=True, now=now))
        self.assertFalse(self.store.story_removed(1, 99, explicit=True, now=now))
        self.assertEqual(self.store.pending(), [])

    def test_inaccessibility_is_not_reported_as_confirmed_deletion(self):
        now = time.time()
        self.store.story(1, 2, '', now, now + 3600)
        self.store.story_removed(1, 2, now=now)
        self.assertIn('изменён доступ', self.store.pending()[0]['text'])


class AsyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.logger = Mock(spec=logging.Logger)
        self.network = Network()

    async def test_unauthorized_identity_is_not_a_healthy_connection(self):
        client = NS(get_me=AsyncMock(return_value=None))
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            await self.network.identity(client)

    async def test_identity_confirms_authenticated_account(self):
        user = NS(id=123)
        self.assertIs(await self.network.identity(NS(get_me=AsyncMock(return_value=user))), user)

    async def test_delivery_failure_retains_job_and_retries(self):
        with self.store.connection() as db:
            self.store.enqueue(db, 'event', 'archive', '<b>literal</b>')
        client = NS(send_message=AsyncMock(side_effect=TimeoutError()))
        delivery = Delivery(self.store, client, self.network, {'log_chat_id': 'me'}, lambda: {}, self.logger)
        await delivery.once()
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM outbox').fetchone()
            self.assertEqual(row['attempts'], 1)
            db.execute('UPDATE outbox SET next_try=0')
        client.send_message.side_effect = None
        await delivery.once()
        self.assertEqual(self.store.pending(), [])
        self.assertIsNone(client.send_message.call_args.kwargs['parse_mode'])

    async def test_floodwait_pauses_account_and_keeps_job(self):
        with self.store.connection() as db:
            self.store.enqueue(db, 'event', 'archive', 'hello')
        client = NS(send_message=AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=12)))
        delivery = Delivery(self.store, client, self.network, {'log_chat_id': 'me'}, lambda: {}, self.logger)
        await delivery.once()
        self.assertGreater(self.network.paused_until, time.monotonic() + 11)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    async def test_auth_error_not_hidden_in_delivery_retry(self):
        with self.store.connection() as db:
            self.store.enqueue(db, 'event', 'archive', 'hello')
        client = NS(send_message=AsyncMock(side_effect=errors.AuthKeyUnregisteredError(request=None)))
        delivery = Delivery(self.store, client, self.network, {'log_chat_id': 'me'}, lambda: {}, self.logger)
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            await delivery.once()

    async def test_service_without_authorization_opens_web_instead_of_console(self):
        client = NS(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                    disconnect=AsyncMock(), start=AsyncMock())
        with patch('userbot.TelegramClient', return_value=client), patch('userbot.read_config',
            return_value={'api_id': 1, 'api_hash': 'test', 'auth_port': 8080, 'auth_host': '192.0.2.10'}), patch('userbot.ROOT', self.root), \
            patch('userbot.web_authorize', new_callable=AsyncMock, side_effect=errors.AuthKeyUnregisteredError(request=None)) as auth, \
            patch('userbot.setup_logging', return_value=self.logger):
            self.assertEqual(await userbot.run(), 78)
            auth.assert_awaited_once()
            self.assertEqual(auth.call_args.kwargs['host'], '192.0.2.10')
        client.start.assert_not_called()
        client.disconnect.assert_awaited()

    async def test_failed_download_does_not_create_final_file(self):
        client = NS(download_media=AsyncMock(side_effect=TimeoutError()))
        media = MediaQueue(client, self.network, self.root / 'media',
                          {'max_media_mb': 1, 'min_free_mb': 1}, self.logger, self.store)
        with self.store.connection() as db:
            self.store.queue_media(db, 'message', 1, 1, bytes(types.MessageMediaPhoto()))
        await media.once()
        self.assertEqual(self.store.status()['media_pending'], 1)
        self.assertFalse(list((self.root / 'media').rglob('*.part')))

    async def test_story_feed_pages_hidden_contacts_and_skipped_items(self):
        now = datetime.now(timezone.utc)
        full = types.StoryItem(id=1, date=now, expire_date=now + timedelta(hours=1),
                               media=types.MessageMediaPhoto(), caption='hello')
        skipped = types.StoryItemSkipped(id=1, date=now, expire_date=now + timedelta(hours=1))
        calls = []

        async def request(query):
            calls.append(query)
            if isinstance(query, functions.contacts.GetContactsRequest):
                return NS(contacts=[NS(user_id=7)], users=[])
            if isinstance(query, functions.stories.GetStoriesByIDRequest):
                return NS(stories=[full])
            if query.hidden:
                return NS(peer_stories=[], has_more=False, state='hidden')
            if query.next:
                return NS(peer_stories=[types.PeerStories(types.PeerUser(99), [full])],
                          has_more=False, state='last')
            return NS(peer_stories=[types.PeerStories(types.PeerUser(7), [skipped])],
                      has_more=True, state='next')

        client = NS(get_input_entity=AsyncMock(return_value=types.InputPeerUser(7, 1)))
        client_type = type('Client', (), {'__call__': lambda self, q: request(q)})
        client_obj = client_type()
        client_obj.get_input_entity = client.get_input_entity
        monitor = StoryMonitor(client_obj, self.network, self.store, self.logger)
        await monitor.scan()
        active = self.store.active_stories()
        self.assertEqual([r['owner_id'] for r in active], [7])
        self.assertTrue(any(getattr(q, 'hidden', False) for q in calls))
        self.assertTrue(any(getattr(q, 'next', False) for q in calls))
        self.assertEqual(self.store.pending_media()[0]['job_key'], 'stories/7_1')
        self.assertTrue(all('read' not in type(q).__name__.lower() for q in calls))

    async def test_story_rpc_failure_never_infers_deletion(self):
        now = time.time()
        self.store.story(7, 1, '', now, now + 3600)
        client_type = type('Client', (), {'__call__': lambda self, q: self.fail()})
        client = client_type()
        client.fail = AsyncMock(side_effect=TimeoutError())
        client.get_input_entity = AsyncMock(return_value=types.InputPeerUser(7, 1))
        monitor = StoryMonitor(client, self.network, self.store, self.logger)
        monitor.contacts = {7}
        await monitor.fetch_ids(7, [1])
        self.assertEqual(len(self.store.active_stories()), 1)
        self.assertEqual(self.store.pending(), [])


class ConfigTest(unittest.TestCase):
    def test_atomic_config_and_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            atomic_config({'admin_username': 'тест'}, path)
            self.assertEqual(read_config(path)['admin_username'], 'тест')
            self.assertFalse(list(Path(folder).glob('.config-*.tmp')))

    def test_stable_admin_id_and_non_text_sender(self):
        config = {'admin_user_id': 123, 'admin_username': 'oldname'}
        private = NS(type='private')
        self.assertTrue(authorised(config, NS(id=123, username=None), private))
        self.assertFalse(authorised(config, NS(id=456, username='oldname'), private))
        self.assertFalse(authorised(config, None, private))
        self.assertFalse(authorised(config, NS(id=123), NS(type='group')))

    def test_caption_and_sender_chat_metadata(self):
        message = NS(chat_id=-1001, id=7, sender_id=-1002, raw_text='caption')
        row = userbot.record_message(message, NS(title='Channel', username='channel'))
        self.assertEqual(row['text'], 'caption')
        self.assertEqual(row['first_name'], 'Channel')


if __name__ == '__main__':
    unittest.main()
