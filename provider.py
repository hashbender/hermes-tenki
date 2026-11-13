"""Tenki terminal-environment provider for hermes-agent.

Registers the Tenki cloud sandbox as a pluggable ``terminal.backend`` value
via hermes-agent's terminal-environment plugin registry (#94400). The
provider owns everything the in-core integration used to hand-wire:
classification flags, availability/doctor/probe checks, the setup hooks, and
resolution of the ``terminal.tenki_*`` settings that core's config→env
bridge does not know about.

Configuration is read with this precedence, mirroring the in-core bridge
semantics (config.yaml wins over stale process env):

1. ``terminal.<key>`` in ``config.yaml``
2. the ``TERMINAL_TENKI_*`` environment variable (Hermes loads ``.env``
   into the process environment; the plugin never reads it itself)
3. the default

One deliberate divergence from core defaults: ``container_persistent``
defaults to **False** for Tenki (terminate-only sandboxes) unless the user
sets ``terminal.container_persistent`` explicitly in config.yaml. Core's generic default of ``true``
cannot be distinguished from an explicit choice by the time it reaches
``container_config``, and a silently-persistent cloud sandbox is the wrong
failure mode.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

_SDK_SPEC = "tenki>=1.4.1,<2"
_GUEST_HOME = "/home/tenki"

#: Core's prompt builder creates a live environment for remote backends at
#: system-prompt build time under this literal task id. A Tenki sandbox is a
#: billable cloud resource, so creation is refused for it and the prompt
#: falls back to :attr:`env_description`.
_PROMPT_PROBE_TASK_ID = "prompt-backend-probe"


def _sdk_missing() -> bool:
    """True when the tenki SDK is neither loaded nor importable.

    ``sys.modules`` is consulted first: an already-loaded (or test-injected)
    module counts as present even when it has no ``__spec__``, which would
    make ``importlib.util.find_spec`` raise.
    """
    import importlib.util
    import sys

    if "tenki" in sys.modules:
        return False
    try:
        return importlib.util.find_spec("tenki") is None
    except (ImportError, ValueError):
        return True


def _terminal_settings() -> Dict[str, Any]:
    """The merged ``terminal:`` config section; ``{}`` fail-soft.

    The merged view (managed-scope overlays, ``${ENV_VAR}`` expansion) is
    what values must be read from. Core ships no defaults for ``tenki_*``
    keys, so for those merged == what the user wrote.
    """
    try:
        from hermes_cli.config import load_config_readonly

        section = load_config_readonly().get("terminal", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        logger.debug("Tenki provider: terminal config unavailable", exc_info=True)
        return {}


def _raw_terminal_settings() -> Dict[str, Any]:
    """The ``terminal:`` section the USER actually wrote; ``{}`` fail-soft.

    Used only for key-PRESENCE checks: the raw (defaults-free) view is how
    the provider distinguishes an explicit user choice from core's generic
    defaults — ``container_persistent`` in particular defaults to ``true``
    in the merged config, which must not read as an explicit opt-in to
    persistent cloud sandboxes.
    """
    try:
        from hermes_cli.config import read_raw_config_readonly

        section = read_raw_config_readonly().get("terminal", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        logger.debug("Tenki provider: raw terminal config unavailable", exc_info=True)
        return {}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int, *, setting: str) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid {setting} value {value!r}: expected a number"
        ) from None


def _as_list(value: Any, *, setting: str) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            parsed = json.loads(value)
        except ValueError:
            raise ValueError(
                f"Invalid {setting} value {value!r}: expected a JSON list"
            ) from None
        if isinstance(parsed, list):
            return parsed
    raise ValueError(f"Invalid {setting} value {value!r}: expected a list")


def _setting(settings: Dict[str, Any], key: str, env_var: str) -> Any:
    """Raw setting value: config.yaml first, then env, else ``None``."""
    if key in settings:
        return settings[key]
    return os.environ.get(env_var)


class TenkiProvider(TerminalEnvironmentProvider):
    """Tenki cloud sandboxes as a pluggable terminal backend."""

    name = "tenki"
    display_name = "Tenki"

    # -- classification -------------------------------------------------
    is_remote = True
    is_container = True
    #: Tenki sandboxes are durable, name-addressed remote resources; two
    #: ephemeral sessions sharing one name could attach and destroy each
    #: other's sandbox (the #82731 contract), so non-persistent mode gets a
    #: per-session sandbox identity.
    session_isolated_when_nonpersistent = True

    #: Declarative hints for cores that grow the corresponding provider
    #: hooks (guest-home cwd exemption, default cwd, prompt-probe opt-out).
    #: Current cores ignore unknown attributes; the provider also carries
    #: its own guards for each until the hooks exist.
    guest_home_root = _GUEST_HOME
    default_cwd = _GUEST_HOME
    probe_at_prompt_build = False

    @property
    def description(self) -> str:
        return "Run commands in a Tenki cloud sandbox (created on demand, terminated by default)."

    @property
    def env_description(self) -> str:
        return (
            "a Tenki cloud sandbox (Linux). Sandbox creation is deferred "
            "until the first terminal command"
        )

    @property
    def cache_path_base(self) -> Optional[str]:
        # Synced Hermes cache files land under the sandbox home (the
        # ``tenki_sync_hermes_home`` flow mirrors ``~/.hermes``).
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({"TENKI_AUTH_TOKEN", "TENKI_API_KEY"})

    # -- availability / UX ----------------------------------------------

    def is_available(self) -> bool:
        if _sdk_missing():
            return False
        try:
            from .tenki_config import has_tenki_auth

            return has_tenki_auth()
        except Exception:
            return False

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        import importlib

        if _sdk_missing():
            detail = ""
            try:
                from tools.lazy_deps import install_specs

                result = install_specs([_SDK_SPEC])
                detail = getattr(result, "reason", "") or ""
                importlib.invalidate_caches()
            except Exception as exc:
                detail = str(exc)
            if _sdk_missing():
                logger.error(
                    "tenki is required for the Tenki terminal backend: "
                    "pip install '%s'%s",
                    _SDK_SPEC,
                    f" ({detail})" if detail else "",
                )
                return False
        try:
            from .tenki_config import TENKI_AUTH_HINT, has_tenki_auth
        except Exception:
            has_tenki_auth = lambda: False  # noqa: E731
            TENKI_AUTH_HINT = "set TENKI_API_KEY in ~/.hermes/.env"
        if not has_tenki_auth():
            logger.error(
                "Tenki backend selected but no Tenki API key was found: %s.",
                TENKI_AUTH_HINT,
            )
            return False
        return True

    def probe(self) -> Tuple[str, str]:
        if _sdk_missing():
            return (
                "needs_setup",
                f"Tenki SDK not found — install `{_SDK_SPEC}` or run `hermes setup terminal`.",
            )
        try:
            from .tenki_config import has_tenki_auth

            authed = has_tenki_auth()
        except Exception:
            authed = False
        if authed:
            return ("ready", "")
        return (
            "needs_setup",
            "Tenki credentials not found — set TENKI_API_KEY in ~/.hermes/.env "
            "(`tenki auth token` prints a key from an existing Tenki CLI login).",
        )

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        rows: List[Tuple[bool, str, str]] = []
        try:
            from .tenki_config import (
                TENKI_AUTH_HINT,
                has_tenki_auth,
                resolve_tenki_workspace_id,
            )

            if has_tenki_auth():
                rows.append((True, "Tenki auth", "(configured)"))
            else:
                rows.append((
                    False,
                    "Tenki auth not found",
                    f"({TENKI_AUTH_HINT})",
                ))
            settings = _terminal_settings()
            workspace = resolve_tenki_workspace_id(
                str(
                    _setting(settings, "tenki_workspace_id", "TERMINAL_TENKI_WORKSPACE_ID")
                    or ""
                )
            )
            if workspace:
                rows.append((True, "Tenki workspace", "(configured)"))
            else:
                rows.append((
                    True,
                    "Tenki workspace",
                    "(not set — the API key's workspace is used; set "
                    "terminal.tenki_workspace_id or TENKI_WORKSPACE_ID to pick one)",
                ))
        except Exception as exc:
            rows.append((False, "Tenki configuration", f"(unreadable: {exc})"))
        if not _sdk_missing():
            rows.append((True, "tenki SDK", "(installed)"))
        else:
            rows.append((
                False,
                "tenki SDK not installed",
                f"(pip install '{_SDK_SPEC}')",
            ))
        return rows

    def setup_instructions(self) -> List[str]:
        return [
            "Cloud sandboxes are created on demand and terminated by default.",
            "Auth: set TENKI_API_KEY in ~/.hermes/.env (`tenki auth token` "
            "prints a key from an existing Tenki CLI login).",
            "Settings live under `terminal:` in config.yaml — tenki_image, "
            "tenki_workspace_id, tenki_api_endpoint, tenki_max_duration, "
            "tenki_idle_timeout, tenki_pause_retention, tenki_sync_hermes_home, "
            "tenki_forward_env (see the plugin README for the full list).",
            "Persistent sandboxes: set `terminal.container_persistent: true` "
            "explicitly; the Tenki backend defaults to terminate-only.",
        ]

    def post_setup(self) -> None:
        import importlib

        if _sdk_missing():
            print("Installing Tenki SDK...")
            try:
                from tools.lazy_deps import install_specs

                result = install_specs([_SDK_SPEC])
                importlib.invalidate_caches()
                if getattr(result, "ok", False) and not _sdk_missing():
                    print("Tenki SDK installed")
                else:
                    reason = getattr(result, "reason", "") or "install failed"
                    print(
                        f"Install did not complete ({reason}) — run manually: "
                        f"pip install '{_SDK_SPEC}'"
                    )
            except Exception as exc:
                print(
                    f"Install failed — run manually: pip install '{_SDK_SPEC}' ({exc})"
                )
        try:
            from .tenki_config import (
                has_tenki_auth,
                resolve_tenki_api_endpoint,
                resolve_tenki_workspace_id,
            )

            settings = _terminal_settings()
            endpoint = resolve_tenki_api_endpoint(
                str(_setting(settings, "tenki_api_endpoint", "TERMINAL_TENKI_API_ENDPOINT") or "")
            )
            workspace = resolve_tenki_workspace_id(
                str(_setting(settings, "tenki_workspace_id", "TERMINAL_TENKI_WORKSPACE_ID") or "")
            )
            print(f"  Endpoint:  {endpoint}")
            print(f"  Workspace: {workspace or '(API key default)'}")
            if has_tenki_auth():
                print("  Tenki auth: already configured")
                return
            print("  Tenki auth not found")
            print(
                "  Paste a Tenki API key (tk_...). With an existing Tenki CLI "
                "login, `tenki auth token` prints one."
            )
            import getpass

            token = getpass.getpass(
                "  TENKI_API_KEY (leave blank to set it later in ~/.hermes/.env): "
            )
            if token.strip():
                from hermes_cli.config import save_env_value

                save_env_value("TENKI_API_KEY", token.strip())
                print("  Saved TENKI_API_KEY to ~/.hermes/.env")
            else:
                print("  Skipped — set TENKI_API_KEY in ~/.hermes/.env before use")
        except Exception as exc:
            print(f"  Tenki setup hook failed: {exc}")

    # -- the factory -----------------------------------------------------

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: Optional[str] = None,
        container_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        if task_id == _PROMPT_PROBE_TASK_ID:
            # Prompt construction must never create a billable cloud sandbox;
            # failing here routes the prompt builder to the static
            # env_description fallback.
            raise RuntimeError(
                "Tenki defers sandbox creation until the first terminal "
                "command; no live probe at prompt build"
            )

        from .tenki_env import TenkiEnvironment

        cc = container_config or {}
        settings = _terminal_settings()

        # RL/benchmark harnesses register per-task overrides; honor the
        # per-task image the way the image ladders do for built-in backends.
        override_image = ""
        try:
            from tools.terminal_tool import resolve_task_overrides

            override_image = str(
                resolve_task_overrides(task_id).get("tenki_image") or ""
            )
        except Exception:
            logger.debug("Tenki provider: task overrides unavailable", exc_info=True)

        return TenkiEnvironment(
            image=override_image
            or image
            or str(_setting(settings, "tenki_image", "TERMINAL_TENKI_IMAGE") or ""),
            cwd=self._resolve_cwd(cwd, settings),
            timeout=timeout,
            cpu=cc.get("container_cpu", 1),
            memory=cc.get("container_memory", 5120),
            disk=cc.get("container_disk", 51200),
            persistent_filesystem=self._resolve_persistent(settings),
            task_id=task_id,
            api_endpoint=str(
                _setting(settings, "tenki_api_endpoint", "TERMINAL_TENKI_API_ENDPOINT") or ""
            ),
            workspace_id=str(
                _setting(settings, "tenki_workspace_id", "TERMINAL_TENKI_WORKSPACE_ID") or ""
            ),
            name_prefix=str(
                _setting(settings, "tenki_name_prefix", "TERMINAL_TENKI_NAME_PREFIX")
                or "hermes"
            ),
            allow_inbound=_as_bool(
                _setting(settings, "tenki_allow_inbound", "TERMINAL_TENKI_ALLOW_INBOUND"),
                False,
            ),
            allow_outbound=_as_bool(
                _setting(settings, "tenki_allow_outbound", "TERMINAL_TENKI_ALLOW_OUTBOUND"),
                True,
            ),
            max_duration=_as_int(
                _setting(settings, "tenki_max_duration", "TERMINAL_TENKI_MAX_DURATION"),
                3600,
                setting="tenki_max_duration",
            ),
            idle_timeout=_as_int(
                _setting(settings, "tenki_idle_timeout", "TERMINAL_TENKI_IDLE_TIMEOUT"),
                0,
                setting="tenki_idle_timeout",
            ),
            pause_retention=_as_int(
                _setting(settings, "tenki_pause_retention", "TERMINAL_TENKI_PAUSE_RETENTION"),
                0,
                setting="tenki_pause_retention",
            ),
            sync_hermes_home=_as_bool(
                _setting(
                    settings, "tenki_sync_hermes_home", "TERMINAL_TENKI_SYNC_HERMES_HOME"
                ),
                False,
            ),
            forward_env=_as_list(
                _setting(settings, "tenki_forward_env", "TERMINAL_TENKI_FORWARD_ENV"),
                setting="tenki_forward_env",
            ),
        )

    @staticmethod
    def _resolve_cwd(cwd: str, settings: Dict[str, Any]) -> str:
        """Recover the intended sandbox cwd from core's sanitized value.

        Core has no guest-home hook for plugin backends: any configured cwd
        under ``/home/`` is discarded as a host path and replaced with the
        generic plugin default ``/root``, which does not exist as a working
        directory in a Tenki sandbox. A cwd inside the Tenki guest home is
        recovered from the raw configuration; anything else core let through
        is honored as-is.
        """
        if cwd and cwd != "/root":
            return cwd
        for raw in (
            os.environ.get("TERMINAL_CWD"),
            settings.get("cwd"),
        ):
            raw = str(raw or "").strip()
            if raw == _GUEST_HOME or raw.startswith(_GUEST_HOME + "/"):
                return raw
        return _GUEST_HOME

    @staticmethod
    def _explicit_persistent_setting() -> Optional[bool]:
        """The user's explicit ``container_persistent`` choice, or ``None``.

        Only the key written in config.yaml (raw view — the merged view
        carries core's generic ``true`` default) proves intent. The process
        env var cannot: core's config→env bridge backfills it from
        merged-config defaults, and ``container_config["container_persistent"]``
        arrives pre-filled with the same default. The plugin does not read
        ``.env`` files, so a ``TERMINAL_CONTAINER_PERSISTENT`` there alone
        leaves the terminate-only default in place.
        """
        raw = _raw_terminal_settings()
        if "container_persistent" in raw:
            return _as_bool(raw["container_persistent"], False)
        return None

    @classmethod
    def _resolve_persistent(cls, settings: Dict[str, Any]) -> bool:
        """Terminate-only unless the user explicitly opted into persistence."""
        explicit = cls._explicit_persistent_setting()
        return False if explicit is None else explicit

    @classmethod
    def seed_process_defaults(cls) -> None:
        """Align core's process-env view with this backend's defaults.

        Core gates per-session sandbox isolation on
        ``TERMINAL_CONTAINER_PERSISTENT`` (default ``"true"``), while this
        backend is terminate-only by default — left unreconciled, core would
        collapse ephemeral sessions onto one shared sandbox identity that
        the plugin then terminates, defeating the
        ``session_isolated_when_nonpersistent`` contract. When Tenki is the
        selected backend and the user made no explicit persistence choice,
        export the backend's ``false`` default so core and plugin agree —
        the same normalization the in-core integration performed on the
        merged config defaults.
        """
        try:
            backend = str(
                _raw_terminal_settings().get("backend")
                or os.environ.get("TERMINAL_ENV")
                or ""
            ).strip().lower()
            if backend != "tenki":
                return
            if cls._explicit_persistent_setting() is None:
                os.environ["TERMINAL_CONTAINER_PERSISTENT"] = "false"
        except Exception:
            logger.debug("Tenki provider: default seeding failed", exc_info=True)
