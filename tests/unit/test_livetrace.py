"""livetrace: traces/live-<kind>-<UTC stamp>/, the trace every live run keeps, pass or fail.

Receipts here are written with receipts.ReceiptWriter, as her loop and the kill switch write them;
the git folders are made by hand, as git leaves them (no git program is needed or run).
"""
import builtins
import datetime as dt
import json
import ntpath
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from doris import livetrace, paths, receipts

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 12, 1, 16, 0, 0, tzinfo=UTC)
SHA = "0123456789abcdef0123456789abcdef01234567"
RUN = "model-live-live-20261201T160000Z"


START = ("observed", {"step": "start", "mode": "live", "model": "claude-sonnet-5-5"})


def as_on_windows():
    """livetrace's files opened as Windows opens a text file: each "\\n" written becomes "\\r\\n"
    unless the open says newline= otherwise. On any OS it shows what a trace file would hold there."""
    real = builtins.open

    def windows_open(file, mode="r", *args, **kw):
        if "b" not in mode and len(args) < 4 and kw.get("newline") is None:
            kw["newline"] = "\r\n"
        return real(file, mode, *args, **kw)
    return mock.patch.object(livetrace, "open", windows_open, create=True)


def model_call(cost):
    return [("called", {"call": "model", "target": "claude-sonnet-5-5"}),
            ("result", {"call_seq": None, "ok": True, "cost_usd": cost})]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="livetrace-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.receipts = os.path.join(self.dir, "state", "receipts.jsonl")

    def write_chain(self, lines):
        """Her receipts for these (kind, payload) lines, a second apart from T0, written as her
        writer writes them; a result whose call_seq is None answers the called line just before it."""
        tick = [T0 - dt.timedelta(seconds=1)]

        def clock():
            tick[0] += dt.timedelta(seconds=1)
            return tick[0]
        w = receipts.ReceiptWriter(self.receipts, RUN, "live", clock=clock)
        last_called = None
        for kind, payload in lines:
            payload = dict(payload)
            if kind == "result" and payload.get("call_seq", 0) is None:
                payload["call_seq"] = last_called
            code = {"decided": "policy.offer_no_clear_time", "checked": "gate.pass", "held": "hold.calendar_down",
                    "halted": "halt.%s" % payload.get("cause")}.get(kind)
            line = w.write(kind, payload, why={"code": code, "text": "A test line."} if code else None)
            if kind == "called":
                last_called = line["seq"]
        return self.receipts


class GitCommitTest(Tmp):
    """The commit the code was checked out at, read from .git itself (no git program)."""

    def git(self, root, files):
        for rel, text in files.items():
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return root

    def test_a_branch_named_by_head_a_packed_ref_and_a_detached_head(self):
        loose = self.git(os.path.join(self.dir, "loose"), {".git/HEAD": "ref: refs/heads/live-integrated\n",
                                                            ".git/refs/heads/live-integrated": SHA + "\n"})
        packed = self.git(os.path.join(self.dir, "packed"), {
            ".git/HEAD": "ref: refs/heads/main\n",
            ".git/packed-refs": "# pack-refs with: peeled fully-peeled sorted\n%s refs/heads/main\n^%s\n"
                                % (SHA, "f" * 40)})
        detached = self.git(os.path.join(self.dir, "detached"), {".git/HEAD": SHA + "\n"})
        for root in (loose, packed, detached):
            with self.subTest(root=os.path.basename(root)):
                self.assertEqual(livetrace.git_commit(root), SHA)

    def test_a_worktree_reads_its_own_head_and_the_shared_refs(self):
        main = self.git(os.path.join(self.dir, "main"), {".git/refs/heads/feature": SHA + "\n",
                                                          ".git/worktrees/wt/HEAD": "ref: refs/heads/feature\n",
                                                          ".git/worktrees/wt/commondir": "../..\n"})
        wt = self.git(os.path.join(self.dir, "wt"), {".git": "gitdir: %s\n" % os.path.join(main, ".git", "worktrees", "wt")})
        self.assertEqual(livetrace.git_commit(wt), SHA)

    def test_no_repository_or_one_it_cannot_read_is_unknown(self):
        bare = os.path.join(self.dir, "none")
        os.makedirs(bare)
        odd = self.git(os.path.join(self.dir, "odd"), {".git/HEAD": "ref: refs/heads/gone\n"})
        bad = self.git(os.path.join(self.dir, "bad"), {".git/HEAD": "not a head\n"})
        short = self.git(os.path.join(self.dir, "short"), {".git/HEAD": "ref: refs/heads/x\n",
                                                            ".git/refs/heads/x": "abc123\n"})
        for root in (bare, odd, bad, short):
            with self.subTest(root=os.path.basename(root)):
                self.assertEqual(livetrace.git_commit(root), "unknown")

    @unittest.skipUnless(os.path.exists(os.path.join(paths.REPO, ".git")) and shutil.which("git"),
                         "this checkout has no .git folder or no git program")
    def test_this_checkout_is_read_as_git_itself_reads_it(self):
        out = subprocess.run(["git", "-C", paths.REPO, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True)
        if out.returncode != 0:
            self.skipTest("git cannot read this checkout")
        self.assertEqual(livetrace.git_commit(paths.REPO), out.stdout.strip())


class CostAndCountsTest(Tmp):

    def test_the_cost_is_what_each_model_calls_own_result_line_names(self):
        path = self.write_chain([START] + model_call(0.0012) + model_call(0.0012) + [
            ("called", {"call": "send", "target": "T2", "to": "sender"}),
            ("result", {"call_seq": None, "ok": True, "state": "handed_over", "cost_usd": 9.0}),
            ("called", {"call": "model", "target": "claude-sonnet-5-5"}),
            ("result", {"call_seq": None, "ok": False, "cost_usd": True}),
            ("summary", {"handled": 1})])
        lines = receipts.read_lines(path)
        self.assertEqual(livetrace.measured_cost(lines), 0.0024)
        self.assertEqual(livetrace.measured_cost([]), 0.0)

    def test_the_counts_come_from_the_receipts(self):
        path = self.write_chain([START, ("observed", {"server_id": "INBOX:1:1"})] + model_call(0.0012) + [
            ("checked", {"passed": True}), ("called", {"call": "send", "target": "T2", "to": "sender"}),
            ("result", {"call_seq": None, "ok": True, "state": "handed_over"}),
            ("decided", {"outcome": "offered"}), ("observed", {"server_id": "INBOX:1:2"}),
            ("error", {"step": "classify"}), ("held", {"outcome": "hold:model_down"}),
            ("summary", {"handled": 1, "held": 1})])
        self.assertEqual(livetrace.counts(receipts.read_lines(path)),
                         {"messages": 2, "model_calls": 1, "sends": 1, "errors": 1, "handled": 1, "held": 1,
                          "human_approvals": 0})


class JudgeTest(Tmp):
    """Pass only when the chain verifies whole, no line is an error, the run did not end on failed
    reads, and it ended by a person's stop (or by itself, exit 0). Holds are her answers, not failures."""

    def judged(self, lines, exit_code):
        return livetrace.judge_run(self.write_chain([START] + lines), exit_code)

    def test_a_persons_stop_is_how_a_long_run_ends_well(self):
        for cause in ("cli", "stop_file", "watch_button", "signal"):
            with self.subTest(cause=cause):
                if os.path.exists(self.receipts):
                    os.remove(self.receipts)
                self.assertEqual(self.judged([("held", {"outcome": "hold:calendar_down"}),
                                              ("halted", {"cause": cause, "worker_exit": 4})], 4), ("pass", None))

    def test_a_run_that_ends_by_itself_passes_on_exit_0_only(self):
        self.assertEqual(self.judged([("summary", {"read_failed": False})], 0), ("pass", None))
        os.remove(self.receipts)
        self.assertEqual(self.judged([("summary", {"read_failed": False})], 1),
                         ("fail", "the run ended with exit 1"))

    def test_the_budget_cap_or_a_halt_file_it_could_not_read_fails(self):
        self.assertEqual(self.judged([("halted", {"cause": "budget_cap"})], 4), ("fail", "halted: budget_cap"))
        os.remove(self.receipts)
        self.assertEqual(self.judged([("halted", {"cause": "stop_file", "halt_file": "unreadable"})], 4),
                         ("fail", "halted: a halt file that could not be read"))

    def test_the_first_error_line_is_the_first_failing_step(self):
        self.assertEqual(self.judged([("error", {"step": "fetch"}), ("error", {"step": "classify"}),
                                      ("halted", {"cause": "cli"})], 4),
                         ("fail", "error line at seq 2 (fetch)"))

    def test_failed_reads_that_ended_the_run_fail_it(self):
        self.assertEqual(self.judged([("summary", {"read_failed": True})], 1),
                         ("fail", "the inbox could not be read, so the run ended"))

    def test_a_chain_that_does_not_verify_whole_fails_naming_why(self):
        outcome, first = self.judged([("observed", {"server_id": "INBOX:1:1"})], 1)
        self.assertEqual(outcome, "fail")
        self.assertTrue(first.startswith("receipt chain: no closing line"), first)
        with open(self.receipts, "rb") as fh:
            data = fh.read()
        with open(self.receipts, "wb") as fh:
            fh.write(data.replace(b"INBOX:1:1", b"INBOX:1:9"))
        outcome, first = livetrace.judge_run(self.receipts, 4)
        self.assertEqual((outcome, first.split(":")[0]), ("fail", "receipt chain"))


class FolderTest(Tmp):

    def test_one_new_folder_per_start_named_for_its_kind_and_utc_second(self):
        root = os.path.join(self.dir, "traces")
        start = dt.datetime(2026, 12, 1, 8, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=-8)))
        first = livetrace.folder_for("smoke", start, root)
        second = livetrace.folder_for("smoke", start, root)
        self.assertEqual(os.path.basename(first), "live-smoke-20261201T160005Z")
        self.assertEqual(os.path.basename(second), "live-smoke-20261201T160005Z-2")
        self.assertEqual(os.path.basename(livetrace.folder_for("run", start, root)), "live-run-20261201T160005Z")
        with self.assertRaises(ValueError):
            livetrace.folder_for("demo", start, root)


class WriteTest(Tmp):

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.dir, "traces")
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        with open(os.path.join(self.repo, ".git", "HEAD"), "w") as fh:
            fh.write(SHA + "\n")
        self.said = []
        times = iter(T0 + dt.timedelta(seconds=n) for n in range(100))
        self.term = livetrace.Printed(out=self.said.append, clock=lambda: next(times))

    def files(self, folder):
        out = {}
        for name in sorted(os.listdir(folder)):
            with open(os.path.join(folder, name), "rb") as fh:
                out[name] = fh.read()
        return out

    def test_the_folder_holds_the_receipts_as_written_a_manifest_a_timeline_and_the_terminal(self):
        path = self.write_chain([START, ("observed", {"server_id": "INBOX:1:1"})] + model_call(0.0024) + [
            ("checked", {"passed": True}), ("called", {"call": "send", "target": "T2", "to": "sender"}),
            ("result", {"call_seq": None, "ok": True, "state": "handed_over"}),
            ("called", {"call": "sent_lookup", "target": "T2"}),
            ("result", {"call_seq": None, "ok": True, "found": True}),
            ("decided", {"outcome": "offered"}), ("halted", {"cause": "cli"})])
        self.term("Live. Her own inbox every 10 minutes.")
        self.term("Halted: stop on the command line.")
        folder = livetrace.write("run", T0, T0 + dt.timedelta(minutes=20), "pass", None, printed=self.term,
                                 receipts_path=path, extra={"run_name": RUN, "exit_code": 4},
                                 traces_root=self.root, repo=self.repo)
        self.assertEqual(folder, os.path.join(self.root, "live-run-20261201T160000Z"))
        files = self.files(folder)
        self.assertEqual(sorted(files), ["manifest.json", "receipts.jsonl", "terminal.log", "timeline.txt"])
        with open(path, "rb") as fh:
            self.assertEqual(files["receipts.jsonl"], fh.read())
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
        self.assertEqual(manifest, {
            "mode": "live", "kind": "run", "started_utc": "2026-12-01T16:00:00Z", "ended_utc": "2026-12-01T16:20:00Z",
            "commit": SHA, "model": "claude-sonnet-5-5", "cost_usd": 0.0024,
            "counts": {"messages": 1, "model_calls": 1, "sends": 1, "errors": 0, "handled": 1, "held": 0,
                       "human_approvals": 0},
            "receipt_chain": {"ok": True, "lines": 11, "reason": "ok"}, "outcome": "pass", "first_failing_step": None,
            "files": ["manifest.json", "receipts.jsonl", "terminal.log", "timeline.txt"],
            "run_name": RUN, "exit_code": 4})
        self.assertEqual(files["timeline.txt"].decode("utf-8").splitlines(), [
            "2026-12-01T16:00:00Z  Live. Her own inbox every 10 minutes.",
            "2026-12-01T16:00:01Z  Halted: stop on the command line.",
            "her loop's receipts (run %s):" % RUN,
            "2026-12-01T16:00:00Z  seq 1  run started: mode live, model claude-sonnet-5-5",
            "2026-12-01T16:00:01Z  seq 2  message read",
            "2026-12-01T16:00:04Z  seq 5  gate passed",
            "2026-12-01T16:00:06Z  seq 7  send T2 to the sender: handed_over",
            "2026-12-01T16:00:08Z  seq 9  her copy in Sent: found",
            "2026-12-01T16:00:09Z  seq 10  decided offered",
            "2026-12-01T16:00:10Z  seq 11  halted: cli"])
        trace_line = "Live trace kept in %s: outcome pass." % folder
        self.assertEqual(self.said[-1], trace_line)
        self.assertEqual(files["terminal.log"].decode("utf-8"), "".join(line + "\n" for line in self.said))

    def test_a_failed_run_writes_the_same_folder_with_outcome_fail_and_its_first_failing_step(self):
        path = self.write_chain([START, ("error", {"step": "fetch"}), ("summary", {"read_failed": True})])
        folder = livetrace.write("run", T0, T0, "fail", "error line at seq 2 (fetch)", printed=self.term,
                                 receipts_path=path, traces_root=self.root, repo=self.repo)
        manifest = json.loads(self.files(folder)["manifest.json"].decode("utf-8"))
        self.assertEqual((manifest["outcome"], manifest["first_failing_step"]), ("fail", "error line at seq 2 (fetch)"))
        self.assertEqual(self.said[-1], "Live trace kept in %s: outcome fail (first failing step: error line at seq 2 "
                                        "(fetch))." % folder)

    def test_with_no_loop_run_there_are_no_receipts_and_the_chain_says_so(self):
        self.term("imap login and inbox count: ok (2 messages in INBOX)")
        folder = livetrace.write("smoke", T0, T0, "fail", "end-to-end test (not run: no --send-to)", printed=self.term,
                                 traces_root=self.root, repo=self.repo, model_id="claude-sonnet-5-5")
        files = self.files(folder)
        self.assertEqual(sorted(files), ["manifest.json", "terminal.log", "timeline.txt"])
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
        self.assertEqual(manifest["receipt_chain"], {"ok": None, "lines": 0, "reason": "no loop ran"})
        self.assertEqual((manifest["model"], manifest["cost_usd"], manifest["files"]),
                         ("claude-sonnet-5-5", 0.0, ["manifest.json", "terminal.log", "timeline.txt"]))
        self.assertEqual(files["timeline.txt"].decode("utf-8"),
                         "2026-12-01T16:00:00Z  imap login and inbox count: ok (2 messages in INBOX)\n")

    def test_as_on_windows_every_file_it_writes_ends_its_lines_in_a_bare_newline(self):
        path = self.write_chain([START, ("halted", {"cause": "cli"})])
        self.term("Live. Her own inbox every 10 minutes.")
        with as_on_windows():
            folder = livetrace.write("run", T0, T0, "pass", None, printed=self.term, receipts_path=path,
                                     traces_root=self.root, repo=self.repo)
        files = self.files(folder)
        self.assertEqual(sorted(files), ["manifest.json", "receipts.jsonl", "terminal.log", "timeline.txt"])
        for name, data in files.items():
            self.assertNotIn(b"\r", data, name)

    def test_the_folder_is_named_from_the_repository_root_with_slashes_on_every_os(self):
        """On Windows os.path.relpath gives traces\\live-smoke-<stamp>: the line shows
        traces/live-smoke-<stamp> there too. A folder outside the repository is shown whole."""
        with mock.patch.object(os, "sep", "\\"), mock.patch.object(os.path, "relpath", ntpath.relpath), \
                mock.patch.object(paths, "REPO", "C:\\doris"):
            self.assertEqual(livetrace._display("C:\\doris\\traces\\live-smoke-20261201T160000Z"),
                             "traces/live-smoke-20261201T160000Z")
            self.assertEqual(livetrace._display("D:\\runs\\live-run-20261201T160000Z"),
                             "D:\\runs\\live-run-20261201T160000Z")
        with mock.patch.object(paths, "REPO", self.repo):
            folder = livetrace.write("smoke", T0, T0, "pass", None, printed=self.term,
                                     traces_root=os.path.join(self.repo, "traces"), repo=self.repo)
        self.assertEqual(self.said[-1], "Live trace kept in traces/live-smoke-20261201T160000Z: outcome pass.")
        self.assertEqual(folder, os.path.join(self.repo, "traces", "live-smoke-20261201T160000Z"))

    def test_the_outcome_is_pass_or_fail(self):
        with self.assertRaises(ValueError):
            livetrace.write("run", T0, T0, "ok", None, printed=self.term, traces_root=self.root, repo=self.repo)
        self.assertFalse(os.path.exists(self.root))


class PrintedTest(Tmp):

    def test_each_line_is_printed_kept_with_its_time_and_added_to_its_file_at_once(self):
        """As on Windows too: every line ends in a bare \\n, the other process's (the worker's own
        Printed on the same file) as much as this one's."""
        said, path = [], os.path.join(self.dir, "terminal.log")
        with as_on_windows():
            term = livetrace.Printed(out=said.append, path=path, clock=lambda: T0)
            term("one")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"one\n")
            livetrace.Printed(out=lambda line: None, path=path)("another process's line")
            term("two")
        self.assertEqual(said, ["one", "two"])
        self.assertEqual(term.lines, [(T0, "one"), (T0, "two")])
        self.assertEqual(term.text(), "one\nanother process's line\ntwo\n")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"one\nanother process's line\ntwo\n")

    def test_without_a_file_its_text_is_its_own_lines_and_a_file_it_cannot_write_still_prints(self):
        said = []
        term = livetrace.Printed(out=said.append, path=os.path.join(self.dir, "no", "such", "folder", "t.log"))
        term("still said")
        self.assertEqual(said, ["still said"])
        self.assertEqual(term.text(), "still said\n")
        bare = livetrace.Printed(out=said.append)
        bare("x")
        self.assertEqual(bare.text(), "x\n")


if __name__ == "__main__":
    unittest.main()
