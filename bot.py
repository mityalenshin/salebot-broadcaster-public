"""
Бот команд — управление рассылками из закрытого рабочего чата.

Реализован на pyTelegramBotAPI (sync, requests-based).
Команды:
  /help          — список команд
  /stats         — сводка по базе
  /preview       — превью текущего черновика на тестовых юзерах
  /send          — запустить массовую рассылку (требует подтверждения)
  /status        — прогресс активной рассылки
  /cancel_lock   — снять lock-файл (если процесс упал)

Текст рассылки — это сообщение, на которое сделан реплай командой /preview или /send.
"""
import logging
import os
import threading
from html import escape

import telebot
from telebot import types
from dotenv import load_dotenv

import audience
import broadcast
import db
import post_builder
import wizard

load_dotenv()
logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("bot")

COMMAND_TOKEN = os.environ["TELEGRAM_COMMAND_BOT_TOKEN"]
ADMIN_USERNAMES = set(u.strip().lower()
                     for u in os.environ["TELEGRAM_ADMIN_USERNAMES"].split(",") if u.strip())
WORK_CHAT_ID = int(os.environ["TELEGRAM_WORK_CHAT_ID"])
TEST_USER_IDS = [int(x) for x in os.environ["TELEGRAM_TEST_USER_IDS"].split(",") if x.strip()]

bot = telebot.TeleBot(COMMAND_TOKEN, parse_mode="HTML", num_threads=4)


def is_authorized(message: types.Message) -> bool:
    user = message.from_user
    if not user or not user.username:
        return False
    if user.username.lower() not in ADMIN_USERNAMES:
        return False
    return message.chat.id == WORK_CHAT_ID or message.chat.type == "private"


def reply_text_html(message: types.Message) -> str:
    """Достать текст или подпись из reply-сообщения с сохранением форматирования."""
    src = message.reply_to_message
    if not src:
        return ""
    # Приоритет: html_text (для текстовых), html_caption (для медиа), потом plain
    if getattr(src, "html_text", None):
        return src.html_text
    if getattr(src, "html_caption", None):
        return src.html_caption
    return src.text or src.caption or ""


def reply_photo_file_id(message: types.Message) -> str | None:
    """Если в реплае есть фото — вернуть file_id самого большого варианта."""
    src = message.reply_to_message
    if not src or not getattr(src, "photo", None):
        return None
    return src.photo[-1].file_id


def build_post_from_reply(message: types.Message) -> tuple[str, str | None, list[list[tuple[str, str]]]]:
    """Из reply-сообщения собрать: (cleaned_text, photo_file_id, buttons)."""
    raw = reply_text_html(message)
    text, buttons = post_builder.parse_buttons(raw)
    file_id = reply_photo_file_id(message)
    return text, file_id, buttons


def build_post_from_message(message: types.Message) -> tuple[str, str | None, list[list[tuple[str, str]]]]:
    """Из самого сообщения (не reply) — текст/подпись + photo + кнопки."""
    raw = (getattr(message, "html_text", None) or getattr(message, "html_caption", None)
           or message.text or message.caption or "")
    cleaned, buttons = post_builder.parse_buttons(raw)
    photo_id = message.photo[-1].file_id if getattr(message, "photo", None) else None
    return cleaned, photo_id, buttons


@bot.message_handler(commands=["help", "start"])
def cmd_help(message):
    if not is_authorized(message):
        return
    bot.send_message(message.chat.id,
        "<b>Команды управления рассылкой</b>\n\n"
        "<code>/stats</code> — сводка по базе\n"
        "<code>/preview</code> — реплай на сообщение → превью на тестовых через @your_main_bot\n"
        "<code>/send</code> — реплай на сообщение → подтверждение → массовая рассылка\n"
        "<code>/status</code> — прогресс активной рассылки\n"
        "<code>/cancel_lock</code> — снять lock-файл\n\n"
        "<b>Что можно отправить в одном сообщении:</b>\n"
        "• Только текст\n"
        "• Текст + фото (фото = подпись к нему, лимит 1024 символа)\n"
        "• Текст + кнопки\n"
        "• Текст + фото + кнопки\n\n"
        "<b>Кнопки</b> — добавляются последними строками в формате:\n"
        "<code>[Текст кнопки | https://url]</code>\n\n"
        "<b>Персонализация:</b>\n"
        "<code>{{name}}</code> — полное имя получателя\n"
        "<code>{{first_name}}</code> — первое слово имени\n\n"
        "<b>Пример сообщения:</b>\n"
        "<pre>Привет, {{first_name}}! Сегодня в 19:00 эфир про SaleBot.\n\n[Записаться | https://example.com/join]</pre>")


def parse_command_args(message) -> str:
    """Достать аргументы после команды. /send tag=voronka → 'tag=voronka'."""
    parts = (message.text or "").split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


@bot.message_handler(commands=["segments"])
def cmd_segments(message):
    if not is_authorized(message):
        return
    s = audience.list_segments(top_n=20)
    body = f"<b>Доступные сегменты для рассылки</b>\n\n"
    body += f"<b>По ботам</b> (active / всего):\n"
    for name, active, total in s["bots"][:10]:
        body += f"  <code>bot={escape(name or '?')}</code> — {active:,} / {total:,}\n"
    body += f"\n<b>По тегам</b> (только в <code>{s['default_bot']}</code>, active / всего):\n"
    if not s["tags"]:
        body += "  (тегов нет)\n"
    for tag, active, total in s["tags"]:
        body += f"  <code>tag={escape(tag)}</code> — {active:,} / {total:,}\n"
    body += (
        "\n<b>Использование:</b>\n"
        "<code>/send tag=voronka_ng_1224</code>\n"
        "<code>/send bot=tech_june25_bot</code>\n"
        "<code>/send</code> — без фильтра (все активные основного бота)"
    )
    bot.send_message(message.chat.id, body)


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not is_authorized(message):
        return
    s = audience.stats()
    by_status = s["by_status"]
    statuses = [
        ("active",      "✅ active     "),
        ("blocked",     "🚫 blocked    "),
        ("deleted",     "💀 deleted    "),
        ("not_started", "⚠️  not_started"),
        ("unknown",     "❓ unknown    "),
        ("invalid",     "⛔ invalid    "),
    ]
    total_main = sum(by_status.values())
    body = (
        f"<b>Сводка</b>\n\n"
        f"Всего в БД: <b>{s['total']:,}</b>\n"
        f"Из них Telegram: <b>{s['tg_total']:,}</b>\n\n"
        f"Основной бот: <code>{s['default_bot']}</code> — <b>{total_main:,}</b>\n"
    )
    for k, label in statuses:
        body += f"  {label} {by_status.get(k, 0):>6,}\n"
    body += "\n<b>Top ботов в проекте:</b>\n"
    for bot_name, n in s["top_bots"][:5]:
        body += f"  <code>{bot_name}</code> — {n:,}\n"
    if s["top_tags"]:
        body += "\n<b>Top тегов в основном боте:</b>\n"
        for tag, n in s["top_tags"][:5]:
            body += f"  <code>{escape(tag)}</code> — {n:,}\n"
    bot.send_message(message.chat.id, body)


@bot.message_handler(commands=["preview"])
def cmd_preview(message):
    if not is_authorized(message):
        return
    if not message.reply_to_message:
        bot.reply_to(message, "⚠️ Сделай реплай командой /preview на сообщение с текстом или фото с подписью.")
        return

    text, photo_file_id, buttons = build_post_from_reply(message)
    if not text and not photo_file_id:
        bot.reply_to(message, "⚠️ В реплае нет ни текста, ни фото.")
        return

    # Для превью подставляем имя автора (чтобы было видно как с {{name}} получится у клиента)
    preview_name = message.from_user.first_name or message.from_user.username or "друг"
    text = broadcast.personalize(text, preview_name)

    # Если есть фото — перезалить через бот рассылок (попутно отправить превью)
    broadcast_file_id = None
    if photo_file_id:
        try:
            # Первый юзер: reupload + готовое превью одним sendPhoto
            broadcast_file_id = post_builder.reupload_photo(
                photo_file_id, TEST_USER_IDS[0],
                caption=text, buttons=buttons,
            )
            # Остальные тестеры — тем же broadcast_file_id, но обычным sendPhoto
            for uid in TEST_USER_IDS[1:]:
                post_builder.send_via_broadcast_bot(uid, text,
                                                  photo_file_id=broadcast_file_id, buttons=buttons)
        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось отправить превью: {e}")
            return
    else:
        for uid in TEST_USER_IDS:
            try:
                post_builder.send_via_broadcast_bot(uid, text, buttons=buttons)
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка отправки на {uid}: {e}")
                return

    # Сохраним подготовленные данные в кэше (по message_id reply'я), чтобы /send переиспользовал
    with PENDING_LOCK:
        PREVIEW_CACHE[message.reply_to_message.message_id] = {
            "text": text,
            "photo_file_id": broadcast_file_id,
            "buttons": buttons,
        }

    btn_summary = ""
    if buttons:
        flat = [b[0] for row in buttons for b in row]
        btn_summary = f"\nКнопок: {len(flat)} → {flat}"
    photo_summary = "\n📸 С фото" if broadcast_file_id else ""

    bot.reply_to(
        message,
        f"📨 Превью отправлено через @{os.environ['TELEGRAM_BROADCAST_BOT_USERNAME']} "
        f"на {len(TEST_USER_IDS)} тестовых.{photo_summary}{btn_summary}\n\n"
        f"Если ОК — реплай тем же сообщением /send для рассылки всем."
    )


# Хранилище подтверждений (in-memory, на время работы процесса)
PENDING: dict[int, dict] = {}
PREVIEW_CACHE: dict[int, dict] = {}  # reply_message_id → {text, photo_file_id, buttons}
PENDING_LOCK = threading.Lock()


@bot.message_handler(commands=["send"])
def cmd_send(message):
    if not is_authorized(message):
        return
    if broadcast.is_running():
        info = broadcast.lock_info()
        bot.reply_to(message, f"⛔ Уже идёт рассылка: <code>{escape(str(info))}</code>")
        return
    if not message.reply_to_message:
        bot.reply_to(message, "⚠️ Сделай реплай командой /send на сообщение с текстом или фото.")
        return

    reply_id = message.reply_to_message.message_id
    # Если /preview уже сделан — берём готовые данные из кэша (с уже перезалитым file_id)
    cached = PREVIEW_CACHE.get(reply_id)
    if cached:
        text = cached["text"]
        photo_file_id = cached["photo_file_id"]
        buttons = cached["buttons"]
    else:
        text, raw_photo_id, buttons = build_post_from_reply(message)
        if not text and not raw_photo_id:
            bot.reply_to(message, "⚠️ В реплае нет ни текста, ни фото.")
            return
        photo_file_id = None
        if raw_photo_id:
            try:
                # При /send без предварительного /preview — техническая перезаливка
                # без подписи (просто чтобы получить file_id для бота рассылок)
                photo_file_id = post_builder.reupload_photo(raw_photo_id, TEST_USER_IDS[0])
            except Exception as e:
                bot.reply_to(message, f"❌ Не удалось перезалить фото: {e}")
                return

    if photo_file_id and len(text) > 1024:
        bot.reply_to(message,
            f"⚠️ Подпись к фото слишком длинная: {len(text)} символов (лимит Telegram — 1024).\n"
            f"Сократи текст или удали фото.")
        return

    # Парсим фильтры из аргументов команды: /send tag=X bot=Y
    filters = audience.parse_filters(parse_command_args(message))
    total, targets = audience.select(**filters)
    if not targets:
        descr = f"фильтр {filters}" if filters else "основного бота"
        bot.reply_to(message, f"⚠️ Нет активных получателей под {descr}. /segments покажет доступные.")
        return

    audience_parts = [f"bot={filters.get('bot_group', audience.DEFAULT_BOT_GROUP)}"]
    if filters.get("tag"):
        audience_parts.append(f"tag={filters['tag']}")
    audience_parts.append("status=active")
    if photo_file_id:
        audience_parts.append("with_photo")
    if buttons:
        audience_parts.append(f"buttons={sum(len(r) for r in buttons)}")

    draft_id = broadcast.create_draft(
        text=text, parse_mode="HTML",
        audience_descr=", ".join(audience_parts),
        total=total,
        started_by=message.from_user.username or str(message.from_user.id),
    )
    with PENDING_LOCK:
        PENDING[draft_id] = {
            "text": text, "targets": targets, "chat_id": message.chat.id,
            "username": message.from_user.username,
            "photo_file_id": photo_file_id, "buttons": buttons,
        }

    preview = text[:200] + ("..." if len(text) > 200 else "")
    extras = []
    if photo_file_id:
        extras.append("📸 фото")
    if buttons:
        n_btn = sum(len(r) for r in buttons)
        extras.append(f"🔘 {n_btn} кнопк{'а' if n_btn==1 else 'и'}")
    extras_str = " + ".join(extras) if extras else "просто текст"

    audience_label = filters.get("bot_group", audience.DEFAULT_BOT_GROUP)
    if filters.get("tag"):
        audience_label += f", tag={filters['tag']}"

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(f"✅ Отправить {total:,}", callback_data=f"confirm:{draft_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{draft_id}"),
    )
    bot.send_message(
        message.chat.id,
        f"<b>Подтверди рассылку #{draft_id}</b>\n\n"
        f"Получателей: <b>{total:,}</b> (<code>{escape(audience_label)}</code>, active)\n"
        f"Формат: {extras_str}\n"
        f"Скорость: ~25/сек, ожидание: ~{max(1, total // 25 // 60)} мин\n\n"
        f"<b>Текст:</b>\n{preview}",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda cb: cb.data.startswith("cancel:"))
def on_cancel(cb):
    draft_id = int(cb.data.split(":")[1])
    with PENDING_LOCK:
        PENDING.pop(draft_id, None)
    with db.connect() as conn:
        conn.execute("UPDATE broadcasts SET status='cancelled' WHERE id=?", (draft_id,))
        conn.commit()
    bot.edit_message_text(f"❌ Рассылка #{draft_id} отменена.",
                         cb.message.chat.id, cb.message.message_id)
    bot.answer_callback_query(cb.id)


@bot.callback_query_handler(func=lambda cb: cb.data.startswith("confirm:"))
def on_confirm(cb):
    draft_id = int(cb.data.split(":")[1])
    with PENDING_LOCK:
        draft = PENDING.pop(draft_id, None)
    if not draft:
        bot.answer_callback_query(cb.id, "Черновик уже не активен.")
        return

    chat_id = draft["chat_id"]
    targets = draft["targets"]
    text = draft["text"]
    photo_file_id = draft.get("photo_file_id")
    buttons = draft.get("buttons")

    bot.edit_message_text(
        f"🚀 Рассылка #{draft_id} запущена ({len(targets):,} получателей)",
        cb.message.chat.id, cb.message.message_id,
    )
    bot.answer_callback_query(cb.id)

    progress_msg = bot.send_message(chat_id, f"📨 0 / {len(targets):,}")
    progress_state = {"last_text": ""}

    def on_progress(p: dict):
        new_text = (
            f"📨 <b>Рассылка #{draft_id}</b>\n"
            f"Отправлено: {p['sent']:,} / {p['total']:,}\n"
            f"Ошибок: {p['failed']:,} (заблокировали в процессе: {p['blocked']:,})"
        )
        if new_text == progress_state["last_text"]:
            return
        progress_state["last_text"] = new_text
        try:
            bot.edit_message_text(new_text, chat_id, progress_msg.message_id)
        except Exception:
            pass

    def run_in_thread():
        try:
            result = broadcast.run(
                broadcast_id=draft_id,
                targets=targets,
                text=text,
                parse_mode="HTML",
                photo_file_id=photo_file_id,
                buttons=buttons,
                on_progress=on_progress,
                progress_every=300,
            )
            bot.send_message(
                chat_id,
                f"✅ <b>Рассылка #{draft_id} завершена</b>\n"
                f"Отправлено: <b>{result['sent']:,}</b>\n"
                f"Ошибок: {result['failed']:,}\n"
                f"Заблокировали в процессе: {result['blocked']:,}\n"
                f"Разбивка ошибок: <code>{escape(str(result['by_error']))}</code>"
            )
        except Exception as e:
            log.exception("broadcast %s failed", draft_id)
            bot.send_message(chat_id, f"❌ Рассылка #{draft_id} упала: {type(e).__name__}: {e}")

    threading.Thread(target=run_in_thread, daemon=True).start()


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not is_authorized(message):
        return
    if not broadcast.is_running():
        bot.reply_to(message, "Активной рассылки нет. Используй /history чтобы посмотреть последние.")
        return
    info = broadcast.lock_info() or {}
    bid = info.get("broadcast_id")
    started_iso = info.get("started_at", "")

    if not bid:
        bot.reply_to(message, f"Lock есть, но broadcast не нашёл: <code>{escape(str(info))}</code>")
        return

    with db.connect() as conn:
        row = conn.execute(
            "SELECT total, sent, failed, blocked, started_at FROM broadcasts WHERE id=?",
            (bid,),
        ).fetchone()
    if not row:
        bot.reply_to(message, f"Broadcast #{bid} не найден в БД.")
        return

    total, sent, failed, blocked = row["total"], row["sent"], row["failed"], row["blocked"]
    done = sent + failed
    pct = (100 * done / total) if total else 0

    import datetime as dt
    elapsed_str = "?"
    eta_str = "?"
    speed_str = "?"
    try:
        started = dt.datetime.fromisoformat(row["started_at"])
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
        elapsed_str = f"{int(elapsed // 60)}м {int(elapsed % 60)}с"
        if done > 0 and elapsed > 0:
            speed = done / elapsed
            speed_str = f"{speed:.1f}/сек"
            remain = total - done
            eta_sec = remain / speed if speed else 0
            eta_str = f"{int(eta_sec // 60)}м {int(eta_sec % 60)}с"
    except Exception:
        pass

    bot.reply_to(
        message,
        f"📨 <b>Рассылка #{bid}</b>\n\n"
        f"Прогресс: <b>{done:,} / {total:,}</b> ({pct:.1f}%)\n"
        f"  ✅ доставлено: {sent:,}\n"
        f"  ❌ ошибок: {failed:,} (включая заблокировавших: {blocked:,})\n\n"
        f"Прошло: {elapsed_str}\n"
        f"Скорость: {speed_str}\n"
        f"Осталось: ~{eta_str}",
    )


@bot.message_handler(commands=["history"])
def cmd_history(message):
    if not is_authorized(message):
        return
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, status, total, sent, failed, blocked,
                      started_at, finished_at, audience, started_by
               FROM broadcasts ORDER BY id DESC LIMIT 10"""
        ).fetchall()
    if not rows:
        bot.reply_to(message, "Истории рассылок пока нет.")
        return

    body = "<b>История рассылок (последние 10)</b>\n\n"
    icons = {"completed": "✅", "running": "🔄", "failed": "❌",
             "cancelled": "🚫", "draft": "📝"}
    for r in rows:
        icon = icons.get(r["status"], "❓")
        when = (r["started_at"] or "")[:19].replace("T", " ")
        body += (
            f"{icon} <b>#{r['id']}</b>  <code>{r['status']}</code>  {when}\n"
            f"   {r['sent']:,} ✅ / {r['failed']:,} ❌ из {r['total']:,}\n"
            f"   <code>{escape(r['audience'] or '')[:60]}</code>\n"
            f"   запустил: @{escape(r['started_by'] or '?')}\n\n"
        )
    body += "Деталь: <code>/broadcast &lt;id&gt;</code>"
    bot.send_message(message.chat.id, body)


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast_details(message):
    if not is_authorized(message):
        return
    args = parse_command_args(message).strip()
    if not args.isdigit():
        bot.reply_to(message, "Использование: <code>/broadcast 12</code>")
        return
    bid = int(args)
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM broadcasts WHERE id=?", (bid,)).fetchone()
    if not r:
        bot.reply_to(message, f"Рассылки #{bid} нет.")
        return

    text_preview = (r["text"] or "")[:300]
    if len(r["text"] or "") > 300:
        text_preview += "..."

    body = (
        f"<b>Рассылка #{r['id']}</b>\n\n"
        f"Статус: <code>{r['status']}</code>\n"
        f"Аудитория: <code>{escape(r['audience'] or '')}</code>\n"
        f"Всего: {r['total']:,}\n"
        f"  ✅ доставлено: {r['sent']:,}\n"
        f"  ❌ ошибок: {r['failed']:,}\n"
        f"  🚫 заблокировали: {r['blocked']:,}\n\n"
        f"Запустил: @{escape(r['started_by'] or '?')}\n"
        f"Старт: {(r['started_at'] or '')[:19].replace('T', ' ')}\n"
        f"Финиш: {(r['finished_at'] or '—')[:19].replace('T', ' ')}\n\n"
        f"<b>Текст:</b>\n<pre>{escape(text_preview)}</pre>"
    )
    bot.send_message(message.chat.id, body)


# ============================================================
# WIZARD: пошаговое создание рассылки
# ============================================================

@bot.message_handler(commands=["new"])
def cmd_new(message):
    if not is_authorized(message):
        return
    wizard.start(message.from_user.id, message.chat.id)
    bot.send_message(
        message.chat.id,
        "📝 <b>Создание рассылки — шаг 1/4</b>\n\n"
        "Пришли сообщение, которое будем рассылать:\n"
        "  • просто текст\n"
        "  • или фото с подписью\n"
        "  • можно добавить кнопки строками\n"
        "    <code>[Текст | https://url]</code> в конце\n\n"
        "Прервать — /cancel"
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if not is_authorized(message):
        return
    if wizard.cancel(message.from_user.id):
        bot.reply_to(message, "🚫 Создание рассылки отменено.")
    else:
        bot.reply_to(message, "Активного визарда нет.")


@bot.message_handler(
    func=lambda m: (m.from_user and wizard.get(m.from_user.id)
                    and wizard.get(m.from_user.id).step == "awaiting_message"
                    and not (m.text and m.text.startswith("/"))),
    content_types=["text", "photo"],
)
def on_wizard_content(message):
    state = wizard.get(message.from_user.id)
    if state is None:
        return
    text, photo_file_id, buttons = build_post_from_message(message)
    if not text and not photo_file_id:
        bot.reply_to(message, "В сообщении нет ни текста, ни фото. Попробуй ещё раз.")
        return
    state.text = text
    state.photo_command_file_id = photo_file_id
    state.buttons = buttons

    # Для превью подставляем имя автора (если есть плейсхолдеры {{name}}/{{first_name}})
    preview_name = message.from_user.first_name or message.from_user.username or "друг"
    preview_text = broadcast.personalize(text, preview_name)

    if photo_file_id:
        if len(text) > 1024:
            bot.reply_to(message,
                f"⚠️ Подпись к фото слишком длинная: {len(text)} символов "
                "(лимит Telegram — 1024). Сократи или убери фото.")
            wizard.cancel(state.user_id)
            return
        try:
            state.photo_broadcast_file_id = post_builder.reupload_photo(
                photo_file_id, TEST_USER_IDS[0],
                caption=preview_text, buttons=buttons,
            )
        except Exception as e:
            bot.reply_to(message, f"❌ Не удалось обработать фото: {e}")
            wizard.cancel(state.user_id)
            return
    else:
        # Текстовое превью на тестовых
        for uid in TEST_USER_IDS:
            try:
                post_builder.send_via_broadcast_bot(uid, preview_text, buttons=buttons)
            except Exception as e:
                bot.reply_to(message, f"❌ Превью на {uid}: {e}")
                wizard.cancel(state.user_id)
                return

    state.step = "bot"
    show_wizard_bot_step(state)


def show_wizard_bot_step(state):
    s = audience.list_segments()
    kb = types.InlineKeyboardMarkup()
    for name, active, total in s["bots"]:
        if not name:
            continue
        # Активные — главные кандидаты, остальные показываем серыми с (0)
        label = f"{'⭐' if active else '·'} {name} ({active:,} active)"
        kb.row(types.InlineKeyboardButton(label, callback_data=f"wiz_bot:{name}"))
    kb.row(types.InlineKeyboardButton("❌ Отмена", callback_data="wiz_cancel"))
    bot.send_message(
        state.chat_id,
        "🤖 <b>Шаг 2/4 — выбери бота</b>\n\n"
        "Сейчас рассылка идёт через тот бот, чей токен прописан в <code>.env</code>.\n"
        "У звёздочек ⭐ есть проверенная активная аудитория.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda cb: cb.data.startswith("wiz_bot:"))
def on_wiz_bot(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        bot.answer_callback_query(cb.id, "Визард не активен")
        return
    state.selected_bot = cb.data.split(":", 1)[1]
    state.step = "segment"
    bot.answer_callback_query(cb.id, f"Бот: {state.selected_bot}")
    try:
        bot.delete_message(cb.message.chat.id, cb.message.message_id)
    except Exception:
        pass
    show_wizard_segment_step(state)


def render_segment_kb(state):
    """Собрать клавиатуру с тогглами тегов на основе текущего state.selected_tags."""
    s = audience.list_segments(top_n=20)
    kb = types.InlineKeyboardMarkup()
    selected = set(state.selected_tags)

    if state.selected_bot == s["default_bot"]:
        for tag, active, total in s["tags"]:
            if active == 0:
                continue
            mark = "☑" if tag in selected else "☐"
            label = f"{mark} #{tag} — {active:,}"[:60]
            kb.row(types.InlineKeyboardButton(label, callback_data=f"wiz_segtoggle:{tag}"))

    # Подсчёт текущей выборки с учётом выбранных тегов
    if selected:
        total_now, _ = audience.select(bot_group=state.selected_bot, tags=list(selected))
        done_label = f"✅ Готово — {total_now:,} получателей"
    else:
        total_now, _ = audience.select(bot_group=state.selected_bot)
        done_label = f"✅ Готово — все активные ({total_now:,})"

    kb.row(types.InlineKeyboardButton(done_label, callback_data="wiz_segdone"))
    if selected:
        kb.row(types.InlineKeyboardButton("⊘ Сбросить выбор", callback_data="wiz_segreset"))
    kb.row(types.InlineKeyboardButton("❌ Отмена", callback_data="wiz_cancel"))
    return kb


def show_wizard_segment_step(state):
    body = (
        f"🎯 <b>Шаг 3/4 — выбери сегмент</b>\n\n"
        f"Бот: <code>{state.selected_bot}</code>\n\n"
        "Тыкай теги, чтобы добавить/убрать (можно несколько).\n"
        "Если ничего не отметишь — пойдёт <b>всем активным</b>."
    )
    msg = bot.send_message(state.chat_id, body, reply_markup=render_segment_kb(state))
    state.segment_message_id = msg.message_id


@bot.callback_query_handler(func=lambda cb: cb.data.startswith("wiz_segtoggle:"))
def on_wiz_seg_toggle(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        bot.answer_callback_query(cb.id, "Визард не активен")
        return
    tag = cb.data.split(":", 1)[1]
    if tag in state.selected_tags:
        state.selected_tags.remove(tag)
        bot.answer_callback_query(cb.id, f"Убрал #{tag}")
    else:
        state.selected_tags.append(tag)
        bot.answer_callback_query(cb.id, f"Добавил #{tag}")
    try:
        bot.edit_message_reply_markup(
            chat_id=state.chat_id, message_id=state.segment_message_id,
            reply_markup=render_segment_kb(state),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_segreset")
def on_wiz_seg_reset(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        return
    state.selected_tags = []
    bot.answer_callback_query(cb.id, "Сброшено")
    try:
        bot.edit_message_reply_markup(
            chat_id=state.chat_id, message_id=state.segment_message_id,
            reply_markup=render_segment_kb(state),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_segdone")
def on_wiz_seg_done(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        bot.answer_callback_query(cb.id, "Визард не активен")
        return
    state.step = "time"
    bot.answer_callback_query(cb.id)
    try:
        bot.delete_message(state.chat_id, state.segment_message_id)
    except Exception:
        pass
    show_wizard_time_step(state)


def show_wizard_time_step(state):
    filters = {"bot_group": state.selected_bot}
    if state.selected_tags:
        filters["tags"] = state.selected_tags
    total, _ = audience.select(**filters)

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🚀 Сейчас", callback_data="wiz_time:now"),
        types.InlineKeyboardButton("⏰ Запланировать", callback_data="wiz_time:later"),
    )
    kb.row(types.InlineKeyboardButton("❌ Отмена", callback_data="wiz_cancel"))
    bot.send_message(
        state.chat_id,
        f"⏰ <b>Шаг 4/4 — когда отправить?</b>\n\n"
        f"Получателей: <b>{total:,}</b>\n"
        f"Скорость: ~25/сек, ожидание: ~{max(1, total // 25 // 60)} мин",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_time:now")
def on_wiz_time_now(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        bot.answer_callback_query(cb.id, "Визард не активен")
        return
    state.scheduled_at = None
    bot.answer_callback_query(cb.id, "Отправим сейчас")
    try:
        bot.delete_message(cb.message.chat.id, cb.message.message_id)
    except Exception:
        pass
    show_wizard_options_step(state)


SPREAD_PRESETS = [
    (0,    "🚀 Сразу (макс. скорость)"),
    (30,   "🐢 30 минут"),
    (60,   "🐢 1 час"),
    (120,  "🐢 2 часа"),
    (240,  "🐢 4 часа"),
]


def render_options_kb(state):
    kb = types.InlineKeyboardMarkup()
    utm_mark = "☑" if state.add_utm else "☐"
    kb.row(types.InlineKeyboardButton(
        f"{utm_mark} 🔗 Добавлять UTM-метки в кнопки",
        callback_data="wiz_opt_utm",
    ))
    for minutes, label in SPREAD_PRESETS:
        mark = "•" if state.spread_minutes == minutes else "○"
        kb.row(types.InlineKeyboardButton(
            f"{mark} {label}",
            callback_data=f"wiz_opt_spread:{minutes}",
        ))
    kb.row(types.InlineKeyboardButton("✅ Готово", callback_data="wiz_opt_done"))
    kb.row(types.InlineKeyboardButton("❌ Отмена", callback_data="wiz_cancel"))
    return kb


def show_wizard_options_step(state):
    msg = bot.send_message(
        state.chat_id,
        "🎛️ <b>Параметры рассылки</b>\n\n"
        "<b>UTM-метки</b> — автоматически добавляют в URL кнопок параметры "
        "<code>utm_source/utm_campaign</code>. Видно в Я.Метрике/GA, "
        "сколько людей перешло по конкретной рассылке.\n\n"
        "<b>Растяжка во времени</b> — если включено, рассылка идёт медленнее, "
        "распределяется по времени. Менее «бот-подозрительно», "
        "лучше антиспам-сигнал у Telegram.",
        reply_markup=render_options_kb(state),
    )
    state.options_message_id = msg.message_id


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_opt_utm")
def on_wiz_opt_utm(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        return
    state.add_utm = not state.add_utm
    bot.answer_callback_query(cb.id, "UTM: вкл" if state.add_utm else "UTM: выкл")
    try:
        bot.edit_message_reply_markup(
            chat_id=state.chat_id, message_id=state.options_message_id,
            reply_markup=render_options_kb(state),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda cb: cb.data.startswith("wiz_opt_spread:"))
def on_wiz_opt_spread(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        return
    minutes = int(cb.data.split(":", 1)[1])
    state.spread_minutes = minutes
    label = next((l for m, l in SPREAD_PRESETS if m == minutes), str(minutes))
    bot.answer_callback_query(cb.id, label)
    try:
        bot.edit_message_reply_markup(
            chat_id=state.chat_id, message_id=state.options_message_id,
            reply_markup=render_options_kb(state),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_opt_done")
def on_wiz_opt_done(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        return
    bot.answer_callback_query(cb.id)
    try:
        bot.delete_message(state.chat_id, state.options_message_id)
    except Exception:
        pass
    show_wizard_confirm(state, cb.from_user.username or str(cb.from_user.id))


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_time:later")
def on_wiz_time_later(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        bot.answer_callback_query(cb.id, "Визард не активен")
        return
    state.step = "awaiting_time_input"
    bot.answer_callback_query(cb.id)
    try:
        bot.delete_message(cb.message.chat.id, cb.message.message_id)
    except Exception:
        pass
    bot.send_message(
        state.chat_id,
        "⏰ <b>Введи дату и время отправки</b>\n\n"
        "Форматы:\n"
        "  <code>14:00</code> — сегодня (или завтра, если уже прошло)\n"
        "  <code>08.05 14:00</code> — 8 мая в 14:00\n"
        "  <code>08.05.2026 14:00</code>\n\n"
        f"Часовой пояс: <code>{wizard.TZ.key}</code>\n"
        "Прервать — /cancel"
    )


@bot.message_handler(
    func=lambda m: (m.from_user and wizard.get(m.from_user.id)
                    and wizard.get(m.from_user.id).step == "awaiting_time_input"
                    and not (m.text and m.text.startswith("/"))),
    content_types=["text"],
)
def on_wizard_time_input(message):
    state = wizard.get(message.from_user.id)
    if not state:
        return
    try:
        when = wizard.parse_when(message.text)
    except ValueError as e:
        bot.reply_to(message, str(e))
        return
    state.scheduled_at = when
    show_wizard_options_step(state)


def show_wizard_confirm(state, started_by_username: str):
    state.step = "confirm"
    state.started_by = started_by_username

    filters = {"bot_group": state.selected_bot}
    if state.selected_tags:
        filters["tags"] = state.selected_tags
    total, _ = audience.select(**filters)

    when_str = "🚀 Сейчас" if state.scheduled_at is None \
        else f"⏰ {wizard.format_when(state.scheduled_at)} ({wizard.TZ.key})"

    extras = []
    if state.photo_broadcast_file_id:
        extras.append("📸 фото")
    if state.buttons:
        extras.append(f"🔘 {sum(len(r) for r in state.buttons)} кн.")
    placeholders_used = []
    if "{{name}}" in state.text:
        placeholders_used.append("{{name}}")
    if "{{first_name}}" in state.text:
        placeholders_used.append("{{first_name}}")
    if placeholders_used:
        extras.append(f"👤 персонализация: {', '.join(placeholders_used)}")
    if state.add_utm:
        extras.append("🔗 UTM-метки")
    if state.spread_minutes:
        extras.append(f"🐢 растяжка {state.spread_minutes} мин")
    extras_str = " + ".join(extras) if extras else "только текст"

    preview = state.text[:300] + ("..." if len(state.text) > 300 else "")

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data="wiz_confirm"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="wiz_cancel"),
    )
    if state.selected_tags:
        segment_label = ", ".join(f"#{t}" for t in state.selected_tags)
    else:
        segment_label = "все активные"

    bot.send_message(
        state.chat_id,
        f"📋 <b>Сводка рассылки</b>\n\n"
        f"Бот: <code>{state.selected_bot}</code>\n"
        f"Сегмент: <code>{escape(segment_label)}</code>\n"
        f"Получателей: <b>{total:,}</b>\n"
        f"Когда: <b>{when_str}</b>\n"
        f"Формат: {extras_str}\n\n"
        f"<b>Текст:</b>\n{preview}\n\n"
        f"Превью уже отправлено в чат с @{os.environ['TELEGRAM_BROADCAST_BOT_USERNAME']}.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_cancel")
def on_wiz_cancel(cb):
    wizard.cancel(cb.from_user.id)
    bot.answer_callback_query(cb.id, "Отменено")
    try:
        bot.edit_message_text("🚫 Создание рассылки отменено.",
                             cb.message.chat.id, cb.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda cb: cb.data == "wiz_confirm")
def on_wiz_confirm(cb):
    state = wizard.get(cb.from_user.id)
    if not state:
        bot.answer_callback_query(cb.id, "Визард уже не активен")
        return

    # Проверка размера: для больших рассылок просим текстовое подтверждение
    filters = {"bot_group": state.selected_bot}
    if state.selected_tags:
        filters["tags"] = state.selected_tags
    total, _ = audience.select(**filters)

    if total > wizard.LARGE_BROADCAST_THRESHOLD:
        state.step = "awaiting_double_confirm"
        bot.answer_callback_query(cb.id)
        try:
            bot.edit_message_reply_markup(
                chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
        bot.send_message(
            state.chat_id,
            f"⚠️ <b>Большая рассылка: {total:,} получателей</b>\n\n"
            "Чтобы запустить — введи в чате слово <code>ОТПРАВИТЬ</code> (заглавными).\n"
            "Любой другой текст или /cancel — рассылка не пойдёт."
        )
        return

    _launch_broadcast_from_wizard(state, cb.from_user)
    bot.answer_callback_query(cb.id)
    try:
        bot.edit_message_reply_markup(
            chat_id=cb.message.chat.id, message_id=cb.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass


@bot.message_handler(
    func=lambda m: (m.from_user and wizard.get(m.from_user.id)
                    and wizard.get(m.from_user.id).step == "awaiting_double_confirm"),
    content_types=["text"],
)
def on_double_confirm(message):
    state = wizard.get(message.from_user.id)
    if not state:
        return
    if message.text.strip() == "ОТПРАВИТЬ":
        _launch_broadcast_from_wizard(state, message.from_user)
    else:
        bot.reply_to(message, "🚫 Не подтверждено. Рассылка отменена.")
        wizard.cancel(state.user_id)


def _launch_broadcast_from_wizard(state, user):
    """Сохранить в БД и запустить (или поставить в очередь)."""
    started_by = user.username or str(user.id)
    bid = wizard.save_to_db(state, started_by)
    is_scheduled = state.scheduled_at is not None
    chat_id = state.chat_id

    bot.send_message(
        chat_id,
        f"{'📅' if is_scheduled else '🚀'} Рассылка #{bid} "
        f"{'запланирована на ' + wizard.format_when(state.scheduled_at) if is_scheduled else 'запущена'}",
    )

    wizard.cancel(state.user_id)

    if is_scheduled:
        return

    # Запускаем сейчас в отдельном потоке
    filters = {"bot_group": state.selected_bot}
    if state.selected_tags:
        filters["tags"] = state.selected_tags
    total, targets = audience.select(**filters)

    progress_msg = bot.send_message(chat_id, f"📨 0 / {total:,}")
    progress_state = {"last": ""}

    def on_progress(p: dict):
        new = (
            f"📨 <b>Рассылка #{bid}</b>\n"
            f"Отправлено: {p['sent']:,} / {p['total']:,}\n"
            f"Ошибок: {p['failed']:,} (заблокировали: {p['blocked']:,})"
        )
        if new == progress_state["last"]:
            return
        progress_state["last"] = new
        try:
            bot.edit_message_text(new, chat_id, progress_msg.message_id)
        except Exception:
            pass

    def run_in_thread():
        try:
            result = broadcast.run(
                broadcast_id=bid,
                targets=targets,
                text=state.text,
                parse_mode="HTML",
                photo_file_id=state.photo_broadcast_file_id,
                buttons=state.buttons,
                add_utm=state.add_utm,
                spread_minutes=state.spread_minutes,
                on_progress=on_progress,
                progress_every=300,
            )
            bot.send_message(
                chat_id,
                f"✅ <b>Рассылка #{bid} завершена</b>\n"
                f"Отправлено: <b>{result['sent']:,}</b>\n"
                f"Ошибок: {result['failed']:,}\n"
                f"Заблокировали в процессе: {result['blocked']:,}\n"
                f"Разбивка ошибок: <code>{escape(str(result['by_error']))}</code>"
            )
        except Exception as e:
            log.exception("broadcast %s failed", bid)
            bot.send_message(chat_id, f"❌ Рассылка #{bid} упала: {type(e).__name__}: {e}")

    threading.Thread(target=run_in_thread, daemon=True).start()


# ============================================================
# Управление запланированными
# ============================================================

@bot.message_handler(commands=["scheduled"])
def cmd_scheduled(message):
    if not is_authorized(message):
        return
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, scheduled_at, total, audience, started_by
               FROM broadcasts WHERE status='scheduled'
               ORDER BY scheduled_at"""
        ).fetchall()
    if not rows:
        bot.reply_to(message, "Нет запланированных рассылок.")
        return
    body = "<b>📅 Запланированные рассылки</b>\n\n"
    import datetime as dt
    for r in rows:
        when = "?"
        try:
            when = wizard.format_when(dt.datetime.fromisoformat(r["scheduled_at"]))
        except Exception:
            pass
        body += (
            f"#{r['id']} — <b>{when}</b> ({wizard.TZ.key})\n"
            f"  получателей: {r['total']:,}\n"
            f"  <code>{escape(r['audience'] or '')[:60]}</code>\n"
            f"  запустил: @{escape(r['started_by'] or '?')}\n"
            f"  отменить: <code>/cancel_scheduled {r['id']}</code>\n\n"
        )
    bot.send_message(message.chat.id, body)


@bot.message_handler(commands=["cancel_scheduled"])
def cmd_cancel_scheduled(message):
    if not is_authorized(message):
        return
    args = parse_command_args(message).strip()
    if not args.isdigit():
        bot.reply_to(message, "Использование: <code>/cancel_scheduled 12</code>")
        return
    bid = int(args)
    with db.connect() as conn:
        r = conn.execute("SELECT status FROM broadcasts WHERE id=?", (bid,)).fetchone()
        if not r:
            bot.reply_to(message, f"Рассылки #{bid} нет.")
            return
        if r["status"] != "scheduled":
            bot.reply_to(message, f"Рассылка #{bid} в статусе <code>{r['status']}</code>, нельзя отменить.")
            return
        conn.execute("UPDATE broadcasts SET status='cancelled' WHERE id=?", (bid,))
        conn.commit()
    bot.reply_to(message, f"🚫 Рассылка #{bid} отменена.")


@bot.message_handler(commands=["cancel_lock"])
def cmd_cancel_lock(message):
    if not is_authorized(message):
        return
    if not broadcast.is_running():
        bot.reply_to(message, "Lock-файла нет.")
        return
    broadcast._release_lock()
    bot.reply_to(message, "🔓 Lock-файл снят.")


BOT_COMMANDS = [
    types.BotCommand("new",              "🆕 Новая рассылка (пошаговый визард)"),
    types.BotCommand("scheduled",        "Запланированные рассылки"),
    types.BotCommand("cancel_scheduled", "Отменить запланированную: /cancel_scheduled <id>"),
    types.BotCommand("cancel",           "Прервать создание рассылки в визарде"),
    types.BotCommand("stats",            "Сводка по базе"),
    types.BotCommand("segments",         "Доступные сегменты (теги, боты)"),
    types.BotCommand("preview",          "Превью (реплаем) — быстрая отправка"),
    types.BotCommand("send",             "Рассылка (реплаем) — быстрая отправка"),
    types.BotCommand("status",           "Прогресс активной рассылки"),
    types.BotCommand("history",          "История последних 10 рассылок"),
    types.BotCommand("broadcast",        "Детали по рассылке: /broadcast <id>"),
    types.BotCommand("cancel_lock",      "Снять lock-файл (если процесс упал)"),
    types.BotCommand("help",             "Справка"),
]


def register_commands():
    """Регистрация меню команд в Telegram-клиенте."""
    # В личке у админов
    bot.set_my_commands(BOT_COMMANDS, scope=types.BotCommandScopeAllPrivateChats())
    # В рабочем чате
    bot.set_my_commands(BOT_COMMANDS, scope=types.BotCommandScopeChat(chat_id=WORK_CHAT_ID))
    # Кнопка-меню рядом со скрепкой → открывает список команд
    bot.set_chat_menu_button(menu_button=types.MenuButtonCommands("commands"))


def scheduler_loop():
    """Раз в минуту проверяем broadcasts со статусом 'scheduled' и запускаем,
    если scheduled_at <= now()."""
    import time as _time
    import datetime as _dt
    while True:
        try:
            now_iso = _dt.datetime.now(_dt.UTC).isoformat()
            with db.connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM broadcasts WHERE status='scheduled' AND scheduled_at <= ?",
                    (now_iso,),
                ).fetchall()
            for r in rows:
                bid = r["id"]
                if broadcast.is_running():
                    log.info(f"Scheduler: рассылка #{bid} ждёт (другая идёт)")
                    break
                log.info(f"Scheduler: запускаю #{bid}")
                # помечаем как взятую в работу до запуска, чтобы не подобрали повторно
                with db.connect() as c:
                    c.execute("UPDATE broadcasts SET status='running' WHERE id=?", (bid,))
                    c.commit()

                progress_msg = bot.send_message(
                    WORK_CHAT_ID,
                    f"🚀 Авто-запуск запланированной #{bid}...\n📨 0",
                )

                def make_progress_handler(mid):
                    last = {"text": ""}
                    def on_progress(p: dict):
                        new = (
                            f"📨 <b>Рассылка #{p['broadcast_id']}</b>\n"
                            f"Отправлено: {p['sent']:,} / {p['total']:,}\n"
                            f"Ошибок: {p['failed']:,} (заблок.: {p['blocked']:,})"
                        )
                        if new == last["text"]:
                            return
                        last["text"] = new
                        try:
                            bot.edit_message_text(new, WORK_CHAT_ID, mid)
                        except Exception:
                            pass
                    return on_progress

                def run_in_thread(broadcast_id, progress_message_id):
                    try:
                        result = wizard.run_scheduled(
                            broadcast_id, on_progress=make_progress_handler(progress_message_id)
                        )
                        bot.send_message(
                            WORK_CHAT_ID,
                            f"✅ <b>Запланированная #{broadcast_id} завершена</b>\n"
                            f"Отправлено: <b>{result['sent']:,}</b>\n"
                            f"Ошибок: {result['failed']:,}\n"
                            f"Заблокировали: {result['blocked']:,}\n"
                            f"Разбивка: <code>{escape(str(result['by_error']))}</code>"
                        )
                    except Exception as e:
                        log.exception("scheduled %s failed", broadcast_id)
                        bot.send_message(WORK_CHAT_ID,
                            f"❌ Запланированная #{broadcast_id} упала: {type(e).__name__}: {e}")

                threading.Thread(
                    target=run_in_thread,
                    args=(bid, progress_msg.message_id),
                    daemon=True,
                ).start()
        except Exception as e:
            log.exception("scheduler tick failed: %s", e)
        _time.sleep(60)


def main():
    db.init_schema()  # вынес сюда, потому что миграцию (новые колонки) надо прогонять при старте
    me = bot.get_me()
    log.info(f"Бот команд запущен: @{me.username} (id={me.id})")
    try:
        register_commands()
        log.info(f"Зарегистрировано {len(BOT_COMMANDS)} команд в меню")
    except Exception as e:
        log.warning(f"Не удалось зарегистрировать команды: {e}")

    # фоновый scheduler для отложенных рассылок
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    log.info("Scheduler фоновый поток запущен")

    bot.infinity_polling(timeout=20, long_polling_timeout=20, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
