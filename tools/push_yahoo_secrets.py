"""Give the scheduled runs Yahoo: copy the local consent into GitHub secrets.

    python tools/push_yahoo_secrets.py            # sets three secrets via gh
    python tools/push_yahoo_secrets.py --check    # says what it WOULD set

Sets YAHOO_CONSUMER_KEY, YAHOO_CONSUMER_SECRET and YAHOO_ACCESS_TOKEN_JSON from
.env. The JSON is the shape yfpy reads on a runner (seven fields, see
REQUIRED). Every value travels to `gh secret set` on stdin and is never
printed - run this yourself; an assistant is not allowed to write the secret
store, and should not be.

Re-run it after any fresh consent (`fcc verify-settings` in a terminal), and if
a run ever warns that Yahoo rotated the refresh token.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REQUIRED = frozenset({
    "access_token", "consumer_key", "consumer_secret", "guid",
    "refresh_token", "token_time", "token_type",
})

_ENV_TO_JSON = {
    "YAHOO_ACCESS_TOKEN": "access_token",
    "YAHOO_CONSUMER_KEY": "consumer_key",
    "YAHOO_CONSUMER_SECRET": "consumer_secret",
    "YAHOO_GUID": "guid",
    "YAHOO_REFRESH_TOKEN": "refresh_token",
    "YAHOO_TOKEN_TIME": "token_time",
    "YAHOO_TOKEN_TYPE": "token_type",
}


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines, comments ignored, surrounding quotes stripped."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def token_json_from_env(text: str) -> str:
    """The YAHOO_ACCESS_TOKEN_JSON value yfpy expects. Raises KeyError naming
    the missing .env variable rather than sending a half-built token."""
    env = parse_env(text)
    payload: dict[str, object] = {}
    for env_key, json_key in _ENV_TO_JSON.items():
        if env_key not in env or not env[env_key]:
            raise KeyError(env_key)
        payload[json_key] = env[env_key]
    payload["token_time"] = float(str(payload["token_time"]))
    assert set(payload) == REQUIRED
    return json.dumps(payload)


def _set_secret(name: str, value: str) -> None:
    subprocess.run(["gh", "secret", "set", name], input=value, text=True, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", default=".env", help="path to the .env file")
    parser.add_argument("--check", action="store_true", help="describe, set nothing")
    args = parser.parse_args(argv)

    text = Path(args.env).read_text(encoding="utf-8")
    env = parse_env(text)
    try:
        token_json = token_json_from_env(text)
    except KeyError as exc:
        print(f"{args.env} is missing {exc.args[0]}; run `fcc setup` and "
              "`fcc verify-settings` first.", file=sys.stderr)
        return 1

    plan = {
        "YAHOO_CONSUMER_KEY": env["YAHOO_CONSUMER_KEY"],
        "YAHOO_CONSUMER_SECRET": env["YAHOO_CONSUMER_SECRET"],
        "YAHOO_ACCESS_TOKEN_JSON": token_json,
    }
    for name, value in plan.items():
        print(f"  {name:<24} {len(value):>5} chars"
              + (" (Client ID begins " + value[:13] + "...)" if name == "YAHOO_CONSUMER_KEY" else ""))
    if args.check:
        print("--check: nothing set.")
        return 0
    for name, value in plan.items():
        _set_secret(name, value)
    print("Set. The next scheduled run uses the live league; `gh secret list` shows the dates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
