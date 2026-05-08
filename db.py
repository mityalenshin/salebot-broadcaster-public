"""
Локальная SQLite-база. Схема и подключение.
"""
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / os.environ.get("DATABASE_PATH", "./data/salebot_local.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY,        -- SaleBot client.id
    platform_id   TEXT,                        -- TG chat_id (как строка)
    client_type   INTEGER,                     -- 1=TG bot, 16=Business, etc.
    name          TEXT,
    avatar        TEXT,
    tag           TEXT,                        -- ключ подписки / источник
    bot_group     TEXT,                        -- имя бота (your_main_bot, ...)
    project_id    INTEGER,
    created_at    TEXT,
    updated_at    TEXT,
    raw_json      TEXT,                        -- полная карточка от API

    -- статус живости (заполняется отдельным скриптом)
    is_active     TEXT DEFAULT 'unknown',      -- unknown|active|blocked|deleted|not_started|invalid|error
    check_error   TEXT,                        -- описание ошибки от Telegram
    checked_at    TEXT                         -- когда проверяли в последний раз
);

CREATE INDEX IF NOT EXISTS idx_clients_group       ON clients(bot_group);
CREATE INDEX IF NOT EXISTS idx_clients_tag         ON clients(tag);
CREATE INDEX IF NOT EXISTS idx_clients_active      ON clients(is_active);
CREATE INDEX IF NOT EXISTS idx_clients_platform_id ON clients(platform_id);

CREATE TABLE IF NOT EXISTS migration_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    fetched     INTEGER,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    text            TEXT NOT NULL,
    photo_file_id   TEXT,
    buttons_json    TEXT,
    created_by      TEXT,
    created_at      TEXT,
    used_count      INTEGER DEFAULT 0,
    last_used_at    TEXT
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT NOT NULL,
    parse_mode      TEXT,
    audience        TEXT,                 -- человекочитаемое описание выборки
    total           INTEGER DEFAULT 0,
    sent            INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    blocked         INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'draft', -- draft|scheduled|running|completed|cancelled|failed
    started_at      TEXT,
    finished_at     TEXT,
    started_by      TEXT,
    -- поля для wizard и scheduling:
    scheduled_at    TEXT,                 -- ISO datetime, если рассылка отложенная
    audience_filters TEXT,                -- JSON {bot_group, tag, ...}
    photo_file_id   TEXT,                 -- file_id от бота рассылок
    buttons_json    TEXT                  -- JSON структуры кнопок
);
"""


def _migrate():
    """Добавить новые колонки в существующую БД (если их ещё нет)."""
    with connect() as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(broadcasts)").fetchall()}
        for col, sql in [
            ("scheduled_at",     "ALTER TABLE broadcasts ADD COLUMN scheduled_at TEXT"),
            ("audience_filters", "ALTER TABLE broadcasts ADD COLUMN audience_filters TEXT"),
            ("photo_file_id",    "ALTER TABLE broadcasts ADD COLUMN photo_file_id TEXT"),
            ("buttons_json",     "ALTER TABLE broadcasts ADD COLUMN buttons_json TEXT"),
            ("options_json",     "ALTER TABLE broadcasts ADD COLUMN options_json TEXT"),
        ]:
            if col not in existing:
                conn.execute(sql)
        conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_schema() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
    _migrate()


def upsert_client(conn: sqlite3.Connection, client: dict) -> None:
    conn.execute(
        """
        INSERT INTO clients (
            id, platform_id, client_type, name, avatar, tag,
            bot_group, project_id, created_at, updated_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            platform_id = excluded.platform_id,
            client_type = excluded.client_type,
            name        = excluded.name,
            avatar      = excluded.avatar,
            tag         = excluded.tag,
            bot_group   = excluded.bot_group,
            project_id  = excluded.project_id,
            updated_at  = excluded.updated_at,
            raw_json    = excluded.raw_json
        """,
        (
            client["id"],
            str(client.get("platform_id") or ""),
            client.get("client_type"),
            client.get("name"),
            client.get("avatar"),
            client.get("tag"),
            client.get("group"),
            client.get("project_id"),
            client.get("created_at"),
            client.get("updated_at"),
            json.dumps(client, ensure_ascii=False),
        ),
    )


def update_alive_status(conn: sqlite3.Connection, client_id: int,
                       status: str, error: str | None, checked_at: str) -> None:
    conn.execute(
        "UPDATE clients SET is_active=?, check_error=?, checked_at=? WHERE id=?",
        (status, error, checked_at, client_id),
    )
