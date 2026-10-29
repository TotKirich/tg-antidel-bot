"""Bounded media work and durable delivery, separate from Telegram event handlers."""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import time
from html import escape

import requests
from telethon import errors, functions, types, utils
from telethon.extensions import BinaryReader
from message_metadata import rich_text

AUTH_ERRORS = (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
               errors.AuthKeyDuplicatedError, errors.SessionExpiredError, errors.UserDeactivatedError)


class Network:
    def __init__(self):
        self.paused_until = 0

    async def call(self, operation, timeout=30):
        await asyncio.sleep(max(0, self.paused_until - time.monotonic()))
        try:
            return await asyncio.wait_for(operation(), timeout=timeout)
        except errors.FloodWaitError as error:
            self.paused_until = max(self.paused_until, time.monotonic() + error.seconds + 1)
            raise

    async def identity(self, client):
        user = await self.call(client.get_me)
        # Telethon get_me() may return None for UnauthorizedError instead of raising.
        if user is None:
            raise errors.AuthKeyUnregisteredError(request=None)
        return user

    async def transfer(self, operation, progress=None, idle_timeout=60, max_timeout=1800):
        started, last_progress = False, time.monotonic()
        def tracking(current, total):
            nonlocal last_progress
            last_progress = time.monotonic()
            if progress:
                progress(current, total)
        async def begin():
            nonlocal started, last_progress
            started, last_progress = True, time.monotonic()
            return await operation(tracking)
        task = asyncio.create_task(self.call(begin, timeout=max_timeout))
        try:
            while not task.done():
                await asyncio.wait([task], timeout=min(5, idle_timeout))
                if not task.done() and started and time.monotonic() - last_progress >= idle_timeout:
                    raise TimeoutError('Transfer stalled')
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def media_bytes(media):
    # Polls, contacts and service media do not have a downloadable file.
    return bytes(media) if isinstance(media, (types.MessageMediaPhoto, types.MessageMediaDocument)) else None


def media_identity(media):
    value = getattr(media, 'document', None) or getattr(media, 'photo', None)
    return f'{type(value).__name__}:{value.id}' if value is not None else hashlib.sha256(bytes(media)).hexdigest()


def checksum(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def durable_checksum(path):
    digest = checksum(path)
    with open(path, 'r+b') as stream:
        os.fsync(stream.fileno())
    return digest


class MediaQueue:
    def __init__(self, client, network, root, config, logger, store):
        self.client, self.network, self.root = client, network, Path(root)
        self.config, self.logger, self.store = config, logger, store
        self.inflight = set()

    async def refresh(self, job):
        if job['kind'] == 'message':
            message = await self.network.call(lambda: self.client.get_messages(
                job['owner_id'], ids=job['item_id']))
            media = message.media if (message and message.id == job['item_id']
                                      and message.chat_id == job['owner_id']) else None
        else:
            peer = await self.network.call(lambda: self.client.get_input_entity(job['owner_id']))
            result = await self.network.call(lambda: self.client(functions.stories.GetStoriesByIDRequest(
                peer=peer, id=[job['item_id']])))
            story = next((s for s in result.stories if isinstance(s, types.StoryItem)
                          and s.id == job['item_id']), None)
            media = story.media if story else None
        payload = media_bytes(media)
        if payload is None:
            raise LookupError('Source media no longer available')
        return payload

    async def once(self):
        job = next((j for j in self.store.pending_media() if j['job_key'] not in self.inflight), None)
        if job is None:
            return False
        key, temporary = job['job_key'], None
        self.inflight.add(key)
        try:
            with BinaryReader(job['payload']) as reader:
                media = reader.tgread_object()
            path = self.root / (key + (utils.get_extension(media) or '.bin'))
            if not path.resolve().is_relative_to(self.root.resolve()):
                raise ValueError('Invalid media destination')
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(str(path) + '.part')
            identity = media_identity(media)
            if job['sha256'] and job['media_identity'] == identity:
                for candidate in (path, temporary):
                    if candidate.is_file() and candidate.stat().st_size == job['staged_size']:
                        if await asyncio.to_thread(checksum, candidate) == job['sha256']:
                            if candidate != path:
                                os.replace(candidate, path)
                            self.store.complete_media(key, str(path))
                            return True
            limit = int(self.config['max_media_mb']) * 1024 * 1024
            reserve = int(self.config['min_free_mb']) * 1024 * 1024

            def progress(current, total):
                if max(current, total) > limit or shutil.disk_usage(self.root).free < reserve:
                    raise ValueError('Media quota reached')

            progress(0, getattr(getattr(media, 'document', None), 'size', 0) or 0)
            saved = await self.network.transfer(lambda tracking: self.client.download_media(
                media, file=str(temporary), progress_callback=tracking), progress=progress)
            if not saved:
                raise LookupError('No media downloaded')
            digest = await asyncio.to_thread(durable_checksum, saved)
            self.store.stage_media(key, Path(saved).stat().st_size, digest, identity)
            os.replace(saved, path)
            self.store.complete_media(key, str(path))
        except AUTH_ERRORS:
            raise
        except errors.FloodWaitError as error:
            self.store.retry_media(key, error.seconds + 1, type(error).__name__)
        except (errors.FileReferenceExpiredError, errors.FileReferenceEmptyError):
            try:
                payload = await self.refresh(job)
                self.store.retry_media(key, 1, 'FileReferenceRefreshed', payload=payload)
            except AUTH_ERRORS:
                raise
            except Exception as error:
                self.store.retry_media(key, 60, type(error).__name__)
        except Exception as error:
            self.store.retry_media(key, min(3600, 5 * 2 ** min(job['attempts'], 10)), type(error).__name__)
            self.logger.warning('Media pending key=%s type=%s', key, type(error).__name__)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            self.inflight.discard(key)
        return True

    async def worker(self):
        while True:
            worked = await self.once()
            await asyncio.sleep(0 if worked else 1)


class Delivery:
    def __init__(self, store, client, network, config, read_config, logger):
        self.store, self.client, self.network = store, client, network
        self.config, self.read_config, self.logger = config, read_config, logger
        self.next_send_at = 0
        self.archive = None
        if config.get('archive_mode') == 'forum':
            from archive import ForumArchive
            self.archive = ForumArchive(store, client, network, logger)

    def notify(self, text, public_source=False):
        current = self.read_config()
        if not current.get('admin_chat_id') or (public_source and not current.get('notify_public_deletions', True)):
            return
        if current.get('admin_user_id') is None or current['admin_chat_id'] != current['admin_user_id']:
            self.logger.error('Notification target does not match the authenticated owner; skipped')
            return
        heading, separator, body = text.partition('\n')
        formatted = '<b>'+escape(heading)+'</b>' + ('\n'+escape(body) if separator else '')
        with requests.post('https://api.telegram.org/bot' + current['bot_token'] + '/sendMessage',
                           json={'chat_id': current['admin_chat_id'], 'text': formatted, 'parse_mode': 'HTML'}, timeout=(5, 15)) as response:
            # Never log an HTTP exception/URL; the URL includes a bot token.
            data = response.json()
            if response.status_code != 200 or not data.get('ok'):
                raise RuntimeError('Bot API rejected notification')

    async def once(self):
        self.store.flush_digests()
        for job in self.store.pending():
            try:
                await asyncio.sleep(max(0, self.next_send_at - time.monotonic()))
                self.next_send_at = time.monotonic() + float(self.config.get('delivery_interval_seconds', 1))
                target = self.config['log_chat_id']
                send_options = {}
                if job['kind'] != 'notify' and self.archive:
                    target, send_options = await self.archive.destination(job)
                    job['text'] = self.archive.refresh_author(job)
                elif job['kind']=='notify' and self.archive and job.get('archive_key'):
                    await self.archive.display_name(job['archive_key'],job.get('archive_title') or 'Неизвестный автор')
                    job['text'] = self.archive.refresh_author(job)
                text, entities = rich_text(job['text'])
                if job['kind'] == 'notify':
                    await asyncio.to_thread(self.notify, job['text'], bool(job.get('public_source')))
                elif job['kind'] == 'media':
                    if Path(job['file_path']).is_file():
                        await self.network.transfer(lambda progress: self.client.send_file(target, job['file_path'],
                            caption=text, formatting_entities=entities, parse_mode=None, force_document=True, progress_callback=progress, **send_options))
                    else:
                        await self.network.call(lambda: self.client.send_message(target,
                            '⚠️ Медиа отсутствует в локальном архиве.', parse_mode=None, **send_options))
                else:
                    await self.network.call(lambda: self.client.send_message(target, text,
                        formatting_entities=entities, parse_mode=None, link_preview=False, **send_options))
                self.store.acknowledge(job['id'])
            except AUTH_ERRORS:
                raise
            except errors.FloodWaitError as error:
                self.store.retry(job['id'], error.seconds + 1)
                break
            except Exception as error:
                self.store.retry(job['id'], min(3600, 5 * 2 ** min(job['attempts'], 10)))
                self.logger.error('Delivery failed job=%s type=%s', job['id'], type(error).__name__)
            finally:
                self.next_send_at = max(self.next_send_at, time.monotonic() + float(self.config.get('delivery_interval_seconds', 1)))

    async def run(self):
        while True:
            await self.once()
            await asyncio.sleep(5)
