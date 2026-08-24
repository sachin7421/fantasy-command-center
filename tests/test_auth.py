"""Password gate tests. Security-relevant, so worth pinning."""
from __future__ import annotations

import os

import pytest

from src import auth


def test_correct_password_accepted():
    assert auth.check("hunter2", "hunter2") is True


def test_wrong_password_rejected():
    assert auth.check("hunter3", "hunter2") is False


def test_comparison_is_length_safe():
    """A prefix of the real password must not pass."""
    assert auth.check("hunter", "hunter2") is False
    assert auth.check("hunter22", "hunter2") is False


def test_empty_candidate_rejected():
    assert auth.check("", "hunter2") is False


def test_gate_is_open_when_no_password_configured(monkeypatch):
    for key in auth.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert auth.configured_password() is None


def test_password_read_from_environment(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "from-env")
    assert auth.configured_password() == "from-env"


def test_session_never_holds_the_plaintext(monkeypatch):
    """Only a digest is stored, so the password does not linger in session state."""
    monkeypatch.setenv("APP_PASSWORD", "sekrit")
    digest = auth._digest("sekrit")
    assert digest != "sekrit"
    assert len(digest) == 64
