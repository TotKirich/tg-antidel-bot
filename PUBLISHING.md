# Первая публикация на GitHub

## Подготовленная папка

Публикуйте содержимое чистой папки `publish/tg-antidel-bot` либо распакованного `tg-antidel-bot-github.zip`. В ней нет прежней папки .git и runtime-данных. Не копируйте в неё конфигурацию или сессию с рабочего сервера.

Включены README, инструкция развёртывания, описание безопасности, исходники, тесты, Dockerfile/Compose, автоустановка Docker, шаблон настроек и правила исключений. Тесты нужны разработчикам и не попадают в runtime-образ; отдельный tg-antidel-bot-runtime.zip их не содержит. Старые скрипты systemd в V2 не включены. Создание действующих ключей выполняется только после запуска мастера на сервере.

## Проверка

```bash
docker build --target test -t tg-antidel-bot:test .
docker run --rm --network none tg-antidel-bot:test
```

Просмотрите содержимое папки: не должно быть config.json, data, *.session*, *.db*, *.sqlite*, *.log или файла .auth-login.url. У шаблона config.example.json значения секретов пустые.

## Новый репозиторий

Создайте на GitHub новый пустой репозиторий без автоматически добавленного README. В подготовленной папке выполните:

```bash
git init -b main
git add README.md DEPLOYMENT.md SECURITY.md PUBLISHING.md .gitignore .gitattributes
git add Dockerfile compose.yaml requirements.txt config.example.json
git add start.sh scripts/Install-Docker.sh
git add '*.py' scripts/Build-Release.py tests
git diff --cached --stat
git diff --cached
git commit -m "Initial public release"
git remote add origin YOUR_NEW_REPOSITORY_URL
git push -u origin main
```

Замените YOUR_NEW_REPOSITORY_URL адресом нового репозитория. Имя и email коммита задаются вашей локальной конфигурацией Git; выберите публичный или GitHub noreply-адрес перед коммитом.

Подготовка сборки сама не выполняет commit, push, создание репозитория или изменение существующей истории. При добавлении к старому репозиторию его история остаётся: чистая папка не удаляет опубликованные ранее секреты. Порядок действий при утечке описан в [SECURITY.md](SECURITY.md).

## Последующие чистые сборки

```bash
python scripts/Build-Release.py
python scripts/Build-Release.py --publish
```

Повторная публикационная сборка не перезаписывает существующую папку, чтобы не уничтожить вашу работу. Укажите новый путь через `--publish-dir PATH`, а затем скопируйте проверенные изменения в свой публичный checkout. Действующие файлы data в checkout должны оставаться исключёнными из Git.
