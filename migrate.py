"""
Фаза 1а — миграция всех клиентов из SaleBot в локальную SQLite.

Идёт пагинацией по 500, rate limit 1 req/sec.
Сохраняет всех клиентов всех ботов проекта (your_main_bot, another_bot и т.д.).
Идемпотентно: повторный запуск обновит существующие записи.

Использование:
    python migrate.py                  # с самого начала
    python migrate.py --offset 35000   # резюмировать с указанной позиции
"""
import argparse
import datetime as dt
import os
import sys
import time
from collections import Counter

import requests
from tqdm import tqdm

import db

API_KEY = os.environ["SALEBOT_API_KEY"]
BASE = f"https://chatter.salebot.pro/api/{API_KEY}"
PAGE_SIZE = 500
RATE_LIMIT_SLEEP = 1.0
RETRY_ATTEMPTS = 5
RETRY_BACKOFF = 5.0  # секунд между попытками после сбоя (растёт линейно)


def fetch_page(session: requests.Session, offset: int) -> list[dict]:
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = session.get(
                f"{BASE}/get_clients",
                params={"limit": PAGE_SIZE, "offset": offset},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "success":
                raise RuntimeError(f"API вернул статус: {data.get('status')}")
            return data.get("clients", [])
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS:
                wait = RETRY_BACKOFF * attempt
                tqdm.write(f"  ⚠ ошибка на offset={offset} (попытка {attempt}): {type(e).__name__} — пауза {wait}s")
                time.sleep(wait)
    raise RuntimeError(f"offset={offset}: все попытки исчерпаны, последняя ошибка: {last_err}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0,
                       help="Начать с этого offset (для возобновления после сбоя)")
    args = parser.parse_args()

    db.init_schema()
    started_at = dt.datetime.now(dt.UTC).isoformat()

    session = requests.Session()
    session.headers["Accept"] = "application/json"

    offset = args.offset
    total = 0
    groups = Counter()
    tags = Counter()
    types = Counter()

    print(f"Старт миграции. БД: {db.DB_PATH}")
    print(f"Page size: {PAGE_SIZE}, sleep между запросами: {RATE_LIMIT_SLEEP}s\n")

    pbar = tqdm(unit=" клиентов", desc="Выгрузка")
    conn = db.connect()
    try:
        while True:
            clients = fetch_page(session, offset)
            if not clients:
                break

            for c in clients:
                db.upsert_client(conn, c)
                groups[c.get("group")] += 1
                tags[c.get("tag")] += 1
                types[c.get("client_type")] += 1

            conn.commit()
            total += len(clients)
            pbar.update(len(clients))

            if len(clients) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(RATE_LIMIT_SLEEP)
    except KeyboardInterrupt:
        print("\nПрервано. Уже выгружено:", total)
    finally:
        pbar.close()
        conn.execute(
            "INSERT INTO migration_log (started_at, finished_at, fetched, note) VALUES (?, ?, ?, ?)",
            (started_at, dt.datetime.now(dt.UTC).isoformat(), total,
             "ok" if offset >= 0 else "interrupted"),
        )
        conn.commit()
        conn.close()

    print("\n" + "=" * 60)
    print(f"Всего выгружено: {total}")
    print("=" * 60)
    print("\nПо ботам (поле group):")
    for g, n in groups.most_common():
        print(f"  {g!r:30s}  {n:>6}")
    print("\nПо client_type:")
    for t, n in types.most_common():
        labels = {1: "TG bot", 16: "TG Business", 0: "?", 2: "VK", 3: "WA"}
        print(f"  {t} ({labels.get(t, '?'):12s}) {n:>6}")
    print("\nTop-15 тегов источника (поле tag):")
    for t, n in tags.most_common(15):
        print(f"  {t!r:40s}  {n:>6}")


if __name__ == "__main__":
    sys.exit(main())
