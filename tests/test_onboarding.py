import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from authweb import AuthServer, PAGE, SCRIPT
from onboarding import prepare, wait_for_link
from runtime import read_config


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for module in ('runtime', 'onboarding'):
            context = patch(module + '.ROOT', self.root)
            context.start()
            self.addCleanup(context.stop)
        self.client = NS(connect=AsyncMock(), disconnect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=False), get_me=AsyncMock(return_value=NS(id=42)))
        self.factory = Mock(return_value=self.client)
        prepare(self.root)
        self.wizard = AuthServer(None, client_factory=self.factory, configure_notifications=True)
        self.http = TestClient(TestServer(self.wizard.app()))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.headers = {'Authorization': 'Bearer ' + self.wizard.key}

    async def submit(self, api_id='12345', api_hash='a'*32):
        self.wizard.next_attempt = 0
        return await self.http.post('/api/auth', headers=self.headers,
            json=dict(state='credentials', api_id=api_id, value=api_hash))

    async def test_clean_prepare_creates_private_data_and_forum_default(self):
        config = read_config()
        self.assertTrue((self.root/'media').is_dir())
        self.assertEqual(config['archive_mode'], 'forum')
        self.assertIsNone(config['api_id'])
        self.assertEqual(config['api_hash'], '')
        self.assertEqual(config['bot_token'], '')
        if os.name != 'nt':
            self.assertEqual((self.root/'config.json').stat().st_mode & 0o777, 0o600)

    async def test_credentials_then_login_without_secret_echo(self):
        self.assertEqual(self.wizard.state, 'credentials')
        response = await self.submit()
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['state'], 'phone')
        self.factory.assert_called_once_with(12345, 'a'*32)
        self.assertEqual(read_config()['api_id'], 12345)
        payload = await (await self.http.get('/api/auth', headers=self.headers)).text()
        self.assertNotIn('a'*32, payload)
        self.assertNotIn('12345', payload)
        self.assertNotIn('a'*32, PAGE)
        self.assertIn('https://my.telegram.org/apps', PAGE)
        self.assertIn('https://t.me/BotFather', PAGE)

    async def test_reconfigure_blanks_preserve_credentials_and_data(self):
        from runtime import update_config
        update_config(dict(api_id=12345, api_hash='b'*32, bot_token='private-token', admin_user_id=42))
        marker=self.root/'media'/'keep'; marker.write_bytes(b'keep')
        prepare(self.root, reconfigure=True)
        self.client.is_user_authorized.return_value=True
        response=await self.submit('', '')
        self.assertEqual((await response.json())['state'], 'notifications')
        self.factory.assert_called_once_with(12345, 'b'*32)
        self.assertEqual(read_config()['bot_token'], 'private-token')
        self.assertTrue(read_config()['setup_requested'])
        self.assertEqual(marker.read_bytes(), b'keep')
        self.assertNotIn('b'*32, await response.text())

    async def test_invalid_credentials_and_foreign_requests_do_not_create_client(self):
        self.assertEqual((await self.submit('abc', 'a'*32)).status, 400)
        self.assertEqual((await self.submit('123', '<unsafe>')).status, 400)
        self.assertEqual((await self.http.post('/api/auth', json=dict(state='credentials',value='a'*32,api_id='123'))).status,403)
        self.factory.assert_not_called()
        self.assertIsNone(read_config()['api_id'])

    async def test_failed_connection_preserves_previous_credentials(self):
        self.client.connect.side_effect=OSError('fixture-private-data')
        response=await self.submit()
        self.assertEqual(response.status, 503)
        self.assertNotIn('fixture-private-data',await response.text())
        self.assertIsNone(read_config()['api_id'])
        self.client.disconnect.assert_awaited_once()

    async def test_full_first_run_completes_only_after_bot_validation(self):
        self.client.send_code_request=AsyncMock(return_value=NS(phone_code_hash='fixture-hash',type=NS(),next_type=None,timeout=60))
        self.client.sign_in=AsyncMock()
        self.assertEqual((await (await self.submit()).json())['state'],'phone')
        self.wizard.next_attempt=0
        response=await self.http.post('/api/auth',headers=self.headers,
            json=dict(state='phone',value='+70000000000'))
        self.assertEqual((await response.json())['state'],'code')
        self.wizard.next_attempt=0
        response=await self.http.post('/api/auth',headers=self.headers,
            json=dict(state='code',value='00000'))
        self.assertEqual((await response.json())['state'],'notifications')
        self.assertFalse(self.wizard.finished.done())
        with patch('notification_setup.check_bot',new_callable=AsyncMock,
                return_value=dict(id=123,is_bot=True,username='fixture_bot')):
            self.wizard.next_attempt=0
            response=await self.http.post('/api/auth',headers=self.headers,
                json=dict(state='notifications',value='fixture-private-token'))
        self.assertEqual((await response.json())['state'],'done')
        self.assertTrue(self.wizard.finished.result())
        self.assertFalse(read_config()['setup_requested'])
        self.assertEqual(read_config()['admin_user_id'],42)
        self.assertEqual(read_config()['bot_token'],'fixture-private-token')
        self.assertNotIn('fixture-private-token',await response.text())

    async def test_reconfigure_refuses_other_owner(self):
        from runtime import update_config
        update_config(dict(admin_user_id=99))
        self.client.is_user_authorized.return_value=True
        self.assertEqual((await self.submit()).status,400)
        self.assertIsNone(read_config()['api_id'])
        self.client.disconnect.assert_awaited_once()

    async def test_terminal_prints_only_temporary_link(self):
        (self.root/'.auth-login.url').write_text('http://127.0.0.1:8080/#temporary-key')
        stream=io.StringIO()
        with redirect_stdout(stream):
            self.assertTrue(wait_for_link(self.root,timeout=1))
        self.assertIn('http://127.0.0.1:8080/#temporary-key',stream.getvalue())
        self.assertNotIn('api_hash',stream.getvalue())

    async def test_can_correct_api_without_restarting_wizard(self):
        await self.submit()
        self.wizard.next_attempt=0
        response=await self.http.post('/api/auth',headers=self.headers,
            json=dict(state='phone',value='',action='credentials'))
        self.assertEqual((await response.json())['state'],'credentials')
        self.client.disconnect.assert_awaited_once()
        self.assertIsNone(self.wizard.client)

    async def test_saved_keys_do_not_hide_required_login_link(self):
        from runtime import update_config
        update_config(dict(api_id=12345,api_hash='e'*32,bot_token='saved-token'))
        stream=io.StringIO()
        with redirect_stdout(stream):
            self.assertFalse(wait_for_link(self.root,timeout=.01))
        self.assertNotIn('Связь с Telegram подтверждена',stream.getvalue())

    async def test_bound_owner_cannot_silently_change_after_restart(self):
        from runtime import update_config,bind_owner
        update_config(dict(admin_user_id=42))
        with self.assertRaises(ValueError):
            bind_owner(99)
        self.assertEqual(read_config()['admin_user_id'],42)

    async def test_existing_configuration_is_not_overwritten(self):
        from runtime import update_config
        update_config(dict(api_id=12345, api_hash='c'*32, archive_mode='saved',auth_port=8787))
        prepare(self.root)
        self.assertEqual(read_config()['archive_mode'],'saved')
        self.assertEqual(read_config()['auth_port'],8787)
        self.assertEqual(read_config()['api_hash'],'c'*32)


@unittest.skipIf(os.name == 'nt', 'Linux shell launcher')
class LauncherTests(unittest.TestCase):
    def test_shell_syntax_and_help(self):
        script=Path(__file__).resolve().parents[1]/'start.sh'
        subprocess.run(['bash','-n',str(script)],check=True)
        help_result=subprocess.run(['bash',str(script),'--help'],capture_output=True,text=True,check=True)
        self.assertIn('--setup',help_result.stdout)

    def test_docker_first_launch_and_reconfigure_with_preserved_files(self):
        project=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); commands=root/'commands'
            docker=root/'docker'
            docker.write_text('''#!/usr/bin/env python3
import os,sys,subprocess,pathlib
with open(os.environ['COMMAND_LOG'],'a') as f: f.write(' '.join(sys.argv[1:])+'\\n')
args=sys.argv[1:]
if 'onboarding.py' in args:
    tail=args[args.index('onboarding.py')+1:]
    raise SystemExit(subprocess.call([sys.executable,'onboarding.py',*tail]))
if 'up' in args:
    (pathlib.Path(os.environ['TG_GUARD_DATA_DIR'])/'.auth-login.url').write_text('http://127.0.0.1:8080/#fixture-key')
''')
            docker.chmod(0o700)
            env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                TG_GUARD_DATA_DIR=str(root/'data'),COMMAND_LOG=str(commands),TG_GUARD_AUTH_HOST='127.0.0.1')
            result=subprocess.run(['bash',str(project/'start.sh'),'--docker'],env=env,capture_output=True,text=True,timeout=15,check=True)
            self.assertIn('http://127.0.0.1:8080/#fixture-key',result.stdout)
            config=json.loads((root/'data/config.json').read_text())
            config.update(api_id=12345,api_hash='d'*32,bot_token='keep-token')
            (root/'data/config.json').write_text(json.dumps(config))
            marker=root/'data/media/keep'; marker.write_bytes(b'keep')
            result=subprocess.run(['bash',str(project/'start.sh'),'--docker','--setup'],env=env,capture_output=True,text=True,timeout=15,check=True)
            self.assertNotIn('d'*32,result.stdout)
            self.assertNotIn('keep-token',result.stdout)
            self.assertEqual(marker.read_bytes(),b'keep')
            self.assertTrue(json.loads((root/'data/config.json').read_text())['setup_requested'])
            log=commands.read_text()
            self.assertIn('stop userbot',log)
            self.assertIn('up -d',log)
