"""The command line: python -m doris demo, run, stop, resume, verify, traps.

The demo and `run` start the real worker as a child process under the kill switch, on a copy of the
sandbox and a throwaway state folder, so the repository's own sandbox/ and state/ are never touched.
`run --live` is tested against fake servers in test_live_loop.
"""

import contextlib
import email
import email.policy
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import caps, paths, receipts, supervisor
from tests.unit import demo_sandbox
from tests.unit.test_restart import mini_sandbox


def fresh_sandbox(tmp):
    return demo_sandbox.copy_committed(os.path.join(tmp, "sandbox"))


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
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DORIS_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)

    def runs(self):
        runs = os.path.join(self.state, "sandbox", "runs")
        return [os.path.join(runs, n) for n in sorted(os.listdir(runs))]


class ModelSource(unittest.TestCase):
    def test_a_key_means_live(self):
        self.assertEqual(cli.demo_source({"ANTHROPIC_API_KEY": "sk-test"}, answers_exist=True), "live")

    def test_recorded_answers_without_a_key_are_replayed(self):
        self.assertEqual(cli.demo_source({"ANTHROPIC_API_KEY": " "}, answers_exist=True), "replayed")

    def test_neither_means_the_stub(self):
        self.assertEqual(cli.demo_source({}, answers_exist=False), "stub")

    def test_the_run_name_starts_with_the_source_then_the_mode(self):
        self.assertRegex(cli.run_name("stub", "sandbox"), r"^model-stub-sandbox-\d{8}T\d{6}Z$")


class TheEnvironmentStaysOut(Isolated):
    """A judge may have a key and a model exported. Inside these tests neither is seen, so the demo
    reads the mail with the stub, as the tests here expect, and never calls the API."""

    def test_no_key_or_model_from_the_environment_reaches_the_demo(self):
        from doris import model
        self.assertEqual(cli.demo_source(os.environ, answers_exist=False), "stub")
        self.assertEqual(model.default_model_id(), model.DEFAULT_MODEL)


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


def whole_lines(path):
    """The receipt lines written so far, read while the worker writes: whole lines only."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    lines = []
    for raw in data.split(b"\n")[:-1]:
        try:
            lines.append(json.loads(raw.decode("utf-8")))
        except ValueError:
            pass
    return lines


def closed(lines):
    return [l for l in lines if l["kind"] in ("decided", "held") and "outcome" in l["payload"]]


class Run(Isolated):
    """`python -m doris run`: the sandbox under the same kill switch, pass after pass, each after a
    wait for her next cycle, until stopped."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.sandbox, "inbox", "02-marcus-request.eml"), "rb") as fh:
            self.judge_mail = b"".join(l for l in fh.read().splitlines(True) if not l.lower().startswith(b"date:"))
        root = mini_sandbox(os.path.join(self.tmp, "mini"), ("01-priya-request.eml",))
        for target, name, value in ((paths, "SANDBOX", root), (cli, "RUN_CYCLE_S", 0.2)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.inbox = os.path.join(root, "inbox")

    def judge(self, failures):
        """Once her first pass has closed Priya's request, a judge drops an email (no Date) into the
        inbox; once that one is closed too, a stop from the command line."""
        deadline, dropped = time.time() + 60, False
        while time.time() < deadline:
            runs = os.path.join(self.state, "sandbox", "runs")
            names = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
            done = closed(whole_lines(os.path.join(runs, names[-1], "receipts.jsonl"))) if names else []
            if not dropped and len(done) == 1:
                with open(os.path.join(self.inbox, "14-judge.eml"), "wb") as fh:
                    fh.write(self.judge_mail)
                dropped = True
            elif dropped and len(done) == 2:
                break
            time.sleep(0.05)
        else:
            failures.append("gave up waiting (dropped=%s)" % dropped)
        supervisor.request_stop("sandbox", "cli")

    def test_run_reads_the_inbox_each_cycle_until_a_stop_and_the_halt_closes_the_chain(self):
        failures = []
        judge = threading.Thread(target=self.judge, args=(failures,))
        judge.start()
        try:
            code, out = run_cli(["run"])
        finally:
            judge.join()
        self.assertEqual(failures, [])
        self.assertEqual(code, supervisor.EXIT_HALTED, out)
        self.assertTrue(out.startswith("Sandbox. "), out)
        self.assertIn("until `python -m doris stop`", out.splitlines()[1])
        (run,) = self.runs()
        self.assertTrue(os.path.basename(run).startswith("model-stub-sandbox-"), run)
        path = os.path.join(run, "receipts.jsonl")
        self.assertTrue(receipts.verify(path, finished=True).ok)
        lines = receipts.read_lines(path)
        self.assertEqual((lines[-1]["kind"], lines[-1]["payload"]["cause"]), ("halted", "cli"))
        observed = [l["payload"]["clock"] for l in lines if l["kind"] == "observed" and "from_addr" in l["payload"]]
        # Priya's on the first pass; the judge's on a later one: the next, or the one after when the judge's
        # file lands just after that pass read the inbox (a busy machine), never the pass that was running
        self.assertEqual(len(observed), 2, observed)
        self.assertEqual(observed[0], "2026-12-01T16:10:00Z")
        self.assertGreater(observed[1], observed[0])
        self.assertTrue(observed[1].endswith("0:00Z"), observed)    # on her ten-minute cycle
        self.assertEqual([l["payload"]["outcome"] for l in closed(lines)], ["book", "offer"])
        with open(os.path.join(run, "config.json"), encoding="utf-8") as fh:
            config = json.load(fh)
        self.assertEqual((config["mode"], config["cycles"], config["cycle_s"], config["digest"], config["clock_mode"]),
                         ("sandbox", None, 0.2, True, "advance"))

    def test_run_sends_the_days_digest_once_from_five_pacific(self):
        root = os.path.dirname(self.inbox)
        with open(os.path.join(root, "clock.txt"), "w", encoding="ascii") as fh:
            fh.write("2026-12-02T00:50:00Z\n")                     # Tue Dec 1, 4:50 PM Pacific
        outbox = os.path.join(root, "outbox")
        failures = []

        def digests():
            return sorted(n for n in os.listdir(outbox) if n.endswith("-S2.eml"))

        def stop_two_passes_after_the_digest():
            deadline, fetched = time.time() + 60, None
            while time.time() < deadline:
                runs = os.path.join(self.state, "sandbox", "runs")
                names = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
                lines = whole_lines(os.path.join(runs, names[-1], "receipts.jsonl")) if names else []
                fetches = len([l for l in lines if l["kind"] == "called" and l["payload"]["call"] == "mail_fetch"])
                if fetched is None and digests():
                    fetched = fetches
                elif fetched is not None and fetches >= fetched + 2:
                    break
                time.sleep(0.05)
            else:
                failures.append("gave up waiting (digest sent: %s)" % (fetched is not None))
            supervisor.request_stop("sandbox", "cli")

        judge = threading.Thread(target=stop_two_passes_after_the_digest)
        judge.start()
        try:
            code, out = run_cli(["run"])
        finally:
            judge.join()
        self.assertEqual(failures, [])
        self.assertEqual(code, supervisor.EXIT_HALTED, out)
        (name,) = digests()
        with open(os.path.join(outbox, name), "rb") as fh:
            sent = email.message_from_bytes(fh.read(), policy=email.policy.default)
        self.assertEqual((str(sent["To"]), str(sent["Date"]), str(sent["Subject"])),
                         (cli.SANDBOX_OWNER, "Wed, 02 Dec 2026 01:00:00 +0000",
                          "Handled 1, held 0, human approvals 0. Tue Dec 1"))
        (run,) = self.runs()
        self.assertTrue(receipts.verify(os.path.join(run, "receipts.jsonl"), finished=True).ok)

    def test_fresh_and_env_belong_to_a_live_run(self):
        for argv in (["run", "--fresh"], ["run", "--env", "some.env"]):
            with self.subTest(argv=argv):
                code, out = run_cli(argv)
                self.assertEqual(code, 2, out)
                self.assertEqual(len(out.strip().splitlines()), 1, out)
                self.assertIn("--live", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox", "runs")))

    def test_a_halt_in_force_refuses_the_run(self):
        supervisor.request_stop("sandbox", "cli")
        code, out = run_cli(["run"])
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT, out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox", "runs")))


class Smoke(unittest.TestCase):
    def test_smoke_is_a_command_and_contacts_nothing_without_its_env_file(self):
        tmp = tempfile.mkdtemp(prefix="doris-cli-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch("socket.create_connection", side_effect=AssertionError("contacted a server")):
            code, out = run_cli(["smoke", "--env", os.path.join(tmp, "missing.env")])
        self.assertEqual(code, 2, out)
        self.assertIn("env file", out)

    def test_the_help_names_smoke(self):
        code, out = run_cli(["--help"])
        self.assertIn("smoke", out)

    def test_the_help_names_run_and_its_live_flags(self):
        code, out = run_cli(["--help"])
        for words in ("run ", "--live", "--fresh", "--env"):
            self.assertIn(words, out)
        self.assertNotIn("run (live)", out)


class NotYet(unittest.TestCase):
    def test_commands_not_built_yet_say_so(self):
        for name in ("redteam", "replay", "watch"):
            code, out = run_cli([name])
            self.assertEqual(code, 2, (name, out))
            self.assertIn("not built yet", out)


if __name__ == "__main__":
    unittest.main()
