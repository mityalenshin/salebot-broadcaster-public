# Production deploy

## Первоначальная настройка VPS

1. **Deploy SSH key** на сервере (одноразово):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/deploy_salebot -N "" -C "deploy-salebot"
cat ~/.ssh/deploy_salebot.pub  # → добавить в Settings → Deploy keys репозитория
```

В `~/.ssh/config` добавить:
```
Host github-salebot
    HostName github.com
    User git
    IdentityFile ~/.ssh/deploy_salebot
    IdentitiesOnly yes
```

2. **Клон и установка**:

```bash
cd /opt
git clone github-salebot:USER/salebot-broadcaster.git
cd salebot-broadcaster
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
mkdir -p data
# заполнить .env (скопировать с локальной машины через scp)
# скопировать data/salebot_local.db (если есть) с локальной машины
```

3. **systemd-сервис** — `salebot-bot.service` положить в `/etc/systemd/system/`,
выполнить:

```bash
systemctl daemon-reload
systemctl enable salebot-bot
systemctl start salebot-bot
```

4. **Бэкап** — `salebot-backup.cron` положить в `/etc/cron.d/salebot-backup`
(ежедневный snapshot БД с ротацией 14 дней).

## Обновление кода

После `git push` локально — на сервере:

```bash
ssh root@VPS /opt/salebot-broadcaster/deploy.sh
```

Скрипт делает: `git pull` → `pip install` → `systemctl restart`.

## Полезные команды

```bash
# логи в реальном времени
journalctl -u salebot-bot -f

# или
tail -f /var/log/salebot-bot.log

# статус
systemctl status salebot-bot

# рестарт
systemctl restart salebot-bot
```
