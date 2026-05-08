"""
Phase 0 — Proof of Concept.
Отправляем одно тестовое сообщение через Telegram Bot API напрямую,
с тем же токеном бота, что подключён к SaleBot.
Цель — убедиться, что квота SaleBot не списывается и воронки не ломаются.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import requests

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

TOKEN = os.environ["TELEGRAM_BROADCAST_BOT_TOKEN"]
TEST_IDS = [int(x) for x in os.environ["TELEGRAM_TEST_USER_IDS"].split(",") if x.strip()]
API = f"https://api.telegram.org/bot{TOKEN}"

def message_text(n: int, total: int) -> str:
    return (
        f"🧪 <b>PoC — Фаза 0 — сообщение {n}/{total}</b>\n\n"
        "Отправлено напрямую через <code>sendMessage</code>, минуя SaleBot.\n"
        f"Время: {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
    )


def me():
    r = requests.get(f"{API}/getMe", timeout=10).json()
    if not r.get("ok"):
        sys.exit(f"getMe failed: {r}")
    u = r["result"]
    print(f"Бот: @{u['username']} (id={u['id']}, name={u['first_name']})")


def send(chat_id: int, text: str):
    r = requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    ).json()
    if r.get("ok"):
        print(f"  → chat_id={chat_id}: OK, message_id={r['result']['message_id']}")
    else:
        print(f"  → chat_id={chat_id}: FAIL — {r.get('description')}")


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print("=" * 50)
    print(f"PoC: отправка {count} сообщ. на {len(TEST_IDS)} получателей")
    print("=" * 50)
    me()
    for n in range(1, count + 1):
        for cid in TEST_IDS:
            send(cid, message_text(n, count))
    print("=" * 50)
    print(f"Всего отправок: {count * len(TEST_IDS)}")
    print("Теперь обнови SaleBot и сравни счётчик квоты.")


if __name__ == "__main__":
    main()
