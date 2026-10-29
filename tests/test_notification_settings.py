# Synthetic phone fixtures; Telegram is mocked in these tests.
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from aiohttp.test_utils import TestClient, TestServer
from authweb import AuthServer
from notification_setup import BotConfigurationError, configure
from runtime import authorised, bind_owner, read_config, update_config
from storage import Store
from transport import Delivery, Network
import notify_bot
import docker_watchdog


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps(dict(bot_token='old', bot_id=1,
            admin_user_id=42, admin_chat_id=42, notify_public_deletions=True)))
        self.root_patch = patch('runtime.ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    async def test_owner_is_automatically_bound_as_recipient(self):
        update_config(dict(admin_user_id=None, admin_chat_id=999, notifications_enabled=True))
        bind_owner(42)
        self.assertEqual(read_config()['admin_chat_id'], 42)
        self.assertEqual(read_config()['admin_user_id'], 42)

    async def test_disabled_notifications_stay_disabled_after_rebinding(self):
        update_config(dict(notifications_enabled=False, admin_chat_id=None))
        bind_owner(42)
        self.assertIsNone(read_config()['admin_chat_id'])

    async def test_username_cannot_claim_owner_or_use_other_private_chat(self):
        self.assertFalse(authorised(dict(admin_username='owner'), NS(id=99, username='owner'), NS(id=99, type='private')))
        self.assertFalse(authorised(dict(admin_user_id=42), NS(id=42), NS(id=99, type='private')))

    async def test_notification_target_must_match_owner(self):
        delivery = Delivery(None, None, None, {}, lambda: dict(admin_user_id=42, admin_chat_id=99), Mock())
        with patch('transport.requests.post') as post:
            delivery.notify('private content')
        post.assert_not_called()

    async def test_new_token_checked_and_saved_with_authenticated_owner(self):
        with patch('notification_setup.check_bot', new_callable=AsyncMock,
                   return_value=dict(id=2, is_bot=True, username='newbot')):
            await configure(NS(get_me=AsyncMock(return_value=NS(id=42))), 'new-token')
        self.assertEqual(read_config()['bot_token'], 'new-token')
        self.assertEqual(read_config()['admin_chat_id'], 42)

    async def test_invalid_token_preserves_previous_config(self):
        with patch('notification_setup.check_bot', new_callable=AsyncMock, side_effect=BotConfigurationError('Invalid')):
            with self.assertRaises(BotConfigurationError):
                await configure(NS(), 'bad')
        self.assertEqual(read_config()['bot_token'], 'old')

    async def test_login_continues_to_notification_setup(self):
        client = NS(sign_in=AsyncMock())
        server = AuthServer(client, configure_notifications=True)
        server.state, server.phone, server.code_hash = 'code', '+70000000000', 'test'
        http = TestClient(TestServer(server.app()))
        await http.start_server()
        self.addAsyncCleanup(http.close)
        headers = {'Authorization': 'Bearer ' + server.key}
        response = await http.post('/api/auth', json={'state': 'code', 'value': '12345'}, headers=headers)
        self.assertEqual((await response.json())['state'], 'notifications')
        self.assertFalse(server.finished.done())
        server.next_attempt = 0
        with patch('authweb.configure', new_callable=AsyncMock) as setup:
            response = await http.post('/api/auth', json={'state': 'notifications', 'value': 'new-token'}, headers=headers)
        self.assertEqual((await response.json())['state'], 'done')
        setup.assert_awaited_once_with(client, 'new-token')

    async def test_public_filter_silences_bot_but_keeps_archive(self):
        store = Store(self.root / 'db')
        store.save(dict(chat_id=-1000000000123, message_id=1, user_id=-1000000000123,
            username='channel', first_name='Channel', last_name='', text='saved', file_path=None,
            date_sent='2026-10-27T00:00:00', chat_type='channel'))
        store.deleted(-1000000000123, 1)
        with patch('storage.time.time', return_value=time.time() + 40):
            store.flush_digests()
        self.assertTrue(next(j for j in store.pending() if j['kind'] == 'notify')['public_source'])
        sender = NS(send_message=AsyncMock())
        delivery = Delivery(store, sender, Network(), dict(log_chat_id='me', delivery_interval_seconds=0),
            lambda: dict(admin_user_id=42, admin_chat_id=42, bot_token='test', notify_public_deletions=False), Mock())
        with patch('transport.requests.post') as post:
            await delivery.once()
        post.assert_not_called()
        sender.send_message.assert_awaited_once()

    async def test_bot_buttons_and_settings_command(self):
        Store(self.root / 'messages.db')
        class FakeBot:
            def __init__(self):
                self.message_handlers, self.handlers = [], {}
                self.sent = []
            def message_handler(self, **kwargs):
                def register(fn):
                    self.message_handlers.append(fn)
                    self.handlers[fn.__name__] = fn
                    return fn
                return register
            def send_message(self, *args, **kwargs): self.sent.append((args,kwargs))
            def stop_polling(self): pass
            def infinity_polling(self, **kwargs):
                msg = NS(text='🔕 Паблики: выключить', from_user=NS(id=42), chat=NS(id=42, type='private'))
                self.handlers['handle'](msg)
                self.saved = read_config()['notify_public_deletions']
                msg.text = '/settings@TestBot'
                self.handlers['settings'](msg)
                msg.text = '📊 Статус'
                self.handlers['handle'](msg)
        bot = FakeBot()
        with patch('telebot.TeleBot', return_value=bot), patch('notify_bot.ROOT', self.root), \
             patch('notify_bot.setup_logging', return_value=Mock()):
            notify_bot.run_bot('old')
        self.assertFalse(bot.saved)
        self.assertTrue((self.root / '.notification-setup.request').exists())
        self.assertIn('Размер БД:', bot.sent[-1][0][1])
        self.assertIn('WAL/SHM:', bot.sent[-1][0][1])
        self.assertIn('Медиа:', bot.sent[-1][0][1])
        self.assertIn('Свободно:', bot.sent[-1][0][1])
        self.assertTrue(any(b['text']=='📊 Статус' for row in bot.sent[-1][1]['reply_markup'].keyboard for b in row))

    async def test_watchdog_terminates_child_when_pulse_is_stale(self):
        child = Mock()
        child.poll.return_value = None
        with patch.dict('os.environ', TG_GUARD_DATA_DIR=str(self.root)), \
             patch('docker_watchdog.subprocess.Popen', return_value=child), \
             patch('docker_watchdog.signal.signal'), \
             patch('docker_watchdog.time.time', side_effect=[1000, 1200]):
            self.assertEqual(docker_watchdog.run(), 1)
        child.terminate.assert_called()
        child.wait.assert_called_with(timeout=30)

    async def test_watchdog_stops_cleanly_on_sigterm(self):
        child = Mock()
        child.poll.return_value = None
        def register(sig, handler):
            if sig == docker_watchdog.signal.SIGTERM:
                handler()
        with patch.dict('os.environ', TG_GUARD_DATA_DIR=str(self.root)), \
             patch('docker_watchdog.subprocess.Popen', return_value=child), \
             patch('docker_watchdog.signal.signal', side_effect=register):
            self.assertEqual(docker_watchdog.run(), 0)
        child.terminate.assert_called()
