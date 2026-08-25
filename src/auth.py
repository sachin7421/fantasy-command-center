"""Password gate for the hosted dashboard.

A Streamlit Community Cloud app is served on a public URL. That is fine for the
code, but the dashboard shows a live draft board and roster decisions that are
worth keeping to yourself during a draft, so the hosted deployment sits behind a
shared password.

Design notes:

* When no password is configured the gate is OPEN. That keeps local use
  friction-free - running on a laptop should not demand a login.
* The comparison uses `hmac.compare_digest`, not `==`, so it does not leak
  information through timing.
* Only a hash of the password is ever held in session state, so the plaintext
  does not linger in Streamlit's session store.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

log = logging.getLogger("fcc.auth")

SESSION_KEY = "_fcc_authenticated"
ENV_KEYS = ("APP_PASSWORD", "DASHBOARD_PASSWORD")


def configured_password() -> str | None:
    """The expected password, from env or Streamlit secrets. None => gate open."""
    # Whitespace is stripped for the same reason as the database URL: a value
    # pasted into a secrets textarea often carries a trailing newline, which
    # would otherwise make the correct password fail to match.
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    try:
        import streamlit as st

        for key in ENV_KEYS:
            if key in st.secrets:
                cleaned = str(st.secrets[key]).strip()
                if cleaned:
                    return cleaned
    except Exception:  # silent: no streamlit secrets here; the env var path follows
        pass
    return None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(_digest(candidate), _digest(expected))


def is_hosted() -> bool:
    """Whether this process is serving a publicly reachable dashboard.

    Streamlit Community Cloud sets HOSTNAME to a container id and provides no
    dedicated marker, so the check is deliberately broad: anything that is not
    obviously a developer's own machine is treated as hosted. Being wrong in
    that direction costs a password prompt; being wrong the other way publishes
    the league to the internet.
    """
    if os.environ.get("FCC_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("FCC_LOCAL", "").strip().lower() in ("1", "true", "yes"):
        return False
    return any(
        os.environ.get(marker)
        for marker in ("STREAMLIT_SERVER_HEADLESS", "STREAMLIT_RUNTIME_ENV",
                       "DYNO", "RENDER", "FLY_APP_NAME", "K_SERVICE")
    )


def require_password() -> bool:
    """Render the gate. Returns True when the app may proceed.

    Call this at the very top of the dashboard, before anything reads the
    database or renders league data.
    """
    import streamlit as st

    expected = configured_password()
    if not expected:
        # Fail CLOSED when hosted. This used to return True: if the secret was
        # absent, misnamed, blank after stripping, or if st.secrets raised for
        # any reason - and both lookups above swallow exceptions - the entire
        # dashboard rendered to the public internet with no warning and no log
        # line. The whole confidentiality of a public URL rested on a secret
        # being present, and nothing checked that it was.
        if is_hosted():
            st.title("Fantasy Command Center")
            st.error(
                "No dashboard password is configured, so this app has locked "
                "itself rather than serve the league publicly."
            )
            st.markdown(
                "Set **APP_PASSWORD** in this app's secrets and reboot it. "
                "To run without a gate deliberately - on your own machine - "
                "set `FCC_LOCAL=1`."
            )
            log.error("APP_PASSWORD is not configured; refusing to serve.")
            st.stop()
        return True  # local use, no gate

    if st.session_state.get(SESSION_KEY) == _digest(expected):
        return True

    st.title("Fantasy Command Center")
    st.caption("This dashboard is password protected.")

    with st.form("login", clear_on_submit=False):
        candidate = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter", type="primary")

    if submitted:
        if check(candidate, expected):
            st.session_state[SESSION_KEY] = _digest(expected)
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()
    return False
