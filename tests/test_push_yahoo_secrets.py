"""The scheduled runs get Yahoo from one secret, built from .env by a tool.

yfpy reads `YAHOO_ACCESS_TOKEN_JSON` on a runner and requires seven fields in
it. Assembling that by hand from .env is seven chances to paste the wrong
value; `tools/push_yahoo_secrets.py` builds it and hands it to `gh secret set`
on stdin, so no value is ever printed.
"""
from __future__ import annotations

import json

from tools.push_yahoo_secrets import REQUIRED, token_json_from_env

ENV = """
# comment
DATABASE_URL=postgres://x
YAHOO_CONSUMER_KEY=key-1
YAHOO_CONSUMER_SECRET="secret-1"
YAHOO_ACCESS_TOKEN=at-1
YAHOO_GUID=guid-1
YAHOO_REFRESH_TOKEN=rt-1
YAHOO_TOKEN_TIME=1759800000.123
YAHOO_TOKEN_TYPE=bearer
"""


def test_the_json_has_exactly_the_fields_yfpy_requires():
    payload = json.loads(token_json_from_env(ENV))
    assert set(payload) == REQUIRED
    assert payload["consumer_secret"] == "secret-1"  # quotes stripped
    assert payload["token_time"] == 1759800000.123  # a number, as yfpy writes it
    assert payload["token_type"] == "bearer"


def test_a_missing_field_is_an_error_not_an_empty_string():
    import pytest

    with pytest.raises(KeyError, match="YAHOO_REFRESH_TOKEN"):
        token_json_from_env(ENV.replace("YAHOO_REFRESH_TOKEN=rt-1\n", ""))
