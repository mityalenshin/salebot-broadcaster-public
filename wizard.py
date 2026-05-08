"""
FSM-визард для создания рассылки.
Шаги: draft → bot → segment → time → confirm.

Хранение состояния — in-memory dict (пропадает при рестарте бота, что ок для MVP).
"""
import datetime as dt
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import audience
import broadcast
import db
import post_builder

load_dotenv()
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Moscow"))


LARGE_BROADCAST_THRESHOLD = 5000  # выше — требуем ввод "ОТПРАВИТЬ" текстом


@dataclass
class WizardState:
    user_id: int
    chat_id: int
    step: str = "awaiting_message"
    text: str = ""
    photo_command_file_id: str | None = None
    photo_broadcast_file_id: str | None = None
    buttons: list = field(default_factory=list)
    selected_bot: str | None = None
    selected_tags: list[str] = field(default_factory=list)  # пустой = все активные
    scheduled_at: dt.datetime | None = None  # None = сейчас (UTC)
    add_utm: bool = False
    spread_minutes: int = 0  # 0 = без растяжки (~25 req/sec)
    segment_message_id: int | None = None
    options_message_id: int | None = None


WIZARDS: dict[int, WizardState] = {}
WIZARDS_LOCK = threading.Lock()


def get(user_id: int) -> WizardState | None:
    with WIZARDS_LOCK:
        return WIZARDS.get(user_id)


def start(user_id: int, chat_id: int) -> WizardState:
    state = WizardState(user_id=user_id, chat_id=chat_id)
    with WIZARDS_LOCK:
        WIZARDS[user_id] = state
    return state


def cancel(user_id: int) -> bool:
    with WIZARDS_LOCK:
        return WIZARDS.pop(user_id, None) is not None


# ---------- парсинг времени ----------

def parse_when(s: str) -> dt.datetime:
    """
    Парсит ввод вроде:
      14:00         — сегодня в 14:00 (или завтра, если уже прошло)
      08.05 14:00   — 8 мая текущего года в 14:00
      08.05.2026 14:00
    Возвращает aware datetime в UTC.
    Бросает ValueError если формат не распознан.
    """
    s = s.strip()
    now_local = dt.datetime.now(TZ)
    candidates = [
        ("%H:%M",          False),  # только время → сегодня
        ("%d.%m %H:%M",    False),
        ("%d.%m.%Y %H:%M", True),
        ("%d.%m.%y %H:%M", True),
    ]
    for fmt, has_year in candidates:
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue

        if fmt == "%H:%M":
            local = now_local.replace(hour=t.hour, minute=t.minute,
                                     second=0, microsecond=0)
            if local <= now_local:
                local += dt.timedelta(days=1)
        elif not has_year:
            local = now_local.replace(month=t.month, day=t.day,
                                     hour=t.hour, minute=t.minute,
                                     second=0, microsecond=0)
            if local <= now_local:
                # если дата уже прошла — следующий год
                local = local.replace(year=local.year + 1)
        else:
            local = t.replace(tzinfo=TZ)

        return local.astimezone(dt.timezone.utc)

    raise ValueError(
        "Не понял формат. Примеры:\n"
        "  <code>14:00</code> — сегодня в 14:00\n"
        "  <code>08.05 14:00</code> — 8 мая в 14:00\n"
        "  <code>08.05.2026 14:00</code>"
    )


def format_when(d: dt.datetime) -> str:
    """Datetime в UTC → строка в локальном часовом поясе."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


# ---------- сохранение draft в БД ----------

def save_to_db(state: WizardState, started_by: str) -> int:
    """Создать запись broadcasts со статусом 'scheduled' или 'draft' и вернуть id."""
    filters = {}
    if state.selected_bot:
        filters["bot_group"] = state.selected_bot
    if state.selected_tags:
        filters["tags"] = state.selected_tags

    audience_descr = []
    audience_descr.append(f"bot={state.selected_bot or audience.DEFAULT_BOT_GROUP}")
    if state.selected_tags:
        audience_descr.append(f"tags={','.join(state.selected_tags)}")
    audience_descr.append("status=active")
    if state.photo_broadcast_file_id:
        audience_descr.append("with_photo")
    if state.buttons:
        audience_descr.append(f"buttons={sum(len(r) for r in state.buttons)}")

    if state.add_utm:
        audience_descr.append("utm")
    if state.spread_minutes:
        audience_descr.append(f"spread={state.spread_minutes}m")

    status = "scheduled" if state.scheduled_at else "draft"
    scheduled_iso = state.scheduled_at.isoformat() if state.scheduled_at else None

    options = {
        "add_utm": state.add_utm,
        "spread_minutes": state.spread_minutes,
    }

    total, _ = audience.select(**filters)

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO broadcasts
               (text, parse_mode, audience, total, status, started_by,
                scheduled_at, audience_filters, photo_file_id, buttons_json, options_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.text, "HTML",
                ", ".join(audience_descr),
                total, status, started_by,
                scheduled_iso,
                json.dumps(filters, ensure_ascii=False),
                state.photo_broadcast_file_id,
                json.dumps(state.buttons, ensure_ascii=False) if state.buttons else None,
                json.dumps(options, ensure_ascii=False),
            ),
        )
        conn.commit()
        return cur.lastrowid


# ---------- запуск отложенной (вызывает scheduler) ----------

def run_scheduled(broadcast_id: int, on_progress=None) -> dict:
    """Восстановить рассылку из БД и запустить."""
    with db.connect() as conn:
        r = conn.execute(
            "SELECT * FROM broadcasts WHERE id=?", (broadcast_id,)
        ).fetchone()
    if not r:
        raise RuntimeError(f"broadcast #{broadcast_id} не найден")
    if r["status"] != "scheduled":
        raise RuntimeError(f"broadcast #{broadcast_id} в статусе '{r['status']}', не scheduled")

    filters = json.loads(r["audience_filters"] or "{}")
    total, targets = audience.select(**filters)

    buttons = json.loads(r["buttons_json"]) if r["buttons_json"] else None
    options = json.loads(r["options_json"] or "{}")

    with db.connect() as conn:
        conn.execute("UPDATE broadcasts SET total=? WHERE id=?", (total, broadcast_id))
        conn.commit()

    return broadcast.run(
        broadcast_id=broadcast_id,
        targets=targets,
        text=r["text"],
        parse_mode=r["parse_mode"] or "HTML",
        photo_file_id=r["photo_file_id"],
        buttons=buttons,
        add_utm=options.get("add_utm", False),
        spread_minutes=options.get("spread_minutes", 0),
        on_progress=on_progress,
        progress_every=300,
    )
