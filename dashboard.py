"""
Дашборд аналитики — закрытая веб-страница для админов.
Защищён DASHBOARD_TOKEN в query string.

Регистрируется как Blueprint в webhook.py:
    from dashboard import bp as dashboard_bp
    app.register_blueprint(dashboard_bp)
"""
import os

from dotenv import load_dotenv
from flask import Blueprint, abort, jsonify, render_template, request

import audience
import db

load_dotenv()
DASHBOARD_TOKEN = os.environ["DASHBOARD_TOKEN"]

bp = Blueprint("dashboard", __name__)


def _check_token():
    if request.args.get("token") != DASHBOARD_TOKEN:
        abort(403)


@bp.get("/dashboard")
def index():
    _check_token()
    return render_template("dashboard.html", token=DASHBOARD_TOKEN)


@bp.get("/api/stats")
def api_stats():
    _check_token()
    s = audience.stats()
    by_status = s["by_status"]
    return jsonify({
        "total": s["total"],
        "tg_total": s["tg_total"],
        "default_bot": s["default_bot"],
        "by_status": by_status,
        "top_bots": s["top_bots"],
    })


@bp.get("/api/timeline")
def api_timeline():
    """Рассылки за последние 30 дней по дням."""
    _check_token()
    with db.connect() as c:
        rows = c.execute(
            """SELECT DATE(started_at) AS day,
                      COUNT(*) AS count,
                      COALESCE(SUM(sent), 0) AS sent,
                      COALESCE(SUM(failed), 0) AS failed,
                      COALESCE(SUM(blocked), 0) AS blocked
               FROM broadcasts
               WHERE started_at IS NOT NULL
                 AND started_at >= DATE('now', '-30 days')
               GROUP BY DATE(started_at)
               ORDER BY day"""
        ).fetchall()
    return jsonify({
        "days": [r["day"] for r in rows],
        "counts": [r["count"] for r in rows],
        "sent":   [r["sent"] for r in rows],
        "failed": [r["failed"] for r in rows],
        "blocked":[r["blocked"] for r in rows],
    })


@bp.get("/api/broadcasts")
def api_broadcasts():
    """Последние 30 рассылок."""
    _check_token()
    with db.connect() as c:
        rows = c.execute(
            """SELECT id, status, total, sent, failed, blocked,
                      audience, started_at, finished_at, scheduled_at, started_by
               FROM broadcasts ORDER BY id DESC LIMIT 30"""
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/api/segments")
def api_segments():
    _check_token()
    s = audience.list_segments(top_n=20)
    return jsonify({
        "default_bot": s["default_bot"],
        "bots": s["bots"],
        "tags": s["tags"],
    })
