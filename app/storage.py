"""SQLite persistence shared by capture, recovery and story monitoring."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time
import secrets
import os
import shutil
from message_metadata import EXTRA_FIELDS, label, render, author, short_date, archive_chunks

BASE_FIELDS = ('chat_id', 'message_id', 'user_id', 'username', 'first_name', 'last_name', 'text', 'file_path', 'date_sent')
CAPTURE_FIELDS = BASE_FIELDS + EXTRA_FIELDS + ('raw_message',)
ROW_FIELDS = ','.join(BASE_FIELDS + EXTRA_FIELDS + ('deleted', 'revision', 'checked_at', 'missing_since', 'deletion_source', 'metadata_refreshed_at'))


def chunks(text, size=1800):
    # At most 3600 UTF-16 units, including all-emoji messages.
    return [text[i:i + size] for i in range(0, len(text), size)] or ['Без текста']


def disk_status(root):
    root = Path(root)
    data_bytes, media_bytes = 0, 0
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if not (Path(directory)/name).is_symlink()]
        for name in files:
            path = Path(directory)/name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            data_bytes += size
            if path.is_relative_to(root/'media'):
                media_bytes += size
    disk = shutil.disk_usage(root)
    return dict(data_bytes=data_bytes, media_bytes=media_bytes,
                disk_total_bytes=disk.total, disk_used_bytes=disk.used, disk_free_bytes=disk.free)


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > 8:
                raise RuntimeError('Database schema is newer than this application')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER, message_id INTEGER, user_id INTEGER, username TEXT,
                first_name TEXT, last_name TEXT, text TEXT, file_path TEXT,
                date_sent TEXT, deleted INTEGER DEFAULT 0,
                PRIMARY KEY(chat_id, message_id))''')
            if 'revision' not in {r['name'] for r in db.execute('PRAGMA table_info(messages)')}:
                db.execute('ALTER TABLE messages ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(messages)')}
            for name, definition in [('checked_at', 'REAL NOT NULL DEFAULT 0'),
                                     ('missing_since', 'REAL'), ('deletion_source', 'TEXT'),
                                     ('metadata_refreshed_at', 'REAL'), ('raw_message', 'BLOB'),
                                     *[(f, 'INTEGER' if f in ('grouped_id', 'reply_to_id') else 'TEXT') for f in EXTRA_FIELDS]]:
                if name not in columns:
                    db.execute(f'ALTER TABLE messages ADD COLUMN {name} {definition}')
            db.executescript('''
                CREATE INDEX IF NOT EXISTS messages_by_id ON messages(message_id);
                CREATE INDEX IF NOT EXISTS messages_by_date ON messages(date_sent);
                CREATE TABLE IF NOT EXISTS edits (
                    chat_id INTEGER, message_id INTEGER, revision INTEGER,
                    old_text TEXT, new_text TEXT, edited_at REAL,
                    PRIMARY KEY(chat_id, message_id, revision));
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, kind TEXT NOT NULL,
                    text TEXT NOT NULL, file_path TEXT, attempts INTEGER DEFAULT 0,
                    next_try REAL DEFAULT 0, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS stories (
                    owner_id INTEGER, story_id INTEGER, caption TEXT, published_at REAL,
                    expires_at REAL, status TEXT NOT NULL DEFAULT 'active', file_path TEXT,
                    PRIMARY KEY(owner_id, story_id));
                CREATE TABLE IF NOT EXISTS health (name TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS media_jobs (
                    job_key TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL, payload BLOB NOT NULL, created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    last_error TEXT);
                CREATE INDEX IF NOT EXISTS media_jobs_due ON media_jobs(next_try);
                CREATE TABLE IF NOT EXISTS deletion_signals (
                    signal_key TEXT PRIMARY KEY, chat_id INTEGER, message_id INTEGER NOT NULL,
                    created_at REAL NOT NULL, next_try REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS messages_recheck ON messages(deleted,checked_at);
                CREATE TABLE IF NOT EXISTS deletion_digests (
                    digest_key TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, source TEXT NOT NULL,
                    count INTEGER NOT NULL, ready_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS history_gaps (
                    chat_id INTEGER PRIMARY KEY, max_id INTEGER NOT NULL,
                    cursor INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS deletion_signals_due ON deletion_signals(resolved,next_try,created_at,signal_key);
                CREATE INDEX IF NOT EXISTS deletion_digests_due ON deletion_digests(ready_at);
            ''')
            media_columns = {r['name'] for r in db.execute('PRAGMA table_info(media_jobs)')}
            for name, definition in [('sha256', 'TEXT'), ('staged_size', 'INTEGER'), ('media_identity', 'TEXT')]:
                if name not in media_columns:
                    db.execute(f'ALTER TABLE media_jobs ADD COLUMN {name} {definition}')
            if 'priority' not in {r['name'] for r in db.execute('PRAGMA table_info(outbox)')}:
                db.execute('ALTER TABLE outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 1')
                db.execute("UPDATE outbox SET priority=0 WHERE kind='notify'")
            db.execute('CREATE INDEX IF NOT EXISTS outbox_due ON outbox(priority,next_try,id)')
            for table in ('outbox', 'deletion_digests'):
                if 'public_source' not in {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN public_source INTEGER NOT NULL DEFAULT 0')
            for name in ('archive_key', 'archive_title'):
                if name not in {r['name'] for r in db.execute('PRAGMA table_info(outbox)')}:
                    db.execute(f'ALTER TABLE outbox ADD COLUMN {name} TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS archive_routes (
                route_key TEXT PRIMARY KEY, title TEXT NOT NULL, marker TEXT NOT NULL,
                random_id INTEGER NOT NULL, target_id INTEGER, access_hash INTEGER, topic_id INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS archive_replayed (
                chat_id INTEGER, message_id INTEGER, PRIMARY KEY(chat_id,message_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS contact_names (
                user_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, username TEXT, updated_at REAL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS channel_settings (
                chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
                is_broadcast INTEGER NOT NULL DEFAULT 1, subscribed INTEGER NOT NULL DEFAULT 0,
                capture_enabled INTEGER NOT NULL DEFAULT 1, notifications_enabled INTEGER NOT NULL DEFAULT 1,
                verified_at REAL NOT NULL DEFAULT 0)''')
            if 'source_chat_id' not in {r['name'] for r in db.execute('PRAGMA table_info(outbox)')}:
                db.execute('ALTER TABLE outbox ADD COLUMN source_chat_id INTEGER')
            db.execute('PRAGMA user_version=8')

    def activate_channel_policy(self):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO health VALUES ('channel_policy_active','1')")
            db.execute('UPDATE channel_settings SET verified_at=0')

    @staticmethod
    def channel_clause(owner):
        # Column names passed here are internal constants, never bot input.
        return f'''(NOT EXISTS (SELECT 1 FROM health WHERE name='channel_policy_active' AND value='1')
            OR {owner}>-1000000000000 OR EXISTS (SELECT 1 FROM channel_settings c
            WHERE c.chat_id={owner} AND (c.is_broadcast=0 OR
            (c.subscribed=1 AND c.capture_enabled=1 AND c.verified_at>strftime('%s','now')-600))))'''

    @classmethod
    def channel_permitted(cls, db, chat_id, notifications=False):
        if chat_id is None or chat_id > -1000000000000:
            return True
        allowed = bool(db.execute(f'SELECT {cls.channel_clause("?")}',
                                 (chat_id, chat_id)).fetchone()[0])
        if allowed and notifications:
            row = db.execute('SELECT is_broadcast,notifications_enabled FROM channel_settings WHERE chat_id=?', (chat_id,)).fetchone()
            return row is None or not row['is_broadcast'] or bool(row['notifications_enabled'])
        return allowed

    def channel_allowed(self, chat_id, notifications=False):
        with self.connection() as db:
            return self.channel_permitted(db, chat_id, notifications)

    def channel_state(self, chat_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM channel_settings WHERE chat_id=?', (chat_id,)).fetchone()
            return dict(row) if row else None

    def set_channel_membership(self, chat_id, subscribed, title='', username='', is_broadcast=True):
        with self.connection() as db:
            db.execute('''INSERT INTO channel_settings(chat_id,title,username,is_broadcast,subscribed,verified_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                title=CASE WHEN excluded.title!='' THEN excluded.title ELSE title END,
                username=excluded.username,is_broadcast=excluded.is_broadcast,
                subscribed=excluded.subscribed,verified_at=excluded.verified_at''',
                (chat_id, label(title, 100), label(username, 40), int(is_broadcast), int(subscribed), time.time()))

    def invalidate_channel(self, chat_id):
        with self.connection() as db:
            db.execute('UPDATE channel_settings SET verified_at=0 WHERE chat_id=?', (chat_id,))

    def sync_channels(self, peers):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE channel_settings SET subscribed=0 WHERE is_broadcast=1')
            for peer in peers:
                db.execute('''INSERT INTO channel_settings(chat_id,title,username,is_broadcast,subscribed,verified_at)
                    VALUES (?,?,?,?,1,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                    username=excluded.username,is_broadcast=excluded.is_broadcast,subscribed=1,verified_at=excluded.verified_at''',
                    (peer['chat_id'], label(peer['title'],100), label(peer['username'],40), int(peer['is_broadcast']), time.time()))
            db.execute("INSERT OR REPLACE INTO health VALUES ('channels_synced',?)", (str(time.time()),))

    def channel_page(self, page=0, size=6):
        with self.connection() as db:
            count = db.execute('SELECT COUNT(*) FROM channel_settings WHERE is_broadcast=1 AND subscribed=1').fetchone()[0]
            page = min(max(0, page), max(0, (count-1)//size))
            rows = db.execute('''SELECT * FROM channel_settings WHERE is_broadcast=1 AND subscribed=1
                ORDER BY title COLLATE NOCASE,chat_id LIMIT ? OFFSET ?''', (size, page*size))
            return [dict(row) for row in rows], page, count

    def set_channel_option(self, chat_id, option, enabled):
        if option not in ('capture_enabled','notifications_enabled') or type(enabled) is not bool:
            raise ValueError('Invalid channel option')
        with self.connection() as db:
            return bool(db.execute(f'''UPDATE channel_settings SET {option}=?
                WHERE chat_id=? AND subscribed=1 AND is_broadcast=1''', (int(enabled),chat_id)).rowcount)

    def block_channel_username(self, username):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT chat_id FROM channel_settings WHERE username=? COLLATE NOCASE
                AND subscribed=1 AND is_broadcast=1''', (username,)).fetchall()
            if len(rows)!=1:
                return None
            chat_id = rows[0]['chat_id']
            db.execute('UPDATE channel_settings SET capture_enabled=0,notifications_enabled=0 WHERE chat_id=?', (chat_id,))
            return chat_id

    @staticmethod
    def source_chat_id(job):
        chat_id = job.get('source_chat_id')
        if chat_id is None:
            # Upgrade queues from v7 without parsing text or inventing an author.
            parts = job['event_key'].split(':')
            if len(parts)>1 and parts[0] in ('delete','edit','digest','forum-replay'):
                try:
                    chat_id = int(parts[1])
                except ValueError:
                    pass
        return chat_id

    def delivery_allowed(self, job):
        chat_id = self.source_chat_id(job)
        if chat_id is None and job.get('public_source'):
            with self.connection() as db:
                if db.execute("SELECT 1 FROM health WHERE name='channel_policy_active' AND value='1'").fetchone():
                    return False
        return self.channel_allowed(chat_id, notifications=job['kind']=='notify')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def enqueue(db, key, kind, text, file_path=None, public_source=False, archive_key=None, archive_title=None, source_chat_id=None):
        db.execute('''INSERT OR IGNORE INTO outbox(event_key,kind,text,file_path,created_at,priority,public_source,archive_key,archive_title,source_chat_id)
                      VALUES (?,?,?,?,?,?,?,?,?,?)''', (key, kind, text, file_path, time.time(), 0 if kind == 'notify' else 1,
                      int(public_source), archive_key, archive_title, source_chat_id))

    @staticmethod
    def archive_context(row):
        if row['user_id'] and row['user_id'] > 0 and row['sender_kind'] not in ('channel', 'sender_chat'):
            title = ' '.join(filter(None, (row['first_name'], row['last_name']))) or row['username']
            return dict(archive_key=f"user:{row['user_id']}",
                        archive_title=label(title or f"Пользователь {row['user_id']}", 90), source_chat_id=row['chat_id'])
        title = row['chat_title'] or ' '.join(filter(None, (row['first_name'], row['last_name'])))
        return dict(archive_key=f"chat:{row['chat_id']}",
                    archive_title=label(title or f"Чат {row['chat_id']}", 90), source_chat_id=row['chat_id'])

    def archive_route(self, key, title):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO archive_routes(route_key,title,marker,random_id) VALUES (?,?,?,?)',
                       (key, label(title, 90), secrets.token_hex(5), secrets.randbits(63)))
            return dict(db.execute('SELECT * FROM archive_routes WHERE route_key=?', (key,)).fetchone())

    def save_archive_route(self, key, target_id, access_hash, topic_id=None):
        with self.connection() as db:
            db.execute('UPDATE archive_routes SET target_id=?,access_hash=?,topic_id=? WHERE route_key=?',
                       (target_id, access_hash, topic_id, key))

    def is_archive_chat(self, chat_id):
        if chat_id is None or chat_id >= -1000000000000:
            return False
        with self.connection() as db:
            return db.execute("SELECT 1 FROM archive_routes WHERE route_key='root' AND target_id=?",
                              (-1000000000000-chat_id,)).fetchone() is not None

    def replay_deleted_archive(self, limit=100):
        # One-time relocation; never changes deletion state or duplicates bot digests.
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(f'''SELECT {ROW_FIELDS} FROM messages m WHERE deleted=1 AND NOT EXISTS
                (SELECT 1 FROM archive_replayed a WHERE a.chat_id=m.chat_id AND a.message_id=m.message_id)
                ORDER BY date_sent,chat_id,message_id LIMIT ?''', (limit,)).fetchall()
            for row in rows:
                key = f"forum-replay:{row['chat_id']}:{row['message_id']}"
                display = self.display_record(db,row)
                body = '📂 Архив ранее восстановленного сообщения\n' + render(display)
                context = self.archive_context(display)
                for i, part in enumerate(archive_chunks(body)):
                    self.enqueue(db, f'{key}:text:{i}', 'archive', part, **context)
                if row['file_path']:
                    self.enqueue(db, key+':media', 'media', body[:450], row['file_path'], **context)
                db.execute('INSERT INTO archive_replayed VALUES (?,?)', (row['chat_id'],row['message_id']))
            return len(rows)

    def archive_topics(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM archive_routes WHERE topic_id IS NOT NULL')]

    def set_archive_title(self, key, title):
        with self.connection() as db:
            db.execute('UPDATE archive_routes SET title=? WHERE route_key=?', (label(title, 90), key))

    def enrich_user(self, user_id, first_name, last_name, username):
        with self.connection() as db:
            db.execute('''UPDATE messages SET first_name=COALESCE(NULLIF(first_name,''),?),
                last_name=COALESCE(NULLIF(last_name,''),?),username=COALESCE(NULLIF(username,''),?) WHERE user_id=?''',
                (first_name,last_name,username,user_id))

    def archive_record(self, chat_id, message_id):
        with self.connection() as db:
            row = db.execute(f'SELECT {ROW_FIELDS} FROM messages WHERE chat_id=? AND message_id=?', (chat_id,message_id)).fetchone()
            return self.display_record(db,row) if row else None

    def replace_contacts(self, contacts):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM contact_names')
            db.executemany('INSERT INTO contact_names VALUES (?,?,?,?,?)',
                           [(user_id,first,last,username,time.time()) for user_id,first,last,username in contacts])

    def contact_title(self, user_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM contact_names WHERE user_id=?',(user_id,)).fetchone()
            return author(row) if row and (row['first_name'] or row['last_name'] or row['username']) else None

    @staticmethod
    def display_record(db, row):
        result = dict(row)
        contact = db.execute('SELECT first_name,last_name,username FROM contact_names WHERE user_id=?',
                             (result.get('user_id'),)).fetchone()
        if contact and any(contact[key] for key in ('first_name','last_name','username')):
            for key in ('first_name','last_name','username'):
                result[key] = contact[key]
        return result

    def save(self, record, media_payload=None):
        record = {**dict.fromkeys(EXTRA_FIELDS + ('raw_message',)), **record}
        with self.connection() as db:
            if not self.channel_permitted(db, record['chat_id']):
                return False
            cursor = db.execute(f"INSERT INTO messages ({','.join(CAPTURE_FIELDS)}) VALUES ({','.join(':' + f for f in CAPTURE_FIELDS)}) ON CONFLICT(chat_id,message_id) DO NOTHING", record)
            if cursor.rowcount and media_payload:
                self.queue_media(db, 'message', record['chat_id'], record['message_id'], media_payload)
            db.execute("INSERT OR REPLACE INTO health VALUES ('last_capture',?)", (str(time.time()),))
            return bool(cursor.rowcount)

    def enrich_metadata(self, record):
        # Preserve known historical values; fill only absent context/content.
        fields = ('user_id', 'username', 'first_name', 'last_name', 'text') + EXTRA_FIELDS + ('raw_message',)
        with self.connection() as db:
            if not self.channel_permitted(db, record['chat_id']):
                return
            values = [record.get(f) for f in fields] + [time.time(), record['chat_id'], record['message_id']]
            updates = ','.join(f'{f}=COALESCE(NULLIF({f},\'\'),?)' for f in fields)
            db.execute(f'UPDATE messages SET {updates},metadata_refreshed_at=? WHERE chat_id=? AND message_id=?', values)

    def attach_media(self, chat_id, message_id, path):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.attach(db, 'message', chat_id, message_id, path)

    @staticmethod
    def digest(db, chat_id, source, public_source=False):
        now = time.time()
        bucket = int(now // 30)
        key = f'{chat_id}:{source}:{bucket}'
        db.execute('''INSERT INTO deletion_digests VALUES (?,?,?,?,?,?)
            ON CONFLICT(digest_key) DO UPDATE SET count=count+1,public_source=MAX(public_source,excluded.public_source)''',
                   (key, chat_id, source, 1, (bucket + 1) * 30 + 2, int(public_source)))

    def flush_digests(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM deletion_digests WHERE ready_at<=? ORDER BY ready_at LIMIT 50', (time.time(),)).fetchall()
            for row in rows:
                action = {'update': 'Удалено', 'history_hidden': 'История скрыта; недоступно'}.get(row['source'], 'Недоступно без события удаления')
                chat = db.execute(f"SELECT {ROW_FIELDS} FROM messages WHERE chat_id=? ORDER BY message_id DESC LIMIT 1", (row['chat_id'],)).fetchone()
                title = author(self.display_record(db,chat)) if chat and chat['chat_type']=='private' else label(chat['chat_title']) if chat else 'Неизвестный источник'
                username = ' @' + label(chat['chat_username']) if chat and chat['chat_username'] and chat['chat_type']!='private' else ''
                text = f"{action} сообщений: {row['count']}\n💬 {title}{username}\nСохранение в архив через очередь."
                if self.channel_permitted(db, row['chat_id'], notifications=True):
                    self.enqueue(db, 'digest:' + row['digest_key'], 'notify', text,
                                 public_source=bool(row['public_source']), source_chat_id=row['chat_id'])
                db.execute('DELETE FROM deletion_digests WHERE digest_key=?', (row['digest_key'],))

    def delete_row(self, db, row, source='update'):
        if row is None or row['deleted'] or not self.channel_permitted(db, row['chat_id']):
            return False
        chat_id, message_id = row['chat_id'], row['message_id']
        key = f'delete:{chat_id}:{message_id}'
        title = {'update': '❌ Сообщение удалено',
                 'history_hidden': '⚠️ История скрыта; сообщение недоступно, удаление не доказано'
                 }.get(source, '⚠️ Сообщение недоступно после двух проверок; событие удаления не получено')
        pending = db.execute('SELECT 1 FROM media_jobs WHERE kind=? AND owner_id=? AND item_id=?',
                             ('message', chat_id, message_id)).fetchone() is not None
        display = self.display_record(db,row)
        body = title + '\n' + render(display, media_pending=pending)
        context = self.archive_context(display)
        for i, part in enumerate(archive_chunks(body)):
            self.enqueue(db, f'{key}:text:{i}', 'archive', part, **context)
        if row['file_path']:
            self.enqueue(db, key + ':media', 'media', body[:450], row['file_path'], **context)
        self.digest(db, chat_id, source, row['chat_type'] == 'channel')
        db.execute('UPDATE messages SET deleted=1,deletion_source=? WHERE chat_id=? AND message_id=?',
                   (source, chat_id, message_id))
        return True

    def deleted(self, chat_id, message_id, source='update'):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if chat_id is None:
                rows = db.execute(f'SELECT {ROW_FIELDS} FROM messages WHERE message_id=? AND chat_id>? LIMIT 2',
                                  (message_id, -1000000000000)).fetchall()
                if len(rows) != 1:
                    return False
                row = rows[0]
            else:
                row = db.execute(f'SELECT {ROW_FIELDS} FROM messages WHERE chat_id=? AND message_id=?',
                                 (chat_id, message_id)).fetchone()
            return self.delete_row(db, row, source)

    def edit(self, chat_id, message_id, text):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?',
                             (chat_id, message_id)).fetchone()
            if row is None or row['deleted'] or (row['text'] or '') == text or not self.channel_permitted(db, chat_id):
                return False
            revision = row['revision'] + 1
            db.execute('INSERT INTO edits VALUES (?,?,?,?,?,?)',
                       (chat_id, message_id, revision, row['text'], text, time.time()))
            display = self.display_record(db,row)
            header, _, previous = render(display).partition('\n\n')
            body = f"✏️ Сообщение изменено\n{header}\n\nБыло:\n{previous}\n\nСтало:\n{text or '[Без текстовой подписи]'}"
            for i, part in enumerate(archive_chunks(body)):
                self.enqueue(db, f'edit:{chat_id}:{message_id}:{revision}:{i}', 'archive', part, **self.archive_context(display))
            db.execute('UPDATE messages SET text=?,revision=? WHERE chat_id=? AND message_id=?',
                       (text, revision, chat_id, message_id))
            return True

    def story(self, owner_id, story_id, caption, published_at, expires_at, media_payload=None):
        with self.connection() as db:
            db.execute('''INSERT INTO stories(owner_id,story_id,caption,published_at,expires_at)
                VALUES (?,?,?,?,?) ON CONFLICT(owner_id,story_id) DO UPDATE
                SET caption=excluded.caption,expires_at=excluded.expires_at''',
                       (owner_id, story_id, caption, published_at, expires_at))
            row = dict(db.execute('SELECT * FROM stories WHERE owner_id=? AND story_id=?',
                                  (owner_id, story_id)).fetchone())
            if media_payload and not row['file_path']:
                self.queue_media(db, 'story', owner_id, story_id, media_payload)
            return row

    def attach_story(self, owner_id, story_id, path):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.attach(db, 'story', owner_id, story_id, path)

    def attach(self, db, kind, owner_id, item_id, path):
        if kind=='message' and not self.channel_permitted(db, owner_id):
            return
        if kind == 'message':
            db.execute('UPDATE messages SET file_path=? WHERE chat_id=? AND message_id=?',
                       (str(path), owner_id, item_id))
            row = db.execute(f'SELECT {ROW_FIELDS} FROM messages WHERE chat_id=? AND message_id=?',
                             (owner_id, item_id)).fetchone()
            recovered, prefix = bool(row and row['deleted']), 'delete'
        else:
            db.execute('UPDATE stories SET file_path=? WHERE owner_id=? AND story_id=?',
                       (str(path), owner_id, item_id))
            row = db.execute('SELECT status FROM stories WHERE owner_id=? AND story_id=?',
                             (owner_id, item_id)).fetchone()
            recovered, prefix = bool(row and row['status'] in ('deleted_early', 'unavailable_early')), 'story'
        if recovered:
            self.enqueue(db, f'{prefix}:{owner_id}:{item_id}:media', 'media',
                         ('📎 Сохранённое медиа\n' + render(self.display_record(db,row)))[:450] if kind == 'message'
                         else f'📎 Сторис автора ID {owner_id} / {item_id}', str(path),
                         **(self.archive_context(row) if kind == 'message' else
                            dict(archive_key=f'user:{owner_id}', archive_title=f'Пользователь {owner_id}')))

    @staticmethod
    def queue_media(db, kind, owner_id, item_id, payload):
        if kind=='message' and not Store.channel_permitted(db, owner_id):
            return
        if len(payload) > 1024 * 1024:
            # An unusual TL object must not roll back already captured text.
            db.execute("INSERT OR REPLACE INTO health VALUES ('media_error','MetadataTooLarge')")
            return
        prefix = 'messages' if kind == 'message' else 'stories'
        db.execute('''INSERT INTO media_jobs(job_key,kind,owner_id,item_id,payload,created_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET payload=excluded.payload''',
                   (f'{prefix}/{owner_id}_{item_id}', kind, owner_id, item_id, payload, time.time()))

    def pending_media(self, limit=4):
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                f'''SELECT * FROM media_jobs WHERE next_try<=? AND
                (kind!='message' OR {self.channel_clause('media_jobs.owner_id')}) ORDER BY created_at,job_key LIMIT ?''',
                (time.time(), limit))]

    def complete_media(self, key, path):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM media_jobs WHERE job_key=?', (key,)).fetchone()
            if row:
                if row['kind']=='message' and not self.channel_permitted(db, row['owner_id']):
                    return
                self.attach(db, row['kind'], row['owner_id'], row['item_id'], path)
                db.execute('DELETE FROM media_jobs WHERE job_key=?', (key,))

    def stage_media(self, key, size, checksum, identity):
        with self.connection() as db:
            db.execute('UPDATE media_jobs SET staged_size=?,sha256=?,media_identity=? WHERE job_key=?',
                       (size, checksum, identity, key))

    def retry_media(self, key, delay, error, payload=None):
        with self.connection() as db:
            db.execute('''UPDATE media_jobs SET attempts=attempts+1,next_try=?,last_error=?,
                payload=COALESCE(?,payload) WHERE job_key=?''', (time.time() + delay, error, payload, key))

    def note_deletion(self, chat_id, message_id):
        key = f'{chat_id}:{message_id}'
        with self.connection() as db:
            if not self.channel_permitted(db, chat_id):
                return key
            db.execute('''INSERT OR IGNORE INTO deletion_signals(signal_key,chat_id,message_id,created_at)
                VALUES (?,?,?,?)''', (key, chat_id, message_id, time.time()))
        return key

    def note_deletions(self, chat_id, message_ids):
        with self.connection() as db:
            if not self.channel_permitted(db, chat_id):
                return
            created_at = time.time()
            db.executemany('''INSERT OR IGNORE INTO deletion_signals(signal_key,chat_id,message_id,created_at)
                VALUES (?,?,?,?)''', ((f'{chat_id}:{mid}', chat_id, mid, created_at) for mid in message_ids))

    def process_deletion_signals(self, limit=100):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            signals = db.execute('''SELECT * FROM deletion_signals WHERE resolved=0 AND next_try<=?
                ORDER BY next_try,created_at,signal_key LIMIT ?''', (time.time(), limit)).fetchall()
            for signal in signals:
                if signal['chat_id'] is None:
                    rows = db.execute(f'SELECT {ROW_FIELDS} FROM messages WHERE message_id=? AND chat_id>? LIMIT 2',
                                      (signal['message_id'], -1000000000000)).fetchall()
                else:
                    rows = db.execute(f'SELECT {ROW_FIELDS} FROM messages WHERE chat_id=? AND message_id=?',
                                      (signal['chat_id'], signal['message_id'])).fetchall()
                if len(rows) == 1:
                    self.delete_row(db, rows[0])
                    resolved, delay = 1, 0
                else:
                    resolved, delay = 0, min(3600, 5 * 2 ** min(signal['attempts'], 10))
                db.execute('''UPDATE deletion_signals SET resolved=?,attempts=attempts+1,next_try=?
                    WHERE signal_key=?''', (resolved, time.time() + delay, signal['signal_key']))
            return len(signals)

    def note_history_gap(self, chat_id, max_id):
        with self.connection() as db:
            if not self.channel_permitted(db, chat_id):
                return
            db.execute('''INSERT INTO history_gaps(chat_id,max_id,created_at) VALUES (?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET max_id=MAX(max_id,excluded.max_id),created_at=excluded.created_at''', (chat_id, max_id, time.time()))

    def process_history_gaps(self, limit=100):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            gap = db.execute('SELECT * FROM history_gaps WHERE cursor<max_id ORDER BY created_at LIMIT 1').fetchone()
            if gap is None:
                return 0
            rows = db.execute(f'''SELECT {ROW_FIELDS} FROM messages WHERE chat_id=? AND message_id>?
                AND message_id<=? ORDER BY message_id LIMIT ?''', (gap['chat_id'], gap['cursor'], gap['max_id'], limit)).fetchall()
            for row in rows:
                self.delete_row(db, row, source='history_hidden')
            cursor = rows[-1]['message_id'] if len(rows) == limit else gap['max_id']
            db.execute('UPDATE history_gaps SET cursor=? WHERE chat_id=?', (cursor, gap['chat_id']))
            return len(rows)

    def deletion_candidates(self, chat_id, message_id):
        with self.connection() as db:
            if chat_id is None:
                rows = db.execute('SELECT * FROM messages WHERE message_id=? AND chat_id>?',
                                  (message_id, -1000000000000))
            else:
                rows = db.execute('SELECT * FROM messages WHERE chat_id=? AND message_id=?', (chat_id, message_id))
            return [dict(r) for r in rows]

    def pending_signals(self, limit=50):
        with self.connection() as db:
            return [dict(r) for r in db.execute('''SELECT * FROM deletion_signals
                WHERE resolved=0 AND next_try<=? ORDER BY created_at LIMIT ?''', (time.time(), limit))]

    def update_signal(self, key, resolved=False, delay=30):
        with self.connection() as db:
            db.execute('''UPDATE deletion_signals SET resolved=?,attempts=attempts+1,next_try=?
                WHERE signal_key=?''', (int(resolved), time.time() + delay, key))

    def recheck_candidates(self, hours=24, limit=50, interval=60):
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with self.connection() as db:
            return [dict(r) for r in db.execute(f'''SELECT {ROW_FIELDS} FROM messages WHERE deleted=0
                AND {self.channel_clause('messages.chat_id')}
                AND date_sent>=? AND checked_at<=? ORDER BY checked_at,date_sent LIMIT ?''',
                (cutoff, time.time() - interval, limit))]

    def record_check(self, chat_id, message_id, result, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT missing_since,deleted FROM messages WHERE chat_id=? AND message_id=?',
                             (chat_id, message_id)).fetchone()
            if row is None or row['deleted']:
                return False
            confirmed = result == 'missing' and row['missing_since'] is not None and now - row['missing_since'] >= 30
            missing_since = (row['missing_since'] if row['missing_since'] is not None else now) if result == 'missing' else None
            db.execute('UPDATE messages SET checked_at=?,missing_since=? WHERE chat_id=? AND message_id=?',
                       (now, missing_since, chat_id, message_id))
            return confirmed

    def story_removed(self, owner_id, story_id, *, explicit=False, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM stories WHERE owner_id=? AND story_id=?',
                             (owner_id, story_id)).fetchone()
            if row is None or row['status'] != 'active':
                return False
            # Grace interval avoids clock skew and expiry races.
            if now >= row['expires_at'] - 60:
                db.execute("UPDATE stories SET status='expired' WHERE owner_id=? AND story_id=?", (owner_id, story_id))
                return False
            state = 'deleted_early' if explicit else 'unavailable_early'
            text = ('❌ Сторис удалена раньше срока.' if explicit else
                    '⚠️ Сторис недоступна раньше срока: удалена или изменён доступ.')
            date = short_date(datetime.fromtimestamp(row['published_at'],timezone.utc).isoformat())
            text += f"\n👤 Пользователь {owner_id}\n🕒 {date}\n\n{row['caption'] or '[Сторис без подписи]'}"
            key = f'story:{owner_id}:{story_id}'
            context = dict(archive_key=f'user:{owner_id}', archive_title=f'Пользователь {owner_id}')
            for i, part in enumerate(chunks(text)):
                self.enqueue(db, f'{key}:text:{i}', 'archive', part, **context)
            self.enqueue(db, key + ':notify', 'notify', text[:1800], **context)
            if row['file_path']:
                self.enqueue(db, key + ':media', 'media', text[:450], row['file_path'], **context)
            db.execute('UPDATE stories SET status=? WHERE owner_id=? AND story_id=?', (state, owner_id, story_id))
            return True

    def active_stories(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM stories WHERE status='active' AND expires_at>?", (time.time() + 60,))]

    def pending(self, limit=10):
        with self.connection() as db:
            notify = [dict(r) for r in db.execute('SELECT * FROM outbox WHERE priority=0 AND next_try<=? ORDER BY next_try,id LIMIT ?', (time.time(), limit))]
            normal = [dict(r) for r in db.execute('SELECT * FROM outbox WHERE priority=1 AND next_try<=? ORDER BY next_try,id LIMIT ?', (time.time(), limit))]
            quota = min(len(notify), max(1, limit // 5))
            jobs = notify[:quota] + normal[:limit - quota]
            return jobs + notify[quota:quota + limit - len(jobs)]

    def acknowledge(self, job_id):
        with self.connection() as db:
            db.execute('DELETE FROM outbox WHERE id=?', (job_id,))

    def retry(self, job_id, delay):
        with self.connection() as db:
            db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?', (time.time() + delay, job_id))

    def heartbeat(self, name, value):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO health VALUES (?,?)', (name, str(value)))

    def status(self):
        with self.connection() as db:
            return self.status_rows(db)

    @staticmethod
    def status_rows(db):
        state = dict(db.execute('SELECT name,value FROM health'))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for key, table in [('outbox', 'outbox'), ('stories', 'stories'), ('media_pending', 'media_jobs')]:
            state[key] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] if table in tables else 0
        state['deletion_pending'] = db.execute('SELECT COUNT(*) FROM deletion_signals WHERE resolved=0').fetchone()[0] if 'deletion_signals' in tables else 0
        state['history_pending'] = db.execute('SELECT COUNT(*) FROM history_gaps WHERE cursor<max_id').fetchone()[0] if 'history_gaps' in tables else 0
        state['digest_pending'] = db.execute('SELECT COUNT(*) FROM deletion_digests').fetchone()[0] if 'deletion_digests' in tables else 0
        return state

    @staticmethod
    def read_status(path):
        # The notification process must not run schema migrations.
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            state = Store.status_rows(db)
            sizes = []
            for suffix in ('', '-wal', '-shm'):
                try:
                    sizes.append(Path(str(path) + suffix).stat().st_size)
                except FileNotFoundError:
                    sizes.append(0)
            state.update(db_bytes=sizes[0], db_total_bytes=sum(sizes))
            state.update(disk_status(Path(path).resolve().parent))
            return state
        except sqlite3.OperationalError:
            return dict(outbox=0, stories=0, media_pending=0, deletion_pending=0, history_pending=0, digest_pending=0)
        finally:
            db.close()

    def cleanup(self, days, media_root):
        cutoff = datetime.now() - timedelta(days=days)
        with self.connection() as db:
            db.execute('DELETE FROM media_jobs WHERE created_at<?', (cutoff.timestamp(),))
            db.execute('DELETE FROM deletion_signals WHERE created_at<?', (cutoff.timestamp(),))
            db.execute('DELETE FROM history_gaps WHERE created_at<?', (cutoff.timestamp(),))
            db.execute('UPDATE deletion_signals SET resolved=2 WHERE resolved=0 AND created_at<?', (time.time() - 86400,))
            db.execute('DELETE FROM outbox WHERE created_at<?', (cutoff.timestamp(),))
            db.execute('DELETE FROM edits WHERE edited_at<?', (cutoff.timestamp(),))
            db.execute('''DELETE FROM messages WHERE date_sent<? AND NOT EXISTS
                (SELECT 1 FROM outbox WHERE outbox.file_path=messages.file_path)''', (cutoff.isoformat(),))
            db.execute("UPDATE stories SET status='expired' WHERE status='active' AND expires_at<=?", (time.time(),))
            db.execute('''DELETE FROM stories WHERE expires_at<? AND NOT EXISTS
                (SELECT 1 FROM outbox WHERE outbox.file_path=stories.file_path)''', (cutoff.timestamp(),))
            protected = {str(Path(r[0]).resolve()) for r in db.execute('''
                SELECT file_path FROM messages WHERE file_path IS NOT NULL UNION
                SELECT file_path FROM stories WHERE file_path IS NOT NULL UNION
                SELECT file_path FROM outbox WHERE file_path IS NOT NULL''')}
        root = Path(media_root).resolve()
        removed = 0
        for path in root.rglob('*'):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if str(path.resolve()) not in protected and path.stat().st_mtime < cutoff.timestamp():
                path.unlink(missing_ok=True)
                removed += 1
        return removed
