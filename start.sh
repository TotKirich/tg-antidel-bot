#!/bin/bash

# Проверка и установка зависимостей
echo "🔧 Проверка зависимостей ..."
apt update >/dev/null 2>&1
apt install -y python3 python3-pip >/dev/null 2>&1
pip3 install -r requirements.txt >/dev/null 2>&1

# Проверка уже запущенных процессов
if pgrep -f "userbot.py" > /dev/null || pgrep -f "notify_bot.py" > /dev/null; then
  echo "✖ Один из процессов уже запущен (userbot.py или notify_bot.py). Остановите их перед повторным запуском."
  exit 1
fi

# Завершение возможных копий
echo "🔁 Завершаем возможные запущенные копии скриптов ..."
pkill -f userbot.py >/dev/null 2>&1
pkill -f notify_bot.py >/dev/null 2>&1

# Запуск
sleep 1
echo "🚀 Запуск notify_bot в фоне …"
nohup python3 notify_bot.py >/dev/null 2>&1 &
sleep 1
echo "🚀 Запуск userbot …"
python3 userbot.py
