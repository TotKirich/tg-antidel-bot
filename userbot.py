
import os
import sqlite3
import json
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import Message
from apscheduler.schedulers.background import BackgroundScheduler

with open('config.json', 'r') as f:
    config = json.load(f)

API_ID = config['api_id']
API_HASH = config['api_hash']
SESSION_NAME = "userbot_session"
LOG_GROUP_ID = "me"
MEDIA_DIR = "media"
LOG_FILE = os.path.join(os.path.dirname(__file__), "log_main.txt")

os.makedirs(MEDIA_DIR, exist_ok=True)

def write_log(msg):
    timestamped = f"{datetime.now().isoformat()} - {msg}"
    print(timestamped)
    with open(LOG_FILE, "a") as f:
        f.write(timestamped + "\n")

import requests
import json
with open("config.json") as f:
    config = json.load(f)
BOT_token = config.get("bot_token")
ADMIN_CHAT_ID = config.get("admin_chat_id")

def notify_admin(msg):
    try:
        response = requests.post(
            url="https://api.telegram.org/bot" + BOT_token + "/sendMessage",
            json={"chat_id": json.load(open("config.json")).get("admin_chat_id", "me"), "text": msg, "parse_mode": "HTML"}
        )
        write_log(f"[notify] отправлено уведомление: {msg}")
    except Exception as e:
        print(f"[notify_error] {e}")

    timestamped = f"{datetime.now().isoformat()} - {msg}"
    print(timestamped)
    with open(LOG_FILE, "a") as f:
        f.write(timestamped + "\n")

chat_map = {}  # user_id -> created private chat_id
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

conn = sqlite3.connect("messages.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER,
    message_id INTEGER,
    user_id INTEGER,
    username TEXT,
    text TEXT,
    file_path TEXT,
    date_sent TEXT,
    deleted INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
)
""")
conn.commit()

def save_media(message: Message):
    if message.media:
        folder = os.path.join(MEDIA_DIR, datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(folder, exist_ok=True)
        if message.photo:
            ext = ".jpg"
        elif message.video:
            ext = ".mp4"
        elif message.voice:
            ext = ".ogg"
        elif message.audio:
            ext = ".mp3"
        elif message.sticker:
            ext = os.path.splitext(message.sticker.file_name)[1] if message.sticker.file_name else ".webp"
        elif message.video_note:
            ext = ".mp4"
        elif message.document and message.document.file_name:
            ext = os.path.splitext(message.document.file_name)[1] or ".bin"
        else:
            ext = ".bin"
        path = os.path.join(folder, f"{message.chat.id}_{message.id}{ext}")
        path = message.download(file_name=path)
        if path:
            write_log(f"[media] Скачано: {path} ({os.path.getsize(path)} байт)")
        else:
            write_log("[media] Не удалось скачать медиа")
        return path
    return None

@app.on_message(filters.incoming)
def handle_message(client, message):
    write_log("[in] сообщение получено")
    media_path = save_media(message)
    cur.execute("""
        INSERT OR REPLACE INTO messages
        (chat_id, message_id, user_id, username, text, file_path, date_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        message.chat.id,
        message.id,
        message.from_user.id if message.from_user else None,
        message.from_user.username if message.from_user else None,
        message.text or "",
        media_path,
        datetime.now().isoformat()
    ))
    conn.commit()
    if media_path:
        write_log("[db] записано в БД: медиа")
    else:
        write_log("[db] записано в БД: текст")

@app.on_deleted_messages()
def handle_deleted(client, messages):
    write_log(f"[del] удалено сообщений: {len(messages)}")
    notify_admin(f"❌ Удалено {len(messages)} сообщений")
    if not messages:
        return

    first_msg = messages[0]
    if not first_msg.chat:
        cur.execute("SELECT chat_id FROM messages WHERE message_id=? ORDER BY date_sent DESC LIMIT 1", (first_msg.id,))
        chat_row = cur.fetchone()
        if chat_row:
            msg_chat_id = chat_row[0]
        else:
            write_log("[fail] не удалось определить chat_id")
            return
    else:
        msg_chat_id = first_msg.chat.id

    cur.execute("SELECT user_id, username FROM messages WHERE chat_id=? AND message_id=?", (msg_chat_id, first_msg.id))
    user_row = cur.fetchone()
    if not user_row:
        write_log("[fail] не удалось определить пользователя для обработки")
        return

    user_id, username = user_row
    username = username or f"user{user_id}"

    if len(messages) >= 10:
        if user_id not in chat_map:
            title = f"❌Del {username}"
            try:
                chat = client.create_group(title=title, users=["me"])
                chat_map[user_id] = chat.id
                write_log(f"[group] создан чат {title} → {chat.id}")
            except Exception as e:
                write_log(f"[error] не удалось создать чат: {e}")
                return
        else:
            write_log(f"[group] найден чат для {username} → {chat_map[user_id]}")
        target_chat = chat_map[user_id]
    else:
        target_chat = LOG_GROUP_ID

    for msg in messages:
        cur.execute("SELECT text, file_path FROM messages WHERE chat_id=? AND message_id=?", (msg_chat_id, msg.id))
        row = cur.fetchone()
        if not row:
            write_log(f"[skip] нет записи в БД: chat_id={msg_chat_id}, msg_id={msg.id}")
            continue
        text, file_path = row

        if text.strip() or not file_path:
            try:
                log = f"❌ Сообщение удалено\n\n👤 Пользователь: @{username or user_id}\n💬 Текст:\n{text or 'Без текста'}"
                client.send_message(target_chat, log)
                write_log("[send] отправлен лог текстового сообщения")
            except Exception as e:
                write_log(f"[error] send_message: {e}")

        if file_path and os.path.exists(file_path):
            try:
                size = os.path.getsize(file_path)
                write_log(f"[media] отправка файла: {file_path}, размер: {size} байт")
                client.send_document(target_chat, file_path, caption=f"📎 Медиа удалённого сообщения от @{username or user_id}")
                write_log("[media] файл отправлен")
            except Exception as e:
                write_log(f"[error] send_document: {e}")

    cur.execute("UPDATE messages SET deleted=1 WHERE chat_id=? AND message_id IN (%s)" %
                ",".join("?" * len(messages)), [msg_chat_id] + [msg.id for msg in messages])
    conn.commit()

@app.on_edited_message()
def handle_edit(client, message):
    write_log("[edit] сообщение изменено")
    cur.execute("SELECT text FROM messages WHERE chat_id=? AND message_id=?", (message.chat.id, message.id))
    old = cur.fetchone()
    if old and old[0] != (message.text or ""):
        log = f"✏️ Сообщение изменено\n\n👤 Пользователь: @{message.from_user.username or message.from_user.id}\n🧾 Было:\n{old[0]}\n\n📄 Стало:\n{message.text}"
        try:
            client.send_message(LOG_GROUP_ID, log)
            write_log("[ok] текст изменения отправлен")
        except Exception as e:
            write_log(f"[error] send_message (edit): {e}")
        cur.execute("UPDATE messages SET text=? WHERE chat_id=? AND message_id=?", (message.text or "", message.chat.id, message.id))
        conn.commit()

def cleanup():
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    cur.execute("DELETE FROM messages WHERE date_sent < ?", (cutoff,))
    conn.commit()

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup, 'interval', days=1)
scheduler.start()

write_log("[run] бот запущен ✅")
app.run()
