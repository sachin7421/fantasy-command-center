"""A job must not start a Yahoo sign-in, whatever is in .env.

Found by running `fcc waivers` on 17 Sep. `.env` held the Client ID and no
token - exactly the state while Yahoo has not attached the scope - and every
season job took "a Client ID exists" to mean "Yahoo is usable". It built the
yfpy query, yfpy started consent, and the job died on

    EOFError: EOF when reading a line   (at "Enter verifier : ")

before it ever reached the typed-in roster. `fcc sync`, the first step of
`fcc daily`, did the same. On GitHub there is no Client ID, which is the only
reason the scheduled runs survived.

Consent is started deliberately, in a terminal, by `fcc verify-settings` -
which is what `fcc setup` itself says to run next. Jobs need a token.
"""
from __future__ import annotations

import pytest

from src import cli


class _Exploding:
    def __getattr__(self, name):
        raise AssertionError(f"a job touched the Yahoo client (.{name}) with no token")


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    """A Context whose .env has a Client ID and no token."""
    (tmp_path / ".env").write_text("YAHOO_CONSUMER_KEY=some-client-id\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        f'paths:\n  env_dir: "{tmp_path.as_posix()}"\nleague:\n  league_id: "1"\n'
        "  season: 2026\n  my_team_id: 3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.Context, "yahoo_configured", lambda self: True)
    monkeypatch.setattr(cli.Context, "yahoo", property(lambda self: _Exploding()))
    return cli.Context(str(tmp_path / "config.yaml"), str(tmp_path / "t.db"))


def test_credentials_without_a_token_are_not_ready(ctx):
    assert ctx.yahoo_ready() is False


def test_credentials_and_a_token_are_ready(ctx, monkeypatch):
    monkeypatch.setenv("YAHOO_ACCESS_TOKEN_JSON", '{"access_token": "x"}')
    assert ctx.yahoo_ready() is True


def test_a_job_falls_back_to_the_typed_roster_instead_of_signing_in(ctx, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(cli.Context, "manual_snapshot", lambda self, s, w: sentinel)
    assert ctx.league_snapshot(2026, 3) is sentinel


def test_playoff_odds_do_not_sign_in_either(ctx):
    assert ctx.playoff_snapshot(2026, 3, 14) is None


def test_sync_skips_the_league_half_and_says_why(ctx, monkeypatch, capsys):
    def _no(*a, **k):
        raise AssertionError("sync reached Yahoo with no token")

    monkeypatch.setattr(cli, "sync_yahoo_league", _no)
    cli._sync_yahoo_if_ready(ctx, 2026, 3, force=False)
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "verify-settings" in out


def test_doctor_names_the_command_that_actually_starts_consent(tmp_path, monkeypatch):
    """It said `fcc setup`, which only stores the Client ID and secret."""
    from src import yahoo_client

    class _Cfg:
        def get(self, key, default=None):
            return str(tmp_path) if key == "paths.env_dir" else default

    class _Ctx:
        cfg = _Cfg()
        yahoo = _Exploding()

        def yahoo_client_id(self):
            return None

    verdict = cli._describe_yahoo_access(_Ctx())
    assert "fcc verify-settings" in verdict
    assert "fcc setup" not in verdict
    assert yahoo_client  # imported for the monkeypatch-free path


def test_the_scope_notification_names_the_consent_command(tmp_path, monkeypatch):
    """It said "for example `fcc doctor`", which never starts consent."""
    from src import yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("attached", ""),
    )
    sent = []

    class _Notifier:
        def send(self, n, force=False):
            sent.append(n)

    class _Cfg:
        def get(self, key, default=None):
            return str(tmp_path) if key == "paths.env_dir" else default

    class _Ctx:
        cfg = _Cfg()

        def yahoo_client_id(self):
            return "id"

        def notifier(self):
            return _Notifier()

    cli.announce_yahoo_scope(_Ctx())
    text = sent[0].text()
    assert "fcc verify-settings" in text
    assert "fcc doctor" not in text
