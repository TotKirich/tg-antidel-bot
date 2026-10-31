"""Build a source-only handoff archive, excluding runtime data and Git history."""
import json
import argparse
import os
import hashlib
from pathlib import Path
import zipfile

FILES = [
    'README.md', 'DEPLOYMENT.md', 'SECURITY.md', 'PUBLISHING.md', '.gitignore', '.gitattributes',
    'requirements.txt', 'config.example.json', 'Dockerfile', 'compose.yaml', '.dockerignore',
    'start.sh', 'install.sh', 'compose.prebuilt.yaml', '.github/workflows/container.yml', 'scripts/Install-Docker.sh',
    'app/userbot.py', 'app/notify_bot.py', 'app/runtime.py', 'app/storage.py', 'app/transport.py', 'app/stories.py',
    'app/authweb.py', 'app/recovery.py', 'app/message_metadata.py', 'app/notification_setup.py', 'app/docker_watchdog.py',
    'app/archive.py', 'app/channels.py', 'app/onboarding.py', 'scripts/Build-Release.py',
    'tests/test_guard.py', 'tests/test_authweb.py', 'tests/test_recovery.py',
    'tests/test_bulk_and_metadata.py', 'tests/test_notification_settings.py',
    'tests/test_archive.py', 'tests/test_onboarding.py', 'tests/test_release.py', 'tests/test_docker_install.py', 'tests/test_prebuilt_install.py', 'tests/test_channels.py',
]


def reviewed_sources(root):
    root = Path(root).resolve()
    example = json.loads((root/'config.example.json').read_text(encoding='utf-8'))
    if example.get('api_id') or example.get('api_hash') or example.get('bot_token'):
        raise ValueError('Example configuration must contain no credentials')
    private_values = set()
    for path in (root/'config.json', root/'data/config.json'):
        if path.is_file():
            saved = json.loads(path.read_text(encoding='utf-8'))
            for field in ('api_hash', 'bot_token', 'admin_username', 'admin_user_id', 'admin_chat_id'):
                value = str(saved.get(field) or '')
                if len(value) >= 6:
                    private_values.add(value.encode('utf-8'))
    sources = {}
    for relative in FILES:
        path = root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Release source must stay in the reviewed workspace')
        data = path.read_bytes()
        if relative.endswith(('.sh', '.py', '.md', '.yaml')):
            data = data.replace(b'\r\n', b'\n')
        if any(value in data for value in private_values):
            raise ValueError(f'Private configuration value detected in source: {relative}')
        sources[relative] = data
    return sources


def build(root, publish_dir=None):
    root = Path(root).resolve()
    sources = reviewed_sources(root)
    destination = Path(publish_dir).resolve() if publish_dir is not None else None
    if destination is not None:
        if destination.exists():
            raise FileExistsError('Publish directory already exists; choose a new --publish-dir')
        destination.mkdir(parents=True)
        for relative, data in sources.items():
            path = destination/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if relative.endswith('.sh') else 0o644)
    target = root/'data/dist/tg-antidel-bot.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for relative, data in sources.items():
            info = zipfile.ZipInfo('tg-antidel-bot/'+relative)
            info.create_system = 3
            info.external_attr = (0o100755 if relative.endswith('.sh') else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    with zipfile.ZipFile(target) as archive:
        if set(archive.namelist()) != {'tg-antidel-bot/'+p for p in FILES} or archive.testzip() is not None:
            raise RuntimeError('Release verification failed')
    print(f'{target} ({len(FILES)} reviewed source files; no runtime data or Git history)')
    runtime_sources={name:data for name,data in sources.items()
        if not name.startswith(('tests/','.github/')) and name not in ('scripts/Build-Release.py','PUBLISHING.md','.gitattributes')}
    runtime_sources['Dockerfile']=sources['Dockerfile'].split(b'FROM base AS test')[0]+b'FROM base AS runtime\nCMD ["python", "docker_watchdog.py"]\n'
    runtime_sources['README.md']=('''# Telegram Delete Guard — пакет развёртывания

Linux: запустите `bash start.sh`. Если Docker отсутствует, он и Compose установятся автоматически на поддерживаемых Ubuntu/Debian через sudo, без отдельного подтверждения. Затем терминал покажет ссылку мастера Telegram/API/бота.

Инструкции: [DEPLOYMENT.md](DEPLOYMENT.md). Данные, секреты и границы защиты: [SECURITY.md](SECURITY.md).

Этот пакет содержит только файлы Docker-развёртывания и документацию. Тесты и инструменты подготовки публикации находятся в полной версии репозитория. Старые systemd-скрипты не включены. `bash start.sh --setup` открывает перенастройку без удаления базы и архива.
''').encode('utf-8')
    runtime_zip=target.with_name('tg-antidel-bot-runtime.zip')
    with zipfile.ZipFile(runtime_zip,'w',zipfile.ZIP_DEFLATED) as archive:
        for relative,data in runtime_sources.items():
            info=zipfile.ZipInfo('tg-antidel-bot/'+relative)
            info.create_system=3
            info.external_attr=(0o100755 if relative.endswith('.sh') else 0o100644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)
    print(f'Runtime archive: {runtime_zip} ({len(runtime_sources)} files; no tests or legacy service scripts)')
    if destination is not None:
        public_zip = target.with_name('tg-antidel-bot-github.zip')
        try:
            public_zip.write_bytes(target.read_bytes())
        except PermissionError:
            # A previously opened ZIP may be locked on Windows; preserve it.
            public_zip=target.with_name('tg-antidel-bot-github-'+hashlib.sha256(target.read_bytes()).hexdigest()[:12]+'.zip')
            public_zip.write_bytes(target.read_bytes())
        if {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()} != set(FILES):
            raise RuntimeError('Publish directory verification failed')
        print(f'Publish directory: {destination}')
        print(f'GitHub archive: {public_zip}')
    return target


if __name__ == '__main__':
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', help='Prepare a new clean GitHub folder and ZIP')
    parser.add_argument('--publish-dir', type=Path, help='New destination; never overwritten')
    args = parser.parse_args()
    build(root, args.publish_dir or (root/'publish/tg-antidel-bot' if args.publish else None))
