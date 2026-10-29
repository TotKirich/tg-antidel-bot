"""First-run preparation and terminal link display, without printing credentials."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import time

from runtime import ROOT, DEFAULTS, atomic_config, read_config, update_config, instance_lock


def prepare(root=ROOT, host=None, port=None, reconfigure=False):
    root = Path(root)
    if host:
        address = ipaddress.IPv4Address(host)
        if address.is_unspecified or address.is_multicast or str(address) == '255.255.255.255':
            raise ValueError('Use a specific IPv4 address')
    if port is not None and not 1024 <= port <= 65535:
        raise ValueError('Port must be 1024..65535')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / 'media').mkdir(exist_ok=True, mode=0o700)
    path = root / 'config.json'
    if not path.exists():
        # Do not import example or historical credentials into a new installation.
        config = dict(DEFAULTS, api_id=None, api_hash='', bot_token='',
                      admin_user_id=None, admin_chat_id=None, notifications_enabled=True,
                      archive_mode='forum', auth_host=host or '127.0.0.1', auth_port=port or 8080)
        atomic_config(config, path)
    else:
        config = read_config(path)
    changes = {}
    if host:
        changes['auth_host'] = str(address)
    if port is not None:
        changes['auth_port'] = port
    if reconfigure:
        changes['setup_requested'] = True
    if changes:
        if root.resolve() != ROOT.resolve():
            raise ValueError('Configuration updates require the active data directory')
        config = update_config(changes)
    return config


def credentials_present(config):
    return (str(config.get('api_id', '')).isdigit() and int(config['api_id']) > 0
            and bool(re.fullmatch(r'[a-fA-F0-9]{32}', str(config.get('api_hash', '')))))


def wait_for_link(root=ROOT, timeout=90):
    """Print the short-lived capability only to the launcher's terminal, never logs."""
    root = Path(root)
    deadline = time.monotonic() + timeout
    started = time.time()
    while time.monotonic() < deadline:
        try:
            link = (root / '.auth-login.url').read_text(encoding='utf-8').strip()
            if re.fullmatch(r'http://[0-9.]+:[0-9]+/#[A-Za-z0-9_-]+', link):
                print('\nОткройте в браузере:\n' + link, flush=True)
                print('Это временная ссылка входа. Не пересылайте её другим людям.', flush=True)
                return True
        except FileNotFoundError:
            pass
        config = read_config(root / 'config.json')
        if credentials_present(config) and config.get('bot_token') and not config.get('setup_requested'):
            try:
                with sqlite3.connect((root/'messages.db').resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
                    health = dict(db.execute('SELECT name,value FROM health'))
                if health.get('auth_required') == '0' and float(health.get('last_ok', 0)) >= started:
                    print('Настройка сохранена. Связь с Telegram подтверждена; ключи скрыты.', flush=True)
                    return True
            except (sqlite3.Error, ValueError):
                pass
        time.sleep(.5)
    print('Форма пока не готова. Проверьте журналы userbot; ключи не выводятся.', flush=True)
    return False


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'link'])
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--reconfigure', action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        if args.reconfigure:
            ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            with instance_lock('userbot'):
                prepare(host=args.host, port=args.port, reconfigure=True)
        else:
            prepare(host=args.host, port=args.port)
        print('Каталог данных: ' + str(ROOT))
        print('База и файлы сохраняются здесь. Закрытая группа-архив создаётся автоматически в режиме forum.')
    else:
        raise SystemExit(0 if wait_for_link() else 1)


if __name__ == '__main__':
    main()
