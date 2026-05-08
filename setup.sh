#!/bin/bash
# =============================================================================
# setup.sh — автоматическая установка SaleBot Broadcaster на чистый Ubuntu VPS.
#
# Что делает:
#   1. Ставит системные пакеты (python3.12, nginx, certbot, git, sqlite3)
#   2. Копирует код в /opt/salebot-broadcaster, ставит venv + зависимости
#   3. Настраивает nginx с SSL (Let's Encrypt) для VPS_DOMAIN
#   4. Регистрирует systemd-сервисы для бота и webhook
#   5. Прописывает ежедневные cron — бэкап БД и check_alive
#
# Перед запуском:
#   - Скопируй .env.example → .env и заполни ВСЕ обязательные значения
#   - VPS_DOMAIN должен быть направлен A-записью на этот сервер (через DNS)
#   - Запускать ОТ ROOT: sudo bash setup.sh
#
# Опциональные переменные:
#   SETUP_EMAIL — email для Let's Encrypt (по умолчанию admin@$VPS_DOMAIN)
# =============================================================================
set -e

# ----- проверки -----
if [ "$EUID" -ne 0 ]; then
    echo "❌ Запускай от root: sudo bash setup.sh"
    exit 1
fi
if [ ! -f .env ]; then
    echo "❌ В текущей директории нет .env"
    echo "   Скопируй .env.example → .env и заполни значения."
    exit 1
fi

# Парсим .env (только KEY=VALUE строки, без кавычек, без пробелов)
set -a
source <(grep -E '^[A-Z_][A-Z_0-9]*=' .env)
set +a

required=(
    SALEBOT_API_KEY
    TELEGRAM_BROADCAST_BOT_TOKEN TELEGRAM_BROADCAST_BOT_USERNAME
    TELEGRAM_COMMAND_BOT_TOKEN   TELEGRAM_COMMAND_BOT_USERNAME
    TELEGRAM_ADMIN_USERNAMES TELEGRAM_WORK_CHAT_ID TELEGRAM_TEST_USER_IDS
    WEBHOOK_SECRET DASHBOARD_TOKEN VPS_DOMAIN
)
missing=()
for var in "${required[@]}"; do
    [ -z "${!var}" ] && missing+=("$var")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "❌ В .env не заполнены обязательные поля:"
    printf '   - %s\n' "${missing[@]}"
    exit 1
fi

INSTALL_DIR=/opt/salebot-broadcaster
DOMAIN="$VPS_DOMAIN"
EMAIL="${SETUP_EMAIL:-admin@${DOMAIN#*.}}"  # по дефолту admin@<основной домен>

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SaleBot Broadcaster — установка"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Домен:        $DOMAIN"
echo "  Email (SSL):  $EMAIL"
echo "  Директория:   $INSTALL_DIR"
echo "  Бот рассылок: @$TELEGRAM_BROADCAST_BOT_USERNAME"
echo "  Бот команд:   @$TELEGRAM_COMMAND_BOT_USERNAME"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# ----- 1. системные пакеты -----
echo "▶ [1/7] системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3.12 python3.12-venv python3-pip \
    git nginx certbot python3-certbot-nginx sqlite3 cron curl
echo "  ✓ установлены"

# ----- 2. копируем код в /opt -----
echo "▶ [2/7] копируем код в $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# rsync без venv, .git, data — сами создадутся
rsync -a --exclude='venv' --exclude='__pycache__' --exclude='data' \
      --exclude='.git' --exclude='*.log' \
      "$(pwd)/" "$INSTALL_DIR/"
cd "$INSTALL_DIR"
echo "  ✓ скопировано"

# ----- 3. python venv -----
echo "▶ [3/7] python venv + зависимости"
[ -d venv ] || python3.12 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
echo "  ✓ установлены"

# ----- 4. БД -----
echo "▶ [4/7] инициализация SQLite"
mkdir -p data
./venv/bin/python -c "import db; db.init_schema()"
echo "  ✓ schema готова"

# ----- 5. nginx + SSL -----
echo "▶ [5/7] nginx + SSL"

# Если нет конфига — поднимем минимальный HTTP, чтобы certbot прошёл
if [ ! -f /etc/nginx/sites-available/$DOMAIN ]; then
    mkdir -p /var/www/$DOMAIN
    echo "<h1>$DOMAIN — setup in progress</h1>" > /var/www/$DOMAIN/index.html

    cat > /etc/nginx/sites-available/$DOMAIN <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    root /var/www/$DOMAIN;
    index index.html;
    location / { try_files \$uri \$uri/ =404; }
}
NGINX
    ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
    nginx -t && systemctl reload nginx
fi

# certbot — если ещё нет сертификата
if ! certbot certificates 2>/dev/null | grep -q "$DOMAIN"; then
    echo "  получаю SSL для $DOMAIN..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --email "$EMAIL" --no-eff-email --redirect 2>&1 | tail -5
fi

# Финальный конфиг с location /webhook /dashboard /api/
cp deploy/nginx-template.conf /etc/nginx/sites-available/$DOMAIN
sed -i "s|EXAMPLE\.COM|$DOMAIN|g" /etc/nginx/sites-available/$DOMAIN
nginx -t && systemctl reload nginx
echo "  ✓ nginx + SSL готовы"

# ----- 6. systemd -----
echo "▶ [6/7] systemd-сервисы"
cp deploy/salebot-bot.service /etc/systemd/system/
cp deploy/salebot-webhook.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now salebot-webhook
systemctl enable --now salebot-bot
sleep 3
for svc in salebot-bot salebot-webhook; do
    if systemctl is-active --quiet "$svc"; then
        echo "  ✓ $svc — running"
    else
        echo "  ❌ $svc — НЕ запущен. Логи: journalctl -u $svc -n 30"
    fi
done

# ----- 7. cron -----
echo "▶ [7/7] cron-задачи"
mkdir -p /root/_backup
cp deploy/salebot-backup.cron      /etc/cron.d/salebot-backup
cp deploy/salebot-check-alive.cron /etc/cron.d/salebot-check-alive
chmod 644 /etc/cron.d/salebot-*
echo "  ✓ ежедневный бэкап БД (03:00 МСК) и check_alive (04:00 МСК)"

# ----- финал -----
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ УСТАНОВКА ЗАВЕРШЕНА"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "  Webhook URL для SaleBot (вписать в Настройки → Webhook):"
echo "    https://$DOMAIN/webhook?secret=$WEBHOOK_SECRET"
echo
echo "  Дашборд аналитики (сохрани в закладки):"
echo "    https://$DOMAIN/dashboard?token=$DASHBOARD_TOKEN"
echo
echo "  Бот команд: @$TELEGRAM_COMMAND_BOT_USERNAME"
echo "    Открой в Telegram, отправь /help — бот должен ответить."
echo
echo "  Первый запуск миграции (выгрузить клиентов из SaleBot, ~часы):"
echo "    cd $INSTALL_DIR && ./venv/bin/python migrate.py"
echo "    после: ./venv/bin/python check_alive.py"
echo
echo "  Логи:"
echo "    journalctl -u salebot-bot -f"
echo "    journalctl -u salebot-webhook -f"
echo
