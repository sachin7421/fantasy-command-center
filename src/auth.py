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
import os

SESSION_KEY = "_fcc_authenticated"
ENV_KEYS = ("APP_PASSWORD", "DASHBOARD_PASSWORD")


def configured_password() -> str | None:
    """The expected password, from env or Streamlit secrets. None => gate open."""
    for key in ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return value
    try:
        import streamlit as st

        for key in ENV_KEYS:
            if key in st.secrets:
                return str(st.secrets[key])
    except Exception:
        pass
    return None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(_digest(candidate), _digest(expected))


def require_password() -> bool:
    """Render the gate. Returns True when the app may proceed.

    Call this at the very top of the dashboard, before anything reads the
    database or renders league data.
    """
    import streamlit as st

    expected = configured_password()
    if not expected:
        return True  # unconfigured: local use, no gate

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
