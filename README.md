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

Auth: set `TENKI_API_KEY` in `~/.hermes/.env` (the installer prompts for
it). If you already use the Tenki CLI, `tenki auth token` prints a key you can
paste. `TENKI_AUTH_TOKEN` is accepted as an alias when `TENKI_API_KEY` is
unset. The plugin reads these only through Hermes's secret scope, so each
profile uses its own key; it never reads the Tenki CLI's login file.

Optional: `TENKI_WORKSPACE_ID` (or `terminal.tenki_workspace_id`) picks a
workspace; without it the API key's own workspace is used.
`TENKI_API_ENDPOINT` overrides the API endpoint.

> **Breaking change in 1.1.0.** Earlier versions also picked up a `tenki
> login` session (and its workspace and endpoint) from
> `~/.config/tenki/config.yaml`, and read `TERMINAL_CONTAINER_PERSISTENT`
> straight from `~/.hermes/.env`. Neither happens any more (Hermes plugin
> catalog rule 11). If you relied on `tenki login`, run
> `tenki auth token` and put the result in `~/.hermes/.env` as
> `TENKI_API_KEY`; if you set persistence in `.env`, move it to
> `terminal.container_persistent: true` in config.yaml.

The `tenki` Python SDK (`tenki>=1.4.1,<2`) is installed on demand through
hermes-agent's lazy-install pipeline, or install it yourself with
`pip install 'tenki>=1.4.1,<2'`.

## Configuration

All settings live under `terminal:` in `config.yaml`. Each also has an
environment-variable form (useful for `~/.hermes/.env`, which Hermes loads
into the environment); config.yaml wins when both are set.

| config.yaml key | env var | default | meaning |
|---|---|---|---|
| `tenki_image` | `TERMINAL_TENKI_IMAGE` | Tenki workspace default | sandbox image/template |
| `tenki_api_endpoint` | `TERMINAL_TENKI_API_ENDPOINT` | `TENKI_API_ENDPOINT`, else `https://api.tenki.cloud` | API endpoint |
| `tenki_workspace_id` | `TERMINAL_TENKI_WORKSPACE_ID` | `TENKI_WORKSPACE_ID`, else the API key's workspace | workspace |
| `tenki_name_prefix` | `TERMINAL_TENKI_NAME_PREFIX` | `hermes` | sandbox name prefix |
| `tenki_allow_inbound` | `TERMINAL_TENKI_ALLOW_INBOUND` | `false` | allow inbound network |
| `tenki_allow_outbound` | `TERMINAL_TENKI_ALLOW_OUTBOUND` | `true` | allow outbound network |
| `tenki_max_duration` | `TERMINAL_TENKI_MAX_DURATION` | `3600` | sandbox max lifetime (s) |
| `tenki_idle_timeout` | `TERMINAL_TENKI_IDLE_TIMEOUT` | `0` (off) | idle auto-pause (s) |
| `tenki_pause_retention` | `TERMINAL_TENKI_PAUSE_RETENTION` | `0` (off) | paused-sandbox retention (s) |
| `tenki_sync_hermes_home` | `TERMINAL_TENKI_SYNC_HERMES_HOME` | `false` | mirror `~/.hermes` into the sandbox |
| `tenki_forward_env` | `TERMINAL_TENKI_FORWARD_ENV` | `[]` | env var names forwarded into the sandbox |
| `container_cpu` / `container_memory` / `container_disk` | `TERMINAL_CONTAINER_*` | 1 / 5120 MB / 51200 MB | sandbox resources |
| `container_persistent` | (config.yaml only) | **`false`** | snapshot + reuse the sandbox across sessions |
| `cwd` | `TERMINAL_CWD` | `/home/tenki` | working directory (guest paths under `/home/tenki` only) |

Notes:

- **Terminate-only by default.** Unlike container backends, an unset
  `container_persistent` means the sandbox is terminated at session end
  (after a durable snapshot when persistence *is* enabled). Set
  `terminal.container_persistent: true` in config.yaml explicitly to keep
  state. `TERMINAL_CONTAINER_PERSISTENT` (shell or `.env`) is deliberately
  ignored for this one key: core's config bridge backfills it from generic
  defaults, so an environment value can't prove user intent.
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

## What the plugin does on your machine

- **Network:** calls the Tenki API (`https://api.tenki.cloud`, or your
  `TENKI_API_ENDPOINT`) to create, run commands in, snapshot, pause and
  terminate sandboxes. Sandboxes are billed to the account that owns the API
  key. No telemetry.
- **Credentials:** reads `TENKI_API_KEY`/`TENKI_AUTH_TOKEN` through Hermes's
  secret scope only. `hermes setup terminal` offers to save a pasted key to
  `~/.hermes/.env` through Hermes's own `save_env_value`.
- **Files:** writes its state under `HERMES_HOME`: `tenki_snapshots.json`
  (snapshot pointers and create-attempt journal) and per-task lock files in
  `locks/tenki/`. With `tenki_sync_hermes_home` or
  `terminal.credential_files` it uploads those files into the sandbox.
- **Process environment:** when `terminal.backend` is `tenki` and
  `terminal.container_persistent` is not set in config.yaml, it sets
  `TERMINAL_CONTAINER_PERSISTENT=false` in the Hermes process at load so
  core's session isolation matches the terminate-only default.
- **Shell:** commands run in the remote sandbox, not on the host. When the
  sandbox lacks passwordless sudo, `sudo` is rewritten to `sudo -n` there;
  `SUDO_PASSWORD` is never sent to the sandbox.
- **Signals:** while syncing files back from the sandbox on the main thread
  it briefly defers `SIGINT` and restores the previous handler afterwards.

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
cd hermes-agent && uv sync --group dev && cd -
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
