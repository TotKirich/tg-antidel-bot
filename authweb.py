"""Temporary authorization UI on an explicitly configured address. Never stores login secrets."""
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import time

from aiohttp import web
from telethon import errors, functions
from runtime import ROOT, sd_notify, read_config, update_config
from transport import AUTH_ERRORS
from notification_setup import BotConfigurationError, configure

PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delete Guard · Вход</title><style>
:root{color-scheme:dark;font:16px system-ui;background:#101820;color:#edf2f7}
body{margin:0;min-height:100vh;display:grid;place-items:center}main{width:min(420px,85vw);padding:36px;background:#192631;border:1px solid #33434f;border-radius:20px}
.brand{color:#69ddc0;font-size:13px;letter-spacing:.14em}h1{font-size:28px;margin:18px 0 12px}p{line-height:1.5;color:#bdcbd6}
label{display:block;margin-top:25px}input,button{box-sizing:border-box;width:100%;font:inherit;border-radius:9px;padding:13px;margin-top:9px}
input{background:#101820;border:1px solid #52616d;color:white}button{background:#69ddc0;color:#102820;border:0;cursor:pointer;font-weight:600;margin-top:20px}button:disabled{opacity:.5}
#error{color:#ffb9ac;min-height:24px}small{color:#94a8b7}a{color:#69ddc0}[hidden]{display:none!important}</style>
<main><div class="brand">TELEGRAM DELETE GUARD</div><h1 id="title">Вход в аккаунт</h1>
<p id="description">Загрузка состояния…</p>
<p id="api-help" hidden>Откройте <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer">my.telegram.org → API development tools</a>, создайте приложение и скопируйте App api_id и App api_hash. Сохранённые ключи не показываются; пустые поля сохраняют текущую пару.</p>
<p id="bot-help" hidden>Откройте <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a> → /newbot. Задайте имя и username бота, скопируйте выданный токен сюда. Затем откройте созданного бота и нажмите «Запустить» (/start), чтобы он мог присылать уведомления.</p>
<form id="form"><div id="api-fields" hidden><label for="api-id">API ID</label><input id="api-id" inputmode="numeric" autocomplete="off" placeholder="App api_id"></div><label id="label" for="value">Номер телефона</label>
<input id="value" required autocomplete="off" placeholder="+7…"><button id="submit">Продолжить</button></form>
<button id="resend" type="button" hidden>Запросить код повторно</button><button id="restart" type="button" hidden>Изменить номер</button>
<button id="qr-start" type="button" hidden>Войти по QR-коду</button><img id="qr-image" alt="QR входа в Telegram" hidden width="280" height="280" style="max-width:100%;background:white;margin-top:20px">
<button id="api-change" type="button" hidden>Изменить API ID / API hash</button>
<p id="error" role="alert"></p><small>После входа и настройки бота эта страница отключится. Код и пароль не сохраняются.</small></main>
<script src="/auth.js" defer></script></html>'''

SCRIPT = '''const key=location.hash.slice(1)||sessionStorage.getItem('guardAuthKey')||'';if(key)sessionStorage.setItem('guardAuthKey',key);history.replaceState(null,'',location.pathname);addEventListener('hashchange',()=>location.reload());
const form=document.querySelector('#form'),input=document.querySelector('#value'),button=document.querySelector('#submit'),resend=document.querySelector('#resend'),restart=document.querySelector('#restart');
let state='phone',resendReady=0,resendAllowed=false;
const qrStart=document.querySelector('#qr-start'),qrImage=document.querySelector('#qr-image');let qrVersion=-1,qrObject=null,polling=false;
async function request(body){const r=await fetch('/api/auth',{method:body?'POST':'GET',headers:{'Authorization':'Bearer '+key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const data=await r.json();document.querySelector('#api-change').hidden=!data.can_change_api;if(!r.ok){const e=new Error(data.message||'Не удалось выполнить запрос');e.data=data;throw e}return data}
document.querySelector('#api-change').addEventListener('click',()=>action('credentials'));
function show(data){state=data.state;document.querySelector('#api-fields').hidden=state!=='credentials';document.querySelector('#api-help').hidden=state!=='credentials';document.querySelector('#bot-help').hidden=state!=='notifications';document.querySelector('#error').textContent=data.message||'';qrStart.hidden=!['phone','code'].includes(state);form.hidden=['qr','done'].includes(state);qrImage.hidden=state!=='qr';if(state!=='qr'&&qrObject){URL.revokeObjectURL(qrObject);qrObject=null;qrImage.removeAttribute('src');qrVersion=-1}if(state==='qr'){document.querySelector('#description').textContent='На телефоне: Telegram → Настройки → Устройства → Подключить устройство. Отсканируйте QR и подтвердите вход. Код обновляется автоматически.';if(data.qr_version&&data.qr_version!==qrVersion){qrVersion=data.qr_version;fetch('/api/qr',{headers:{Authorization:'Bearer '+key}}).then(r=>{if(!r.ok)throw Error('QR недоступен');return r.blob()}).then(blob=>{if(state!=='qr')return;if(qrObject)URL.revokeObjectURL(qrObject);qrObject=URL.createObjectURL(blob);qrImage.src=qrObject}).catch(e=>{qrVersion=-1;document.querySelector('#error').textContent=e.message})}}resend.hidden=state!=='code';restart.hidden=state!=='code';resendAllowed=!!data.can_resend;resendReady=Date.now()+1000*(data.resend_after||0);tick();if(state==='qr')return;if(state==='done'){sessionStorage.removeItem('guardAuthKey');form.hidden=true;document.querySelector('#title').textContent='Готово';document.querySelector('#description').textContent='Мониторинг запускается. База и файлы сохраняются автоматически. Закрытая группа «Архив Telegram» создаётся в режиме forum. Не забудьте нажать /start в боте уведомлений. Временная страница отключается.';return}
const fields={credentials:['API hash','App api_hash','password','Шаг 1 из 3 · Ключи приложения Telegram'],phone:['Номер телефона','+7…','tel','Шаг 2 из 3 · Введите номер Telegram или войдите по QR-коду.'],code:['Код входа','Код входа','text',data.delivery_message||'Введите код выбранного Telegram способа доставки.'],password:['Пароль 2FA','Пароль','password','Введите пароль двухэтапной аутентификации.'],notifications:['Токен бота уведомлений','Токен BotFather','password','Шаг 3 из 3 · Введите токен нового бота или оставьте поле пустым для проверки сохранённого.']};const f=fields[state];document.querySelector('#title').textContent=state==='credentials'?'Настройка Telegram':state==='notifications'?'Уведомления':'Вход в аккаунт';document.querySelector('#label').textContent=f[0];input.placeholder=f[1];input.type=f[2];input.required=!['notifications','credentials'].includes(state);input.value='';document.querySelector('#api-id').value='';document.querySelector('#description').textContent=f[3];input.focus()}
function tick(){const seconds=Math.max(0,Math.ceil((resendReady-Date.now())/1000));resend.disabled=!resendAllowed||seconds>0;resend.textContent=!resendAllowed?'Telegram пока не предложил повторную доставку':seconds?'Повторить через '+seconds+' с':'Запросить код повторно'}setInterval(tick,1000);
async function action(name){resend.disabled=true;restart.disabled=true;button.disabled=true;try{show(await request({state,value:'',action:name}))}catch(e){if(e.data)show(e.data);document.querySelector('#error').textContent=e.message}finally{restart.disabled=false;button.disabled=false}}
resend.addEventListener('click',()=>action('resend'));restart.addEventListener('click',()=>action('restart'));
qrStart.addEventListener('click',()=>action('qr'));setInterval(async()=>{if(state!=='qr'||polling)return;polling=true;try{show(await request())}catch(e){document.querySelector('#error').textContent=e.message}finally{polling=false}},1500);
form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;const value=input.value,api_id=document.querySelector('#api-id').value.trim();input.value='';document.querySelector('#api-id').value='';try{show(await request({state,value,api_id}))}catch(e){if(e.data&&e.data.state!==state)show(e.data);document.querySelector('#error').textContent=e.message}finally{button.disabled=false}});
request().then(show).catch(e=>{form.hidden=true;document.querySelector('#description').textContent='Откройте полную ссылку из файла .auth-login.url на сервере.';document.querySelector('#error').textContent=e.message});'''


class AuthServer:
    def __init__(self, client, port=8787, url_file=None, configure_notifications=False, settings_only=False,
                 host='127.0.0.1', client_factory=None):
        self.client, self.port, self.host = client, port, host
        self.client_factory = client_factory
        self.url_file = Path(url_file or ROOT / '.auth-login.url')
        self.key = secrets.token_urlsafe(32)
        self.state, self.phone, self.code_hash = ('notifications' if settings_only else 'phone'), None, None
        if client is None:
            self.state = 'credentials'
        self.configure_notifications = configure_notifications
        self.finished = asyncio.get_running_loop().create_future()
        self.lock = asyncio.Lock()
        self.next_attempt = 0
        self.failures = 0
        self.delivery_type, self.next_delivery = None, None
        self.resend_at = 0
        self.qr_task, self.qr_svg, self.qr_version, self.qr_error = None, None, 0, None

    @web.middleware
    async def headers(self, request, handler):
        response = await handler(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def app(self):
        app = web.Application(client_max_size=4096, middlewares=[self.headers])
        app.router.add_get('/', self.page)
        app.router.add_get('/auth.js', self.script)
        app.router.add_get('/api/auth', self.api)
        app.router.add_post('/api/auth', self.api)
        app.router.add_get('/api/qr', self.qr_image)
        return app

    async def page(self, request):
        return web.Response(text=PAGE, content_type='text/html')

    async def script(self, request):
        return web.Response(text=SCRIPT, content_type='application/javascript')

    def response(self, message=None, status=200):
        payload = {'state': self.state, 'can_change_api': bool(self.client_factory and self.state in ('phone','code','password','notifications'))}
        if self.state == 'qr':
            payload['qr_version'] = self.qr_version
        if self.qr_error and not message:
            payload['message'] = self.qr_error
        if self.state == 'code':
            descriptions = {
                'SentCodeTypeApp': 'Telegram сообщил об отправке в служебный чат Telegram на уже авторизованных устройствах. Проверьте этот чат в приложении телефона и на компьютере.',
                'SentCodeTypeSms': 'Telegram выбрал SMS на указанный номер. Проверьте сообщения телефона.',
                'SentCodeTypeCall': 'Telegram выбрал голосовой звонок с кодом.',
                'SentCodeTypeFlashCall': 'Telegram выбрал короткий звонок; кодом служит номер входящего звонка.',
                'SentCodeTypeMissedCall': 'Telegram выбрал пропущенный звонок; нужны последние цифры номера звонящего.',
                'SentCodeTypeEmailCode': 'Telegram сообщил об отправке кода на почту, настроенную для входа.',
                'SentCodeTypeFirebaseSms': 'Telegram требует доставку SMS через официальный мобильный клиент. Если доступна повторная доставка, попробуйте её после таймера.',
                'SentCodeTypeSetUpEmailRequired': 'Telegram требует сначала настроить почту для входа в официальном приложении.',
                'SentCodeTypeFragmentSms': 'Telegram выбрал доставку через Fragment; сообщение в приложении Telegram не ожидается.',
                'SentCodeTypeSmsWord': 'Telegram выбрал SMS со словом вместо цифрового кода.',
                'SentCodeTypeSmsPhrase': 'Telegram выбрал SMS с фразой вместо цифрового кода.',
            }
            payload.update(delivery_type=self.delivery_type,
                delivery_message=descriptions.get(self.delivery_type, 'Telegram принял запрос кода. Способ доставки не распознан этой версией клиента.'),
                can_resend=self.next_delivery is not None,
                resend_after=max(0, math.ceil(self.resend_at-time.monotonic())))
        if message:
            payload['message'] = message
        return web.json_response(payload, status=status)

    async def api(self, request):
        if not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.key):
            return self.response('Нужна полная ссылка для входа.', 403)
        # Header authentication prevents cross-site requests; also reject foreign origins.
        origin = request.headers.get('Origin')
        allowed_origins = {f'http://{self.host}:{self.port}'}
        if self.host == '127.0.0.1':
            allowed_origins.add(f'http://localhost:{self.port}')
        if origin and origin not in allowed_origins:
            return self.response('Недопустимый источник запроса.', 403)
        if request.method == 'GET':
            return self.response()
        if self.lock.locked():
            return self.response('Предыдущий запрос ещё выполняется.', 409)
        if time.monotonic() < self.next_attempt:
            return self.response('Подождите перед следующей попыткой.', 429)
        if self.state == 'done':
            return self.response()
        try:
            data = await request.json()
            value = data.get('value', '')
            if not isinstance(value, str) or len(value) > 512 or data.get('state') != self.state:
                return self.response('Неверный этап входа.', 400)
            async with self.lock:
                self.next_attempt = time.monotonic() + 3
                action = data.get('action', 'submit')
                if action == 'credentials' and self.client_factory and self.state in ('phone','code','password','notifications'):
                    await asyncio.wait_for(self.client.disconnect(), 20)
                    self.client = None
                    self.state, self.phone, self.code_hash = 'credentials', None, None
                elif action == 'qr' and self.state in ('phone', 'code'):
                    self.phone, self.code_hash = None, None
                    self.state, self.qr_error = 'qr', None
                    self.qr_task = asyncio.create_task(self.qr_flow())
                    await asyncio.sleep(0)
                elif action == 'restart' and self.state == 'code':
                    self.state, self.phone, self.code_hash = 'phone', None, None
                    self.delivery_type, self.next_delivery = None, None
                elif action == 'resend' and self.state == 'code':
                    if self.next_delivery is None:
                        return self.response('Telegram не предложил повторную доставку.', 400)
                    if time.monotonic() < self.resend_at:
                        return self.response('Дождитесь окончания таймера Telegram.', 429)
                    sent = await asyncio.wait_for(self.client(functions.auth.ResendCodeRequest(
                        phone_number=self.phone, phone_code_hash=self.code_hash)), 30)
                    await self.accept_code(sent)
                elif action != 'submit':
                    return self.response('Неизвестное действие.', 400)
                elif self.state == 'credentials':
                    config = read_config()
                    api_id, api_hash = data.get('api_id', ''), value.strip()
                    if not isinstance(api_id, str) or (api_id and not re.fullmatch(r'[1-9][0-9]{0,9}', api_id)):
                        return self.response('API ID должен быть положительным числом.', 400)
                    if not api_id:
                        api_id = str(config.get('api_id') or '')
                    if not api_hash:
                        api_hash = config.get('api_hash') or ''
                    if not re.fullmatch(r'[1-9][0-9]{0,9}', api_id) or not re.fullmatch(r'[a-fA-F0-9]{32}', api_hash):
                        return self.response('Введите API ID и API hash из my.telegram.org/apps. При перенастройке пустые поля сохраняют текущие ключи.', 400)
                    candidate = self.client_factory(int(api_id), api_hash)
                    try:
                        await asyncio.wait_for(candidate.connect(), 30)
                        authorized = await asyncio.wait_for(candidate.is_user_authorized(), 30)
                        if authorized:
                            me = await asyncio.wait_for(candidate.get_me(), 30)
                            owner = config.get('admin_user_id')
                            if me is None or (owner is not None and me.id != owner):
                                raise ValueError('Owner mismatch')
                        update_config(dict(api_id=int(api_id), api_hash=api_hash))
                    except BaseException:
                        await candidate.disconnect()
                        raise
                    self.client = candidate
                    self.state = 'notifications' if authorized else 'phone'
                elif self.state == 'phone':
                    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', value.strip()):
                        return self.response('Нужен номер с кодом страны, например +7…', 400)
                    sent = await asyncio.wait_for(self.client.send_code_request(value.strip()), 30)
                    self.phone = value.strip()
                    await self.accept_code(sent)
                elif self.state == 'code':
                    await asyncio.wait_for(self.client.sign_in(phone=self.phone, code=value.strip(),
                                                              phone_code_hash=self.code_hash), 30)
                    self.authorized()
                elif self.state == 'password':
                    await asyncio.wait_for(self.client.sign_in(password=value), 30)
                    self.authorized()
                elif self.state == 'notifications':
                    await configure(self.client, value)
                    update_config(dict(setup_requested=False))
                    self.complete()
                return self.response()
        except errors.SessionPasswordNeededError:
            self.state = 'password'
            return self.response()
        except errors.PhoneCodeExpiredError:
            self.state, self.phone, self.code_hash = 'phone', None, None
            return self.response('Код истёк. Начните вход заново.', 400)
        except errors.FloodWaitError as error:
            self.next_attempt = time.monotonic() + error.seconds + 1
            self.resend_at = max(self.resend_at, self.next_attempt)
            return self.response('Telegram ограничил попытки. Повторите позже.', 429)
        except AUTH_ERRORS as error:
            if not self.finished.done():
                self.finished.set_exception(error)
            return self.response('Сессия отклонена. Страница будет создана заново.', 503)
        except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError, errors.PhoneNumberInvalidError):
            self.failures += 1
            if self.failures >= 5:
                self.next_attempt = time.monotonic() + 300
            return self.response('Проверьте введённые данные.', 400)
        except errors.RPCError as error:
            name = type(error).__name__
            logging.getLogger('userbot').warning('Login RPC rejected type=%s', name)
            messages = {
                'ApiIdInvalidError': 'Telegram отклонил api_id/api_hash. Проверьте пару ключей приложения в конфигурации.',
                'ApiIdPublishedFloodError': 'Telegram ограничил опубликованные ключи приложения. Используйте собственную пару api_id/api_hash.',
                'PhoneNumberBannedError': 'Telegram заблокировал вход для этого номера.',
                'PhoneNumberFloodError': 'Telegram ограничил запросы для номера. Не повторяйте запросы сейчас.',
                'SendCodeUnavailableError': 'Telegram сейчас не предоставляет другой способ доставки кода.',
                'AuthRestartError': 'Telegram просит начать вход заново. Измените номер и повторите позднее.',
            }
            return self.response(messages.get(name, 'Telegram отклонил запрос. Тип ошибки сохранён в журнале без номера и кода.'), 400)
        except BotConfigurationError as error:
            return self.response(str(error), 400)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return self.response('Некорректные данные запроса.', 400)
        except Exception:
            # No exception text: a library may include the phone number in it.
            return self.response('Не удалось связаться с Telegram. Повторите запрос.', 503)

    async def accept_code(self, sent):
        self.code_hash = getattr(sent, 'phone_code_hash', None)
        if self.code_hash is None:
            if await asyncio.wait_for(self.client.is_user_authorized(), 30):
                self.authorized()
                return
            raise ValueError('Missing code hash')
        self.delivery_type = type(getattr(sent, 'type', None)).__name__
        self.next_delivery = getattr(sent, 'next_type', None)
        self.resend_at = time.monotonic() + max(30, int(getattr(sent, 'timeout', None) or 60))
        logging.getLogger('userbot').info('Login delivery type=%s next=%s timeout=%s',
            self.delivery_type, type(self.next_delivery).__name__, max(30, int(getattr(sent, 'timeout', None) or 60)))
        self.state = 'code'

    async def qr_image(self, request):
        if not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.key):
            return web.Response(status=403)
        origin = request.headers.get('Origin')
        allowed = {f'http://{self.host}:{self.port}'}
        if self.host == '127.0.0.1':
            allowed.add(f'http://localhost:{self.port}')
        if origin and origin not in allowed:
            return web.Response(status=403)
        if self.state != 'qr' or not self.qr_svg:
            return web.Response(status=404)
        return web.Response(body=self.qr_svg, content_type='image/svg+xml')

    async def qr_flow(self):
        try:
            # Telethon enables updates before GetState, which a fresh session rejects.
            # UpdateLoginToken must still be received for the QR waiter.
            try:
                await asyncio.wait_for(self.client.set_receive_updates(True), 30)
            except errors.AuthKeyUnregisteredError:
                pass
            import qrcode
            from qrcode.image.svg import SvgPathFillImage
            while self.state == 'qr':
                login = await asyncio.wait_for(self.client.qr_login(), 30)
                waiter = asyncio.create_task(login.wait())
                try:
                    await asyncio.sleep(0)  # Install UpdateLoginToken handler before publishing QR.
                    self.qr_svg = qrcode.make(login.url, image_factory=SvgPathFillImage).to_string()
                    self.qr_version += 1
                    await waiter
                    self.qr_svg = None
                    self.authorized()
                    return
                except asyncio.TimeoutError:
                    self.qr_svg = None  # Expired token: export and publish a fresh one.
                finally:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
        except errors.SessionPasswordNeededError:
            self.state = 'password'
        except AUTH_ERRORS as error:
            if not self.finished.done():
                self.finished.set_exception(error)
        except Exception as error:
            logging.getLogger('userbot').warning('QR login failed type=%s', type(error).__name__)
            self.qr_error = 'QR-вход не завершён. Повторите позже; подробности типа ошибки в журнале.'
            if isinstance(error, errors.FloodWaitError):
                self.next_attempt = time.monotonic() + error.seconds + 1
            self.state = 'phone'
        finally:
            self.qr_svg = None
            try:
                await asyncio.wait_for(self.client.set_receive_updates(False), 10)
            except Exception as error:
                logging.getLogger('userbot').warning('QR update cleanup failed type=%s', type(error).__name__)

    def complete(self):
        self.state, self.phone, self.code_hash = 'done', None, None
        if not self.finished.done():
            self.finished.set_result(True)

    def authorized(self):
        self.phone, self.code_hash = None, None
        if self.configure_notifications:
            self.state = 'notifications'
        else:
            self.complete()

    async def wait(self, stop=None):
        runner = web.AppRunner(self.app(), access_log=None)
        stop_task = asyncio.create_task(stop.wait()) if stop else None
        try:
            await runner.setup()
            await web.TCPSite(runner, self.host, self.port).start()
            fd = os.open(self.url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(f'http://{self.host}:{self.port}/#{self.key}\n')
            sd_notify('READY=1\nSTATUS=Authorization required; see .auth-login.url')
            while not self.finished.done():
                sd_notify('WATCHDOG=1')
                await asyncio.wait([self.finished, *([stop_task] if stop_task else [])],
                                   timeout=30, return_when=asyncio.FIRST_COMPLETED)
                if stop_task and stop_task.done():
                    return False
            self.finished.result()
            await asyncio.sleep(1)  # Let the browser receive and render the success response.
            return True
        finally:
            if self.qr_task:
                self.qr_task.cancel()
                await asyncio.gather(self.qr_task, return_exceptions=True)
            if stop_task:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
            self.phone, self.code_hash = None, None
            self.url_file.unlink(missing_ok=True)
            await runner.cleanup()


async def web_authorize(client, port=8787, stop=None, settings_only=False, host='127.0.0.1'):
    success = await AuthServer(client, port, configure_notifications=True, settings_only=settings_only, host=host).wait(stop)
    if success:
        (ROOT / '.notification-setup.request').unlink(missing_ok=True)
    return success
