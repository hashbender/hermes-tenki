"""Test harness for the hermes-tenki plugin.

Loads the plugin package as ``hermes_tenki`` (the repo root is the plugin
directory, so it is not importable by its on-disk name) and replicates the
hermetic-environment guarantees the hermes-agent test suite provides via its
own conftest: no real credentials, no real ``~/.hermes``, deterministic
terminal configuration.

Run with the hermes-agent virtualenv active (the plugin imports hermes-agent
core modules): ``uv run --project <hermes-agent> pytest`` from this repo, or
plain ``pytest`` in an env with hermes-agent installed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin_package() -> None:
    if "hermes_tenki" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "hermes_tenki",
        _REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(_REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_tenki"] = module
    spec.loader.exec_module(module)


_load_plugin_package()


#: Credential and behavior env vars that must never leak from the developer's
#: (or CI runner's) real environment into a test.
_SCRUBBED_ENV_VARS = (
    "TENKI_AUTH_TOKEN",
    "TENKI_API_KEY",
    "TENKI_CONFIG_PATH",
    "TENKI_WORKSPACE_ID",
    "TENKI_WORKSPACE",
    "TENKI_API_ENDPOINT",
    "TENKI_API_URL",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "SUDO_PASSWORD",
    "TERMINAL_ENV",
    "TERMINAL_CWD",
    "TERMINAL_CONTAINER_PERSISTENT",
    "TERMINAL_TENKI_IMAGE",
    "TERMINAL_TENKI_API_ENDPOINT",
    "TERMINAL_TENKI_WORKSPACE_ID",
    "TERMINAL_TENKI_NAME_PREFIX",
    "TERMINAL_TENKI_ALLOW_INBOUND",
    "TERMINAL_TENKI_ALLOW_OUTBOUND",
    "TERMINAL_TENKI_MAX_DURATION",
    "TERMINAL_TENKI_IDLE_TIMEOUT",
    "TERMINAL_TENKI_PAUSE_RETENTION",
    "TERMINAL_TENKI_SYNC_HERMES_HOME",
    "TERMINAL_TENKI_FORWARD_ENV",
    "HERMES_PROFILE",
)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch, tmp_path):
    """Mirror hermes-agent's hermetic test env for this plugin's suite.

    ``HERMES_HOME`` is redirected to a per-test tmpdir because the Tenki
    backend persists durable state under it (snapshot/journal store, task
    ownership locks) — without the redirect a test would write into the real
    ``~/.hermes``.
    """
    for name in _SCRUBBED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Pin (not just delete) the Tenki CLI config path: unset, it falls back
    # to the developer's real ~/.config/tenki/config.yaml, so a test that
    # forgets to set it would read a real `tenki login` credential and pass
    # locally while failing in CI.
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "absent-tenki-cli.yaml"))
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    # The provider reads config.yaml through hermes-agent's cached loader;
    # drop the cache so one test's HERMES_HOME never bleeds into the next.
    try:
        from hermes_cli import config as hermes_config

        hermes_config._LOAD_CONFIG_CACHE.clear()
        hermes_config._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass
    yield
