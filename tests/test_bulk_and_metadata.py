# Synthetic phone fixtures; Telegram is mocked in these tests.
import asyncio
from datetime import datetime, timezone
import tempfile
from pathlib import Path
import threading
import time
import tracemalloc
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import types
from message_metadata import capture, content_type, render
from recovery import Reconciler
from storage import Store
from transport import Delivery, Network


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.config = dict(recheck_hours=24, recheck_batch_size=50, recheck_interval_seconds=60)

    def seed(self, count, chat=7):
        with self.store.connection() as db:
            db.executemany('''INSERT INTO messages(chat_id,message_id,user_id,username,first_name,
                last_name,text,date_sent) VALUES (?,?,?,?,?,?,?,?)''',
                ((chat, i, 100 + i % 10, f'user{i % 10}', f'Author{i % 10}', '',
                  f'message-{i}', datetime.now().isoformat()) for i in range(1, count + 1)))

    def scalar(self, sql):
        with self.store.connection() as db:
            return db.execute(sql).fetchone()[0]


class BulkTests(Fixture, unittest.TestCase):
    def test_large_captions_and_media_references_use_bounded_python_memory(self):
        count = 3000
        self.seed(count)
        with self.store.connection() as db:
            db.execute('UPDATE messages SET text=?,file_path=?,message_kind=?',
                       ('x' * 4096, str(self.root / 'media' / 'saved.jpg'), 'photo'))
        self.store.note_deletions(7, range(1, count + 1))
        tracemalloc.start()
        try:
            while self.store.process_deletion_signals(100):
                pass
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM messages WHERE deleted=1'), count)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox WHERE kind='media'"), count)
        self.assertGreaterEqual(self.scalar("SELECT SUM(LENGTH(text)) FROM outbox WHERE kind='archive'"), count * 4096)
        self.assertLess(peak, 16 * 1024 * 1024)
        print(f'\nBulk payload benchmark: {count} captions x4096 + media refs; Python peak {peak / 1024 / 1024:.2f} MiB')

    def test_ten_thousand_deletions_are_bounded_resumable_and_idempotent(self):
        count = 10000
        self.seed(count)
        started = time.perf_counter()
        with patch('storage.time.time', return_value=1000):
            self.store.note_deletions(7, range(1, count + 1))
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM messages WHERE deleted=1'), 0)
            self.assertEqual(self.store.process_deletion_signals(100), 100)
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM messages WHERE deleted=1'), 100)
            self.store = Store(self.store.path)  # Simulated restart between pages.
            while self.store.process_deletion_signals(100):
                pass
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM messages WHERE deleted=1'), count)
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM outbox'), count)
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM deletion_digests'), 1)
            self.store.note_deletions(7, range(1, count + 1))
            self.assertEqual(self.store.process_deletion_signals(), 0)
            self.assertEqual(self.scalar('SELECT COUNT(*) FROM outbox'), count)
        with patch('storage.time.time', return_value=1035):
            self.store.flush_digests()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM outbox WHERE kind='notify'"), 1)
        with self.store.connection() as db:
            text = db.execute("SELECT text FROM outbox WHERE kind='notify'").fetchone()[0]
            self.assertIn('10000', text)
            self.assertIn('Author1 @user1', db.execute("SELECT text FROM outbox WHERE event_key='delete:7:1:text:0'").fetchone()[0])
        print(f'\nBulk benchmark: {count} messages, {time.perf_counter() - started:.2f}s, pages <=100')

    def test_history_hidden_range_is_paged_and_not_called_deletion(self):
        self.seed(300, chat=-1000000000123)
        self.store.note_history_gap(-1000000000123, 250)
        self.assertEqual(self.store.process_history_gaps(100), 100)
        self.store = Store(self.store.path)
        while self.store.process_history_gaps(100):
            pass
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM messages WHERE deleted=1'), 250)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM messages WHERE deletion_source='history_hidden'"), 250)
        self.assertIn('удаление не доказано', self.store.pending()[0]['text'])
        self.store.note_history_gap(-1000000000123, 300)
        self.store.process_history_gaps(100)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM messages WHERE deleted=1'), 300)

    def test_notification_priority_does_not_starve_recovery(self):
        with self.store.connection() as db:
            for i in range(20):
                self.store.enqueue(db, f'n{i}', 'notify', 'summary')
                self.store.enqueue(db, f'a{i}', 'archive', 'restore')
        pending = self.store.pending(10)
        self.assertEqual(sum(j['kind'] == 'notify' for j in pending), 2)
        self.assertEqual(sum(j['kind'] == 'archive' for j in pending), 8)

    def test_recheck_does_not_load_raw_snapshots(self):
        self.seed(1)
        with self.store.connection() as db:
            db.execute('UPDATE messages SET raw_message=?', (b'x' * 500000,))
        self.assertNotIn('raw_message', self.store.recheck_candidates()[0])

    def test_metadata_fill_preserves_original_text_and_known_author(self):
        self.seed(1)
        self.store.enrich_metadata(dict(chat_id=7, message_id=1, text='edited elsewhere',
            first_name='new name', chat_title='Readable chat', message_kind='text'))
        with self.store.connection() as db:
            row = dict(db.execute('SELECT * FROM messages').fetchone())
        self.assertEqual(row['text'], 'message-1')
        self.assertEqual(row['first_name'], 'Author1')
        self.assertEqual(row['chat_title'], 'Readable chat')

    def test_late_media_caption_contains_origin_and_author(self):
        self.seed(1)
        self.store.enrich_metadata(dict(chat_id=7, message_id=1, chat_title='Test chat', message_kind='photo'))
        self.store.deleted(7, 1)
        self.store.attach_media(7, 1, self.root / 'photo.jpg')
        caption = next(j['text'] for j in self.store.pending() if j['kind'] == 'media')
        self.assertIn('Test chat', caption)
        self.assertIn('Author1 @user1', caption)
        self.assertNotIn('сообщение: 1', caption)


class MetadataTests(unittest.TestCase):
    def message(self, **kwargs):
        return NS(chat_id=7, id=1, sender_id=10, raw_text=kwargs.pop('text', ''), **kwargs)

    def test_photo_without_caption_is_explicit_not_lost_text(self):
        message = self.message(media=types.MessageMediaPhoto(photo=types.PhotoEmpty(3)))
        record = capture(message, NS(first_name='Alice', last_name='', username='alice'),
                         NS(first_name='Alice', last_name='', username='alice'))
        rendered = render(record)
        self.assertIn('Фото без подписи', rendered)
        self.assertNotIn('ID чата:', rendered)
        self.assertIn('@alice', rendered)

    def test_real_channel_post_keeps_channel_id_and_raw_snapshot(self):
        message = types.Message(id=2, peer_id=types.PeerChannel(123), post=True,
                                date=datetime.now(timezone.utc), message='channel post')
        record = capture(message, chat=types.Channel(id=123, title='News', photo=types.ChatPhotoEmpty(), date=None))
        self.assertEqual(record['user_id'], -1000000000123)
        self.assertEqual(record['chat_type'], 'channel')
        self.assertEqual(record['chat_title'], 'News')
        self.assertIsInstance(record['raw_message'], bytes)

    def test_unknown_group_author_is_not_assigned_to_group_owner(self):
        message = NS(chat_id=-42, id=1, sender_id=None, raw_text='anonymous')
        record = capture(message, chat=NS(title='Group'))
        self.assertIsNone(record['user_id'])
        self.assertIn('Автор неизвестен', render(record))

    def test_voice_document_has_human_readable_type(self):
        media = types.MessageMediaDocument(document=NS(attributes=[types.DocumentAttributeAudio(duration=4, voice=True)]))
        self.assertEqual(content_type(self.message(media=media)), ('voice', 'Голосовое сообщение'))

    def test_contact_and_poll_preserve_non_text_meaning(self):
        contact = types.MessageMediaContact(phone_number='+70000000000', first_name='Test', last_name='', vcard='', user_id=1)
        kind, summary = content_type(self.message(media=contact))
        self.assertEqual(kind, 'contact')
        self.assertIn('Контакт:', summary)
        poll = types.MessageMediaPoll(poll=NS(question=NS(text='Question?')), results=NS())
        self.assertEqual(content_type(self.message(media=poll)), ('poll', 'Опрос: Question?'))

    def test_caption_album_and_forward_source_remain_distinct(self):
        message = self.message(text='caption', media=types.MessageMediaPhoto(photo=types.PhotoEmpty(3)),
                               grouped_id=88, fwd_from=NS(from_name='Original author', from_id=None))
        record = capture(message, NS(first_name='Sender', last_name='', username='sender'))
        self.assertEqual(record['text'], 'caption')
        text = render(record)
        self.assertIn('Часть медиаальбома', text)
        self.assertIn('Переслано от: Original author', text)
        self.assertIn('👤 Sender @sender', text)

    def test_legacy_empty_record_is_not_presented_as_known_empty_message(self):
        legacy = dict(chat_id=7, message_id=1, user_id=None, text='', file_path=None)
        text = render(legacy)
        self.assertIn('Содержимое отсутствует в старой записи', text)
        self.assertNotIn('None', text)


class AsyncBulkTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_active_slow_transfer_is_allowed_past_idle_window(self):
        async def transfer(progress):
            for i in range(6):
                progress(i, 6)
                await asyncio.sleep(.03)
            return 'complete'
        result = await Network().transfer(transfer, idle_timeout=.1, max_timeout=1)
        self.assertEqual(result, 'complete')

    async def test_stalled_transfer_is_cancelled(self):
        cancelled = asyncio.Event()
        async def transfer(progress):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with self.assertRaises(TimeoutError):
            await Network().transfer(transfer, idle_timeout=.02, max_timeout=1)
        self.assertTrue(cancelled.is_set())

    async def test_transfer_has_total_deadline_despite_progress(self):
        async def transfer(progress):
            while True:
                progress(1, 10)
                await asyncio.sleep(.01)
        with self.assertRaises(TimeoutError):
            await Network().transfer(transfer, idle_timeout=.1, max_timeout=.04)

    async def test_bulk_intake_does_not_block_event_loop(self):
        started, release = threading.Event(), threading.Event()
        original = self.store.note_deletions
        def held(chat, ids):
            started.set()
            release.wait(5)
            original(chat, ids)
        reconciler = Reconciler(None, None, self.store, Mock(), self.config)
        with patch.object(self.store, 'note_deletions', side_effect=held):
            task = asyncio.create_task(reconciler.deletions(7, range(10000)))
            try:
                await asyncio.to_thread(started.wait, 2)
                self.assertFalse(task.done())
                # Can still accept a new message while the intake worker is busy.
                self.seed(1, chat=8)
                await asyncio.sleep(.01)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM deletion_signals'), 10000)

    async def test_history_update_is_recorded_without_enumerating_messages(self):
        reconciler = Reconciler(None, None, self.store, Mock(), self.config)
        await reconciler.raw_update(types.UpdateChannelAvailableMessages(channel_id=123, available_min_id=50000))
        self.assertEqual(self.scalar('SELECT max_id FROM history_gaps'), 50000)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM deletion_signals'), 0)

    async def test_delivery_requests_pacing_between_successful_sends(self):
        with self.store.connection() as db:
            self.store.enqueue(db, 'a', 'archive', 'one')
            self.store.enqueue(db, 'b', 'archive', 'two')
        client = NS(send_message=AsyncMock())
        delivery = Delivery(self.store, client, Network(), {'log_chat_id': 'me', 'delivery_interval_seconds': 1}, lambda: {}, Mock())
        with patch('transport.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await delivery.once()
        self.assertEqual(client.send_message.await_count, 2)
        self.assertTrue(any(call.args[0] >= .9 for call in sleep.await_args_list))


if __name__ == '__main__':
    unittest.main()
