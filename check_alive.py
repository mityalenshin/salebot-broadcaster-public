"""
Фаза 1б — проверка живости клиентов.

Использует sendChatAction (не шлёт видимое сообщение пользователю)
для определения, заблокирован ли бот / удалён ли аккаунт.

По умолчанию проверяет только клиентов того бота, что в TELEGRAM_BROADCAST_BOT_USERNAME.
Параллельность — 25 потоков, что даёт ~25-30 req/sec (потолок Telegram).
"""
import datetime as dt
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm

import broadcast
import db

TOKEN = os.environ["TELEGRAM_BROADCAST_BOT_TOKEN"]
BOT_USERNAME = os.environ["TELEGRAM_BROADCAST_BOT_USERNAME"]
TG_API = f"https://api.telegram.org/bot{TOKEN}"

WORKERS = 20
RATE_PER_SECOND = 18           # запас под лимит Telegram (~30/сек)
MAX_RATE_LIMIT_RETRIES = 4
RECHECK = "--recheck" in sys.argv

session = requests.Session()
session.headers["Accept"] = "application/json"


class RateLimiter:
    """Token bucket: гарантирует, что не более N запросов в секунду уходят суммарно от всех потоков."""
    def __init__(self, rate_per_second: float):
        self.interval = 1.0 / rate_per_second
        self.lock = threading.Lock()
        self.next_slot = time.monotonic()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_slot:
                wait = self.next_slot - now
                self.next_slot += self.interval
            else:
                wait = 0
                self.next_slot = now + self.interval
        if wait > 0:
            time.sleep(wait)


limiter = RateLimiter(RATE_PER_SECOND)


def classify_response(status_code: int, payload: dict) -> tuple[str, str | None]:
    """
    Возвращает (status, error).
    status: active | blocked | deleted | not_started | invalid | error
    """
    if payload.get("ok"):
        return "active", None

    desc = (payload.get("description") or "").lower()
    if "blocked" in desc:
        return "blocked", payload.get("description")
    if "deactivated" in desc:
        return "deleted", payload.get("description")
    if "can't initiate" in desc or "not found" in desc:
        return "not_started", payload.get("description")
    if status_code in (400, 403, 404):
        return "invalid", payload.get("description")
    return "error", payload.get("description") or f"HTTP {status_code}"


def check_one(chat_id: str) -> tuple[str, str | None]:
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        limiter.acquire()
        try:
            r = session.post(
                f"{TG_API}/sendChatAction",
                json={"chat_id": int(chat_id), "action": "typing"},
                timeout=20,
            )
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            return "error", str(e)[:200]

        if r.status_code == 429:
            retry_after = int(payload.get("parameters", {}).get("retry_after", 5))
            time.sleep(min(retry_after + 1, 60))
            continue
        return classify_response(r.status_code, payload)
    return "error", "rate limit retries exhausted"


def select_targets(conn) -> list[tuple[int, str]]:
    where = "bot_group = ? AND client_type = 1 AND platform_id != ''"
    if not RECHECK:
        where += " AND is_active = 'unknown'"
    rows = conn.execute(
        f"SELECT id, platform_id FROM clients WHERE {where}",
        (BOT_USERNAME,),
    ).fetchall()
    return [(r["id"], r["platform_id"]) for r in rows]


# Запись в БД из нескольких потоков — через лок и общий cursor
write_lock = threading.Lock()


def main():
    # Если параллельно идёт рассылка — не дёргаем тот же токен и не превышаем rate
    if broadcast.is_running():
        print("Активная рассылка — пропускаем check_alive чтобы не конфликтовать с rate limit.")
        return

    conn = db.connect()
    targets = select_targets(conn)

    if not targets:
        print(f"[{dt.datetime.now(dt.UTC).isoformat()}] Нет клиентов для проверки — все уже проверены.")
        return

    print(f"[{dt.datetime.now(dt.UTC).isoformat()}] Бот: @{BOT_USERNAME}, проверяем {len(targets):,} клиентов")

    counters = {"active": 0, "blocked": 0, "deleted": 0,
                "not_started": 0, "invalid": 0, "error": 0}

    def worker(item):
        cid, pid = item
        status, err = check_one(pid)
        return cid, status, err

    # Отключаем прогресс-бар при запуске не из терминала (cron)
    pbar = tqdm(total=len(targets), unit=" клиентов",
                disable=not sys.stdout.isatty())
    pending_writes = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, t) for t in targets]
        try:
            for fut in as_completed(futures):
                cid, status, err = fut.result()
                checked_at = dt.datetime.now(dt.UTC).isoformat()
                with write_lock:
                    db.update_alive_status(conn, cid, status, err, checked_at)
                    pending_writes += 1
                    if pending_writes >= 200:
                        conn.commit()
                        pending_writes = 0
                counters[status] += 1
                pbar.update(1)
                pbar.set_postfix(counters)
        except KeyboardInterrupt:
            print("\nОстанавливаю...")

    conn.commit()
    pbar.close()

    print("\n" + "=" * 60)
    print("Итого по статусам:")
    print("=" * 60)
    for k, v in counters.items():
        print(f"  {k:14s}  {v:>6,}")
    total = sum(counters.values())
    if total:
        active = counters["active"]
        print(f"\n  Доля активных: {active}/{total} = {100 * active / total:.1f}%")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
