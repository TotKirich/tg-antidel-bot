#!/bin/bash

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Создаю systemd-сервисы..."

cat <<EOF | sudo tee /etc/systemd/system/userbot.service
[Unit]
Description=Telegram UserBot
After=network.target

[Service]
WorkingDirectory=$BASE_DIR
ExecStart=/usr/bin/python3 $BASE_DIR/userbot.py
Restart=always
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
EOF

cat <<EOF | sudo tee /etc/systemd/system/notifybot.service
[Unit]
Description=Telegram NotifyBot
After=network.target

[Service]
WorkingDirectory=$BASE_DIR
ExecStart=/usr/bin/python3 $BASE_DIR/notify_bot.py
Restart=always
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
EOF

echo "Перезагружаю systemd и включаю сервисы..."
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable userbot.service notifybot.service
sudo systemctl start userbot.service notifybot.service

echo "✅ Сервисы установлены и запущены."
