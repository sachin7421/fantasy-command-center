"""Is the hosted dashboard actually up?

    python tools/check_deploy.py            # URL from README's Live link
    python tools/check_deploy.py --url ...

Streamlit Community Cloud answers /healthz with {"status":"ok"} without the
password gate (/_stcore/health returns the app shell there - checked 7 Oct
2026). The METHOD audit (20 Sep 2026) listed the
absence of any post-deploy check as the second-highest risk: the gate
proved the code, and nothing proved the URL. The daily workflow runs this
after its own doctor step.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path

DEFAULT_TIMEOUT = 30.0


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    import requests

    response = requests.get(url, timeout=timeout)
    return response.status_code, response.text[:200]


def check(base_url: str, get: Callable[[str, float], tuple[int, str]] = _http_get,
          timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """(passed, one-line verdict) for the app at base_url."""
    url = base_url.rstrip("/") + "/healthz"
    try:
        status, body = get(url, timeout)
    except Exception as exc:
        return False, f"unreachable ({type(exc).__name__}: {exc})"
    text = body.strip().lower()
    if status == 200 and ("ok" in text[:40]):
        return True, f"healthy ({status} {body.strip()[:20]})"
    return False, f"unhealthy (HTTP {status}: {body.strip()[:80]})"


def url_from_readme(readme: Path = Path("README.md")) -> str | None:
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"\*\*Live:\*\*\s*<(https://[^>]+)>", text)
    return match.group(1) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=None)
    args = parser.parse_args(argv)
    url = args.url or url_from_readme()
    if not url:
        print("deploy check: no URL (pass --url or keep the **Live:** link in README.md)",
              file=sys.stderr)
        return 2
    passed, verdict = check(url)
    print(f"deploy check: {url} -> {verdict}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
