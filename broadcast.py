"""
Синхронный движок массовой рассылки через Telegram Bot API.

Реализация: requests + ThreadPoolExecutor (так же, как check_alive — проверено работает).

Особенности:
- Глобальный rate limiter (потолок ~30 req/sec для бота, берём 25 для запаса)
- Уважение Retry-After из ответов 429
- На лету помечает клиентов, заблокировавших бота → исключение из будущих рассылок
- Lock-файл, чтобы одновременно не запускались две рассылки
- Прогресс через callback (для UI бота)
"""
import datetime as dt
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests
from dotenv import load_dotenv

import db

load_dotenv()

TOKEN = os.environ["TELEGRAM_BROADCAST_BOT_TOKEN"]
TG_API = f"https://api.telegram.org/bot{TOKEN}"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
LOCK_FILE = DATA_DIR / "broadcast.lock"

RATE_PER_SECOND = 25
WORKERS = 30
MAX_429_RETRIES = 4
SEND_TIMEOUT = 30


class BroadcastInProgress(Exception):
    pass


def is_running() -> bool:
    return LOCK_FILE.exists()


def lock_info() -> dict | None:
    if not LOCK_FILE.exists():
        return None
    try:
        return json.loads(LOCK_FILE.read_text())
    except Exception:
        return {"raw": LOCK_FILE.read_text()}


def _acquire_lock(broadcast_id: int) -> None:
    if LOCK_FILE.exists():
        raise BroadcastInProgress(f"Уже запущена рассылка: {lock_info()}")
    LOCK_FILE.write_text(json.dumps({
        "broadcast_id": broadcast_id,
        "pid": os.getpid(),
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
    }))


def _release_lock() -> None:
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


class RateLimiter:
    """Token bucket: гарантирует, что не более N req/sec уходит суммарно от всех потоков."""
    def __init__(self, rate_per_second: float):
        self.interval = 1.0 / rate_per_second
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            time.sleep(wait)


def create_draft(text: str, parse_mode: str | None,
                audience_descr: str, total: int, started_by: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO broadcasts (text, parse_mode, audience, total, status, started_by)
               VALUES (?, ?, ?, ?, 'draft', ?)""",
            (text, parse_mode, audience_descr, total, started_by),
        )
        return cur.lastrowid


FALLBACK_NAME = "друг"  # подставляется, если у клиента имя пустое


def personalize(template: str, name: str | None) -> str:
    """
    Подстановка плейсхолдеров в тексте сообщения.
      {{name}}        — полное имя клиента
      {{first_name}}  — первое слово имени
    Если имя клиента пустое — подставляется FALLBACK_NAME.
    """
    if not template:
        return template
    if "{{name}}" not in template and "{{first_name}}" not in template:
        return template  # нет плейсхолдеров — быстрый путь
    n = (name or "").strip()
    if not n:
        n = FALLBACK_NAME
    first = n.split()[0] if n else FALLBACK_NAME
    return template.replace("{{name}}", n).replace("{{first_name}}", first)


def _classify_send_error(desc: str | None) -> str:
    desc_l = (desc or "").lower()
    if "blocked" in desc_l:
        return "blocked"
    if "deactivated" in desc_l:
        return "deleted"
    if "not found" in desc_l or "can't initiate" in desc_l:
        return "not_started"
    return "error"


def build_reply_markup(buttons: list[list[tuple[str, str]]] | None) -> dict | None:
    """buttons — список рядов, каждый ряд — список (text, url). Возвращает inline_keyboard."""
    if not buttons:
        return None
    return {
        "inline_keyboard": [
            [{"text": text, "url": url} for text, url in row]
            for row in buttons if row
        ]
    }


def add_utm_to_buttons(buttons: list[list[tuple[str, str]]] | None,
                      broadcast_id: int) -> list[list[tuple[str, str]]] | None:
    """К каждой URL-кнопке добавить utm-параметры для отслеживания в Я.Метрике/GA."""
    if not buttons:
        return buttons
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    out = []
    for row in buttons:
        new_row = []
        for text, url in row:
            try:
                p = urlparse(url)
                qs = dict(parse_qsl(p.query))
                qs.setdefault("utm_source", "tg_broadcast")
                qs.setdefault("utm_medium", "button")
                qs.setdefault("utm_campaign", str(broadcast_id))
                qs.setdefault("utm_content", text[:50].replace(" ", "_") or "btn")
                new_url = urlunparse(p._replace(query=urlencode(qs)))
                new_row.append((text, new_url))
            except Exception:
                new_row.append((text, url))  # на любой проблемный URL — оставляем как есть
        out.append(new_row)
    return out


def _send_one(session: requests.Session, limiter: RateLimiter,
              chat_id: str, text: str, parse_mode: str | None,
              photo_file_id: str | None = None,
              reply_markup: dict | None = None) -> tuple[bool, str | None]:
    """Отправляет одно сообщение. Если photo_file_id задан — sendPhoto с caption, иначе sendMessage."""
    if photo_file_id:
        method = "sendPhoto"
        payload = {"chat_id": int(chat_id), "photo": photo_file_id, "caption": text}
    else:
        method = "sendMessage"
        payload = {"chat_id": int(chat_id), "text": text}

    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(MAX_429_RETRIES):
        limiter.acquire()
        try:
            r = session.post(f"{TG_API}/{method}", json=payload, timeout=SEND_TIMEOUT)
            data = r.json()
        except (requests.RequestException, ValueError):
            return False, "error"

        if data.get("ok"):
            return True, None

        if r.status_code == 429:
            retry_after = int(data.get("parameters", {}).get("retry_after", 5))
            time.sleep(min(retry_after + 1, 60))
            continue

        return False, _classify_send_error(data.get("description"))

    return False, "error"


# Защищаем общий cursor SQLite — обновлять статус из множества потоков.
_db_lock = threading.Lock()


def run(broadcast_id: int,
        targets: list[tuple[int, str, str | None]],
        text: str,
        parse_mode: str | None = "HTML",
        photo_file_id: str | None = None,
        buttons: list[list[tuple[str, str]]] | None = None,
        add_utm: bool = False,
        spread_minutes: int = 0,
        on_progress: Callable[[dict], None] | None = None,
        progress_every: int = 300) -> dict:
    """
    Запустить рассылку. targets = [(client_id, platform_id, name), ...].
    photo_file_id — file_id фото от ИМЕНИ БОТА РАССЫЛОК.
    buttons — список рядов кнопок: [[(text, url), ...], ...].
    add_utm — если True, в URL-кнопок добавляются utm-параметры для аналитики.
    spread_minutes — растянуть рассылку на N минут (0 = на максимальной скорости).
    Текст может содержать плейсхолдеры {{name}} / {{first_name}}.
    Блокирующая.
    """
    if add_utm:
        buttons = add_utm_to_buttons(buttons, broadcast_id)
    reply_markup = build_reply_markup(buttons)
    has_personalization = "{{name}}" in (text or "") or "{{first_name}}" in (text or "")

    # Эффективная скорость: если задана растяжка — равномерно за указанные минуты
    if spread_minutes > 0 and len(targets) > 0:
        effective_rate = max(0.05, len(targets) / (spread_minutes * 60))
    else:
        effective_rate = RATE_PER_SECOND
    _acquire_lock(broadcast_id)
    started_at = dt.datetime.now(dt.UTC).isoformat()

    with db.connect() as conn:
        conn.execute(
            "UPDATE broadcasts SET status='running', started_at=?, total=? WHERE id=?",
            (started_at, len(targets), broadcast_id),
        )
        conn.commit()

    limiter = RateLimiter(effective_rate)
    session = requests.Session()
    session.headers["Accept"] = "application/json"

    # Счётчики — защищены через lock, потому что обновляются из разных потоков.
    state = {"sent": 0, "failed": 0, "blocked_now": 0, "by_error": {}}
    state_lock = threading.Lock()

    def worker(item: tuple[int, str, str | None]):
        client_id, platform_id, client_name = item
        msg_text = personalize(text, client_name) if has_personalization else text
        ok, err = _send_one(session, limiter, platform_id, msg_text, parse_mode,
                           photo_file_id=photo_file_id, reply_markup=reply_markup)

        with state_lock:
            if ok:
                state["sent"] += 1
            else:
                state["failed"] += 1
                state["by_error"][err] = state["by_error"].get(err, 0) + 1
                if err in ("blocked", "deleted", "not_started"):
                    if err == "blocked":
                        state["blocked_now"] += 1

        if not ok and err in ("blocked", "deleted", "not_started"):
            with _db_lock, db.connect() as c:
                db.update_alive_status(c, client_id, err, None,
                                      dt.datetime.now(dt.UTC).isoformat())
                c.commit()

        with state_lock:
            done = state["sent"] + state["failed"]
            should_emit = on_progress and done % progress_every == 0
        if should_emit:
            try:
                on_progress({
                    "broadcast_id": broadcast_id,
                    "sent": state["sent"], "failed": state["failed"],
                    "blocked": state["blocked_now"],
                    "total": len(targets),
                    "by_error": dict(state["by_error"]),
                })
            except Exception:
                pass  # не валим рассылку из-за UI

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(worker, t) for t in targets]
            for _ in as_completed(futures):
                pass

        finished_at = dt.datetime.now(dt.UTC).isoformat()
        with db.connect() as conn:
            conn.execute(
                """UPDATE broadcasts
                   SET status='completed', finished_at=?, sent=?, failed=?, blocked=?
                   WHERE id=?""",
                (finished_at, state["sent"], state["failed"],
                 state["blocked_now"], broadcast_id),
            )
            conn.commit()

        result = {
            "broadcast_id": broadcast_id,
            "sent": state["sent"], "failed": state["failed"],
            "blocked": state["blocked_now"],
            "total": len(targets), "by_error": dict(state["by_error"]),
            "started_at": started_at, "finished_at": finished_at,
        }
        if on_progress:
            try:
                on_progress(result)
            except Exception:
                pass
        return result

    except BaseException:
        with db.connect() as conn:
            conn.execute(
                "UPDATE broadcasts SET status='failed', finished_at=?, sent=?, failed=? WHERE id=?",
                (dt.datetime.now(dt.UTC).isoformat(), state["sent"],
                 state["failed"], broadcast_id),
            )
            conn.commit()
        raise
    finally:
        _release_lock()
