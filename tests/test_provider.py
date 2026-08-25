"""Tests for TenkiProvider — the plugin's registration/classification surface.

The TenkiEnvironment behavior suite lives in test_tenki_environment.py; this
file covers what the provider itself owns: classification flags, settings
resolution (config.yaml vs TERMINAL_TENKI_* env precedence), the sanitized-cwd
recovery, the terminate-only persistence default, the prompt-probe guard, and
real registration + dispatch through hermes-agent's terminal registry.
"""

from __future__ import annotations

import sys
import types

import pytest

from hermes_tenki.provider import TenkiProvider

from test_tenki_environment import _clear_tenki_auth_env, _install_fake_tenki


def _write_terminal_config(tmp_path, monkeypatch, body: str) -> None:
    import os

    home = os.environ["HERMES_HOME"]
    with open(f"{home}/config.yaml", "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        from hermes_cli import config as hermes_config

        hermes_config._LOAD_CONFIG_CACHE.clear()
        hermes_config._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass


class _CaptureEnv:
    created_kwargs: dict = {}

    def __init__(self, **kwargs):
        _CaptureEnv.created_kwargs = kwargs


@pytest.fixture()
def capture_env(monkeypatch):
    import hermes_tenki.tenki_env as tenki_env

    _CaptureEnv.created_kwargs = {}
    monkeypatch.setattr(tenki_env, "TenkiEnvironment", _CaptureEnv)
    return _CaptureEnv


def test_classification_flags():
    provider = TenkiProvider()
    assert provider.name == "tenki"
    assert provider.is_remote is True
    assert provider.is_container is True
    assert provider.session_isolated_when_nonpersistent is True
    assert provider.skip_container_guards is True
    assert provider.cache_path_base == "~/.hermes"
    assert provider.strip_env_keys == frozenset(
        {"TENKI_AUTH_TOKEN", "TENKI_API_KEY"}
    )
    # Forward-looking declarative hints for cores that grow these hooks.
    assert provider.guest_home_root == "/home/tenki"
    assert provider.default_cwd == "/home/tenki"
    assert provider.probe_at_prompt_build is False


def test_prompt_probe_task_id_never_creates_a_sandbox(capture_env):
    provider = TenkiProvider()
    with pytest.raises(RuntimeError, match="defers sandbox creation"):
        provider.create_environment(
            cwd="/root", timeout=60, task_id="prompt-backend-probe"
        )
    assert capture_env.created_kwargs == {}


def test_settings_resolution_config_yaml_wins_over_env(
    capture_env, monkeypatch, tmp_path
):
    _write_terminal_config(
        tmp_path,
        monkeypatch,
        "terminal:\n"
        "  tenki_image: cfg-image\n"
        "  tenki_workspace_id: cfg-workspace\n"
        "  tenki_max_duration: 1234\n"
        "  tenki_forward_env: [FOO, BAR]\n",
    )
    monkeypatch.setenv("TERMINAL_TENKI_IMAGE", "env-image")
    monkeypatch.setenv("TERMINAL_TENKI_WORKSPACE_ID", "env-workspace")
    monkeypatch.setenv("TERMINAL_TENKI_IDLE_TIMEOUT", "300")

    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")

    kwargs = capture_env.created_kwargs
    assert kwargs["image"] == "cfg-image"
    assert kwargs["workspace_id"] == "cfg-workspace"
    assert kwargs["max_duration"] == 1234
    assert kwargs["forward_env"] == ["FOO", "BAR"]
    # Env fills keys config.yaml does not set.
    assert kwargs["idle_timeout"] == 300


def test_env_settings_parse_json_and_bool_strings(capture_env, monkeypatch):
    monkeypatch.setenv("TERMINAL_TENKI_FORWARD_ENV", '["A", "B"]')
    monkeypatch.setenv("TERMINAL_TENKI_ALLOW_OUTBOUND", "false")
    monkeypatch.setenv("TERMINAL_TENKI_SYNC_HERMES_HOME", "true")

    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")

    kwargs = capture_env.created_kwargs
    assert kwargs["forward_env"] == ["A", "B"]
    assert kwargs["allow_outbound"] is False
    assert kwargs["sync_hermes_home"] is True


def test_container_resources_come_from_container_config(capture_env):
    TenkiProvider().create_environment(
        cwd="/root",
        timeout=60,
        task_id="t",
        container_config={
            "container_cpu": 4,
            "container_memory": 8192,
            "container_disk": 20480,
        },
    )
    kwargs = capture_env.created_kwargs
    assert (kwargs["cpu"], kwargs["memory"], kwargs["disk"]) == (4, 8192, 20480)


def test_sanitized_root_cwd_recovers_guest_home_subpath(capture_env, monkeypatch):
    # Core discards /home/* cwds for plugin backends and passes its generic
    # "/root" default; the raw TERMINAL_CWD still holds the user's intent.
    monkeypatch.setenv("TERMINAL_CWD", "/home/tenki/project")
    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")
    assert capture_env.created_kwargs["cwd"] == "/home/tenki/project"


def test_sanitized_root_cwd_never_adopts_host_paths(capture_env, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", "/Users/someone/project")
    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")
    assert capture_env.created_kwargs["cwd"] == "/home/tenki"


def test_explicit_non_root_cwd_is_honored(capture_env):
    TenkiProvider().create_environment(
        cwd="/home/tenki/explicit", timeout=60, task_id="t"
    )
    assert capture_env.created_kwargs["cwd"] == "/home/tenki/explicit"


def test_persistence_defaults_to_terminate_only(capture_env):
    # container_config carries core's generic default of True; the provider
    # must not treat that as an explicit user choice.
    TenkiProvider().create_environment(
        cwd="/root",
        timeout=60,
        task_id="t",
        container_config={"container_persistent": True},
    )
    assert capture_env.created_kwargs["persistent_filesystem"] is False


def test_persistence_ignores_bridge_backfilled_process_env(capture_env, monkeypatch):
    # Core's config→env bridge backfills TERMINAL_CONTAINER_PERSISTENT="True"
    # from the merged-config default; a process-env value therefore cannot
    # prove user intent and must not flip the terminate-only default.
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "True")
    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")
    assert capture_env.created_kwargs["persistent_filesystem"] is False


def test_persistence_honors_explicit_config(capture_env, monkeypatch, tmp_path):
    _write_terminal_config(
        tmp_path, monkeypatch, "terminal:\n  container_persistent: true\n"
    )
    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")
    assert capture_env.created_kwargs["persistent_filesystem"] is True


def test_persistence_honors_explicit_dotenv(capture_env, monkeypatch):
    import os

    with open(f"{os.environ['HERMES_HOME']}/.env", "w", encoding="utf-8") as fh:
        fh.write("TERMINAL_CONTAINER_PERSISTENT=true\n")
    TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")
    assert capture_env.created_kwargs["persistent_filesystem"] is True


def test_seed_process_defaults_exports_terminate_only(monkeypatch, tmp_path):
    # With backend=tenki selected and no explicit persistence choice, the
    # load-time seed must align core's session-isolation gate (which reads
    # the process env var) with the plugin's terminate-only default — even
    # over a bridge-backfilled "True".
    _write_terminal_config(tmp_path, monkeypatch, "terminal:\n  backend: tenki\n")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "True")
    TenkiProvider.seed_process_defaults()
    import os

    assert os.environ["TERMINAL_CONTAINER_PERSISTENT"] == "false"


def test_seed_process_defaults_respects_explicit_choice(monkeypatch, tmp_path):
    _write_terminal_config(
        tmp_path,
        monkeypatch,
        "terminal:\n  backend: tenki\n  container_persistent: true\n",
    )
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "True")
    TenkiProvider.seed_process_defaults()
    import os

    assert os.environ["TERMINAL_CONTAINER_PERSISTENT"] == "True"


def test_seed_process_defaults_leaves_other_backends_alone(monkeypatch, tmp_path):
    _write_terminal_config(tmp_path, monkeypatch, "terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "True")
    TenkiProvider.seed_process_defaults()
    import os

    assert os.environ["TERMINAL_CONTAINER_PERSISTENT"] == "True"


def test_invalid_numeric_setting_fails_loud(capture_env, monkeypatch):
    monkeypatch.setenv("TERMINAL_TENKI_MAX_DURATION", "not-a-number")
    with pytest.raises(ValueError, match="tenki_max_duration"):
        TenkiProvider().create_environment(cwd="/root", timeout=60, task_id="t")


def test_per_task_image_override_reaches_the_factory(capture_env, monkeypatch):
    from tools import terminal_tool

    terminal_tool.register_task_env_overrides("task-img", {"tenki_image": "rl:1"})
    try:
        TenkiProvider().create_environment(
            cwd="/root", timeout=60, task_id="task-img", image="core-image"
        )
    finally:
        terminal_tool.clear_task_env_overrides("task-img")
    assert capture_env.created_kwargs["image"] == "rl:1"


def test_probe_reports_sdk_and_auth_readiness(monkeypatch, tmp_path):
    provider = TenkiProvider()
    _clear_tenki_auth_env(monkeypatch)

    for name in [m for m in sys.modules if m == "tenki" or m.startswith("tenki.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    import importlib.util as _ilu

    real_find_spec = _ilu.find_spec
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name, *a, **k: None
        if name == "tenki"
        else real_find_spec(name, *a, **k),
    )
    status, detail = provider.probe()
    assert status == "needs_setup"
    assert "SDK" in detail

    monkeypatch.setitem(sys.modules, "tenki", types.ModuleType("tenki"))
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    status, detail = provider.probe()
    assert status == "needs_setup"
    assert "credentials" in detail

    monkeypatch.setenv("TENKI_AUTH_TOKEN", "tok")
    assert provider.probe() == ("ready", "")


def test_doctor_checks_rows(monkeypatch, tmp_path):
    provider = TenkiProvider()
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "tenki", types.ModuleType("tenki"))
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("TENKI_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TERMINAL_TENKI_WORKSPACE_ID", "ws-1")

    rows = provider.doctor_checks()
    by_label = {label: (ok, detail) for ok, label, detail in rows}
    assert by_label["Tenki auth"][0] is True
    assert by_label["Tenki workspace"][0] is True
    assert by_label["tenki SDK"][0] is True


def test_registry_registration_and_terminal_dispatch(monkeypatch, tmp_path):
    """End to end on a stock core: register → classify → dispatch → stamp."""
    from agent import terminal_env_registry as registry
    from tools import terminal_tool

    _install_fake_tenki(monkeypatch)
    _clear_tenki_auth_env(monkeypatch)
    monkeypatch.setenv("TENKI_CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        "auth_token: tok-secret\n", encoding="utf-8"
    )

    provider = TenkiProvider()
    registry.register_provider(provider)
    try:
        assert registry.get_provider("tenki") is provider
        assert "tenki" in registry.plugin_backend_names()
        assert registry.provider_flag("tenki", "is_container", False) is True
        assert terminal_tool._is_container_backend("tenki") is True
        assert {"TENKI_AUTH_TOKEN", "TENKI_API_KEY"} <= set(
            registry.plugin_strip_env_keys()
        )

        import hermes_tenki.tenki_env as tenki_env

        monkeypatch.setattr(
            tenki_env.TenkiEnvironment, "init_session", lambda self: None
        )
        env = terminal_tool._create_environment(
            env_type="tenki",
            image="",
            cwd="/root",
            timeout=60,
            container_config={"container_cpu": 1},
            task_id="dispatch-e2e",
        )
        try:
            assert isinstance(env, tenki_env.TenkiEnvironment)
            assert getattr(env, "_hermes_backend_name", "") == "tenki"
            assert env.cwd.startswith("/home/tenki")
        finally:
            env._release_task_ownership()
    finally:
        registry.restore_registration("tenki", provider, None)


def test_registered_name_does_not_collide_with_builtins():
    from agent.terminal_env_registry import BUILTIN_BACKEND_NAMES

    assert TenkiProvider().name not in BUILTIN_BACKEND_NAMES
