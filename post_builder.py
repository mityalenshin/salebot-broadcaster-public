"""
Парсинг поста с кнопками и подготовка медиа для рассылки.

Формат сообщения:
    <текст подписи / поста>

    [Текст кнопки 1 | https://url1]
    [Текст кнопки 2 | https://url2]

Каждая «кнопка» — это последняя строка вида `[текст | url]`.
Подряд идущие строки в этом формате собираются как кнопки (один ряд = одна кнопка).
Парсинг идёт с конца, до первой строки, которая не подходит под формат
(не считая пустых строк-разделителей).
"""
import os
import re
from io import BytesIO

import requests
from dotenv import load_dotenv

load_dotenv()

COMMAND_TOKEN = os.environ["TELEGRAM_COMMAND_BOT_TOKEN"]
BROADCAST_TOKEN = os.environ["TELEGRAM_BROADCAST_BOT_TOKEN"]

BUTTON_RE = re.compile(r"^\[(.+?)\s*\|\s*(\S+?)\]\s*$")


def parse_buttons(text: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """
    Возвращает (очищенный_текст, [[(text, url)], ...]).
    Каждая кнопка — отдельный ряд (один ряд = одна кнопка).
    """
    if not text:
        return "", []
    lines = text.split("\n")
    buttons: list[tuple[str, str]] = []

    while lines:
        last = lines[-1].rstrip()
        if last == "":
            lines.pop()
            continue
        m = BUTTON_RE.match(last)
        if not m:
            break
        buttons.insert(0, (m.group(1).strip(), m.group(2).strip()))
        lines.pop()

    cleaned = "\n".join(lines).rstrip()
    rows = [[btn] for btn in buttons]  # 1 кнопка = 1 ряд
    return cleaned, rows


def reupload_photo(command_bot_file_id: str, target_chat_id: int,
                  caption: str = "", buttons: list[list[tuple[str, str]]] | None = None,
                  parse_mode: str = "HTML") -> str:
    """
    Перезалить фото от ИМЕНИ бота рассылок (TELEGRAM_BROADCAST_BOT_TOKEN).
    Бот команд скачивает по своему file_id, бот рассылок отправляет его на target_chat_id
    с указанной подписью и кнопками — это и есть готовое превью.

    Возвращает file_id, валидный для бота рассылок (для последующих массовых отправок).
    """
    # 1. Узнать file_path у бота команд
    r = requests.get(
        f"https://api.telegram.org/bot{COMMAND_TOKEN}/getFile",
        params={"file_id": command_bot_file_id},
        timeout=15,
    ).json()
    if not r.get("ok"):
        raise RuntimeError(f"getFile failed: {r}")
    file_path = r["result"]["file_path"]

    # 2. Скачать файл
    file_resp = requests.get(
        f"https://api.telegram.org/file/bot{COMMAND_TOKEN}/{file_path}",
        timeout=60,
    )
    file_resp.raise_for_status()

    # 3. Подготовить multipart payload
    files = {"photo": ("photo.jpg", BytesIO(file_resp.content))}
    data: dict = {"chat_id": target_chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = parse_mode
    if buttons:
        import json as _json
        data["reply_markup"] = _json.dumps({
            "inline_keyboard": [
                [{"text": t, "url": u} for t, u in row] for row in buttons if row
            ]
        }, ensure_ascii=False)

    # 4. Залить через бота рассылок
    r = requests.post(
        f"https://api.telegram.org/bot{BROADCAST_TOKEN}/sendPhoto",
        data=data, files=files, timeout=60,
    ).json()
    if not r.get("ok"):
        raise RuntimeError(f"sendPhoto (reupload) failed: {r.get('description')}")

    # У результата photo — массив с разными resolution. Берём самое большое (последнее).
    return r["result"]["photo"][-1]["file_id"]


def send_via_broadcast_bot(chat_id: int, text: str,
                          photo_file_id: str | None = None,
                          buttons: list[list[tuple[str, str]]] | None = None,
                          parse_mode: str = "HTML") -> dict:
    """
    Простая обёртка для отправки одного сообщения через бот рассылок (для превью).
    Возвращает result от Telegram API.
    """
    reply_markup = None
    if buttons:
        reply_markup = {
            "inline_keyboard": [
                [{"text": text_, "url": url} for text_, url in row]
                for row in buttons if row
            ]
        }

    if photo_file_id:
        method = "sendPhoto"
        payload = {"chat_id": chat_id, "photo": photo_file_id,
                  "caption": text, "parse_mode": parse_mode}
    else:
        method = "sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

    if reply_markup:
        payload["reply_markup"] = reply_markup

    r = requests.post(
        f"https://api.telegram.org/bot{BROADCAST_TOKEN}/{method}",
        json=payload, timeout=30,
    ).json()
    if not r.get("ok"):
        raise RuntimeError(f"{method} failed: {r.get('description')}")
    return r["result"]
