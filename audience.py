"""
Выборка целевой аудитории для рассылки из локальной SQLite.

По умолчанию: все активные TG-клиенты основного бота.
Поддерживает фильтры: bot_group, tag (точное совпадение или LIKE).
"""
from typing import Iterator
import os

from dotenv import load_dotenv

import db

load_dotenv()
DEFAULT_BOT_GROUP = os.environ["TELEGRAM_BROADCAST_BOT_USERNAME"]


def select(bot_group: str | None = None,
           tag: str | None = None,
           tags: list[str] | None = None,
           include_unknown: bool = False) -> tuple[int, list[tuple[int, str, str | None]]]:
    """
    Возвращает (total_count, [(client_id, platform_id, name), ...]).

    bot_group — None = по умолчанию TELEGRAM_BROADCAST_BOT_USERNAME, '*' = все боты.
    tag — одиночный тег (для совместимости с /send tag=X).
    tags — список тегов; объединение по OR. Можно совмещать с tag.
    include_unknown — если True, добавляет клиентов с неизвестным статусом.
    """
    where = ["client_type = 1", "platform_id != ''"]
    params: list = []

    if bot_group != "*":
        where.append("bot_group = ?")
        params.append(bot_group or DEFAULT_BOT_GROUP)

    statuses = ["active"]
    if include_unknown:
        statuses.append("unknown")
    where.append(f"is_active IN ({','.join('?' for _ in statuses)})")
    params.extend(statuses)

    all_tags = []
    if tag:
        all_tags.append(tag)
    if tags:
        all_tags.extend(tags)
    # дедуп с сохранением порядка
    all_tags = list(dict.fromkeys(all_tags))
    if all_tags:
        placeholders = ",".join("?" for _ in all_tags)
        where.append(f"tag IN ({placeholders})")
        params.extend(all_tags)

    sql = f"""
        SELECT id, platform_id, name
        FROM clients
        WHERE {' AND '.join(where)}
    """

    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return len(rows), [(r["id"], r["platform_id"], r["name"]) for r in rows]


def parse_filters(args: str) -> dict:
    """
    Распарсить строку вида 'tag=voronka_ng_1224 bot=your_main_bot'
    или 'tags=a,b,c bot=your_main_bot'.
    """
    out = {}
    for part in args.strip().split():
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if not v:
            continue
        if k == "tag":
            out["tag"] = v
        elif k == "tags":
            out["tags"] = [t.strip() for t in v.split(",") if t.strip()]
        elif k in ("bot", "bot_group"):
            out["bot_group"] = v
    return out


def list_segments(top_n: int = 30) -> dict:
    """
    Доступные сегменты — теги и боты, с числом активных в каждом.
    Используется для команды /segments.
    """
    with db.connect() as conn:
        # Боты — все, у кого есть TG-клиенты
        bots = conn.execute(
            """SELECT bot_group,
                      SUM(CASE WHEN is_active='active' THEN 1 ELSE 0 END) AS active,
                      COUNT(*) AS total
               FROM clients WHERE client_type=1
               GROUP BY bot_group ORDER BY total DESC""",
        ).fetchall()

        # Теги — только в основном боте (по умолчанию)
        tags = conn.execute(
            """SELECT tag,
                      SUM(CASE WHEN is_active='active' THEN 1 ELSE 0 END) AS active,
                      COUNT(*) AS total
               FROM clients
               WHERE client_type=1 AND bot_group=? AND tag IS NOT NULL AND tag != ''
               GROUP BY tag ORDER BY active DESC LIMIT ?""",
            (DEFAULT_BOT_GROUP, top_n),
        ).fetchall()

    return {
        "default_bot": DEFAULT_BOT_GROUP,
        "bots": [(r[0], r[1] or 0, r[2]) for r in bots],
        "tags": [(r[0], r[1] or 0, r[2]) for r in tags],
    }


def stats() -> dict:
    """Сводка по базе: всего, по статусам, по ботам."""
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        tg_total = conn.execute("SELECT COUNT(*) FROM clients WHERE client_type=1").fetchone()[0]

        by_status = {}
        for r in conn.execute(
            "SELECT is_active, COUNT(*) FROM clients WHERE bot_group=? AND client_type=1 GROUP BY is_active",
            (DEFAULT_BOT_GROUP,),
        ):
            by_status[r[0]] = r[1]

        top_bots = conn.execute(
            "SELECT bot_group, COUNT(*) FROM clients WHERE client_type=1 "
            "GROUP BY bot_group ORDER BY 2 DESC LIMIT 10"
        ).fetchall()

        top_tags = conn.execute(
            "SELECT tag, COUNT(*) FROM clients WHERE bot_group=? AND client_type=1 AND tag IS NOT NULL "
            "GROUP BY tag ORDER BY 2 DESC LIMIT 10",
            (DEFAULT_BOT_GROUP,),
        ).fetchall()

    return {
        "total": total,
        "tg_total": tg_total,
        "default_bot": DEFAULT_BOT_GROUP,
        "by_status": by_status,
        "top_bots": [(r[0], r[1]) for r in top_bots],
        "top_tags": [(r[0], r[1]) for r in top_tags],
    }
