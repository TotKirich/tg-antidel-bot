# Synthetic phone fixtures; Telegram is mocked in these tests.
import asyncio
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from telethon import errors, types, functions
from authweb import AuthServer
import userbot


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.telegram = NS(send_code_request=AsyncMock(return_value=NS(phone_code_hash='hash')),
                           sign_in=AsyncMock(), is_user_authorized=AsyncMock(return_value=True))
        self.auth = AuthServer(self.telegram)
        self.http = TestClient(TestServer(self.auth.app()))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.headers = {'Authorization': 'Bearer ' + self.auth.key}
        self.addAsyncCleanup(self.stop_qr)

    async def stop_qr(self):
        if self.auth.qr_task:
            self.auth.qr_task.cancel()
            await asyncio.gather(self.auth.qr_task, return_exceptions=True)

    async def test_qr_is_protected_and_confirmation_advances_to_notifications(self):
        confirmed = asyncio.Event()
        login = NS(url='tg://login?token=private-fixture-token', wait=confirmed.wait)
        self.telegram.set_receive_updates = AsyncMock(side_effect=[errors.AuthKeyUnregisteredError(request=None), None])
        self.telegram.qr_login = AsyncMock(return_value=login)
        self.auth.configure_notifications = True
        response = await self.http.post('/api/auth', json=dict(state='phone', value='', action='qr'), headers=self.headers)
        self.assertEqual((await response.json())['state'], 'qr')
        for _ in range(100):
            if self.auth.qr_svg:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.auth.qr_svg)
        self.assertEqual((await self.http.get('/api/qr')).status, 403)
        response = await self.http.get('/api/qr', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertIn('image/svg+xml', response.headers['Content-Type'])
        self.assertNotIn('private-fixture-token', await response.text())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        confirmed.set()
        await asyncio.wait_for(self.auth.qr_task, 2)
        self.assertEqual(self.auth.state, 'notifications')
        self.assertIsNone(self.auth.qr_svg)
        self.telegram.set_receive_updates.assert_any_await(True)
        self.telegram.set_receive_updates.assert_any_await(False)

    async def test_qr_requires_two_factor_password_when_telegram_requests_it(self):
        self.telegram.set_receive_updates = AsyncMock()
        self.telegram.qr_login = AsyncMock(return_value=NS(url='tg://login?token=fixture',
            wait=AsyncMock(side_effect=errors.SessionPasswordNeededError(request=None))))
        self.auth.state = 'qr'
        await self.auth.qr_flow()
        self.assertEqual(self.auth.state, 'password')
        self.assertIsNone(self.auth.qr_svg)

    async def test_expired_qr_refreshes_and_cancellation_stops_waiter(self):
        waiting = asyncio.Event()
        self.telegram.set_receive_updates = AsyncMock()
        first = NS(url='tg://login?token=first', wait=AsyncMock(side_effect=asyncio.TimeoutError()))
        second = NS(url='tg://login?token=second', wait=waiting.wait)
        self.telegram.qr_login = AsyncMock(side_effect=[first, second])
        self.auth.state = 'qr'
        self.auth.qr_task = asyncio.create_task(self.auth.qr_flow())
        for _ in range(100):
            if self.auth.qr_version == 2:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.auth.qr_version, 2)
        await self.stop_qr()
        self.assertIsNone(self.auth.qr_svg)
        self.telegram.set_receive_updates.assert_any_await(False)

    async def post(self, state, value):
        self.auth.next_attempt = 0
        return await self.http.post('/api/auth', json={'state': state, 'value': value}, headers=self.headers)

    async def test_capability_required_and_foreign_origin_rejected(self):
        self.assertEqual((await self.http.get('/api/auth')).status, 403)
        response = await self.http.get('/api/auth', headers={**self.headers, 'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.http.get('/api/auth', headers=self.headers)).status, 200)

    async def test_lan_origin_requires_configured_address_and_capability(self):
        self.auth.host, self.auth.port = '192.0.2.10', 8080
        headers = {**self.headers, 'Origin': 'http://192.0.2.10:8080'}
        self.assertEqual((await self.http.get('/api/auth', headers=headers)).status, 200)
        self.assertEqual((await self.http.get('/api/auth', headers={'Origin': headers['Origin']})).status, 403)
        headers['Origin'] = 'http://192.0.2.10:8081'
        self.assertEqual((await self.http.get('/api/auth', headers=headers)).status, 403)
        headers['Origin'] = 'http://127.0.0.1:8080'
        self.assertEqual((await self.http.get('/api/auth', headers=headers)).status, 403)

    async def test_phone_code_and_two_factor_success_discards_secrets(self):
        response = await self.post('phone', '+70000000000')
        self.assertEqual((await response.json())['state'], 'code')
        self.telegram.sign_in.side_effect = errors.SessionPasswordNeededError(request=None)
        response = await self.post('code', '12345')
        self.assertEqual((await response.json())['state'], 'password')
        self.telegram.sign_in.side_effect = None
        response = await self.post('password', 'only-in-memory')
        self.assertEqual((await response.json())['state'], 'done')
        self.assertTrue(self.auth.finished.result())
        self.assertIsNone(self.auth.phone)
        self.assertIsNone(self.auth.code_hash)

    async def test_expired_code_returns_phone_step_without_exposing_phone(self):
        await self.post('phone', '+70000000000')
        self.telegram.sign_in.side_effect = errors.PhoneCodeExpiredError(request=None)
        response = await self.post('code', '12345')
        self.assertEqual(response.status, 400)
        data = await response.json()
        self.assertEqual(data['state'], 'phone')
        self.assertNotIn('70000000000', str(data))

    async def test_actual_delivery_method_and_timeout_are_shown_without_secrets(self):
        self.telegram.send_code_request.return_value = types.auth.SentCode(
            types.auth.SentCodeTypeApp(length=5), 'private-hash',
            next_type=types.auth.CodeTypeSms(), timeout=90)
        data = await (await self.post('phone', '+70000000000')).json()
        self.assertEqual(data['delivery_type'], 'SentCodeTypeApp')
        self.assertTrue(data['can_resend'])
        self.assertGreaterEqual(data['resend_after'], 89)
        self.assertIn('служебный чат', data['delivery_message'])
        self.assertNotIn('70000000000', str(data))
        self.assertNotIn('private-hash', str(data))

    async def test_resend_respects_timeout_and_uses_updated_hash(self):
        self.auth.client = AsyncMock(return_value=types.auth.SentCode(
            types.auth.SentCodeTypeSms(length=5), 'new-hash', timeout=60))
        self.auth.phone, self.auth.code_hash, self.auth.state = '+70000000000', 'old-hash', 'code'
        self.auth.next_delivery = types.auth.CodeTypeSms()
        self.auth.resend_at = time.monotonic() + 90
        self.auth.next_attempt = 0
        response = await self.http.post('/api/auth', json=dict(state='code', value='', action='resend'), headers=self.headers)
        self.assertEqual(response.status, 429)
        self.auth.client.assert_not_awaited()
        self.auth.next_attempt, self.auth.resend_at = 0, 0
        response = await self.http.post('/api/auth', json=dict(state='code', value='', action='resend'), headers=self.headers)
        self.assertEqual(response.status, 200)
        request = self.auth.client.call_args.args[0]
        self.assertIsInstance(request, functions.auth.ResendCodeRequest)
        self.assertEqual(request.phone_code_hash, 'old-hash')
        self.assertEqual(self.auth.code_hash, 'new-hash')
        self.assertEqual((await response.json())['delivery_type'], 'SentCodeTypeSms')

    async def test_restart_clears_phone_hash_and_returns_phone_step(self):
        await self.post('phone', '+70000000000')
        self.auth.next_attempt = 0
        response = await self.http.post('/api/auth', json=dict(state='code', value='', action='restart'), headers=self.headers)
        self.assertEqual((await response.json())['state'], 'phone')
        self.assertIsNone(self.auth.phone)
        self.assertIsNone(self.auth.code_hash)

    async def test_invalid_api_credentials_have_specific_safe_message(self):
        self.telegram.send_code_request.side_effect = errors.ApiIdInvalidError(request=None)
        response = await self.post('phone', '+70000000000')
        self.assertEqual(response.status, 400)
        data = await response.json()
        self.assertIn('api_id/api_hash', data['message'])
        self.assertNotIn('70000000000', str(data))

    async def test_bad_password_keeps_password_step(self):
        self.auth.state = 'password'
        self.telegram.sign_in.side_effect = errors.PasswordHashInvalidError(request=None)
        response = await self.post('password', 'bad')
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())['state'], 'password')
        self.assertFalse(self.auth.finished.done())

    async def test_rate_limit_and_duplicate_state(self):
        await self.post('phone', '+70000000000')
        response = await self.http.post('/api/auth', json={'state': 'code', 'value': '12345'}, headers=self.headers)
        self.assertEqual(response.status, 429)
        response = await self.post('phone', '+70000000000')
        self.assertEqual(response.status, 400)

    async def test_content_security_and_no_secret_in_html(self):
        response = await self.http.get('/')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertNotIn(self.auth.key, await response.text())

    async def test_rejected_session_propagates_for_reauthorization(self):
        self.telegram.send_code_request.side_effect = errors.AuthKeyUnregisteredError(request=None)
        self.assertEqual((await self.post('phone', '+70000000000')).status, 503)
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            self.auth.finished.result()

    async def test_server_closes_port_and_removes_link_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            link = Path(directory) / '.auth-login.url'
            server = AuthServer(self.telegram, port=port, url_file=link)
            task = asyncio.create_task(server.wait())
            try:
                for _ in range(100):
                    if link.exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(link.exists())
                async with ClientSession() as http:
                    async with http.get(f'http://127.0.0.1:{port}/') as response:
                        self.assertEqual(response.status, 200)
                server.complete()
                self.assertTrue(await asyncio.wait_for(task, 3))
                self.assertFalse(link.exists())
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_session_revocation_preserves_backup_and_reopens_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'guard_mtproto.session'
            original.write_bytes(b'rejected-session-fixture')
            with patch('userbot.ROOT', root), patch('userbot.run', new_callable=AsyncMock,
                side_effect=[78, 0]) as run, patch('userbot.asyncio.sleep', new_callable=AsyncMock):
                self.assertEqual(await userbot.serve(), 0)
            self.assertEqual(run.await_count, 2)
            self.assertFalse(original.exists())
            backups = list(root.glob('guard_mtproto.session.rejected.*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b'rejected-session-fixture')
