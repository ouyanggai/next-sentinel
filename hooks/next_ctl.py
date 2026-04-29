#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
HOOKS_DIR = Path(os.environ.get("NEXT_HOOKS_DIR", CODEX_HOME / "hooks")).expanduser()
AUTOMATION_ID = os.environ.get("NEXT_AUTOMATION_ID", "automation-2")

CONFIG_PATH = Path(os.environ.get("NEXT_CODEX_CONFIG", CODEX_HOME / "config.toml")).expanduser()
AUTOMATION_PATH = Path(
    os.environ.get("NEXT_AUTOMATION_TOML", CODEX_HOME / "automations" / AUTOMATION_ID / "automation.toml")
).expanduser()
AUTOMATION_DB_PATH = Path(os.environ.get("NEXT_AUTOMATION_DB", CODEX_HOME / "sqlite" / "codex-dev.db")).expanduser()
ROUTER_CONFIG_PATH = Path(os.environ.get("NEXT_ROUTER_CONFIG", HOOKS_DIR / "next_router_config.json")).expanduser()
DISABLED_PATH = Path(os.environ.get("NEXT_ROUTER_DISABLED", HOOKS_DIR / "NEXT_ROUTER_DISABLED")).expanduser()
STATE_DIR = Path(os.environ.get("NEXT_ROUTER_STATE_DIR", HOOKS_DIR / ".next-router-state")).expanduser()
LOG_PATH = Path(os.environ.get("NEXT_ROUTER_LOG", HOOKS_DIR / "next_router.log")).expanduser()
SESSION_ROOT = Path(os.environ.get("NEXT_SESSION_ROOT", CODEX_HOME / "sessions")).expanduser()
ONE_SHOT_WATCH_SECONDS = int(os.environ.get("NEXT_ONE_SHOT_WATCH_SECONDS", "180"))
SESSION_TAIL_BYTES = int(os.environ.get("NEXT_SESSION_TAIL_BYTES", str(1024 * 1024)))
QUOTA_ERROR_CODE = "usage_limit_exceeded"


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def replace_toml_string(text, key, value):
    pattern = rf'(?m)^{re.escape(key)}\s*=\s*".*"$'
    replacement = f'{key} = "{value}"'
    if re.search(pattern, text):
        return re.sub(pattern, replacement, text, count=1)
    return text.rstrip() + "\n" + replacement + "\n"


def set_automation_status(status, next_run_at=None):
    text = read_text(AUTOMATION_PATH)
    changed = False
    if text:
        write_text(AUTOMATION_PATH, replace_toml_string(text, "status", status))
        changed = True
    if AUTOMATION_DB_PATH.exists():
        updated_at = int(time.time() * 1000)
        with sqlite3.connect(AUTOMATION_DB_PATH) as conn:
            if status == "PAUSED":
                conn.execute(
                    "update automations set status = ?, next_run_at = null, updated_at = ? where id = ?",
                    (status, updated_at, AUTOMATION_ID),
                )
            elif next_run_at is None:
                conn.execute(
                    "update automations set status = ?, updated_at = ? where id = ?",
                    (status, updated_at, AUTOMATION_ID),
                )
            else:
                conn.execute(
                    "update automations set status = ?, next_run_at = ?, updated_at = ? where id = ?",
                    (status, next_run_at, updated_at, AUTOMATION_ID),
                )
        changed = True
    return changed


def schedule_automation_now(start_watcher=True):
    now_ms = int(time.time() * 1000)
    text = read_text(AUTOMATION_PATH)
    changed = False
    if text:
        write_text(AUTOMATION_PATH, replace_toml_string(text, "status", "ACTIVE"))
        changed = True
    if AUTOMATION_DB_PATH.exists():
        with sqlite3.connect(AUTOMATION_DB_PATH) as conn:
            conn.execute(
                "update automations set status = ?, next_run_at = ?, updated_at = ? where id = ?",
                ("ACTIVE", now_ms, now_ms, AUTOMATION_ID),
            )
        changed = True
    if changed and start_watcher:
        start_one_shot_watcher(now_ms)
    return changed, now_ms


def start_one_shot_watcher(triggered_at_ms):
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "finish-trigger", str(triggered_at_ms)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def finish_one_shot_trigger(triggered_at_ms, wait_seconds=ONE_SHOT_WATCH_SECONDS, poll_interval=2):
    deadline = time.time() + wait_seconds
    while True:
        automation = get_automation_db_status()
        last_run_at = (automation or {}).get("last_run_at")
        if last_run_at and int(last_run_at) >= int(triggered_at_ms):
            break
        if time.time() >= deadline:
            break
        time.sleep(poll_interval)
    return set_automation_status("PAUSED")


def get_automation_db_status():
    if not AUTOMATION_DB_PATH.exists():
        return None
    try:
        with sqlite3.connect(AUTOMATION_DB_PATH) as conn:
            row = conn.execute(
                "select status, rrule, next_run_at, last_run_at from automations where id = ?",
                (AUTOMATION_ID,),
            ).fetchone()
        if not row:
            return None
        return {"status": row[0], "rrule": row[1], "next_run_at": row[2], "last_run_at": row[3]}
    except Exception:
        return None


def get_toml_bool(text, key):
    match = re.search(rf'(?m)^{re.escape(key)}\s*=\s*(true|false)\s*$', text)
    if not match:
        return None
    return match.group(1) == "true"


def get_toml_string(text, key):
    match = re.search(rf'(?m)^{re.escape(key)}\s*=\s*"([^"]*)"\s*$', text)
    if not match:
        return None
    return match.group(1)


def load_router_config():
    try:
        return json.loads(ROUTER_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def state_files():
    if not STATE_DIR.exists():
        return []
    return sorted(STATE_DIR.glob("*.json"))


def session_log_candidates(session_id):
    if not session_id or not SESSION_ROOT.exists():
        return []
    return sorted(
        SESSION_ROOT.glob(f"**/*{session_id}.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def read_tail(path, max_bytes=SESSION_TAIL_BYTES):
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_session_tail(path):
    latest = {
        "task_started": None,
        "task_complete": None,
        "quota_error": None,
        "quota_message": None,
        "path": str(path),
    }
    for line in read_tail(path).splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        timestamp = event.get("timestamp")
        payload = event.get("payload") or {}
        if event.get("type") != "event_msg":
            continue
        event_type = payload.get("type")
        if event_type == "task_started":
            latest["task_started"] = timestamp
        elif event_type == "task_complete":
            latest["task_complete"] = timestamp
        elif event_type == "error" and payload.get("codex_error_info") == QUOTA_ERROR_CODE:
            latest["quota_error"] = timestamp
            latest["quota_message"] = payload.get("message") or ""
    return latest


def retry_hint(message):
    if not message:
        return ""
    match = re.search(r"try again at ([^.]+)", message)
    return match.group(1).strip() if match else ""


def target_session_status(router_config=None):
    router_config = router_config or load_router_config()
    for session_id in router_config.get("target_sessions") or []:
        candidates = session_log_candidates(session_id)
        if not candidates:
            continue
        latest = parse_session_tail(candidates[0])
        latest["session_id"] = session_id
        quota_error = latest.get("quota_error")
        task_started = latest.get("task_started")
        task_complete = latest.get("task_complete")
        if quota_error and (not task_started or quota_error >= task_started):
            latest["status"] = "QUOTA_BLOCKED"
            latest["retry_hint"] = retry_hint(latest.get("quota_message"))
            return latest
        if task_started and (not task_complete or task_started > task_complete):
            latest["status"] = "RUNNING"
            return latest
        if task_complete:
            latest["status"] = "COMPLETE"
            return latest
        latest["status"] = "UNKNOWN"
        return latest
    return {"status": "UNKNOWN"}


def print_status():
    config = read_text(CONFIG_PATH)
    automation = read_text(AUTOMATION_PATH)
    router_config = load_router_config()
    target_status = target_session_status(router_config)

    codex_hooks = get_toml_bool(config, "codex_hooks")
    automation_status = get_toml_string(automation, "status")
    automation_rrule = get_toml_string(automation, "rrule")
    automation_db = get_automation_db_status()
    disabled = DISABLED_PATH.exists()

    print(f"NEXT hooks: {'STOPPED' if disabled else 'ACTIVE'}")
    print(f"codex_hooks feature: {codex_hooks}")
    print(f"SessionStart hook: {'next_session_start.py' in config}")
    print(f"Stop hook: {'next_stop_router.py' in config}")
    print(f"{AUTOMATION_ID} toml: {automation_status or 'UNKNOWN'}")
    print(f"{AUTOMATION_ID} db: {(automation_db or {}).get('status') or 'UNKNOWN'}")
    print(f"{AUTOMATION_ID} schedule: {(automation_db or {}).get('rrule') or automation_rrule or 'UNKNOWN'}")
    print(f"{AUTOMATION_ID} next_run_at: {(automation_db or {}).get('next_run_at') or 'UNKNOWN'}")
    print("target_sessions: " + ", ".join(router_config.get("target_sessions") or []))
    print("target_cwds: " + ", ".join(router_config.get("target_cwds") or []))
    target_line = f"target_status: {target_status.get('status') or 'UNKNOWN'}"
    if target_status.get("session_id"):
        target_line += f" session={target_status['session_id']}"
    if target_status.get("retry_hint"):
        target_line += f" retry_after={target_status['retry_hint']}"
    print(target_line)
    print(f"state_files: {len(state_files())}")
    print("last_hook_events:")
    if LOG_PATH.exists():
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        for line in lines:
            print("  " + line)
    else:
        print("  none")
    print("note: hooks are event-driven, not a resident daemon.")


def start():
    if DISABLED_PATH.exists():
        DISABLED_PATH.unlink()
    set_automation_status("PAUSED")
    print(f"NEXT hooks ACTIVE; {AUTOMATION_ID} PAUSED (fallback is one-shot)")


def stop():
    DISABLED_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISABLED_PATH.write_text("disabled\n", encoding="utf-8")
    set_automation_status("PAUSED")
    print(f"NEXT hooks STOPPED; {AUTOMATION_ID} PAUSED")


def trigger():
    target_status = target_session_status()
    changed, now_ms = schedule_automation_now()
    if changed:
        if target_status.get("status") == "QUOTA_BLOCKED":
            hint = target_status.get("retry_hint")
            suffix = f"; previous quota retry_after={hint}" if hint else "; previous quota block detected"
            print(f"{AUTOMATION_ID} quota retry scheduled once now: {now_ms}{suffix}")
        else:
            print(f"{AUTOMATION_ID} scheduled once now: {now_ms}")
    else:
        print(f"{AUTOMATION_ID} not found")
        sys.exit(1)


def usage():
    print("usage: next_ctl.py status|start|stop|trigger")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print_status()
    elif cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd in ("trigger", "fallback", "run-now"):
        trigger()
    elif cmd == "finish-trigger":
        triggered_at_ms = int(sys.argv[2]) if len(sys.argv) > 2 else int(time.time() * 1000)
        finish_one_shot_trigger(triggered_at_ms)
    else:
        usage()
        sys.exit(2)


if __name__ == "__main__":
    main()
