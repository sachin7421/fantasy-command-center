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
from collections.abc import Callable, Sequence

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

        found = _search_secrets(st.secrets)
        if found:
            return found
    except Exception as exc:
        # NOT silent. "No secrets file" is normal off Streamlit, but a MALFORMED
        # one - unquoted value, bad TOML - raises here too, and swallowing that
        # turns a typo into a silent fall back to an empty local database.
        log.info("Could not read Streamlit secrets: %s", exc)
    return None


def _search_secrets(secrets: Any, depth: int = 0) -> str | None:
    """Find a connection string anywhere in the secrets, not just at the top.

    Streamlit's secrets are TOML, and a section header puts everything after it
    inside that section - so

        [connections]
        DATABASE_URL = "postgresql://..."

    is NOT reachable as `st.secrets["DATABASE_URL"]`. The app then falls back to
    a local SQLite file and reports itself as having no data, with a perfectly
    correct connection string sitting in the secrets the whole time. Nesting is
    an easy thing to do by accident and an impossible thing to see, so the
    lookup descends one level rather than insisting on the top.
    """
    if depth > 2:
        return None
    try:
        keys = list(secrets.keys())
    except Exception:  # silent: not a mapping, so there is nothing to search
        return None
    present = set(keys)

    for key in DB_URL_KEYS:
        if key in present:
            candidate = _clean(str(secrets[key]))
            if candidate:
                return candidate

    for key in keys:
        try:
            value = secrets[key]
        except Exception:  # silent: one unreadable key must not hide the others
            continue
        if hasattr(value, "keys"):
            nested = _search_secrets(value, depth + 1)
            if nested:
                return nested
    return None


def secret_key_names() -> list[str]:
    """Top-level names present in Streamlit secrets. NEVER their values.

    For telling a human "the app can see APP_PASSWORD and DB_URL" when it is
    looking for DATABASE_URL - which is the whole diagnosis in one line.
    """
    try:
        import streamlit as st

        return sorted(str(k) for k in st.secrets)
    except Exception:  # silent: no streamlit, or no secrets file - both mean none
        return []


def diagnose_database_url() -> str:
    """One sentence naming why there is no Postgres connection.

    This exists because of how the diagnosis was actually consumed. The details
    were being printed as small grey captions UNDER a red error box, and the
    person reading it did the obvious thing - copied the red line - so three
    rounds of debugging happened without the decisive fact ever leaving their
    screen. A diagnostic that is not in the part people copy is not a
    diagnostic. This goes in the headline.

    Names only, never values: a connection string is a database superuser
    password and must not be rendered into a page or pasted into a chat.
    """
    for key in DB_URL_KEYS:
        raw = os.environ.get(key)
        if raw and raw.strip():
            cleaned = _clean(raw)
            if is_postgres_url(cleaned):
                return f"{key} is set in the environment and looks valid."
            return (
                f"{key} is set in the environment but is not a Postgres URL "
                f"({_shape(cleaned)}). It must start with postgresql://"
            )

    names = secret_key_names()
    if not names:
        return (
            "This app can read NO secrets at all. Either none are saved for "
            "this app, or the secrets are not valid TOML - every value needs "
            "to be quoted, like DATABASE_URL = \"postgresql://...\""
        )

    visible = [k for k in DB_URL_KEYS if k in names]
    if not visible:
        return (
            "DATABASE_URL is NOT among the secrets this app can see. It can "
            "see: " + ", ".join(names) + ". Add DATABASE_URL to this app's "
            "secrets - and check you are editing the right app if more than "
            "one is deployed."
        )

    key = visible[0]
    try:
        import streamlit as st

        raw = _clean(str(st.secrets[key]))
    except Exception as exc:
        log.info("Could not read %s from secrets: %s", key, exc)
        return f"{key} is present but could not be read back from secrets."

    if not raw:
        return f"{key} is present but empty."
    if is_postgres_url(raw):
        return (
            f"{key} is present and looks like a valid Postgres URL "
            f"({_shape(raw)}) - yet this app is still on SQLite. Reboot the "
            "app from Manage app to clear the cached connection."
        )
    return (
        f"{key} is present but is not a Postgres URL ({_shape(raw)}). It must "
        "be the FULL connection string, starting postgresql:// and ending "
        "/postgres - a common mistake is pasting only the password."
    )


def _shape(value: str) -> str:
    """Describe a secret without revealing it.

    Length and character classes are enough to tell a password from a URL, and
    reveal nothing usable. An earlier version of this printed the first 13
    characters of the value, which put a live database password into a chat
    transcript. Never again: no branch of this function may return any
    substring of `value`.
    """
    if not value:
        return "empty"
    kinds = []
    if "://" in value:
        kinds.append("has a scheme")
    else:
        kinds.append("no :// scheme")
    if "@" in value:
        kinds.append("has @")
    if any(c.isspace() for c in value):
        kinds.append("contains whitespace")
    return f"{len(value)} chars, " + ", ".join(kinds)


def _clean(url: str) -> str:
    """Strip whitespace and stray quotes from a pasted connection string.

    Secret-management UIs are textareas, and a paste routinely carries a
    trailing newline. Postgres then reads the database name as "postgres\\n"
    and refuses the connection with `database "postgres\\n" does not exist` -
    a failure that only appears in the deployment, never locally.
    """
    return _repair_missing_scheme(url.strip().strip('"').strip("'").strip())


#: `user:password@host:port/database` - a complete DSN with only the scheme
#: missing. Anchored at both ends, and every part required, so this can only
#: match something that is already a connection string.
_SCHEMELESS_DSN = re.compile(
    r"^[^\s:/@]+:[^\s@]+@[^\s:/@]+(?::\d+)?/[^\s/@]+$"
)


def _repair_missing_scheme(url: str) -> str:
    """Add `postgresql://` to a connection string that lost it in the paste.

    This is the single most common way the secret gets set wrong, and it is
    worth repairing rather than rejecting. A Supabase connection string is
    shown as

        postgresql://postgres.abcdefgh:PASSWORD@aws-0-x.pooler.supabase.com:6543/postgres

    and the natural thing to select by eye is the part that looks like an
    address - starting at the username. The result is a value that is complete
    and correct except for thirteen missing characters, which then fails the
    scheme check and falls back to an empty local SQLite file. Somebody then
    spends an evening looking for a bug in the database layer.

    The pattern is anchored and demands a password, a host and a database name,
    so it cannot match a bare password or a truncated fragment - those still
    fail loudly, which is what should happen to them.
    """
    if not url or "://" in url:
        return url
    if _SCHEMELESS_DSN.match(url):
        log.info("connection string was missing its scheme; assuming postgresql://")
        return "postgresql://" + url
    return url


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

    def __init__(
        self,
        connection: Any,
        dialect: str,
        url: str | None = None,
        reopen: Callable[[], Any] | None = None,
    ):
        self._conn = connection
        self.dialect = dialect
        self.url = url
        #: How to build a fresh driver connection if this one dies. Without it
        #: a dropped connection is permanent: the dashboard caches this object
        #: in st.cache_resource, Supabase's pooler closes it when idle, and
        #: every subsequent page load raises "the connection is closed" until
        #: somebody reboots the app.
        self._reopen = reopen

    def _revive(self) -> bool:
        """Replace a dead connection. True if a live one is now in place."""
        if self._reopen is None:
            return False
        try:
            self._conn = self._reopen()
        except Exception as exc:  # pragma: no cover - depends on the server
            log.warning("Could not reopen the database connection: %s", exc)
            return False
        log.info("Database connection was closed; reopened it.")
        return True

    @staticmethod
    def _is_disconnect(exc: Exception) -> bool:
        """Whether this error means the connection is gone rather than the query bad.

        Matched on the message because psycopg raises the same OperationalError
        class for a dead socket and for a genuine server error, and retrying a
        bad query would just fail twice.
        """
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "connection is closed", "connection already closed",
                "server closed the connection", "connection has been closed",
                "terminating connection", "ssl connection has been closed",
                "consuming input failed", "eof detected",
            )
        )

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
        try:
            cursor = self._conn.cursor()
            cursor.execute(translated, params or ())
            return Cursor(cursor, self.dialect)
        except Exception as exc:
            if not self._is_disconnect(exc) or not self._revive():
                # A failed statement leaves the transaction aborted, and every
                # later query on it fails with a message about THAT rather than
                # about the real cause. Roll back so the next one is judged on
                # its own merits.
                self._safe_rollback()
                raise
            cursor = self._conn.cursor()
            cursor.execute(translated, params or ())
            return Cursor(cursor, self.dialect)

    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # silent: rolling back a dead connection is a no-op
            pass

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
        # silent: closing twice, or closing a dropped connection, is not an error
        except Exception:  # pragma: no cover
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
        return next(iter(row.values())) if isinstance(row, dict) else row[0]

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

    def _open():
        """Build a connection. Kept as a closure so the Database can
        rebuild itself after the pooler drops an idle connection."""
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
            # Keep the socket alive through idle periods.
            #
            # A dashboard sits untouched for hours and then someone opens it.
            # In between, three separate things want to hang up: the operating
            # system, any NAT or load balancer on the path, and Supabase's own
            # pooler. TCP keepalives stop the first two - the kernel sends a
            # probe every 30 seconds of idleness, which keeps the mapping alive
            # and detects a genuinely dead peer in about 80 seconds instead of
            # on the next query.
            #
            # It cannot stop the third: if the pooler decides to close an idle
            # connection, it closes. That is what `reopen` below is for. The two
            # together mean the drop usually does not happen, and is invisible
            # when it does.
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        return conn

    conn = _open()
    return Database(conn, "postgres", url, reopen=_open)


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
