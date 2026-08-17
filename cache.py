"""Cache so repeated lookups don't hammer ESI / zKillboard.

Uses a local SQLite file by default. If TURSO_DATABASE_URL is set (e.g. on
Render, where the local disk is wiped on every restart/deploy), it connects
to a remote Turso database instead, so the cache survives restarts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "cache.db"
TTL_SECONDS = 6 * 60 * 60  # 6 hours

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

if TURSO_URL:
    import libsql

_schema_ready = False


def _connect():
    global _schema_ready
    if TURSO_URL:
        conn = libsql.connect(TURSO_URL, auth_token=TURSO_AUTH_TOKEN)
    else:
        conn = sqlite3.connect(DB_PATH)
    if not _schema_ready:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS character_cache (
                character_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS type_name_cache (
                type_id INTEGER PRIMARY KEY,
                name TEXT,
                category_id INTEGER,
                group_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_category_cache (
                group_id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        _schema_ready = True
    return conn


def get(character_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT data, fetched_at FROM character_cache WHERE character_id = ?",
            (character_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    data_json, fetched_at = row
    if time.time() - fetched_at > TTL_SECONDS:
        return None
    return json.loads(data_json)


def set(character_id: int, name: str, data: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO character_cache (character_id, name, data, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                name = excluded.name,
                data = excluded.data,
                fetched_at = excluded.fetched_at
            """,
            (character_id, name, json.dumps(data), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_type_name(type_id: int) -> str | None:
    """Ship/item type names never change, so this cache has no TTL."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT name FROM type_name_cache WHERE type_id = ?", (type_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def set_type_name(type_id: int, name: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO type_name_cache (type_id, name) VALUES (?, ?)
            ON CONFLICT(type_id) DO UPDATE SET name = excluded.name
            """,
            (type_id, name),
        )
        conn.commit()
    finally:
        conn.close()


def get_type_category(type_id: int) -> int | None:
    """Item type -> category mapping never changes, so this cache has no TTL."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT category_id FROM type_name_cache WHERE type_id = ?", (type_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


def set_type_category(type_id: int, category_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO type_name_cache (type_id, category_id) VALUES (?, ?)
            ON CONFLICT(type_id) DO UPDATE SET category_id = excluded.category_id
            """,
            (type_id, category_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_type_group_name(type_id: int) -> str | None:
    """Item type -> group name never changes, so this cache has no TTL."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT group_name FROM type_name_cache WHERE type_id = ?", (type_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


def set_type_group_name(type_id: int, group_name: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO type_name_cache (type_id, group_name) VALUES (?, ?)
            ON CONFLICT(type_id) DO UPDATE SET group_name = excluded.group_name
            """,
            (type_id, group_name),
        )
        conn.commit()
    finally:
        conn.close()


def get_group_category(group_id: int) -> int | None:
    """Item group -> category mapping never changes, so this cache has no TTL."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT category_id FROM group_category_cache WHERE group_id = ?", (group_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def set_group_category(group_id: int, category_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO group_category_cache (group_id, category_id) VALUES (?, ?)",
            (group_id, category_id),
        )
        conn.commit()
    finally:
        conn.close()
