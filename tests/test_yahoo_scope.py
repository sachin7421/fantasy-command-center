"""Knowing the day Yahoo attaches Fantasy Sports, without anyone signing in.

Yahoo said "access is live" on 15 Sep and it was not: the app had only its
OpenID permissions. Every attempt on our side - fresh consent, a token one
minute old - returned `additional_authorization_required`, and each attempt
needed a person at a browser.

Yahoo's authorization endpoint answers the question before any sign-in. Asking
for `scope=fspt-r` on an app without the scope redirects to an error page with
`error=invalid_scope`; on an app with it, it redirects to the login page. So
the check is one unauthenticated GET that can run every morning.

The Location headers below are Yahoo's real responses, recorded 17 Sep 2026
with the client ID replaced.
"""
from __future__ import annotations

import logging

import pytest
import requests

#: Real: this app, scope=fspt-r. The scope is not attached.
LOCATION_INVALID_SCOPE = (
    "https://api.login.yahoo.com/oauth2/error?client_id=CLIENT_ID"
    "&error=invalid_scope&error_description=invalid+scope"
)
#: Real: this app, scope=openid - a scope it HAS. Sent on to sign in.
LOCATION_LOGIN = (
    "https://login.yahoo.com?src=oauth&client_id=CLIENT_ID&crumb=CRUMB"
    "&done=https%3A%2F%2Fapi.login.yahoo.com%2Foauth2%2Fauthorize%3Fclient_id"
    "%3DCLIENT_ID%26language%3Den-us%26redirect_uri%3Doob%26response_type"
    "%3Dcode%26scope%3Dopenid&lang=en-us&redirect_uri=oob"
)
#: Real: a client ID Yahoo does not know.
LOCATION_BAD_CLIENT = (
    "https://api.login.yahoo.com/oauth2/error?client_id=not-a-real-client"
    "&error=unauthorized_client&error_description=invalid+client+id"
)


class _Resp:
    def __init__(self, status: int, location: str | None) -> None:
        self.status_code = status
        self.headers = {"Location": location} if location else {}


def _getter(*responses):
    """A stand-in for requests.get that records its calls."""
    calls: list[dict] = []
    queue = list(responses)

    def get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    get.calls = calls  # type: ignore[attr-defined]
    return get


def test_invalid_scope_means_not_attached():
    from src.yahoo_client import probe_fantasy_scope

    result = probe_fantasy_scope("CLIENT_ID", get=_getter(_Resp(302, LOCATION_INVALID_SCOPE)))
    assert result.verdict == "not-attached"


def test_a_redirect_to_sign_in_means_attached():
    from src.yahoo_client import probe_fantasy_scope

    result = probe_fantasy_scope("CLIENT_ID", get=_getter(_Resp(302, LOCATION_LOGIN)))
    assert result.verdict == "attached"


def test_an_unknown_client_is_not_reported_as_a_missing_scope():
    """Two failures with different remedies: wait for Yahoo, or fix the ID."""
    from src.yahoo_client import probe_fantasy_scope

    result = probe_fantasy_scope("CLIENT_ID", get=_getter(_Resp(302, LOCATION_BAD_CLIENT)))
    assert result.verdict == "bad-client"


def test_the_request_asks_for_fantasy_read_and_does_not_follow_redirects():
    """Following the redirect would land on a login page and lose the answer."""
    from src.yahoo_client import probe_fantasy_scope

    get = _getter(_Resp(302, LOCATION_LOGIN))
    probe_fantasy_scope("CLIENT_ID", get=get)
    call = get.calls[0]
    assert call["url"] == "https://api.login.yahoo.com/oauth2/request_auth"
    assert call["params"]["scope"] == "fspt-r"
    assert call["params"]["client_id"] == "CLIENT_ID"
    assert call["allow_redirects"] is False
    assert call["timeout"] > 0


def test_a_network_failure_is_retried_once():
    from src.yahoo_client import probe_fantasy_scope

    get = _getter(requests.ConnectionError("reset"), _Resp(302, LOCATION_LOGIN))
    assert probe_fantasy_scope("CLIENT_ID", get=get).verdict == "attached"
    assert len(get.calls) == 2


def test_a_network_failure_that_persists_is_unknown_and_said_out_loud(caplog):
    """Not "not-attached": an outage must not read as Yahoo's answer."""
    from src.yahoo_client import probe_fantasy_scope

    get = _getter(requests.Timeout("slow"), requests.Timeout("slow"))
    with caplog.at_level(logging.WARNING):
        result = probe_fantasy_scope("CLIENT_ID", get=get)
    assert result.verdict == "unknown"
    assert "slow" in result.detail
    assert any("scope" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.parametrize("resp", [_Resp(200, None), _Resp(302, None), _Resp(500, None)])
def test_an_unrecognised_answer_is_unknown_not_a_guess(resp):
    from src.yahoo_client import probe_fantasy_scope

    assert probe_fantasy_scope("CLIENT_ID", get=_getter(resp)).verdict == "unknown"


# --- the token the scheduled runs actually use ---------------------------------


def test_the_json_token_the_runner_uses_counts_as_a_stored_token(tmp_path, monkeypatch):
    """Regression. GitHub Actions supplies YAHOO_ACCESS_TOKEN_JSON, not the
    separate lines yfpy writes locally. `has_stored_token` looked only at the
    local names, so every scheduled `doctor` would have reported "consent was
    never completed" on a runner holding a perfectly good token.
    """
    from src.yahoo_client import has_stored_token

    class _Cfg:
        def get(self, key, default=None):
            return str(tmp_path) if key == "paths.env_dir" else default

    monkeypatch.setenv("YAHOO_ACCESS_TOKEN_JSON", '{"access_token": "x"}')
    assert has_stored_token(_Cfg()) is True


# --- what doctor says ------------------------------------------------------------


class _Cfg:
    def __init__(self, env_dir):
        self._env_dir = str(env_dir)

    def get(self, key, default=None):
        return self._env_dir if key == "paths.env_dir" else default


class _Exploding:
    def __getattr__(self, name):
        raise AssertionError(f"probe touched the Yahoo client (.{name})")


def _ctx(tmp_path, client_id="CLIENT_ID"):
    class _Ctx:
        cfg = _Cfg(tmp_path)
        yahoo = _Exploding()

        def yahoo_client_id(self):
            return client_id

    return _Ctx()


def test_doctor_names_the_missing_scope_and_does_not_send_you_to_setup(tmp_path, monkeypatch):
    """With the scope unattached, re-running consent cannot help - and the old
    message told the user to do exactly that."""
    from src import cli, yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("not-attached", "invalid_scope"),
    )
    verdict = cli._describe_yahoo_access(_ctx(tmp_path))
    assert "has not attached Fantasy Sports" in verdict
    assert "fcc setup" not in verdict


def test_doctor_says_when_the_scope_arrives_and_consent_is_next(tmp_path, monkeypatch):
    from src import cli, yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("attached", ""),
    )
    verdict = cli._describe_yahoo_access(_ctx(tmp_path))
    assert "Yahoo now accepts the Fantasy Sports scope" in verdict
    assert "consent" in verdict


def test_doctor_falls_back_to_the_old_answer_when_the_check_cannot_tell(tmp_path, monkeypatch):
    from src import cli, yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("unknown", "timed out"),
    )
    verdict = cli._describe_yahoo_access(_ctx(tmp_path))
    assert "consent was never completed" in verdict
    assert "could not check" in verdict


# --- the morning announcement ----------------------------------------------------


class _Notifier:
    def __init__(self):
        self.sent = []

    def send(self, notification, force=False):
        self.sent.append(notification)
        return {"sent": True}


def _announce_ctx(tmp_path, notifier):
    ctx = _ctx(tmp_path)
    ctx.notifier = lambda: notifier  # type: ignore[attr-defined]
    return ctx


def test_announce_is_silent_while_yahoo_has_not_attached_the_scope(tmp_path, monkeypatch):
    from src import cli, yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("not-attached", "invalid_scope"),
    )
    notifier = _Notifier()
    assert cli.announce_yahoo_scope(_announce_ctx(tmp_path, notifier)) == cli.EXIT_OK
    assert notifier.sent == []


def test_announce_tells_you_the_day_it_arrives(tmp_path, monkeypatch):
    from src import cli, yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("attached", ""),
    )
    notifier = _Notifier()
    assert cli.announce_yahoo_scope(_announce_ctx(tmp_path, notifier)) == cli.EXIT_OK
    assert len(notifier.sent) == 1
    assert "Fantasy Sports" in notifier.sent[0].title


def test_announce_stops_once_a_token_exists(tmp_path, monkeypatch):
    """After consent, the reminder has done its job; `doctor` takes over."""
    from src import cli, yahoo_client

    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("attached", ""),
    )
    monkeypatch.setenv("YAHOO_ACCESS_TOKEN_JSON", '{"access_token": "x"}')
    notifier = _Notifier()
    cli.announce_yahoo_scope(_announce_ctx(tmp_path, notifier))
    assert notifier.sent == []


def test_announce_without_a_client_id_says_so_rather_than_passing(tmp_path, capsys):
    """The GitHub runner has no YAHOO_CONSUMER_KEY secret today. A check that
    quietly skips there would look like "not attached yet" forever."""
    from src import cli

    ctx = _ctx(tmp_path, client_id=None)
    ctx.notifier = _Notifier  # type: ignore[attr-defined]
    assert cli.announce_yahoo_scope(ctx) == cli.EXIT_FAIL
    assert "YAHOO_CONSUMER_KEY" in capsys.readouterr().out


# --- the check must survive a database that is busy --------------------------


def test_the_scope_check_answers_even_with_no_database(tmp_path, monkeypatch, capsys):
    """It died at startup on GitHub, 18 and 19 Sep.

    `fcc sync` had overrun the job timeout and its orphan still held locks, so
    building a Context raised "canceling statement due to statement timeout"
    and the run reported exit 2. Yahoo's answer needs no database at all: the
    check that exists to survive a Yahoo outage must also survive ours. It
    cannot notify without one, and says so.
    """
    from src import cli, yahoo_client

    def _no_context(*a, **k):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(cli, "Context", _no_context)
    monkeypatch.setattr(
        yahoo_client, "probe_fantasy_scope",
        lambda client_id, **kw: yahoo_client.ScopeCheck("not-attached", "invalid_scope"),
    )
    monkeypatch.setenv("YAHOO_CONSUMER_KEY", "some-client-id")
    (tmp_path / "config.yaml").write_text("league:\n  league_id: \"1\"\n", encoding="utf-8")

    code = cli.main(["--config", str(tmp_path / "config.yaml"), "yahoo-scope"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "not-attached" in out
    assert "database" in out.lower()


def test_another_command_still_fails_when_the_database_is_gone(tmp_path, monkeypatch, capsys):
    """Only the scope check gets this door: every other command needs data."""
    from src import cli

    def _no_context(*a, **k):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(cli, "Context", _no_context)
    (tmp_path / "config.yaml").write_text("league:\n  league_id: \"1\"\n", encoding="utf-8")
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "rank"]) == cli.EXIT_FAIL
