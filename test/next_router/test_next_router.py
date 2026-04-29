import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STOP_ROUTER = load_module("next_stop_router", ROOT / "hooks" / "next_stop_router.py")
CTL = load_module("next_ctl", ROOT / "hooks" / "next_ctl.py")


class NextMarkerRoutingTest(unittest.TestCase):
    def test_routes_to_implementation_when_marker_is_followed_by_context(self):
        message = """实现切片已完成。

NEXT: 实现

<skill>
这段是后续上下文，不应该覆盖 NEXT 标识。
</skill>
"""

        self.assertEqual(STOP_ROUTER.find_next_marker(message), "实现")

    def test_uses_the_last_explicit_marker(self):
        message = """上一轮：
NEXT: 审查

本轮决定继续实现：
NEXT: 实现
"""

        self.assertEqual(STOP_ROUTER.find_next_marker(message), "实现")

    def test_protocol_choice_line_is_not_a_marker(self):
        message = "NEXT: 继续/实现/修复/审查/发布/停止"

        self.assertIsNone(STOP_ROUTER.find_next_marker(message))

    def test_implementation_message_sends_skill_links(self):
        message = STOP_ROUTER.MESSAGES["实现"]

        self.assertIn("[$incremental-implementation](", message)
        self.assertIn("/incremental-implementation/SKILL.md)", message)
        self.assertIn("[$test-driven-development](", message)
        self.assertIn("/test-driven-development/SKILL.md)", message)
        self.assertFalse(message.startswith("继续\n"))

    def test_configured_skill_root_is_used_for_messages(self):
        messages = STOP_ROUTER.build_messages({"skill_root": "/tmp/custom-skills"})

        self.assertIn(
            "[$code-review-and-quality](/tmp/custom-skills/code-review-and-quality/SKILL.md)",
            messages["审查"],
        )


class StopRouterMainTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_path = root / "next_router_config.json"
        self.disabled_path = root / "NEXT_ROUTER_DISABLED"
        self.log_path = root / "next_router.log"
        self.state_dir = root / ".next-router-state"
        self.originals = {
            "CONFIG_PATH": STOP_ROUTER.CONFIG_PATH,
            "DISABLED_PATH": STOP_ROUTER.DISABLED_PATH,
            "LOG_PATH": STOP_ROUTER.LOG_PATH,
            "STATE_DIR": STOP_ROUTER.STATE_DIR,
        }
        STOP_ROUTER.CONFIG_PATH = self.config_path
        STOP_ROUTER.DISABLED_PATH = self.disabled_path
        STOP_ROUTER.LOG_PATH = self.log_path
        STOP_ROUTER.STATE_DIR = self.state_dir

    def tearDown(self):
        for key, value in self.originals.items():
            setattr(STOP_ROUTER, key, value)
        self.temp_dir.cleanup()

    def test_stop_marker_does_not_emit_followup(self):
        self.config_path.write_text(
            json.dumps({"target_sessions": ["session-1"], "max_auto_continuations": 24}),
            encoding="utf-8",
        )
        payload = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "last_assistant_message": "完成。\nNEXT: 停止",
        }

        stdin = StringIO(json.dumps(payload, ensure_ascii=False))
        stdout = StringIO()
        old_stdin = STOP_ROUTER.sys.stdin
        try:
            STOP_ROUTER.sys.stdin = stdin
            with redirect_stdout(stdout):
                STOP_ROUTER.main()
        finally:
            STOP_ROUTER.sys.stdin = old_stdin

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn('"result": "stop"', self.log_path.read_text(encoding="utf-8"))


class OneShotAutomationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.automation_toml = root / "automation.toml"
        self.automation_db = root / "codex-dev.db"
        self.disabled_path = root / "NEXT_ROUTER_DISABLED"
        self.state_dir = root / ".next-router-state"

        self.automation_toml.write_text(
            'version = 1\nid = "automation-2"\nstatus = "PAUSED"\nrrule = "FREQ=MINUTELY;INTERVAL=1"\n',
            encoding="utf-8",
        )
        with sqlite3.connect(self.automation_db) as conn:
            conn.execute(
                """
                create table automations (
                    id text primary key,
                    name text not null,
                    prompt text not null,
                    status text not null,
                    next_run_at integer,
                    last_run_at integer,
                    cwds text not null default '[]',
                    rrule text not null default 'FREQ=MINUTELY;INTERVAL=1',
                    created_at integer not null,
                    updated_at integer not null
                )
                """
            )
            conn.execute(
                """
                insert into automations
                    (id, name, prompt, status, next_run_at, last_run_at, cwds, rrule, created_at, updated_at)
                values
                    ('automation-2', 'test', 'prompt', 'PAUSED', null, null, '[]', 'FREQ=MINUTELY;INTERVAL=1', 1, 1)
                """
            )

        self.originals = {
            "AUTOMATION_PATH": CTL.AUTOMATION_PATH,
            "AUTOMATION_DB_PATH": CTL.AUTOMATION_DB_PATH,
            "DISABLED_PATH": CTL.DISABLED_PATH,
            "STATE_DIR": CTL.STATE_DIR,
            "SESSION_ROOT": CTL.SESSION_ROOT,
            "ROUTER_CONFIG_PATH": CTL.ROUTER_CONFIG_PATH,
        }
        CTL.AUTOMATION_PATH = self.automation_toml
        CTL.AUTOMATION_DB_PATH = self.automation_db
        CTL.DISABLED_PATH = self.disabled_path
        CTL.STATE_DIR = self.state_dir
        CTL.SESSION_ROOT = root / "sessions"
        CTL.ROUTER_CONFIG_PATH = root / "next_router_config.json"
        CTL.SESSION_ROOT.mkdir(parents=True)
        CTL.ROUTER_CONFIG_PATH.write_text(
            json.dumps({"target_sessions": ["target-session"]}),
            encoding="utf-8",
        )

    def tearDown(self):
        for key, value in self.originals.items():
            setattr(CTL, key, value)
        self.temp_dir.cleanup()

    def automation_row(self):
        with sqlite3.connect(self.automation_db) as conn:
            return conn.execute(
                "select status, rrule, next_run_at from automations where id = 'automation-2'"
            ).fetchone()

    def test_start_enables_hooks_without_starting_minutely_fallback(self):
        with redirect_stdout(StringIO()):
            CTL.start()

        self.assertFalse(self.disabled_path.exists())
        self.assertEqual(self.automation_row(), ("PAUSED", "FREQ=MINUTELY;INTERVAL=1", None))
        self.assertIn('status = "PAUSED"', self.automation_toml.read_text(encoding="utf-8"))

    def test_trigger_is_finished_by_pausing_the_recurring_fallback(self):
        changed, trigger_at = CTL.schedule_automation_now(start_watcher=False)
        self.assertTrue(changed)
        active_status, rrule, next_run_at = self.automation_row()
        self.assertEqual(active_status, "ACTIVE")
        self.assertEqual(rrule, "FREQ=MINUTELY;INTERVAL=1")
        self.assertEqual(next_run_at, trigger_at)

        CTL.finish_one_shot_trigger(trigger_at, wait_seconds=0)

        self.assertEqual(self.automation_row(), ("PAUSED", "FREQ=MINUTELY;INTERVAL=1", None))
        self.assertIn('status = "PAUSED"', self.automation_toml.read_text(encoding="utf-8"))

    def write_session_log(self, lines):
        session_log = CTL.SESSION_ROOT / "2026" / "04" / "29" / "rollout-target-session.jsonl"
        session_log.parent.mkdir(parents=True, exist_ok=True)
        session_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return session_log

    def test_target_status_reports_quota_block_even_when_task_complete_follows_error(self):
        self.write_session_log([
            '{"timestamp":"2026-04-29T09:38:26.568Z","type":"event_msg","payload":{"type":"task_started"}}',
            '{"timestamp":"2026-04-29T09:38:28.217Z","type":"event_msg","payload":{"type":"error","message":"You have hit your usage limit. try again at 8:52 PM.","codex_error_info":"usage_limit_exceeded"}}',
            '{"timestamp":"2026-04-29T09:38:28.221Z","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"old result"}}',
        ])

        status = CTL.target_session_status()

        self.assertEqual(status["status"], "QUOTA_BLOCKED")
        self.assertEqual(status["retry_hint"], "8:52 PM")

    def test_target_status_recovers_when_a_new_turn_starts_after_quota_error(self):
        self.write_session_log([
            '{"timestamp":"2026-04-29T09:38:26.568Z","type":"event_msg","payload":{"type":"task_started"}}',
            '{"timestamp":"2026-04-29T09:38:28.217Z","type":"event_msg","payload":{"type":"error","message":"You have hit your usage limit. try again at 8:52 PM.","codex_error_info":"usage_limit_exceeded"}}',
            '{"timestamp":"2026-04-29T10:02:43.827Z","type":"event_msg","payload":{"type":"task_started"}}',
        ])

        status = CTL.target_session_status()

        self.assertEqual(status["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
