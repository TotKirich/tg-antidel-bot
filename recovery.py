"""Durable deletion triggers and bounded, conservative reconciliation."""
import asyncio
import time

from telethon import errors, types
from message_metadata import capture
from transport import AUTH_ERRORS, media_bytes


class Reconciler:
    def __init__(self, client, network, store, logger, config):
        self.client, self.network, self.store = client, network, store
        self.logger, self.config = logger, config

    def deletion(self, chat_id, message_id):
        key = self.store.note_deletion(chat_id, message_id)
        rows = self.store.deletion_candidates(chat_id, message_id)
        if len(rows) == 1:
            self.store.deleted(rows[0]['chat_id'], message_id)
            self.store.update_signal(key, resolved=True)

    def signals(self):
        return self.store.process_deletion_signals(100) + self.store.process_history_gaps(100)

    async def deletions(self, chat_id, message_ids):
        # A whole Telegram batch is durable before recovery starts. No media
        # sending/rendering or per-message connections in the update handler.
        await asyncio.to_thread(self.store.note_deletions, chat_id, message_ids)

    async def raw_update(self, update):
        if isinstance(update, types.UpdateChannelAvailableMessages):
            await asyncio.to_thread(self.store.note_history_gap,
                -1000000000000 - update.channel_id, update.available_min_id)

    async def recent(self):
        grouped = {}
        for row in self.store.recheck_candidates(hours=int(self.config['recheck_hours']),
            limit=int(self.config['recheck_batch_size']), interval=int(self.config['recheck_interval_seconds'])):
            grouped.setdefault(row['chat_id'], []).append(row)
        for chat_id, rows in grouped.items():
            try:
                peer = await self.network.call(lambda: self.client.get_entity(chat_id))
                if getattr(peer, 'left', False) or getattr(peer, 'deactivated', False):
                    raise LookupError('Chat not accessible')
                ids = [r['message_id'] for r in rows]
                messages = await self.network.call(lambda: self.client.get_messages(peer, ids=ids))
                if not isinstance(messages, (list, tuple)) or len(messages) != len(rows):
                    raise LookupError('Incomplete message response')
                for row, message in zip(rows, messages):
                    if message is None or (isinstance(message, types.MessageEmpty) and message.id == row['message_id']):
                        result = 'missing'
                    elif getattr(message, 'id', None) == row['message_id'] and getattr(message, 'chat_id', None) == chat_id:
                        result = 'present'
                        self.store.enrich_metadata(capture(message, getattr(message, 'sender', None), peer))
                        payload = media_bytes(getattr(message, 'media', None))
                        if not row['file_path'] and payload:
                            with self.store.connection() as db:
                                self.store.queue_media(db, 'message', chat_id, row['message_id'], payload)
                    else:
                        result = 'unknown'
                    if self.store.record_check(chat_id, row['message_id'], result):
                        self.store.deleted(chat_id, row['message_id'], source='recheck')
                await asyncio.sleep(.1)
            except AUTH_ERRORS:
                raise
            except errors.FloodWaitError:
                return
            except Exception as error:
                # A failed read is not evidence of deletion and breaks a chain
                # of missing observations. Advance the cursor for fair batching.
                for row in rows:
                    self.store.record_check(chat_id, row['message_id'], 'unknown')
                self.logger.warning('Recheck unavailable chat=%s type=%s', chat_id, type(error).__name__)
        self.store.heartbeat('recheck_ok', time.time())

    async def run(self):
        async def signal_worker():
            while True:
                processed = await asyncio.to_thread(self.signals)
                await asyncio.sleep(.05 if processed else .5)

        async def check_worker():
            while True:
                await self.recent()
                await asyncio.sleep(int(self.config['recheck_interval_seconds']))

        tasks = [asyncio.create_task(signal_worker()), asyncio.create_task(check_worker())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
