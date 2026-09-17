"""A diagnostic must never be the thing that blocks.

`fcc doctor` printed "Enter verifier :" and waited on stdin. It reported the
right verdict afterwards, but it had to START an OAuth consent flow to reach
it: the probe called `fetch_teams()`, which builds the yfpy query, which begins
consent when no token exists.

`browser_callback=sys.stdin.isatty()` does not prevent this. That flag only
decides whether a BROWSER opens; with no TTY yfpy still falls back to prompting
for a verifier on stdin. So the headless guard that looked like it covered
scheduled runs covered only half of them, and `doctor` runs on a schedule.

This is the fifth time in this project that the tool reporting the problem was
itself the problem, so the property is pinned rather than trusted.
"""
from __future__ import annotations

import pytest

from src.yahoo_client import has_stored_token


class _Cfg:
    """Minimal stand-in for Config - only `get` is used by the helper."""

    def __init__(self, env_dir):
        self._env_dir = str(env_dir)

    def get(self, key, default=None):
        return self._env_dir if key == "paths.env_dir" else default


def test_no_token_when_env_file_absent(tmp_path):
    assert has_stored_token(_Cfg(tmp_path)) is False


def test_no_token_when_only_consumer_credentials_present(tmp_path):
    """Credentials existing is not consent having happened.

    This is the state the user was actually in, and the state `doctor` used to
    resolve by starting a consent flow.
    """
    (tmp_path / ".env").write_text(
        "YAHOO_CONSUMER_KEY=abc123\nYAHOO_CONSUMER_SECRET=shh\n", encoding="utf-8"
    )
    assert has_stored_token(_Cfg(tmp_path)) is False


def test_blank_token_line_does_not_count(tmp_path):
    """A key with an empty value is not a token.

    Clearing consent by blanking the value rather than deleting the line is an
    obvious thing to do by hand, and it must not read as "still authorised".
    """
    (tmp_path / ".env").write_text(
        'YAHOO_ACCESS_TOKEN=\nYAHOO_REFRESH_TOKEN=""\n', encoding="utf-8"
    )
    assert has_stored_token(_Cfg(tmp_path)) is False


@pytest.mark.parametrize("key", ["YAHOO_ACCESS_TOKEN", "YAHOO_REFRESH_TOKEN"])
def test_token_in_env_file_is_found(tmp_path, key):
    (tmp_path / ".env").write_text(f"{key}=some-value\n", encoding="utf-8")
    assert has_stored_token(_Cfg(tmp_path)) is True


def test_token_in_environment_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("YAHOO_ACCESS_TOKEN", "some-value")
    assert has_stored_token(_Cfg(tmp_path)) is True


def test_probe_never_builds_a_query_without_a_token(tmp_path, monkeypatch):
    """The regression itself: no token means no Yahoo call, at all.

    Asserted by making any attempt to reach Yahoo an explicit failure. If the
    probe ever goes back to asking Yahoo whether it is authorised, this fails
    instead of hanging a scheduled job.
    """
    from src.cli import _describe_yahoo_access

    class _Exploding:
        def __getattr__(self, name):
            raise AssertionError(
                f"probe touched the Yahoo client (.{name}) with no stored token"
            )

    class _Ctx:
        cfg = _Cfg(tmp_path)
        yahoo = _Exploding()

        def yahoo_client_id(self):
            return None  # no Client ID, so no scope check either

    verdict = _describe_yahoo_access(_Ctx())
    assert "consent was never completed" in verdict


def test_probe_does_not_read_stdin(tmp_path, monkeypatch):
    """Nothing in the health check may prompt.

    Independent of the check above: a future probe could avoid the client and
    still prompt for something. Reading stdin during `doctor` is the defect.
    """
    from src.cli import _describe_yahoo_access

    def _no(*a, **k):
        raise AssertionError("doctor prompted for input")

    monkeypatch.setattr("builtins.input", _no)

    class _Ctx:
        cfg = _Cfg(tmp_path)
        yahoo = None

        def yahoo_client_id(self):
            return None  # no Client ID, so no scope check either

    assert "consent was never completed" in _describe_yahoo_access(_Ctx())
