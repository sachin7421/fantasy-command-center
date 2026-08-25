"""Notification delivery with deduplication (spec 6, 6.6).

Channels are independent and each is skipped silently when unconfigured, so a
missing SMTP password never breaks a job. Every notification passes through the
dedup gate first: the same unchanged fact is never sent twice inside the
configured window, which is what keeps a daily monitor from becoming noise.
"""
from __future__ import annotations

import hashlib
import json
import logging
import smtplib
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from email.message import EmailMessage
from typing import Any

import requests

from src import db
from src.config import Config, env
from src.storage import Database

log = logging.getLogger(__name__)

# Discord hard-caps message content at 2000 characters.
DISCORD_LIMIT = 1900


@dataclass
class Notification:
    title: str
    lines: list[str] = field(default_factory=list)
    job: str = "manual"
    urgency: str = "normal"       # low | normal | high
    season: int | None = None
    week: int | None = None
    subtitle: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def body(self) -> str:
        return "\n".join(self.lines)

    def text(self) -> str:
        return f"{self.title}\n" + self.body() if self.lines else self.title

    def dedup_key(self) -> str:
        """Identity of the *fact*, not the delivery.

        Deliberately excludes timestamps so that re-running a job with unchanged
        findings produces the same key and is suppressed.
        """
        raw = json.dumps(
            {"title": self.title, "lines": self.lines, "job": self.job},
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Notifier:
    def __init__(self, cfg: Config, conn: Database):
        self.cfg = cfg
        self.conn = conn
        self.dedup_hours = int(cfg.get("notifications.dedup_window_hours", 72))

    # -- dedup ---------------------------------------------------------------

    def already_sent(self, notification: Notification) -> bool:
        cutoff = (
            datetime.now(UTC) - timedelta(hours=self.dedup_hours)
        ).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT 1 FROM recommendations WHERE job=? AND dedup_key=? "
            "AND notified_at IS NOT NULL AND notified_at > ? LIMIT 1",
            (notification.job, notification.dedup_key(), cutoff),
        ).fetchone()
        return row is not None

    def record(self, notification: Notification, notified: bool) -> int:
        cursor = self.conn.execute(
            "INSERT INTO recommendations(job, season, week, payload_json, dedup_key, "
            "created_at, notified_at) VALUES (?,?,?,?,?,?,?) RETURNING id",
            (
                notification.job,
                notification.season,
                notification.week,
                json.dumps(
                    {
                        "title": notification.title,
                        "lines": notification.lines,
                        "payload": notification.payload,
                    }
                ),
                notification.dedup_key(),
                db.utcnow(),
                db.utcnow() if notified else None,
            ),
        )
        row = cursor.fetchone()
        self.conn.commit()
        if row is None:
            return 0
        return row["id"] if not isinstance(row, tuple) else row[0]

    # -- delivery ------------------------------------------------------------

    def send(self, notification: Notification, force: bool = False) -> dict[str, Any]:
        """Deliver on every configured channel. Always records, even if muted."""
        if not force and self.already_sent(notification):
            self.record(notification, notified=False)
            log.info("Suppressed duplicate notification: %s", notification.title)
            return {"sent": False, "reason": "duplicate", "channels": []}

        delivered: list[str] = []
        errors: dict[str, str] = {}

        for name, fn in (
            ("discord", self._send_discord),
            ("email", self._send_email),
            ("desktop", self._send_desktop),
        ):
            if not self.cfg.get(f"notifications.{name}.enabled", False):
                continue
            try:
                if fn(notification):
                    delivered.append(name)
            except Exception as exc:
                errors[name] = str(exc)
                log.warning("Notification via %s failed: %s", name, exc)

        self.record(notification, notified=bool(delivered))
        return {"sent": bool(delivered), "channels": delivered, "errors": errors}

    def _send_discord(self, n: Notification) -> bool:
        url = env("DISCORD_WEBHOOK_URL") or self.cfg.get("notifications.discord.webhook_url")
        if not url:
            log.info("Discord enabled but DISCORD_WEBHOOK_URL is not set; skipping.")
            return False
        prefix = {"high": "**[URGENT]** ", "low": "", "normal": ""}.get(n.urgency, "")
        content = f"{prefix}**{n.title}**\n{n.body()}"
        if len(content) > DISCORD_LIMIT:
            content = content[: DISCORD_LIMIT - 20] + "\n... (truncated)"
        response = requests.post(url, json={"content": content}, timeout=15)
        response.raise_for_status()
        return True

    def _send_email(self, n: Notification) -> bool:
        """Send a multipart message: styled HTML with a plain-text fallback.

        Credentials come from the environment, never config, so nothing secret
        is ever committed. Gmail requires an app password here rather than the
        account password.
        """
        from src import email_render

        host = self.cfg.get("notifications.email.smtp_host")
        to_addr = self.cfg.get("notifications.email.to_addr")
        from_addr = self.cfg.get("notifications.email.from_addr") or to_addr
        username = env("SMTP_USERNAME") or from_addr
        password = env("SMTP_PASSWORD")

        if not (host and to_addr and from_addr):
            log.info("Email enabled but SMTP settings are incomplete; skipping.")
            return False
        if not password:
            log.info("Email enabled but SMTP_PASSWORD is not set; skipping.")
            return False

        dashboard_url = self.cfg.get("notifications.dashboard_url")
        subject_prefix = {"high": "[Fantasy] ACTION - ", "low": "[Fantasy] "}.get(
            n.urgency, "[Fantasy] "
        )

        message = EmailMessage()
        message["Subject"] = f"{subject_prefix}{n.title}"
        message["From"] = from_addr
        message["To"] = to_addr
        message.set_content(email_render.render_text(n.title, n.lines, dashboard_url))
        message.add_alternative(
            email_render.render(
                n.title,
                n.lines,
                urgency=n.urgency,
                dashboard_url=dashboard_url,
                subtitle=n.subtitle or None,
            ),
            subtype="html",
        )

        port = int(self.cfg.get("notifications.email.smtp_port", 587))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(message)
        return True

    def _send_desktop(self, n: Notification) -> bool:
        """Best-effort local toast; never fatal if the platform disagrees."""
        summary = n.body().splitlines()[0] if n.lines else ""
        try:
            if sys.platform == "win32":
                script = (
                    "[Windows.UI.Notifications.ToastNotificationManager, "
                    "Windows.UI.Notifications, ContentType=WindowsRuntime] > $null; "
                    f"$t=[Windows.UI.Notifications.ToastNotificationManager]::"
                    f"GetTemplateContent(0); "
                    f"$t.GetElementsByTagName('text').Item(0).AppendChild("
                    f"$t.CreateTextNode('{_escape(n.title)}')) > $null; "
                    "[Windows.UI.Notifications.ToastNotificationManager]::"
                    "CreateToastNotifier('Fantasy Command Center').Show("
                    "[Windows.UI.Notifications.ToastNotification]::new($t))"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", script],
                    check=False, capture_output=True, timeout=20,
                )
            elif sys.platform == "darwin":
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{_escape(summary)}" with title "{_escape(n.title)}"'],
                    check=False, capture_output=True, timeout=20,
                )
            else:
                subprocess.run(
                    ["notify-send", n.title, summary],
                    check=False, capture_output=True, timeout=20,
                )
            return True
        except Exception as exc:
            log.info("Desktop notification unavailable: %s", exc)
            return False


def _escape(text: str) -> str:
    return text.replace("'", " ").replace('"', " ").replace("\n", " ")
