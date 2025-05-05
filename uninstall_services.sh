#!/bin/bash

echo "Отключаю и удаляю systemd-сервисы..."

sudo systemctl stop userbot.service notifybot.service
sudo systemctl disable userbot.service notifybot.service
sudo rm -f /etc/systemd/system/userbot.service /etc/systemd/system/notifybot.service

sudo systemctl daemon-reexec
sudo systemctl daemon-reload

echo "🧹 Сервисы удалены."
