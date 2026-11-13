"""Tenki auth/endpoint/workspace resolution.

Every value comes from Hermes: explicit ``terminal.tenki_*`` config, or an
environment variable read through :func:`agent.secret_scope.get_secret` (which
honours the active profile under a multiplexed gateway). The plugin never
reads the Tenki CLI's own config/login file or any ``.env`` file; Hermes loads
``~/.hermes/.env`` itself. Users with an existing Tenki CLI login can print a
key for ``TENKI_API_KEY`` with ``tenki auth token``.
"""

from __future__ import annotations

from typing import Any

TENKI_DEFAULT_API_ENDPOINT = "https://api.tenki.cloud"

#: Credential env vars, in precedence order. ``TENKI_API_KEY`` is the
#: manifest's ``requires_env`` entry; ``TENKI_AUTH_TOKEN`` is the SDK's alias.
TENKI_AUTH_ENV_VARS = ("TENKI_API_KEY", "TENKI_AUTH_TOKEN")

#: One-line, user-facing hint for missing credentials.
TENKI_AUTH_HINT = (
    "set TENKI_API_KEY in ~/.hermes/.env (`tenki auth token` prints a key "
    "from an existing Tenki CLI login)"
)


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _scoped_env(name: str) -> str:
    """Read an env var honoring the active profile secret scope.

    Under a multiplexed gateway turn a profile scope is installed, and the
    value must come from *that* profile's secrets — never from a raw
    ``os.environ`` read that could hold another profile's value. When no
    multiplexing is active this behaves exactly like ``os.getenv``.
    """
    try:
        from agent.secret_scope import get_secret

        return _string(get_secret(name, ""))
    except Exception:
        # Fail closed: an unscoped read under active multiplexing (or any
        # resolution error) must NOT silently leak a process-global value.
        return ""


def resolve_tenki_api_endpoint(explicit: str = "") -> str:
    """Explicit config, then ``TENKI_API_ENDPOINT``/``TENKI_API_URL``, then
    the public default."""
    explicit = _string(explicit)
    if explicit:
        return explicit
    for env_name in ("TENKI_API_ENDPOINT", "TENKI_API_URL"):
        value = _scoped_env(env_name)
        if value:
            return value
    return TENKI_DEFAULT_API_ENDPOINT


def resolve_tenki_workspace_id(explicit: str = "") -> str:
    """Explicit config, then ``TENKI_WORKSPACE_ID``/``TENKI_WORKSPACE``.

    Empty when neither is set: the SDK then lets the API key's own workspace
    apply, so a workspace id is optional for normal sessions.
    """
    explicit = _string(explicit)
    if explicit:
        return explicit
    for env_name in ("TENKI_WORKSPACE_ID", "TENKI_WORKSPACE"):
        value = _scoped_env(env_name)
        if value:
            return value
    return ""


def resolve_tenki_auth_token() -> str:
    """The Tenki API key from ``TENKI_API_KEY`` (or ``TENKI_AUTH_TOKEN``).

    Passed to the SDK verbatim (``tk_...`` keys go out as bearer keys). Never
    logged or persisted.
    """
    for env_name in TENKI_AUTH_ENV_VARS:
        value = _scoped_env(env_name)
        if value:
            return value
    return ""


def has_tenki_auth() -> bool:
    return bool(resolve_tenki_auth_token())
