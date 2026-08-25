"""Database backend abstraction: SQLite locally, Postgres when hosted.

The app runs in two places and must behave identically in both:

  - on a laptop against `data/league.db` (no setup, works offline)
  - on Streamlit Cloud against Supabase Postgres (always on, shared across
    every device, and the long-term store for season-over-season analysis)

Rather than adopt an ORM and rewrite every query, this wraps the two drivers
behind one small interface. The SQL in the rest of the codebase is already
standard enough to run on both; the differences that remain are handled here:

  - placeholders: sqlite3 wants `?`, psycopg wants `%s`
  - row access: sqlite3.Row vs psycopg dict rows (both support row["col"])
  - DDL: AUTOINCREMENT/PRAGMA vs IDENTITY (see src/db.py schema variants)

Which backend is used is decided by DATABASE_URL. If it is unset, SQLite.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from collections.abc import Sequence

log = logging.getLogger(__name__)

POSTGRES_SCHEMES = ("postgres://", "postgresql://", "postgresql+psycopg://")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(POSTGRES_SCHEMES)


DB_URL_KEYS = ("DATABASE_URL", "SUPABASE_DB_URL", "POSTGRES_URL")


def database_url() -> str | None:
    """Connection string from the environment or Streamlit secrets.

    Both are checked because the two hosts differ: locally the value comes from
    `.env` into `os.environ`, while Streamlit Community Cloud supplies it
    through `st.secrets`. Reading only the environment would let a hosted
    deployment fall back to an empty local SQLite file and look like it had
    simply lost all its data - a silent failure worth ruling out.
    """
    for key in DB_URL_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return _clean(value)

    try:
        import streamlit as st

        for key in DB_URL_KEYS:
            if key in st.secrets:
                return _clean(str(st.secrets[key]))
    except Exception:
        # Not running under Streamlit, or no secrets file: fall through.
        pass
    return None


def _clean(url: str) -> str:
    """Strip whitespace and stray quotes from a pasted connection string.

    Secret-management UIs are textareas, and a paste routinely carries a
    trailing newline. Postgres then reads the database name as "postgres\\n"
    and refuses the connection with `database "postgres\\n" does not exist` -
    a failure that only appears in the deployment, never locally.
    """
    return url.strip().strip('"').strip("'").strip()


# --- placeholder translation -------------------------------------------------

# Matches a bare `?` placeholder, skipping any inside quoted strings.
_PLACEHOLDER = re.compile(r"\?(?=(?:[^']*'[^']*')*[^']*$)")


def to_postgres_sql(sql: str) -> str:
    """Rewrite `?` placeholders to `%s` for psycopg.

    Named `:param` placeholders are left alone: psycopg accepts `%(name)s`, so
    those queries are converted separately by `_named_to_pyformat`.
    """
    return _PLACEHOLDER.sub("%s", sql)


_NAMED = re.compile(r"(?<![:\w]):([a-zA-Z_]\w*)")


def _named_to_pyformat(sql: str) -> str:
    """`:season` -> `%(season)s`, leaving `::cast` syntax untouched."""
    return _NAMED.sub(r"%(\1)s", sql)


# --- cursor / connection wrappers -------------------------------------------

class Cursor:
    """Thin cursor wrapper so both drivers present the same surface."""

    def __init__(self, cursor: Any, dialect: str):
        self._cursor = cursor
        self._dialect = dialect

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        if self._dialect == "sqlite":
            return self._cursor.lastrowid
        # Postgres callers use RETURNING; nothing sensible to give here.
        return None


class Database:
    """One connection, either SQLite or Postgres."""

    def __init__(self, connection: Any, dialect: str, url: str | None = None):
        self._conn = connection
        self.dialect = dialect
        self.url = url

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgres"

    # -- statement execution -------------------------------------------------

    def execute(self, sql: str, params: Sequence | dict | None = None) -> Cursor:
        if self.dialect == "sqlite":
            cursor = self._conn.execute(sql, params or ())
            return Cursor(cursor, self.dialect)

        translated = (
            _named_to_pyformat(sql) if isinstance(params, dict) else to_postgres_sql(sql)
        )
        cursor = self._conn.cursor()
        cursor.execute(translated, params or ())
        return Cursor(cursor, self.dialect)

    def executescript(self, script: str) -> None:
        """Run a multi-statement DDL script."""
        if self.dialect == "sqlite":
            self._conn.executescript(script)
            self._conn.commit()
            return
        with self._conn.cursor() as cursor:
            cursor.execute(script)
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - closing twice is not an error
            pass

    # -- convenience ---------------------------------------------------------

    def fetchone(self, sql: str, params: Sequence | dict | None = None) -> Any:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence | dict | None = None) -> list[Any]:
        return self.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence | dict | None = None) -> Any:
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return list(row.values())[0] if isinstance(row, dict) else row[0]

    def table_exists(self, name: str) -> bool:
        if self.dialect == "sqlite":
            return bool(
                self.fetchone(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                )
            )
        return bool(
            self.fetchone(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=%s",
                (name,),
            )
        )


# --- connecting --------------------------------------------------------------

def connect_sqlite(path: str | Path, same_thread: bool = True) -> Database:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    return Database(conn, "sqlite", str(p))


def connect_postgres(url: str) -> Database:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "DATABASE_URL points at Postgres but psycopg is not installed. "
            "Run: pip install 'psycopg[binary]'"
        ) from exc

    # Whitespace first: a pasted secret often carries a trailing newline.
    url = _clean(url)
    # Supabase hands out postgres:// URLs; psycopg wants postgresql://.
    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]

    # Require TLS unless the caller has already said otherwise. libpq defaults
    # to sslmode=prefer, which is opportunistic: no certificate verification and
    # a silent fallback to plaintext if the server declines. For a connection
    # string that carries a database superuser password across the public
    # internet, that default is not acceptable. `require` encrypts without
    # pinning a CA; set sslmode=verify-full in DATABASE_URL once you have the
    # provider's root certificate installed, and this will respect it.
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"

    conn = psycopg.connect(
        dsn,
        row_factory=dict_row,
        autocommit=False,
        connect_timeout=20,
        # psycopg promotes a statement to a server-side PREPARE after a few
        # executions. Supabase's transaction pooler (port 6543) multiplexes
        # statements across backends, so a prepared statement can be issued on a
        # connection that never saw the PREPARE - which fails at runtime, and
        # only under load. Disabling the promotion makes the app safe on the
        # direct connection and on either pooler.
        prepare_threshold=None,
    )
    return Database(conn, "postgres", url)


def connect(
    db_path: str | Path | None = None,
    url: str | None = None,
    same_thread: bool = True,
    force_sqlite: bool = False,
) -> Database:
    """Open the configured database.

    Precedence: `force_sqlite`, then an explicit `url`, then DATABASE_URL from
    the environment, then the local SQLite file. This means the same code path
    serves a laptop with no configuration and a cloud deployment with one secret
    set.

    `force_sqlite` exists because naming a file is an unambiguous statement of
    intent. `fcc --db scratch.db` silently opening the hosted Postgres instead -
    because .env happened to define DATABASE_URL - is not a preference being
    overridden, it is the wrong database, and it took a test run writing fixture
    rows into the live league to notice.
    """
    if force_sqlite:
        return connect_sqlite(db_path or "data/league.db", same_thread=same_thread)
    target = url or database_url()
    if is_postgres_url(target):
        return connect_postgres(target)
    return connect_sqlite(db_path or "data/league.db", same_thread=same_thread)
