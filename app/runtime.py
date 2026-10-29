"""Configuration and process utilities. Importing does not start any clients."""
import json
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import tempfile

ROOT = Path(os.environ.get('TG_GUARD_DATA_DIR', Path(__file__).resolve().parent)).resolve()
DEFAULTS = dict(retention_days=14, max_media_mb=100, min_free_mb=512,
                stories_enabled=True, stories_poll_seconds=300, log_chat_id='me', auth_port=8787,
                recheck_hours=24, recheck_batch_size=50, recheck_interval_seconds=60,
                delivery_interval_seconds=1, notify_public_deletions=True, auth_host='127.0.0.1', archive_mode='saved')


def read_config(path=None):
    with open(path or ROOT / 'config.json', encoding='utf-8') as stream:
        saved = json.load(stream)
        if 'notifications_enabled' not in saved:
            saved['notifications_enabled'] = bool(saved.get('admin_chat_id')) if saved.get('admin_user_id') is not None else True
        config = {**DEFAULTS, **saved}
    for key in ('retention_days', 'max_media_mb', 'min_free_mb'):
        if int(config[key]) <= 0:
            raise ValueError(f'{key} must be positive')
    if int(config['stories_poll_seconds']) < 60:
        raise ValueError('stories_poll_seconds must be >=60')
    if not 1024 <= int(config['auth_port']) <= 65535:
        raise ValueError('auth_port must be between 1024 and 65535')
    address = ipaddress.IPv4Address(config['auth_host'])
    if address.is_unspecified or address.is_multicast or str(address) == '255.255.255.255':
        raise ValueError('auth_host must be a specific IPv4 address of this server')
    if not 1 <= int(config['recheck_hours']) <= int(config['retention_days']) * 24:
        raise ValueError('recheck_hours must fit within retention_days')
    if not 1 <= int(config['recheck_batch_size']) <= 100 or int(config['recheck_interval_seconds']) < 60:
        raise ValueError('recheck_batch_size must be 1..100, recheck_interval_seconds must be >=60')
    if float(config['delivery_interval_seconds']) < 1:
        raise ValueError('delivery_interval_seconds must be >=1')
    if config['archive_mode'] not in ('saved', 'forum'):
        raise ValueError('archive_mode must be saved or forum')
    return config


def atomic_config(config, path=None):
    path = Path(path or ROOT / 'config.json')
    handle, temporary = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_config(changes):
    # Separate processes share this advisory lock; read the latest JSON under it.
    stream = open(ROOT / '.config.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            stream.write(b'0')
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        config = read_config()
        config.update(changes(config) if callable(changes) else changes)
        atomic_config(config)
        return config
    finally:
        stream.close()


def owner_settings(config, user_id):
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError('A positive Telegram user ID is required')
    return dict(admin_user_id=user_id,
                admin_chat_id=user_id if config.get('notifications_enabled', True) else None)


def bind_owner(user_id):
    def change(config):
        if config.get('admin_user_id') is not None and config['admin_user_id'] != user_id:
            raise ValueError('Data directory belongs to another Telegram account')
        return owner_settings(config, user_id)
    return update_config(change)


def setup_logging(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
        for handler in (logging.StreamHandler(), RotatingFileHandler(
                ROOT / f'{name}.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
    return logger


def sd_notify(message):
    # Docker supervisor reads this pulse; it is refreshed by the event loop.
    if os.environ.get('TG_GUARD_DATA_DIR'):
        import time
        (ROOT / '.heartbeat').write_text(str(time.time()), encoding='ascii')
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(address)
        client.sendall(message.encode())


def authorised(config, user, chat):
    if user is None or chat is None or chat.type != 'private':
        return False
    if getattr(chat, 'id', user.id) != user.id:
        return False
    if config.get('admin_user_id') is not None:
        return user.id == int(config['admin_user_id'])
    return False  # Only the ID bound by authenticated Telegram login can administer the bot.


def instance_lock(name):
    """Hold the returned file open for the process lifetime; never kill another copy."""
    stream = open(ROOT / f'.{name}.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError:
        stream.close()
        raise RuntimeError('Another instance is already running') from None
