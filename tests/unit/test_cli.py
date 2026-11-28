"""The command line: python -m doris demo, stop, resume, verify, traps.

The demo runs the real worker as a child process under the kill switch, on a copy of the sandbox
and a throwaway state folder, so the repository's own sandbox/ and state/ are never touched.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import caps, paths, receipts, supervisor
from tests.unit import demo_sandbox


def fresh_sandbox(tmp):
    return demo_sandbox.copy_committed(paths.SANDBOX, os.path.join(tmp, "sandbox"))


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


def summary_of(receipts_path):
    (line,) = [l for l in receipts.read_lines(receipts_path) if l["kind"] == "summary"]
    return line["payload"]


class Isolated(unittest.TestCase):
    """paths.SANDBOX and paths.STATE point at a temp copy for the whole test; the staged calendar
    timeout and the retry wait are short, so the demo day runs in seconds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox = fresh_sandbox(self.tmp)
        self.state = os.path.join(self.tmp, "state")
        for target, name, value in ((paths, "SANDBOX", self.sandbox), (paths, "STATE", self.state),
                                    (caps, "CALENDAR_TIMEOUT_S", 0.3), (caps, "CALENDAR_RETRY_WAIT_S", 0.0)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def runs(self):
        runs = os.path.join(self.state, "sandbox", "runs")
        return [os.path.join(runs, n) for n in sorted(os.listdir(runs))]
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DORIS_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)


class ModelSource(unittest.TestCase):
    def test_a_key_means_live(self):
        self.assertEqual(cli.demo_source({"ANTHROPIC_API_KEY": "sk-test"}, answers_exist=True), "live")

    def test_recorded_answers_without_a_key_are_replayed(self):
        self.assertEqual(cli.demo_source({"ANTHROPIC_API_KEY": " "}, answers_exist=True), "replayed")

    def test_neither_means_the_stub(self):
        self.assertEqual(cli.demo_source({}, answers_exist=False), "stub")

    def test_the_run_name_starts_with_the_source_then_the_mode(self):
        self.assertRegex(cli.run_name("stub", "sandbox"), r"^model-stub-sandbox-\d{8}T\d{6}Z$")


class Demo(Isolated):
    def test_the_demo_day_runs_under_the_kill_switch_and_says_where_answers_come_from(self):
        code, out = run_cli(["demo"])
        self.assertEqual(code, 0, out)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("Sandbox. "), first)
        self.assertIn("stub", first)
        (run,) = self.runs()
        self.assertTrue(os.path.basename(run).startswith("model-stub-sandbox-"), run)
        receipts_path = os.path.join(run, "receipts.jsonl")
        self.assertTrue(receipts.verify(receipts_path).ok)
        s = summary_of(receipts_path)
        self.assertEqual((s["handled"], s["held"], s["human_approvals"]), (13, 2, 0))
        with open(os.path.join(run, "config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual((config["model_source"], config["calendar_timeout_s"]), ("stub", 0.3))

    def test_a_second_demo_runs_only_new_mail_and_sends_nothing_twice(self):
        run_cli(["demo"])
        sent = sorted(os.listdir(os.path.join(self.sandbox, "outbox")))
        code, out = run_cli(["demo"])
        self.assertEqual(code, 0, out)
        s = summary_of(os.path.join(self.runs()[-1], "receipts.jsonl"))
        self.assertEqual((s["handled"], s["held"], s["sends"]), (0, 0, 0))
        self.assertEqual(sorted(os.listdir(os.path.join(self.sandbox, "outbox"))), sent)

    def test_a_halt_in_force_refuses_the_demo(self):
        supervisor.request_stop("sandbox", "cli")
        code, out = run_cli(["demo"])
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT, out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox", "runs")))


class StopAndResume(Isolated):
    def test_stop_halts_sandbox_and_live_and_resume_clears_both(self):
        code, out = run_cli(["stop"])
        self.assertEqual(code, 0, out)
        self.assertEqual(supervisor.halt_cause("sandbox"), "cli")
        self.assertEqual(supervisor.halt_cause("live"), "cli")
        code, out = run_cli(["resume"])
        self.assertEqual(code, 0, out)
        self.assertIsNone(supervisor.halt_cause("sandbox"))
        self.assertIsNone(supervisor.halt_cause("live"))

    def test_resume_with_nothing_halted_says_so(self):
        code, out = run_cli(["resume"])
        self.assertEqual(code, 0, out)
        self.assertIn("nothing", out.lower())


class Verify(Isolated):
    def setUp(self):
        super().setUp()
        run_cli(["demo"])
        runs = os.path.join(self.state, "sandbox", "runs")
        self.path = os.path.join(runs, os.listdir(runs)[0], "receipts.jsonl")

    def test_verify_checks_the_latest_run_by_default(self):
        code, out = run_cli(["verify"])
        self.assertEqual(code, 0, out)
        self.assertIn("ok", out)

    def test_one_changed_character_fails_at_that_line(self):
        copy = os.path.join(self.tmp, "copy.jsonl")
        with open(self.path, "rb") as fh:
            lines = fh.read().split(b"\n")
        self.assertIn(b'"ts":"2026-', lines[4])
        lines[4] = lines[4].replace(b'"ts":"2026-', b'"ts":"2027-', 1)     # one character, seq 5
        with open(copy, "wb") as fh:
            fh.write(b"\n".join(lines))
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertIn("seq 5", out)


    def test_lines_cut_from_the_end_fail(self):
        copy = os.path.join(self.tmp, "cut.jsonl")
        with open(self.path, "rb") as fh:
            lines = fh.read().split(b"\n")[:-1]
        with open(copy, "wb") as fh:
            fh.write(b"".join(l + b"\n" for l in lines[:-3]))
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertIn("no closing line", out)


class NotYet(unittest.TestCase):
    def test_commands_not_built_yet_say_so(self):
        for name in ("run", "redteam", "replay", "watch"):
            code, out = run_cli([name])
            self.assertEqual(code, 2, (name, out))
            self.assertIn("not built yet", out)


if __name__ == "__main__":
    unittest.main()
