# hermes-tenki

[Tenki](https://tenki.cloud) cloud sandbox terminal backend for
[hermes-agent](https://github.com/NousResearch/hermes-agent), packaged as a
standalone plugin. Registers `tenki` as a `terminal.backend` value through
hermes-agent's terminal-environment plugin registry, so the agent's
`terminal`, `execute_code`, and file tools all run inside an on-demand Tenki
cloud sandbox.

Sandboxes are **created on demand and terminated by default** (opt into
persistence explicitly). The backend carries crash-safe create-attempt
journaling — the Tenki API has no create idempotency key, so the plugin
records every create attempt durably before the RPC and reconciles uncertain
outcomes on the next startup rather than leaking or double-creating billable
sandboxes.

## Install

```bash
hermes plugins install hashbender/hermes-tenki
hermes plugins enable tenki
hermes config set terminal.backend tenki
```

Or run the wizard: `hermes setup terminal` and pick **Tenki**.

The installer's security scan will show a *caution* summary and ask for
confirmation — expected for a sandbox backend: the code legitimately
rewrites `sudo` invocations and drives the sandbox's remote `exec` API.

Auth (any one of):

- `tenki login` (uses the Tenki CLI's config)
- `TENKI_AUTH_TOKEN` or `TENKI_API_KEY` in the environment or `~/.hermes/.env`

The `tenki` Python SDK (`tenki>=1.4.1,<2`) is installed on demand through
hermes-agent's lazy-install pipeline, or install it yourself with
`pip install 'tenki>=1.4.1,<2'`.

## Configuration

All settings live under `terminal:` in `config.yaml`. Each also has an
environment-variable form (useful for `~/.hermes/.env`); config.yaml wins
when both are set.

| config.yaml key | env var | default | meaning |
|---|---|---|---|
| `tenki_image` | `TERMINAL_TENKI_IMAGE` | Tenki workspace default | sandbox image/template |
| `tenki_api_endpoint` | `TERMINAL_TENKI_API_ENDPOINT` | `https://api.tenki.cloud` | API endpoint |
| `tenki_workspace_id` | `TERMINAL_TENKI_WORKSPACE_ID` | from `tenki login` | workspace |
| `tenki_name_prefix` | `TERMINAL_TENKI_NAME_PREFIX` | `hermes` | sandbox name prefix |
| `tenki_allow_inbound` | `TERMINAL_TENKI_ALLOW_INBOUND` | `false` | allow inbound network |
| `tenki_allow_outbound` | `TERMINAL_TENKI_ALLOW_OUTBOUND` | `true` | allow outbound network |
| `tenki_max_duration` | `TERMINAL_TENKI_MAX_DURATION` | `3600` | sandbox max lifetime (s) |
| `tenki_idle_timeout` | `TERMINAL_TENKI_IDLE_TIMEOUT` | `0` (off) | idle auto-pause (s) |
| `tenki_pause_retention` | `TERMINAL_TENKI_PAUSE_RETENTION` | `0` (off) | paused-sandbox retention (s) |
| `tenki_sync_hermes_home` | `TERMINAL_TENKI_SYNC_HERMES_HOME` | `false` | mirror `~/.hermes` into the sandbox |
| `tenki_forward_env` | `TERMINAL_TENKI_FORWARD_ENV` | `[]` | env var names forwarded into the sandbox |
| `container_cpu` / `container_memory` / `container_disk` | `TERMINAL_CONTAINER_*` | 1 / 5120 MB / 51200 MB | sandbox resources |
| `container_persistent` | `TERMINAL_CONTAINER_PERSISTENT` | **`false`** | snapshot + reuse the sandbox across sessions |
| `cwd` | `TERMINAL_CWD` | `/home/tenki` | working directory (guest paths under `/home/tenki` only) |

Notes:

- **Terminate-only by default.** Unlike container backends, an unset
  `container_persistent` means the sandbox is terminated at session end
  (after a durable snapshot when persistence *is* enabled). Set
  `terminal.container_persistent: true` in config.yaml (or
  `TERMINAL_CONTAINER_PERSISTENT=true` in `~/.hermes/.env`) explicitly to
  keep state. A plain shell-exported variable is deliberately ignored for
  this one key: core's config bridge backfills it from generic defaults, so
  a process-env value can't prove user intent.
- **Credentials never enter the sandbox** unless you opt in: the control
  plane token (`TENKI_AUTH_TOKEN`/`TENKI_API_KEY`) is stripped from every
  agent subprocess and is only forwarded into the guest if you list it in
  `tenki_forward_env` yourself.
- `hermes config set terminal.tenki_image ...` prints an "unknown key"
  warning on current hermes-agent (core's schema doesn't know plugin keys);
  the value is still written and honored.
- **When uninstalling or disabling the plugin**, also remove
  `TENKI_AUTH_TOKEN`/`TENKI_API_KEY` from `~/.hermes/.env`: the subprocess
  secret-stripping the plugin registers only applies while it is loaded, so
  a leftover token would become readable by model-authored shell commands.

## Core version compatibility

The plugin targets hermes-agent ≥ the pluggable-terminal-backends release
(PR #94400). A few niceties depend on small core hooks that don't exist yet;
the plugin degrades gracefully without them:

- **Working-directory handling**: core's cwd sanitizer has no notion of a
  plugin backend's guest home, so it rewrites `/home/tenki/...` cwds to its
  generic `/root` default. The plugin recovers the intended path itself
  (config/`TERMINAL_CWD` values under `/home/tenki` are honored; `cd` state
  is tracked in-environment). The provider declares `guest_home_root` /
  `default_cwd` for cores that grow the hook.
- **Prompt-build probing**: core live-probes remote backends when building
  the system prompt; the plugin refuses that probe (a Tenki sandbox is a
  billable resource) and the prompt falls back to a static description. The
  provider declares `probe_at_prompt_build = False` for cores that honor it.
- **Dangerous-command approval skipping**: the provider declares
  `skip_container_guards`, which current core does not consult for plugin
  backends yet — commands go through the standard approval flow until it
  does.

## Development

Tests run against a stock hermes-agent checkout (no Tenki credentials or
network needed — the SDK is faked; one contract test activates when the real
SDK is installed and still makes no network calls):

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent && uv sync --extra dev && cd -
uv pip install --python hermes-agent/.venv/bin/python 'tenki>=1.4.1,<2'
hermes-agent/.venv/bin/python -m pytest tests/
```

Layout: the repo root is the plugin directory (`plugin.yaml` +
`__init__.py`). `tenki_env.py` is the `TenkiEnvironment` backend,
`provider.py` the registry provider, `tenki_config.py` auth/endpoint
resolution, and `_compat.py` / `_file_sync.py` vendored helpers that are
candidates to move into hermes-agent core.

## License

MIT
