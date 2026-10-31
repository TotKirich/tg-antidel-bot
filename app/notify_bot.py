"""Administrator controls and health status. Importing never starts polling."""
import time
import threading
import re

from runtime import ROOT, authorised, instance_lock, read_config, setup_logging, update_config
from storage import Store


def run_bot(token):
    import telebot
    from telebot import apihelper

    logger = setup_logging('notifybot')
    apihelper.CONNECT_TIMEOUT = 5
    apihelper.READ_TIMEOUT = 30
    bot = telebot.TeleBot(token, threaded=False)
    channel_sessions = {}

    def allowed(message):
        if authorised(read_config(), message.from_user, message.chat):
            return True
        bot.send_message(message.chat.id, '⛔ У вас нет доступа.')
        return False

    def channel_menu(chat_id, page=0, selected=None, message_id=None):
        channel_sessions[chat_id] = time.monotonic() + 600
        if not (ROOT / 'messages.db').exists():
            bot.send_message(chat_id, 'Список пабликов появится после запуска сервиса.')
            return
        store = Store(ROOT / 'messages.db')
        rows, page, count = store.channel_page(page)
        markup = telebot.types.InlineKeyboardMarkup()
        if selected is None:
            body = ('🎯 Отключить точечно\nТолько каналы, на которые вы подписаны. Выберите канал.\n'
                    '💾 — сохранение и архив, 🔔 — уведомления бота.\n'
                    'Или отправьте @логин канала: отключатся и сохранение, и уведомления.\n'
                    'Общий переключатель уведомлений и фильтр пабликов также действуют.')
            for row in rows:
                title = row['title'] or ('@'+row['username'] if row['username'] else 'Без названия')
                state = ('💾' if row['capture_enabled'] else '⏸') + (' 🔔' if row['notifications_enabled'] else ' 🔕')
                markup.add(telebot.types.InlineKeyboardButton(state+' '+title[:55],
                    callback_data=f"channels:open:{row['chat_id']}:{page}"))
            if not count:
                body += '\nПодписанных каналов пока нет или список ещё не обновлён.'
            navigation = []
            if page:
                navigation.append(telebot.types.InlineKeyboardButton('←',callback_data=f'channels:list:{page-1}'))
            if (page+1)*6<count:
                navigation.append(telebot.types.InlineKeyboardButton('→',callback_data=f'channels:list:{page+1}'))
            if navigation:
                markup.row(*navigation)
        else:
            row = store.channel_state(selected)
            if not row or not row['subscribed'] or not row['is_broadcast']:
                bot.send_message(chat_id, 'Канал больше не находится в списке подписок. Откройте список заново.')
                return
            title = row['title'] or ('@'+row['username'] if row['username'] else 'Без названия')
            body = f"📢 {title}\nСохранение: {'включено' if row['capture_enabled'] else 'выключено'}"
            body += f"\nУведомления: {'включены' if row['notifications_enabled'] else 'выключены'}"
            body += ('\nОтключение сохранения останавливает новые записи, правки, перепроверки и отправку в архив. '
                     'Ранее сохранённые данные не удаляются. Уведомления требуют включённого сохранения.')
            for option, label in [('capture_enabled','💾 Сохранение'),('notifications_enabled','🔔 Уведомления')]:
                action = 'выключить' if row[option] else 'включить'
                short = 'save' if option=='capture_enabled' else 'notify'
                markup.add(telebot.types.InlineKeyboardButton(label+': '+action,
                    callback_data=f"channels:{short}:{selected}:{1-int(row[option])}:{page}"))
            markup.add(telebot.types.InlineKeyboardButton('← Список',callback_data=f'channels:list:{page}'))
        markup.add(telebot.types.InlineKeyboardButton('Закрыть раздел',callback_data='channels:exit'))
        if message_id is not None:
            bot.edit_message_text(body,chat_id=chat_id,message_id=message_id,reply_markup=markup)
        else:
            bot.send_message(chat_id,body,reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith('channels:')))
    def channels_callback(call):
        message = getattr(call,'message',None)
        if message is None or not authorised(read_config(),call.from_user,message.chat):
            bot.answer_callback_query(call.id,'Нет доступа.')
            return
        data = call.data or ''
        if data=='channels:exit':
            channel_sessions.pop(message.chat.id,None)
            bot.answer_callback_query(call.id)
            bot.send_message(message.chat.id,'Раздел закрыт. Ввод логина больше не меняет настройки.')
            return
        match = re.fullmatch(r'channels:list:(\d{1,6})',data)
        if match:
            bot.answer_callback_query(call.id)
            channel_menu(message.chat.id,int(match[1]),message_id=getattr(message,'message_id',None))
            return
        match = re.fullmatch(r'channels:(open|save|notify):(-\d{10,16}):(?:(0|1):)?(\d{1,6})',data)
        if not match or (match[1]=='open') != (match[3] is None):
            bot.answer_callback_query(call.id,'Некорректная кнопка.')
            return
        action, chat_id, enabled, page = match[1],int(match[2]),match[3],int(match[4])
        if action!='open':
            store = Store(ROOT/'messages.db')
            option = 'capture_enabled' if action=='save' else 'notifications_enabled'
            if not store.set_channel_option(chat_id,option,enabled=='1'):
                bot.answer_callback_query(call.id,'Канал больше не в подписках.')
                return
        bot.answer_callback_query(call.id,'Сохранено.' if action!='open' else None)
        channel_menu(message.chat.id,page,chat_id,message_id=getattr(message,'message_id',None))

    @bot.message_handler(commands=['start', 'status'])
    def start(message):
        if not allowed(message):
            return
        channel_sessions.pop(message.chat.id,None)
        config = read_config()
        if config['notifications_enabled']:
            update_config(dict(admin_chat_id=message.from_user.id))
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add('✅ Включить уведомления', '🚫 Выключить уведомления')
        markup.add('📢 Паблики: включить', '🔕 Паблики: выключить')
        markup.add('📊 Статус')
        markup.add('🎯 Отключить точечно')
        state = 'включены' if config.get('admin_chat_id') else 'выключены'
        body = f'Уведомления {state}.'
        body += '\nУдаления из каналов: ' + ('включены' if config['notify_public_deletions'] else 'выключены')
        if (ROOT / 'messages.db').exists():
            status = Store.read_status(ROOT / 'messages.db')
            age = time.time() - float(status.get('last_ok', 0))
            body += '\nTelegram: ' + ('требуется вход через веб-страницу' if status.get('auth_required') == '1'
                else 'недавно отвечал' if age < 120 else 'нет свежего подтверждения связи')
            body += f"\nОчередь отправки: {status['outbox']}\nСторис в БД: {status['stories']}"
            body += f"\nСкачивания: {status['media_pending']}\nНеоднозначные/ожидающие удаления: {status['deletion_pending']}"
            body += f"\nДиапазоны скрытой истории: {status['history_pending']}\nОжидающие сводки: {status['digest_pending']}"
            body += f"\nРазмер БД: {status.get('db_bytes', 0) / 1024**2:.2f} МиБ"
            body += f"\nБД со служебными WAL/SHM: {status.get('db_total_bytes', 0) / 1024**2:.2f} МиБ"
            body += f"\nМедиа: {status.get('media_bytes', 0) / 1024**2:.1f} МиБ"
            body += f"\nВсего данных сервиса: {status.get('data_bytes', 0) / 1024**2:.1f} МиБ"
            total, used, free = (status.get(key, 0) for key in ('disk_total_bytes','disk_used_bytes','disk_free_bytes'))
            percent = used/total*100 if total else 0
            body += f"\nРаздел: {used / 1024**3:.1f} / {total / 1024**3:.1f} ГиБ ({percent:.1f}% занято)"
            body += f"\nСвободно: {free / 1024**3:.1f} ГиБ; резерв: {int(config['min_free_mb'])} МиБ"
            if free < int(config['min_free_mb'])*1024**2*2:
                body += '\n⚠️ Мало места: свободный объём меньше двойного резерва. Скачивания при достижении резерва приостанавливаются.'
        else:
            body += '\nUserbot ещё не создал БД.'
        bot.send_message(message.chat.id, body, reply_markup=markup)

    @bot.message_handler(commands=['channels'])
    def channels_command(message):
        if allowed(message):
            channel_menu(message.chat.id)

    @bot.message_handler(content_types=['text'])
    def handle(message):
        if not allowed(message):
            return
        config = read_config()
        if message.text not in ('🎯 Отключить точечно','⚙️ Паблики') and not re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}',message.text or ''):
            channel_sessions.pop(message.chat.id,None)
        if message.text in ('📊 Статус', 'Статус'):
            start(message)
        elif message.text in ('🎯 Отключить точечно','⚙️ Паблики'):
            channel_menu(message.chat.id)
        elif message.text == '✅ Включить уведомления':
            update_config(dict(admin_user_id=message.from_user.id, admin_chat_id=message.chat.id, notifications_enabled=True))
            bot.send_message(message.chat.id, '✅ Уведомления включены.')
        elif message.text == '🚫 Выключить уведомления':
            update_config(dict(admin_chat_id=None, notifications_enabled=False))
            bot.send_message(message.chat.id, '🚫 Уведомления отключены.')
        elif message.text in ('📢 Паблики: включить', '🔕 Паблики: выключить'):
            enabled = message.text.startswith('📢')
            update_config(dict(notify_public_deletions=enabled))
            bot.send_message(message.chat.id, 'Уведомления удаления из каналов ' + ('включены.' if enabled else 'выключены. Архив продолжает сохраняться.'))
        elif channel_sessions.get(message.chat.id,0)>time.monotonic():
            username = (message.text or '').removeprefix('@')
            if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}',username):
                store = Store(ROOT/'messages.db')
                chat_id = store.block_channel_username(username)
                if chat_id is None:
                    bot.send_message(message.chat.id,'Канал не найден среди ваших подписок. Список обновляется автоматически; попробуйте выбрать его кнопкой.')
                else:
                    bot.send_message(message.chat.id,'Сохранение и уведомления для @'+username+' выключены.')
                    channel_menu(message.chat.id,selected=chat_id)

    @bot.message_handler(commands=['settings', 'check_bot'])
    def settings(message):
        if not allowed(message):
            return
        channel_sessions.pop(message.chat.id,None)
        if message.text.split()[0].split('@')[0] == '/settings':
            (ROOT / '.notification-setup.request').touch()
            bot.send_message(message.chat.id, 'Форма проверки/замены токена откроется в течение 30 секунд. Откройте полную ссылку из .auth-login.url на сервере. Для loopback-адреса нужен SSH-туннель. Токен сюда не отправляйте.')
        else:
            try:
                identity = bot.get_me()
                bot.send_message(message.chat.id, f'API отвечает: @{identity.username}, ID {identity.id}')
            except Exception:
                bot.send_message(message.chat.id, 'Проверка API не удалась. Для настройки используйте локальную веб-форму.')

    # Register command handler before the catch-all text handler.
    bot.message_handlers.insert(0, bot.message_handlers.pop())

    finished = threading.Event()
    def watch_token():
        while not finished.wait(2):
            try:
                if read_config().get('bot_token') != token:
                    bot.stop_polling()
                    return
            except (OSError, ValueError):
                pass
    threading.Thread(target=watch_token, daemon=True).start()

    logger.info('Notification bot started')
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=['message','callback_query'])
    finally:
        finished.set()
        bot.stop_polling()


def run():
    while True:
        try:
            token = read_config().get('bot_token')
        except FileNotFoundError:
            time.sleep(2)
            continue
        if not token:
            time.sleep(2)
            continue
        try:
            run_bot(token)
        except Exception as error:
            setup_logging('notifybot').error('Notification polling failed type=%s', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    import os
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with instance_lock('notifybot'):
        run()
