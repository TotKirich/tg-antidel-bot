"""Telegram Delete Guard v2. Import-safe; service mode never prompts for login."""
import argparse
import asyncio
import os
import signal
import sys
import time

from telethon import TelegramClient, events
from authweb import AuthServer, web_authorize
from channels import ChannelPolicy
from recovery import Reconciler
from message_metadata import capture
from runtime import ROOT, bind_owner, instance_lock, read_config, sd_notify, setup_logging
from storage import Store
from stories import StoryMonitor
from transport import AUTH_ERRORS, Delivery, MediaQueue, Network, media_bytes


def record_message(message, sender):
    return capture(message, sender)


async def run(login=False):
    config = read_config()
    logger = setup_logging('userbot')
    # A distinct session: Pyrogram and Telethon session formats are incompatible.
    def make_client(api_id, api_hash):
        return TelegramClient(str(ROOT / 'guard_mtproto'), api_id, api_hash,
            timeout=10, request_retries=2, connection_retries=5, retry_delay=3,
            flood_sleep_threshold=0, sequential_updates=True, receive_updates=False)
    client = None
    tasks = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    fatal = loop.create_future()
    if not login:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
    try:
        if config.get('setup_requested') or not config.get('api_id') or not config.get('api_hash'):
            wizard = AuthServer(None, int(config['auth_port']), host=config['auth_host'],
                configure_notifications=True, client_factory=make_client)
            try:
                if not await wizard.wait(stop):
                    return 0
            finally:
                client = wizard.client
            config = read_config()
        else:
            client = make_client(int(config['api_id']), config['api_hash'])
        if login:
            await client.start()
            logger.info('Interactive authorization completed; session saved')
            return 0
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=30):
            Store(ROOT / 'messages.db').heartbeat('auth_required', 1)
            logger.warning('Authorization required; open the complete URL from .auth-login.url')
            if not await web_authorize(client, int(config['auth_port']), stop, host=config['auth_host']):
                return 0
        elif not config.get('bot_token'):
            if not await web_authorize(client, int(config['auth_port']), stop, settings_only=True, host=config['auth_host']):
                return 0
        config = read_config()
        store = Store(ROOT / 'messages.db')
        # Activate the fail-closed broadcast filter before any queue can run.
        store.heartbeat('auth_required', 0)
        network = Network()
        channels = ChannelPolicy(client, network, store, logger)
        media = MediaQueue(client, network, ROOT / 'media', config, logger, store)
        monitor = StoryMonitor(client, network, store, logger, int(config['stories_poll_seconds']))
        recovery = Reconciler(client, network, store, logger, config)
        delivery = Delivery(store, client, network, config, read_config, logger)
        me = await network.identity(client)
        await asyncio.to_thread(bind_owner, me.id)
        store.heartbeat('started_at', time.time())
        store.heartbeat('last_ok', time.time())
        store.heartbeat('stories_enabled', int(bool(config['stories_enabled'])))
        try:
            await channels.refresh()
        except AUTH_ERRORS:
            raise
        except Exception as error:
            logger.warning('Channel subscription sync pending type=%s',type(error).__name__)
        try:
            await monitor.refresh_contacts()
        except AUTH_ERRORS:
            raise
        except Exception as error:
            logger.warning('Contact names refresh pending type=%s',type(error).__name__)

        def fail(error):
            logger.error('Capture failed type=%s', type(error).__name__)
            if not fatal.done():
                fatal.set_exception(error)

        @client.on(events.NewMessage(incoming=True))
        async def handle_message(event):
            try:
                # Prevent loops when archives arrive in Saved Messages or a chosen log chat.
                if event.chat_id == me.id or event.chat_id == config['log_chat_id'] or await asyncio.to_thread(store.is_archive_chat, event.chat_id):
                    return
                message = event.message
                if not await channels.admit(event.chat_id, getattr(message, 'chat', None)):
                    return
                # Use cached sender only; metadata capture must not wait for a network lookup.
                record = record_message(message, message.sender)
                state = store.channel_state(event.chat_id) if event.chat_id < -1000000000000 else None
                if state and state['is_broadcast']:
                    record.update(chat_type='channel', chat_title=state['title'], chat_username=state['username'])
                await asyncio.to_thread(store.save, record,
                                        media_payload=media_bytes(message.media))
            except Exception as error:
                fail(error)

        @client.on(events.MessageDeleted())
        async def handle_deleted(event):
            try:
                if await asyncio.to_thread(store.is_archive_chat, event.chat_id) or not await channels.admit(event.chat_id):
                    return
                await recovery.deletions(event.chat_id, event.deleted_ids)
            except Exception as error:
                fail(error)

        @client.on(events.MessageEdited(incoming=True))
        async def handle_edit(event):
            try:
                if event.chat_id == me.id or await asyncio.to_thread(store.is_archive_chat, event.chat_id):
                    return
                if not await channels.admit(event.chat_id, getattr(event.message, 'chat', None)):
                    return
                store.edit(event.chat_id, event.id, event.raw_text or '')
                store.enrich_metadata(capture(event.message))
            except Exception as error:
                fail(error)

        @client.on(events.Raw())
        async def handle_raw(update):
            try:
                await channels.raw_update(update)
                await recovery.raw_update(update)
                if config['stories_enabled']:
                    await monitor.raw_update(update)
            except Exception as error:
                fail(error)

        async def health():
            while True:
                if (ROOT / '.notification-setup.request').exists():
                    await web_authorize(client, int(config['auth_port']), stop, settings_only=True, host=config['auth_host'])
                if time.monotonic() >= network.paused_until:
                    await network.identity(client)
                    store.heartbeat('last_ok', time.time())
                sd_notify('WATCHDOG=1')
                await asyncio.sleep(30)

        async def cleanup():
            while True:
                removed = await asyncio.to_thread(store.cleanup, int(config['retention_days']), ROOT / 'media')
                logger.info('Cleanup removed_files=%s', removed)
                await asyncio.sleep(3600)

        async def contact_names():
            while True:
                if not monitor.contacts_refreshed or time.monotonic()-monitor.contacts_refreshed>=3600:
                    try:
                        await monitor.refresh_contacts()
                    except AUTH_ERRORS:
                        raise
                    except Exception as error:
                        logger.warning('Contact names refresh pending type=%s',type(error).__name__)
                await asyncio.sleep(60)

        tasks = [asyncio.create_task(delivery.run()), asyncio.create_task(media.worker()),
                 asyncio.create_task(media.worker()), asyncio.create_task(health()),
                 asyncio.create_task(cleanup()), asyncio.create_task(stop.wait())]
        if config['stories_enabled']:
            tasks.append(asyncio.create_task(monitor.run()))
        else:
            tasks.append(asyncio.create_task(contact_names()))
        tasks.append(asyncio.create_task(recovery.run()))
        tasks.append(asyncio.create_task(channels.run()))
        if delivery.archive:
            tasks.append(asyncio.create_task(delivery.archive.maintain()))
        # Register handlers before fetching missed updates, after a normal connection.
        await network.call(lambda: client.set_receive_updates(True))
        await network.call(client.catch_up)
        sd_notify('READY=1')
        logger.info('Capture started; stories=%s', config['stories_enabled'])
        done, _ = await asyncio.wait([*tasks, fatal, client.disconnected],
                                     return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        return 0 if stop.is_set() else 1
    except AUTH_ERRORS:
        logger.error('Telegram authorization invalid; web login will reopen. Session will be archived.')
        return 78
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not fatal.done():
            fatal.cancel()
        if client is not None:
            await asyncio.wait_for(client.disconnect(), timeout=20)


async def serve(login=False):
    while True:
        result = await run(login)
        if result != 78 or login:
            return result
        # Telethon has closed the session DB in run(). Preserve rejected credentials
        # before creating a fresh auth key; never reuse Pyrogram's session format.
        session = ROOT / 'guard_mtproto.session'
        if session.exists():
            session.replace(ROOT / f'guard_mtproto.session.rejected.{time.time_ns()}')
        await asyncio.sleep(3)


if __name__ == '__main__':
    os.umask(0o077)
    from onboarding import prepare
    prepare()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login', action='store_true', help='Interactive Telegram authorization only')
    args = parser.parse_args()
    os.chdir(ROOT)  # Legacy relative media paths in messages.db remain valid.
    try:
        with instance_lock('userbot'):
            sys.exit(asyncio.run(serve(args.login)))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        setup_logging('userbot').error('Service failed type=%s', type(error).__name__)
        sys.exit(1)
