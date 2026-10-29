"""Administrator controls and health status. Importing never starts polling."""
import time
import threading

from runtime import ROOT, authorised, instance_lock, read_config, setup_logging, update_config
from storage import Store


def run_bot(token):
    import telebot
    from telebot import apihelper

    logger = setup_logging('notifybot')
    apihelper.CONNECT_TIMEOUT = 5
    apihelper.READ_TIMEOUT = 30
    bot = telebot.TeleBot(token, threaded=False)

    def allowed(message):
        if authorised(read_config(), message.from_user, message.chat):
            return True
        bot.send_message(message.chat.id, '⛔ У вас нет доступа.')
        return False

    @bot.message_handler(commands=['start', 'status'])
    def start(message):
        if not allowed(message):
            return
        config = read_config()
        if config['notifications_enabled']:
            update_config(dict(admin_chat_id=message.from_user.id))
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add('✅ Включить уведомления', '🚫 Выключить уведомления')
        markup.add('📢 Паблики: включить', '🔕 Паблики: выключить')
        markup.add('📊 Статус')
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

    @bot.message_handler(content_types=['text'])
    def handle(message):
        if not allowed(message):
            return
        config = read_config()
        if message.text in ('📊 Статус', 'Статус'):
            start(message)
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

    @bot.message_handler(commands=['settings', 'check_bot'])
    def settings(message):
        if not allowed(message):
            return
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
        bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=['message'])
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
