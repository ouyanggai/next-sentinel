# Next Sentinel

[中文](README.md)

macOS menu bar tool that lets long-running Codex tasks auto-advance through implementation, review, fixes, and release — while reducing idle token costs.

## Why It Exists

Long Codex tasks hit two practical problems:

1. **Token waste** — Heartbeat automations restore the session, read context, and decide on every tick. The longer the task, the higher the idle cost.
2. **Misrouted steps** — Without a protocol, automation may keep implementing when review is needed, or publish when bugs remain.

Next Sentinel's approach: hooks handle the main loop, automation only covers restarts. Daily progression is event-driven via `SessionStart` and `Stop` hooks — no polling.

## Features

- macOS menu bar app showing `NEXT`
- Watches Codex App launch (`com.openai.codex`), triggers fallback after 60 seconds
- Menu shows hook status, automation status, schedule, recent actions
- Manual start/stop NEXT, trigger fallback, open logs
- Detects target-session `usage_limit_exceeded`, shows target quota blocking, and lets you retry once after switching accounts
- Complete hook scripts: `next_session_start.py`, `next_stop_router.py`, `next_ctl.py`
- Install scripts, Codex restart script, config examples, test suite

## Requirements

- macOS 13+
- Xcode Command Line Tools (`swiftc`, `iconutil`)
- Python 3
- Pillow (icon rendering)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/ouyanggai/next-sentinel.git
cd next-sentinel
```

### 2. Install hook scripts

```bash
./scripts/install-hooks.sh
```

Copies `next_ctl.py`, `next_session_start.py`, `next_stop_router.py` to `~/.codex/hooks/`.

Custom Codex directory:

```bash
CODEX_HOME="$HOME/.codex" ./scripts/install-hooks.sh
```

### 3. Configure Codex hooks

Merge `examples/config.toml.snippet` into `~/.codex/config.toml`:

```toml
[features]
codex_hooks = true

[[hooks.SessionStart]]
matcher = "startup|resume|clear"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python3 ~/.codex/hooks/next_session_start.py"
timeout = 10
statusMessage = "Loading NEXT protocol"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "python3 ~/.codex/hooks/next_stop_router.py"
timeout = 30
statusMessage = "Routing NEXT"
```

### 4. Configure target sessions

Edit `~/.codex/hooks/next_router_config.json`:

```json
{
  "target_sessions": ["replace-with-target-session-id"],
  "target_cwds": ["/absolute/path/to/your/project"],
  "max_auto_continuations": 24,
  "skill_root": "~/.codex/skills"
}
```

`target_sessions` pins to a session, `target_cwds` pins to a project directory. Either match enables NEXT routing.

### 5. Configure automation-2

```bash
mkdir -p ~/.codex/automations/automation-2
```

Use `examples/automation-2.toml` as template for `~/.codex/automations/automation-2/automation.toml`.

Key: `status` defaults to `PAUSED`, returns to `PAUSED` after fallback completes.

### 6. Build the menu bar app

```bash
python3 -m pip install pillow
./build.sh
```

Launch:

```bash
open "$HOME/Applications/Next Sentinel.app"
```

Install elsewhere:

```bash
NEXT_SENTINEL_INSTALL_DIR="/Applications" ./build.sh
```

## Usage

### Commands

```bash
# Check status
python3 ~/.codex/hooks/next_ctl.py status

# Enable NEXT
python3 ~/.codex/hooks/next_ctl.py start

# Pause NEXT
python3 ~/.codex/hooks/next_ctl.py stop

# Trigger fallback
python3 ~/.codex/hooks/next_ctl.py trigger
```

If the target session was last stopped by a quota error, `status` prints `target_status: QUOTA_BLOCKED`. After switching accounts or waiting for quota recovery, run `trigger` once to retry. This does not re-enable minute polling; the automation still returns to `PAUSED`.

### NEXT Protocol

Agent writes a marker at end of each turn:

```text
NEXT: continue   → sends plain text "继续"
NEXT: implement  → sends incremental-implementation + test-driven-development
NEXT: fix        → sends incremental-implementation + test-driven-development
NEXT: review     → sends code-review-and-quality
NEXT: ship       → sends shipping-and-launch
NEXT: stop       → sends nothing
```

Skills from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills). Next Sentinel connects them to the Codex session lifecycle via the `NEXT:` protocol.

## How It Works

```text
Codex session starts/resumes
      ↓
SessionStart hook injects NEXT protocol
      ↓
Agent runs task, writes NEXT marker at end
      ↓
Stop hook reads last explicit NEXT marker
      ↓
Router sends plain text or skill link
      ↓
Next turn continues under matching skill
```

Restart fallback:

```text
Cockpit Tools switches account (optional)
      ↓
Codex App restarts
      ↓
Next Sentinel detects launch
      ↓
Wait 60 seconds
      ↓
next_ctl.py trigger schedules automation-2 once
      ↓
automation-2 resumes target session, routes by NEXT marker
      ↓
automation-2 returns to PAUSED
```

See [docs/usage-associations.md](docs/usage-associations.md).

## Project Structure

```text
next-sentinel/
├── Assets/           # Menu bar and app icon assets
├── Sources/          # AppKit menu bar app
├── hooks/            # Codex hooks and control scripts
├── scripts/          # Hook installer and Codex restart helper
├── examples/         # Config, automation, cockpit examples
├── docs/             # Usage associations and workflow notes
├── test/             # Hook routing and automation tests
├── build.sh
└── README.md
```

Build output goes to `build/` and `dist/`, not tracked by Git.

## Status Output

`next_ctl.py status` example:

```text
NEXT hooks: ACTIVE
codex_hooks feature: True
SessionStart hook: True
Stop hook: True
automation-2 toml: PAUSED
automation-2 db: PAUSED
target_status: COMPLETE session=019dd35c-44dc-7f21-a513-46d07b3b10b1
```

- `NEXT hooks: ACTIVE` — routing enabled
- `NEXT hooks: STOPPED` — paused via `NEXT_ROUTER_DISABLED`
- `automation-2 db: PAUSED` — fallback idle
- `automation-2 db: ACTIVE` — waiting for trigger execution
- `target_status: QUOTA_BLOCKED` — target session was blocked by quota; switch accounts or wait for quota recovery, then trigger once
- `target_status: RUNNING` — a newer target turn has started and has not completed yet
- `target_status: COMPLETE` — the latest target turn has a completion record

## Tests

```bash
python3 -m unittest discover -s test -p 'test_*.py'
```

Coverage:
- `NEXT:` marker with trailing context still routes correctly
- Multiple `NEXT:` markers use the last explicit one
- Protocol description lines not misidentified
- Implement/fix routes send both skill links
- `automation-2` returns to `PAUSED` after one-shot trigger
- Quota errors stay visible as `QUOTA_BLOCKED` even when Codex records a `task_complete` event afterward
- A new `task_started` after a quota error recovers the target status to `RUNNING`

## Cockpit Account Switching

Cockpit Tools is external. This repo does not call private APIs or store real auth data.

Example state file: `examples/cockpit_codex_auth.example.json`

Restart Codex after switching:

```bash
./scripts/restart-codex.sh
```

## Security

- Do not commit `~/.codex/.cockpit_codex_auth.json`
- Do not commit Codex `auth.json`, sqlite databases, session state, or logs
- Values in `examples/` are placeholders
- Hook scripts support `CODEX_HOME`, `NEXT_HOOKS_DIR`, `NEXT_SKILL_ROOT` env vars

## Credits

TDD, incremental implementation, review, and shipping workflows from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills). Local skill ecosystem compatible with Jim Liu's [baoyu-skills](https://github.com/jimliu/baoyu-skills).

## License

MIT
