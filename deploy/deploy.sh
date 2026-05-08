#!/bin/bash
# Подтянуть свежий код из main, обновить зависимости, перезапустить сервис.
set -e
cd /opt/salebot-broadcaster
git fetch
git reset --hard origin/main
./venv/bin/pip install -q -r requirements.txt
systemctl restart salebot-bot
sleep 2
systemctl status salebot-bot --no-pager | head -10
echo
echo "deployed: $(git log --oneline -1)"
