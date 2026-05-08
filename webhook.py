"""
Webhook-приёмник от SaleBot.

SaleBot шлёт POST на этот endpoint при каждом входящем/исходящем сообщении.
Мы извлекаем данные клиента и UPSERT'им их в локальную SQLite —
так база поддерживается актуальной без ручной миграции.

Запускается через waitress (production WSGI) на 127.0.0.1:8080.
nginx проксирует https://VPS_DOMAIN/webhook → 127.0.0.1:8080.
"""
import datetime as dt
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import db
from dashboard import bp as dashboard_bp

load_dotenv()
logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("webhook")

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
LISTEN_HOST = os.environ.get("WEBHOOK_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WEBHOOK_LISTEN_PORT", "8080"))

app = Flask(__name__)
app.register_blueprint(dashboard_bp)


@app.get("/webhook/health")
def health():
    """Простой эндпоинт для проверки, что сервер живой."""
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    return jsonify({"ok": True, "clients_in_db": n})


@app.post("/webhook")
def on_webhook():
    """Принять сообщение от SaleBot, UPSERT клиента в БД."""
    # Защита: секрет в query string
    if request.args.get("secret") != WEBHOOK_SECRET:
        log.warning("webhook: forbidden — invalid secret from %s", request.remote_addr)
        return jsonify({"ok": False, "error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    client = payload.get("client") or {}
    client_id = client.get("id")

    if not client_id:
        # Может прийти событие без клиента (системное) — отвечаем 200, чтобы SaleBot не ретраил
        return "ok", 200

    # Скастуем структуру под schema clients (см. db.py / migrate.py)
    record = {
        "id": client_id,
        "platform_id": client.get("recepient") or client.get("platform_id") or "",
        "client_type": client.get("client_type"),
        "name": client.get("name"),
        "avatar": client.get("avatar"),
        "tag": client.get("tag"),
        "group": client.get("group"),
        "project_id": payload.get("project_id"),
        "created_at": client.get("created_at"),
        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        "message_id": payload.get("message_id"),
        "custom_answer": client.get("custom_answer"),
        "operator_start_dialog": client.get("operator_start_dialog"),
    }

    try:
        with db.connect() as conn:
            db.upsert_client(conn, record)
            conn.commit()
    except Exception as e:
        log.exception("webhook: db error: %s", e)
        # Возвращаем 200 всё равно — иначе SaleBot будет агрессивно ретраить
        return jsonify({"ok": False, "error": "internal"}), 200

    is_input = payload.get("is_input")
    log.info("webhook: client %s (type=%s, group=%s, name=%s) — %s",
             client_id, record["client_type"], record["group"], record["name"],
             "in" if is_input else "out")

    return "ok", 200


def main():
    db.init_schema()
    log.info(f"webhook listening on {LISTEN_HOST}:{LISTEN_PORT}")
    from waitress import serve
    serve(app, host=LISTEN_HOST, port=LISTEN_PORT, threads=8, _quiet=True)


if __name__ == "__main__":
    main()
