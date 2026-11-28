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

    def test_the_demo_ends_with_the_owners_story_from_her_receipts(self):
        code, out = run_cli(["demo"])
        self.assertEqual(code, 0, out)
        story_lines = out.partition("What Doris did for Shane")[2]        # her worker's own lines go to the terminal
        self.assertIn("15 emails came in: 13 handled by her", story_lines)
        self.assertIn("2 held for Shane", story_lines)
        self.assertIn("0 human approvals", story_lines)
        self.assertRegex(story_lines, r"Receipts: \d+ lines, chain verified\.")

    def old_days(self):
        old = os.path.join(self.state, "old")
        return [os.path.join(old, n) for n in sorted(os.listdir(old))] if os.path.isdir(old) else []

    def her_outputs(self):
        """What a run wrote into the sandbox: her sends, and her bookings and staged events."""
        return (sorted(n for n in os.listdir(os.path.join(self.sandbox, "outbox")) if n != ".gitkeep"),
                sorted(n for n in os.listdir(os.path.join(self.sandbox, "calendar")) if demo_sandbox.is_run_event(n)))

    def test_a_second_demo_is_a_fresh_day_and_the_last_one_is_kept_in_state_old(self):
        code, first_out = run_cli(["demo"])
        self.assertEqual(code, 0, first_out)
        self.assertNotIn("A fresh sandbox day", first_out)
        (first_run,) = self.runs()
        sent, booked = self.her_outputs()
        self.assertTrue(sent and booked)
        code, out = run_cli(["demo"])
        self.assertEqual(code, 0, out)
        (run,) = self.runs()                             # the new day's state holds only its own run
        s = summary_of(os.path.join(run, "receipts.jsonl"))
        self.assertEqual((s["handled"], s["held"], s["human_approvals"], s["bookings"]), (13, 2, 0, 2))
        self.assertEqual(self.her_outputs(), (sent, booked))      # the same day's sends and bookings, once
        (old,) = self.old_days()
        self.assertTrue(os.path.basename(old).startswith("sandbox-"), old)
        self.assertTrue(os.path.isfile(os.path.join(old, "ledger.sqlite")))
        kept = os.path.join(old, "runs", os.path.basename(first_run), "receipts.jsonl")
        self.assertTrue(receipts.verify(kept, finished=True).ok)
        self.assertEqual(sorted(os.listdir(os.path.join(old, "outbox"))), sent)
        self.assertEqual(sorted(os.listdir(os.path.join(old, "calendar"))), booked)
        self.assertTrue(os.path.isfile(os.path.join(self.sandbox, "outbox", ".gitkeep")))
        self.assertIn("A fresh sandbox day: the last demo's state and her sends and bookings are kept in ", out)
        self.assertIn(os.path.basename(old), out)

    def test_after_a_halted_run_the_demo_moves_nothing_and_reads_the_dropped_mail_again(self):
        def stop_after_the_first_message():
            deadline = time.time() + 60
            while time.time() < deadline:
                runs = os.path.join(self.state, "sandbox", "runs")
                names = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
                if names and closed(whole_lines(os.path.join(runs, names[-1], "receipts.jsonl"))):
                    break
                time.sleep(0.02)
            supervisor.request_stop("sandbox", "cli")

        stopper = threading.Thread(target=stop_after_the_first_message)
        stopper.start()
        code, out = run_cli(["demo"])
        stopper.join()
        self.assertEqual(code, supervisor.EXIT_HALTED, out)           # the story never changes the exit code
        self.assertIn("up to the stop, from her receipts.", out)
        self.assertIn("Nothing more goes out until python -m doris resume.", out)
        (halted_run,) = self.runs()
        self.assertEqual(run_cli(["resume"])[0], 0)
        code, out = run_cli(["demo"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("A fresh sandbox day", out)
        self.assertEqual(self.old_days(), [])
        self.assertEqual(self.runs()[0], halted_run)
        self.assertEqual(len(self.runs()), 2)
        sent, _ = self.her_outputs()
        self.assertEqual(len([n for n in sent if n.endswith("-S2.eml")]), 1, sent)

    def test_a_day_that_closed_after_a_halted_line_or_never_closed_is_not_finished(self):
        def last_run(*kinds):
            run = os.path.join(self.state, "sandbox", "runs", "run-%d" % len(kinds))
            os.makedirs(run)
            with open(os.path.join(run, "receipts.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("".join(json.dumps({"kind": k, "payload": {}}) + "\n" for k in kinds))

        self.assertFalse(cli.last_day_finished())                 # no run yet
        last_run("run_start", "summary")
        self.assertTrue(cli.last_day_finished())
        last_run("run_start", "halted", "summary")                # her supervisor gone: she closed it herself
        self.assertFalse(cli.last_day_finished())
        last_run("run_start", "fetched", "decided", "called")     # a crash, or a run still going
        self.assertFalse(cli.last_day_finished())

    def test_a_move_that_fails_refuses_the_demo_and_loses_nothing(self):
        run_cli(["demo"])
        (first_run,) = self.runs()
        before = self.her_outputs()
        real = cli._move

        def refuses_one_send(src, dst):
            if os.path.dirname(src) == os.path.join(self.sandbox, "outbox") and src.endswith("-S2.eml"):
                raise PermissionError(13, "The process cannot access the file", src)
            real(src, dst)

        with mock.patch.object(cli, "_move", refuses_one_send):
            code, out = run_cli(["demo"])
        self.assertNotEqual(code, 0, out)
        self.assertIn("Not started", out)
        self.assertIn("-S2.eml", out)
        self.assertIn("PermissionError", out)
        self.assertEqual(self.runs(), [first_run])                # no new run, and the last one is where it was
        self.assertTrue(os.path.isfile(os.path.join(self.state, "sandbox", "ledger.sqlite")))
        self.assertEqual(self.her_outputs(), before)
        self.assertEqual(self.old_days(), [])
        self.assertFalse({"outbox", "calendar"} & set(os.listdir(os.path.join(self.state, "sandbox"))))

    def test_a_move_that_cannot_be_undone_says_what_moved_where(self):
        run_cli(["demo"])
        real, calls = cli._move, []

        def refuses_from_the_third(src, dst):
            calls.append(src)
            if len(calls) >= 3:
                raise PermissionError(13, "The process cannot access the file", src)
            real(src, dst)

        with mock.patch.object(cli, "_move", refuses_from_the_third):
            code, out = run_cli(["demo"])
        self.assertNotEqual(code, 0, out)
        (old,) = self.old_days()
        self.assertIn("could not be put back", out)
        self.assertIn(os.path.basename(old), out)
        self.assertTrue(os.path.isfile(os.path.join(old, "ledger.sqlite")))    # moved, kept, and said so
        self.assertEqual(os.listdir(os.path.join(old, "outbox")), [os.path.basename(calls[1])])
        self.assertIn("%s is now at " % cli._display(calls[1]), out)
        self.assertIn("%s is now at " % cli._display(os.path.join(self.state, "sandbox")), out)
        self.assertNotIn("run the demo again", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox")))
        left = self.her_outputs()
        code, out = run_cli(["demo"])           # a demo now is refused, never a second day sent beside the first
        self.assertEqual(code, 1, out)
        self.assertIn("still in the sandbox", out)
        self.assertEqual(self.her_outputs(), left)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox")))

    def test_a_kept_days_folder_that_cannot_be_made_refuses_the_demo_and_moves_nothing(self):
        run_cli(["demo"])
        (first_run,) = self.runs()
        before = self.her_outputs()
        with open(os.path.join(self.state, "old"), "w", encoding="utf-8"):
            pass                                # state/old is a file, so no folder can be made there
        code, out = run_cli(["demo"])
        self.assertEqual(code, 1, out)
        self.assertIn("Not started: the last demo day could not be moved aside.", out)
        self.assertIn("Nothing was moved", out)
        self.assertEqual(self.runs(), [first_run])
        self.assertEqual(self.her_outputs(), before)

    def test_her_sends_left_in_the_sandbox_without_their_state_refuse_the_demo(self):
        run_cli(["demo"])
        before = self.her_outputs()
        os.rename(os.path.join(self.state, "sandbox"), os.path.join(self.tmp, "elsewhere"))   # state gone, sends not
        code, out = run_cli(["demo"])
        self.assertEqual(code, 1, out)
        self.assertIn("a demo now would send and book them all again", out)
        self.assertIn(before[0][0], out)
        self.assertEqual(self.her_outputs(), before)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox")))

    def test_a_halt_in_force_refuses_the_demo(self):
        supervisor.request_stop("sandbox", "cli")
        code, out = run_cli(["demo"])
        self.assertEqual(code, supervisor.EXIT_REFUSED_HALT, out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "sandbox", "runs")))


class PrintedPaths(unittest.TestCase):
    """A judge runs from src/, so a printed path is one they can paste from where they stand."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="doris-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "doris")
        os.makedirs(os.path.join(self.repo, "src"))
        patcher = mock.patch.object(paths, "REPO", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.receipts = os.path.join(self.repo, "state", "sandbox", "runs", "r1", "receipts.jsonl")

    def from_folder(self, folder, path):
        with mock.patch.object(os, "getcwd", lambda: folder):
            return cli._display(path)

    def test_a_path_in_the_repository_is_shown_from_the_working_folder(self):
        self.assertEqual(self.from_folder(os.path.join(self.repo, "src"), self.receipts),
                         os.path.join("..", "state", "sandbox", "runs", "r1", "receipts.jsonl"))
        self.assertEqual(self.from_folder(self.repo, self.receipts),
                         os.path.join("state", "sandbox", "runs", "r1", "receipts.jsonl"))

    def test_her_worker_runs_in_src_and_still_shows_paths_from_the_folder_the_command_was_run_in(self):
        seen = {}
        run_dir = os.path.join(self.repo, "state", "sandbox", "runs", "r1")
        os.makedirs(run_dir)

        def started(*args, **kwargs):
            seen["from"] = os.environ.get(cli.SHOWN_FROM)
            with mock.patch.object(os, "getcwd", lambda: os.path.join(self.repo, "src")):   # the worker's own
                seen["shown"] = cli._display(self.receipts)
            return 0

        with mock.patch.object(supervisor, "run_supervised", started), \
                mock.patch.object(os, "getcwd", lambda: self.repo):
            cli.supervise("sandbox", os.path.dirname(os.path.dirname(run_dir)), "r1", run_dir, {})
        self.assertEqual(seen["from"], self.repo)
        self.assertEqual(seen["shown"], os.path.join("state", "sandbox", "runs", "r1", "receipts.jsonl"))
        self.assertNotIn(cli.SHOWN_FROM, os.environ)

    def test_a_path_outside_the_repository_is_shown_whole(self):
        outside = os.path.join(self.tmp, "elsewhere", "receipts.jsonl")
        self.assertEqual(self.from_folder(os.path.join(self.repo, "src"), outside), outside)

    def test_a_path_on_another_drive_is_shown_whole(self):
        with mock.patch.object(os.path, "relpath", side_effect=ValueError("path is on mount 'D:'")):
            self.assertEqual(cli._display(self.receipts), self.receipts)


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

    def refusing(self, *modes):
        real = supervisor.request_stop

        def request_stop(mode, cause="cli", state_dir=None):
            if mode in modes:
                raise supervisor.HaltWriteError("HALT could not be written (PermissionError, 5 tries): "
                                                "the stop is NOT in force")
            return real(mode, cause, state_dir=state_dir)

        return mock.patch.object(supervisor, "request_stop", request_stop)

    def test_a_stop_the_sandbox_refuses_still_halts_live_and_says_which_did_not_land(self):
        with self.refusing("sandbox"):
            code, out = run_cli(["stop"])
        self.assertNotEqual(code, 0, out)
        self.assertEqual(supervisor.halt_cause("live"), "cli")          # the live stop was not lost
        self.assertIsNone(supervisor.halt_cause("sandbox"))
        self.assertIn("NOT halted in the sandbox: HaltWriteError: HALT could not be written", out)
        self.assertIn("Halted live.", out)
        self.assertNotIn("Stopped: Doris is halted in the sandbox and live", out)

    def test_a_stop_live_refuses_still_halts_the_sandbox_and_exits_non_zero(self):
        with self.refusing("live"):
            code, out = run_cli(["stop"])
        self.assertNotEqual(code, 0, out)
        self.assertEqual(supervisor.halt_cause("sandbox"), "cli")
        self.assertIsNone(supervisor.halt_cause("live"))
        self.assertIn("NOT halted live: HaltWriteError", out)
        self.assertIn("Halted in the sandbox.", out)

    def test_a_stop_that_lands_nowhere_says_so_for_each_and_claims_no_halt(self):
        with self.refusing("sandbox", "live"):
            code, out = run_cli(["stop"])
        self.assertNotEqual(code, 0, out)
        self.assertIn("NOT halted in the sandbox", out)
        self.assertIn("NOT halted live", out)
        self.assertNotIn("Halted", out.replace("NOT halted", ""))
        self.assertNotIn("Stopped:", out)

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


    def changed_copy(self, name, ending=b"\n", change=5, tail=b""):
        """A copy of the run's receipts with one character of seq `change` changed and every line ending `ending`."""
        copy = os.path.join(self.tmp, name)
        with open(self.path, "rb") as fh:
            lines = fh.read().split(b"\n")[:-1]
        if change:
            self.assertIn(b'"ts":"2026-', lines[change - 1])
            lines[change - 1] = lines[change - 1].replace(b'"ts":"2026-', b'"ts":"2027-', 1)
        with open(copy, "wb") as fh:
            fh.write(b"".join(l + ending for l in lines) + tail)
        return copy, len(lines)

    def test_ok_is_said_once(self):
        code, out = run_cli(["verify"])
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\Aok: \d+ lines, every hash matches the line before it \([^()]*receipts\.jsonl\)\.\n\Z")

    def test_a_torn_tail_moved_aside_is_noted_after_the_ok(self):
        path = os.path.join(self.tmp, "runs", "r", "receipts.jsonl")
        receipts.ReceiptWriter(path, "r", "sandbox").write("observed", {}, subject="startup")
        with open(path, "ab") as fh:
            fh.write(b'{"torn')
        receipts.ReceiptWriter(path, "r", "sandbox").write("summary", {"handled": 0, "held": 0}, subject="run")
        code, out = run_cli(["verify", path])
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\Aok: 3 lines, every hash matches the line before it \([^()]*\)\. "
                              r"Torn tail moved aside at seq 2\.\n\Z")

    def test_the_broken_seq_is_said_once(self):
        copy, _n = self.changed_copy("copy.jsonl")
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"\ABROKEN at seq 5: the hash does not match the line\. 4 lines verified before it "
                              r"\([^()]*copy\.jsonl\)\.\n\Z")

    def test_one_line_ahead_of_the_break_is_one_line(self):
        copy, _n = self.changed_copy("second.jsonl", change=2)
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("BROKEN at seq 2: the hash does not match the line. 1 line verified before it ("),
                        out)

    def test_a_torn_tail_is_said_once(self):
        copy, n = self.changed_copy("torn.jsonl", change=0, tail=b'{"torn')
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("BROKEN at seq %d: torn tail: 6 bytes with no whole line. %d lines verified "
                                       "before it (" % (n + 1, n)), out)

    def test_a_powershell_resave_still_names_the_changed_line(self):
        # PowerShell's Set-Content writes CRLF line endings: every line changes at once, and the edited
        # line must not hide behind "BROKEN at seq 1 ... 0 lines verified"
        copy, n = self.changed_copy("resaved.jsonl", ending=b"\r\n")
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("BROKEN at seq 5: the hash does not match the line; "), out)
        self.assertIn("%d of %d lines now end CRLF, not LF" % (n, n), out)
        self.assertIn(" 4 lines verified before it (", out)
        self.assertNotIn("seq 1:", out)

    def test_a_resave_that_changed_only_the_line_endings_says_so_and_still_fails(self):
        copy, n = self.changed_copy("endings.jsonl", ending=b"\r\n", change=0)
        code, out = run_cli(["verify", copy])
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("BROKEN at seq 1: the line endings were changed: %d of %d lines now end CRLF, "
                                       "not LF" % (n, n)), out)
        self.assertIn("every line still matches its hash without them (", out)
        self.assertNotIn("canonical", out)
        self.assertNotIn("0 lines verified", out)

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
        self.assertIn("until `%s`" % paths.command("stop"), out.splitlines()[1])
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


class Help(unittest.TestCase):
    """The help spells the interpreter as it runs (paths.command: py on Windows, as the README says there;
    elsewhere this interpreter's own name, python3 under the README). The env file's default is our
    server's own path, and the help says so: no judge is ever asked for it."""

    def usage(self, command):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.assertRaises(SystemExit) as ctx:
            cli.main([command, "-h"])
        self.assertEqual(ctx.exception.code, 0)
        return " ".join(out.getvalue().split())

    def test_every_commands_usage_line_names_the_interpreter_running(self):
        for command in ("demo", "run", "traps", "stop", "resume", "smoke", "replay", "watch", "redteam"):
            self.assertTrue(self.usage(command).startswith("usage: %s " % paths.command(command)), command)

    def test_the_top_help_names_the_interpreter_running_everywhere(self):
        code, out = run_cli(["--help"])
        self.assertTrue(out.startswith(paths.command() + " <command>"), out)
        self.assertEqual(out.count("-m doris"), out.count(paths.command()), out)

    def test_the_interpreter_is_py_on_windows_else_its_own_name(self):
        with mock.patch.object(paths.os, "name", "nt"):
            self.assertEqual(paths.command("stop"), "py -m doris stop")
        with mock.patch.object(paths.os, "name", "posix"), \
                mock.patch.object(paths.sys, "executable", "/usr/local/bin/python3.9"):
            self.assertEqual(paths.command("resume"), "python3.9 -m doris resume")
            self.assertEqual(paths.command(), "python3.9 -m doris")
        with mock.patch.object(paths.os, "name", "posix"), mock.patch.object(paths.sys, "executable", ""):
            self.assertEqual(paths.command("demo"), "python3 -m doris demo")

    def test_the_env_default_is_named_as_our_servers_never_a_judges(self):
        wanted = "(default %s, on our server; never asked of a judge)" % paths.LIVE_ENV
        self.assertIn(wanted, self.usage("run"))
        self.assertIn(wanted, self.usage("smoke"))
        self.assertIn("default %s, on our server; never asked of a judge" % paths.LIVE_ENV,
                      " ".join(run_cli(["--help"])[1].split()))


class TheAuditMonitoringAndRedteamCommands(unittest.TestCase):
    """replay (PR #8), watch (PR #9) and redteam (PR #10) are each one line in COMMANDS that hands its
    arguments to the module's own main and returns its exit code; none says "not built yet"."""

    def test_each_hands_its_arguments_to_its_modules_main_and_returns_its_code(self):
        import importlib
        for name, code in (("replay", 7), ("watch", 5), ("redteam", 3)):
            module = importlib.import_module("doris." + name)
            with self.subTest(name), mock.patch.object(module, "main", return_value=code) as main:
                got, out = run_cli([name, "--x", "y"])
                self.assertEqual(got, code)
                main.assert_called_once_with(["--x", "y"])
                self.assertNotIn("not built yet", out)

    def test_watch_with_a_port_out_of_range_is_not_started(self):
        code, out = run_cli(["watch", "--port", "70000"])
        self.assertEqual(code, 2, out)
        self.assertIn("Not started", out)

    def test_the_help_names_all_three_and_none_is_marked_not_built(self):
        code, out = run_cli(["--help"])
        for name in ("replay", "watch", "redteam"):
            self.assertIn("    %s " % name, out)
        self.assertNotIn("not built yet", out)


if __name__ == "__main__":
    unittest.main()
