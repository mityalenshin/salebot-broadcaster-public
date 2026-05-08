# 🤖 Промпт для Claude Code

Скопируй текст ниже, **замени значения в скобках `[...]`** на свои собранные данные
(см. [QUICKSTART.md](QUICKSTART.md)) и отправь в Claude Code (CLI или в чат).

---

## Промпт для копирования

```
Привет. Я хочу развернуть систему рассылок salebot-broadcaster на своём VPS.
Это open-source проект для Telegram-рассылок поверх SaleBot — экономит квоту тарифа.
Репозиторий: https://github.com/mityalenshin/salebot-broadcaster

Тебе нужно:
1. Подключиться к моему VPS по SSH.
2. Скачать код, заполнить .env моими данными.
3. Запустить setup.sh, который сам поставит всё (Python, nginx, SSL, systemd).
4. Запустить миграцию клиентов из SaleBot и check_alive.
5. Дать мне финальные ссылки (webhook URL для SaleBot, ссылка на дашборд).
6. По ходу объяснять что делаешь, показывать прогресс.

ДАННЫЕ ДЛЯ РАЗВЁРТЫВАНИЯ:

VPS:
  IP:           [IP_адрес_сервера]
  SSH-пользователь: root
  SSH-пароль:   [пароль]   ← ИЛИ путь к ключу: ~/.ssh/[имя_ключа]
  Email для SSL: [email]

Домен:
  [salebot.myname.ru]   ← поддомен, A-запись уже направлена на VPS

Telegram бот рассылок (тот же, что в SaleBot):
  TOKEN:    [токен]
  USERNAME: [username_без_@]

Telegram бот команд (новый):
  TOKEN:    [токен]
  USERNAME: [username_без_@]

SaleBot:
  API_KEY:  [api_ключ]

Доступ к боту команд:
  Мой Telegram username (без @): [mityalenshin]
  Мой chat_id (личный):           [123456789]
  Chat_id рабочей группы:         [-100123456789]

ПЛАН РАБОТЫ:

1. Подключись по SSH к VPS, проверь что доступ есть и сервер чистый Ubuntu 22+.
2. Установи sshpass на своей машине (если SSH-пароль) — это для безопасной передачи
   пароля в команды.
3. На VPS:
   a) Создай deploy SSH-ключ для приватного доступа к репо (если репо приватный)
      ИЛИ просто склонируй публичный репо.
      git clone https://github.com/mityalenshin/salebot-broadcaster.git /root/salebot-broadcaster
   b) cd /root/salebot-broadcaster
   c) cp .env.example .env
   d) Открой .env и аккуратно впиши все мои данные выше. WEBHOOK_SECRET и DASHBOARD_TOKEN
      сгенерируй командами:
        WEBHOOK_SECRET=$(openssl rand -hex 32)
        DASHBOARD_TOKEN=$(openssl rand -hex 32)
      Сохрани их в .env.
   e) Сначала проверь, что DNS A-записи для домена ведут на этот VPS:
        dig +short [salebot.myname.ru]
      Должен вернуть IP VPS. Если нет — попроси меня настроить DNS.
   f) sudo bash setup.sh
      Скрипт всё развернёт. Если упадёт — покажи мне ошибку и предложи решение.
4. После завершения setup.sh:
   a) systemctl status salebot-bot salebot-webhook  — должны быть active.
   b) curl https://[salebot.myname.ru]/webhook/health   — должен вернуть JSON.
   c) В Telegram открой бота команд, отправь /help — он должен ответить.
5. Запусти первичную миграцию (это долго, 6-12 часов на 50k клиентов):
     cd /opt/salebot-broadcaster && ./venv/bin/python migrate.py
   Лучше запустить в screen или nohup, чтобы не зависеть от SSH-сессии.
   Например: nohup ./venv/bin/python migrate.py > data/migrate.log 2>&1 &
6. После миграции — проверь активность:
     ./venv/bin/python check_alive.py > data/check_alive.log 2>&1 &
   Это ещё ~30 минут.
7. В конце дай мне:
   - Webhook URL для вписывания в SaleBot (с реальным секретом из моего .env)
   - Ссылку на дашборд (с реальным токеном)
   - Команду @username бота команд (его и так знаю, но напомни)
   - Краткую сводку: всего клиентов в БД, % активных.

ВАЖНО:
- Не выводи в чат значения секретов (токены, пароли) — они должны жить в .env на VPS.
  Если нужно показать webhook URL — выведи через bash, не в текст ответа.
- Не коммить .env в git, не отправляй .env никуда.
- Если что-то непонятно — спрашивай. Лучше одно уточнение, чем сделать неправильно.

Начинай.
```

---

## Что произойдёт после отправки промпта

Claude Code:

1. Подключится по SSH к твоему VPS (запросит подтверждение).
2. Покажет план действий и попросит подтверждения перед каждым шагом.
3. Будет в реальном времени показывать прогресс.
4. Когда упрётся в неоднозначность — спросит у тебя.
5. В конце выдаст итоговые ссылки.

Если что-то пойдёт не так — Claude сам диагностирует и предложит решение.
Просто отвечай на его вопросы.

## После того как развернулось

1. **Открой бота команд в Telegram** → `/help` → проверь, что бот отвечает.
2. **Открой дашборд** по ссылке из ответа Claude.
3. **Впиши webhook URL в SaleBot** (Настройки → Webhook).
4. **Сделай тестовую рассылку через `/new`** — пройди визард до конца на маленький сегмент.

Если всё работает — всё, ты в продакшне. Поздравляю.

---

## Если что-то пошло не так

- **Бот не отвечает на /help?**
  ```
  ssh root@[IP] "systemctl status salebot-bot && journalctl -u salebot-bot -n 30"
  ```
- **Webhook не работает?** — проверь `https://твой-домен/webhook/health` в браузере.
  Должен показать JSON. Если 404 — проблема с nginx, если timeout — webhook упал.
- **SSL не получился?** — обычно из-за того, что DNS ещё не обновился. Подожди 15 минут
  и переустанови вручную: `certbot --nginx -d твой-домен`.

Все логи на VPS:
```
/var/log/salebot-bot.log
/var/log/salebot-webhook.log
/var/log/salebot-check-alive.log
```
