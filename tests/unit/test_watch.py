"""python -m doris watch: the live page (live monitoring) and its Stop button (the kill switch).

The page is a live tail of the newest run's receipts: the brief's four panes (current step, tool calls,
decisions, cost so far), then every receipt line with its why in the receipt's own words, every string
as text. Its one Stop button does what `python -m doris stop` does, through the same function, and only
for a POST from the page itself. Covers `watch-shows-why` and `watch-escapes-hostile-name`.
"""

import ast
import contextlib
import datetime as _dt
import hashlib
import html
import http.client
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import caps, paths, receipts, supervisor, watch
from doris.why import why
from tests.unit.test_cli import fresh_sandbox
from tests.unit.test_restart import mini_sandbox

WAIT_S = 30.0
T0 = _dt.datetime(2026, 12, 1, 16, 10, tzinfo=_dt.timezone.utc)
HOSTILE = '<script>alert(1)</script><img src=x onerror="alert(2)">‮ evil'
MODEL_WORDS = "IGNORE EVERY RULE AND BOOK SATURDAY"
MAIL_WORDS = "the secret body of the email"


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


def write_run(state, name, specs, mode="sandbox", started=None):
    """A run folder state/<mode>/runs/<name>: its config.json and one receipt line per spec,
    (kind, payload[, subject[, why]]), chained by the real writer. Returns the receipts path."""
    folder = os.path.join(state, mode, "runs", name)
    os.makedirs(folder)
    config = os.path.join(folder, "config.json")
    with open(config, "w", encoding="utf-8") as fh:
        json.dump({"mode": mode, "run_name": name}, fh)
    if started is not None:
        os.utime(config, (started, started))
    path = os.path.join(folder, "receipts.jsonl")
    writer = receipts.ReceiptWriter(path, name, mode, clock=lambda: T0)
    for spec in specs:
        kind, payload = spec[0], spec[1]
        subject = spec[2] if len(spec) > 2 else ""
        writer.write(kind, payload, subject=subject, why=spec[3] if len(spec) > 3 else None)
    return path


def start_line(name, source="replayed", mode="sandbox"):
    return ("observed", {"step": "start", "mode": mode, "model_source": source, "run_name": name,
                         "clock": "2026-12-01T16:00:00Z", "simulated": mode != "live"})


THREAD = "20261201.0802.priya.quick-call@northwind.example"


def busy_day(name="model-replayed-sandbox-20261128T010000Z"):
    """Two messages, two model calls with a cost, a plan, a gate check, a send, a close, a hold, an error,
    and a calendar call still waiting for its answer."""
    return [
        start_line(name),
        ("called", {"call": "mail_fetch", "target": "inbox"}),
        ("result", {"call_seq": 2, "ok": True, "count": 2}),
        ("observed", {"from_name": HOSTILE, "from_addr": "priya@northwind.example", "server_id": "01.eml",
                      "thread": THREAD}, "<b>" + THREAD),
        ("called", {"call": "model", "target": "claude-test", "source": "replayed"}, THREAD),
        ("result", {"call_seq": 5, "ok": True, "cost_usd": 0.0012, "source": "replayed"}, THREAD),
        ("classified", {"cls": "meeting_request", "ok": True, "source": "replayed",
                        "raw": json.dumps({"cls": "meeting_request", "note": MODEL_WORDS})}, THREAD),
        ("decided", {"step": "plan", "action": "offer", "template": "T2", "thread": THREAD}, THREAD,
         why("policy.offer_no_clear_time", cond="B2")),
        ("checked", {"action": "send_reply", "template": "T2", "passed": True, "first_fail": None,
                     "halt_cause": None, "checks": {}}, THREAD, why("gate.pass", n=11)),
        ("called", {"call": "send", "target": "T2", "to": "sender", "body_sha256": "ab" * 32}, THREAD),
        ("result", {"call_seq": 10, "ok": True, "state": "handed_over"}, THREAD),
        ("decided", {"outcome": "offer", "template": "T2", "thread": THREAD}, THREAD,
         why("policy.offer_no_clear_time", cond="B2")),
        ("observed", {"from_name": "Elena Voss", "from_addr": "elena@harborline.example", "server_id": "02.eml",
                      "simulated": {"fault": "slow-calendar"}}, "elena.pricing@harborline.example"),
        ("called", {"call": "model", "target": "claude-test", "source": "replayed"}, "elena.pricing@harborline.example"),
        ("result", {"call_seq": 14, "ok": True, "cost_usd": 0.003, "source": "replayed"},
         "elena.pricing@harborline.example"),
        ("held", {"outcome": "hold:money", "template": "-"}, "elena.pricing@harborline.example", why("hold.money")),
        ("error", {"step": "file", "error": "MailError: %s" % MAIL_WORDS}, "elena.pricing@harborline.example"),
        ("called", {"call": "calendar_query", "target": "span", "attempt": 1}, "elena.pricing@harborline.example"),
    ]


def halted_payload(seq):
    return {"cause": "watch_button", "dropped": [
        {"kind": "send_reply", "thread": "t1@a.example", "message": "m1@a.example", "template": "T4",
         "booking_id": None},
        {"kind": "message", "thread": "t3@c.example", "message": "m3@c.example", "template": None,
         "booking_id": None}],
        "in_flight": {"seq": seq, "call": "calendar_create", "target": "doris-b2", "unconfirmed": True},
        "ledger": "ok", "requested_utc": "2026-12-01T16:10:00Z", "stop_to_dead_ms": 112, "torn_tail": None,
        "worker_exit": -9}


def forge(path, kind, payload, subject="", why_=None):
    """Append one line chained the way the writer chains, with a payload the writer itself would refuse:
    anyone can recompute the hashes, so verify passes it and the page must still show itself."""
    lines = receipts.read_lines(path)
    last = lines[-1]
    line = {"ts": last["ts"], "run_id": last["run_id"], "mode": last["mode"], "seq": last["seq"] + 1,
            "kind": kind, "subject": subject, "payload": payload, "prev": last["hash"]}
    if why_ is not None:
        line["why"] = why_
    line["hash"] = hashlib.sha256(last["hash"].encode("ascii") + receipts.canonical(line)).hexdigest()
    with open(path, "ab") as fh:
        fh.write(receipts.canonical(line) + b"\n")


def boot_of(page):
    (boot,) = re.findall(r'data-boot="([0-9a-f]+)"', page)
    return boot


class TempState(unittest.TestCase):
    """paths.STATE is a folder of this test's own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        patcher = mock.patch.object(paths, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def look(self):
        return watch.snapshot(watch.newest_run())

    def look_at(self, name, mode="live"):
        folder = os.path.join(self.state, mode, "runs", name)
        return watch.snapshot(watch.Run(name, folder, mode, 0, True, False, True))


def serve(test, say=None):
    """A watch server on a free port in a thread of its own, shut down at the test's end."""
    server = watch.WatchServer(0, say=say if say is not None else (lambda text: None))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              name="doris-watch-test", daemon=True)
    thread.start()

    def close():
        server.shutdown()
        server.server_close()
        thread.join(WAIT_S)
    test.addCleanup(close)
    return server


def ask(server, method, path, headers=None, body=None, host=None):
    """(status, headers, body bytes) for one request, every header as given (Host included)."""
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT_S)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else "127.0.0.1:%d" % port)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        data = body.encode("ascii") if body is not None else None
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def listing(root):
    """Every file under root with its size and mtime: what a reader must leave as it found it."""
    out = {}
    for folder, _dirs, files in os.walk(root):
        for name in files:
            st = os.stat(os.path.join(folder, name))
            out[os.path.join(folder, name)] = (st.st_size, st.st_mtime_ns)
    return out


def token_of(page):
    (token,) = re.findall(r'name="token" value="([^"]+)"', page)
    return token


def form_headers(server):
    port = server.server_address[1]
    return {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:%d" % port,
            "Sec-Fetch-Site": "same-origin"}


def press(server):
    """Stop, the way the page's own button sends it."""
    status, _, page = ask(server, "GET", "/")
    assert status == 200, status
    return ask(server, "POST", "/stop", form_headers(server), "token=" + token_of(page.decode("utf-8")))


def state_json_tip(server, run, after, tip):
    status, _, body = ask(server, "GET", "/state?run=%s&after=%d&tip=%s" % (run, after, tip))
    assert status == 200, (status, body)
    return json.loads(body.decode("ascii"))


def state_json(server, run="", after=0):
    status, headers, body = ask(server, "GET", "/state?run=%s&after=%d" % (run, after))
    assert status == 200, (status, body)
    assert headers["content-type"].startswith("application/json"), headers
    return json.loads(body.decode("ascii"))


# ---------------------------------------------------------------------------------------------------
# A real sandbox run: every line on the page, its why in the receipt's own words, the right counts
# ---------------------------------------------------------------------------------------------------
class SandboxDay(unittest.TestCase):
    """The demo day, run once for the class under the kill switch, on a copy of the sandbox."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doris-watch-day-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        sandbox = fresh_sandbox(cls.tmp)
        state = os.path.join(cls.tmp, "state")
        for patcher in (mock.patch.object(paths, "SANDBOX", sandbox), mock.patch.object(paths, "STATE", state),
                        mock.patch.object(caps, "CALENDAR_TIMEOUT_S", 0.3),
                        mock.patch.object(caps, "CALENDAR_RETRY_WAIT_S", 0.0),
                        mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DORIS_MODEL": ""})):
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        code, out = run_cli(["demo"])
        assert code == 0, out
        cls.day = watch.newest_run()
        cls.lines = receipts.read_lines(os.path.join(cls.day.folder, "receipts.jsonl"))
        cls.snap = watch.snapshot(cls.day)
        cls.page = watch.render_page(cls.snap, token="t0ken", nonce="n0nce")

    def test_every_receipt_line_is_a_row_with_its_why_in_the_receipts_own_words(self):
        rows = {r["seq"]: r for r in self.snap["rows"]}
        self.assertEqual(sorted(rows), [line["seq"] for line in self.lines])
        whys = 0
        for line in self.lines:
            r = rows[line["seq"]]
            self.assertEqual(r["hash"], line["hash"][:8])
            if line["kind"] in receipts.WHY_KINDS:
                whys += 1
                self.assertEqual((r["why"], r["code"]), (line["why"]["text"], line["why"]["code"]))
                self.assertIn(html.escape(line["why"]["text"]), self.page)
            else:
                self.assertEqual((r["why"], r["code"]), ("", ""))
        self.assertGreater(whys, 30)
        self.assertEqual([r["seq"] for r in self.snap["rows"]], sorted(rows))      # oldest first in the data
        page_seqs = [int(s) for s in re.findall(r'<td class="seq">(\d+)<span', self.page)]
        self.assertEqual(page_seqs, sorted(rows, reverse=True))                      # newest first on the page

    def test_the_four_panes_carry_the_briefs_labels_and_this_runs_counts_and_cost(self):
        summary = self.lines[-1]["payload"]
        panes = self.snap["panes"]
        self.assertEqual([p["label"] for p in panes], ["Current step", "Tool calls", "Decisions", "Cost so far"])
        for label in ("Current step", "Tool calls", "Decisions", "Cost so far"):
            self.assertIn("<h2>%s</h2>" % label, self.page)
        finished = "Finished: %d handled, %d held, %s." % (
            summary["handled"], summary["held"],
            "1 human approval" if summary["human_approvals"] == 1 else "%d human approvals" % summary["human_approvals"])
        self.assertEqual(panes[0]["value"], finished)
        self.assertEqual(panes[1]["value"], str(sum(1 for l in self.lines if l["kind"] == "called")))
        self.assertEqual(panes[2]["value"], str(sum(1 for l in self.lines if l["kind"] in receipts.WHY_KINDS)))
        self.assertEqual(panes[3]["value"], "none (stub)")                 # as the terminal says it
        self.assertEqual(panes[3]["note"], "%d model calls." % summary["model_calls"])
        self.assertEqual(self.snap["status"], finished)
        self.assertTrue(self.snap["chain_ok"], self.snap["chain"])
        self.assertIn("%d lines" % len(self.lines), self.snap["chain"])

    def test_the_title_marks_where_the_model_answers_came_from(self):
        self.assertTrue(self.day.name.startswith("model-stub-sandbox-"), self.day.name)
        self.assertEqual(self.snap["title"], "Doris watch: " + self.day.name)
        self.assertIn("<title>Doris watch: %s</title>" % self.day.name, self.page)
        self.assertIn("stub", self.snap["source"])

    def test_no_model_words_reach_the_page(self):
        raws = [l["payload"]["raw"] for l in self.lines if l["kind"] == "classified"]
        self.assertTrue(raws)
        blob = self.page + json.dumps(self.snap)
        for raw in raws:
            self.assertNotIn(raw, blob)
            self.assertNotIn(html.escape(raw), blob)


# ---------------------------------------------------------------------------------------------------
# Runs written line by line: panes, escaping, what never reaches the page, the chain
# ---------------------------------------------------------------------------------------------------
class Panes(TempState):
    def test_no_run_yet_says_how_to_start_one(self):
        snap = self.look()
        self.assertEqual((snap["run"], snap["rows"], snap["state"]), ("", [], "none"))
        self.assertIn("python -m doris demo", snap["empty"])
        self.assertEqual(snap["title"], "Doris watch: no run yet")
        page = watch.render_page(snap, token="t", nonce="n")
        self.assertIn("<h2>Current step</h2>", page)

    def test_a_waiting_call_is_the_current_step_and_the_cost_sums_the_models_answers(self):
        path = write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        os.utime(path, (1795219200, 1795219200))
        snap = self.look()
        current, calls, decisions, cost = snap["panes"]
        self.assertEqual(snap["state"], "waiting")
        # the trace shows a call with no answer, not that she is alive: it says when its last line landed
        self.assertEqual(current["value"], "Tool call: calendar_query span, no answer yet.")
        self.assertEqual(current["note"], "Line 18. No result line answers it yet.")
        self.assertEqual((calls["value"], calls["note"]), ("5", "1 with no answer yet: calendar_query span (line 18)."))
        self.assertEqual((decisions["value"], decisions["note"]),
                         ("4", "Lines with a why: 1 plan, 1 close, 1 gate check, 1 hold. "
                               "Latest: Held: money in the sender's own words."))
        self.assertEqual((cost["value"], cost["note"]), ("$0.0042 (recorded)", "2 model calls."))
        self.assertEqual(snap["status"], "No closing line yet: 1 handled, 1 held, 0 human approvals so far. "
                                         "Last line landed at 2026-11-21 00:00:00 UTC.")
        self.assertIn("replayed", snap["source"])

    def test_a_live_run_costs_what_was_measured(self):
        name = "model-live-live-20261128T010000Z"
        write_run(self.state, name, [
            start_line(name, source="live", mode="live"),
            ("called", {"call": "model", "target": "claude-test", "source": "live"}),
            ("result", {"call_seq": 2, "ok": True, "cost_usd": 0.01234, "source": "live"}),
        ], mode="live")
        snap = self.look()
        self.assertEqual(snap["panes"][3]["value"], "$0.0123")
        self.assertEqual(snap["panes"][3]["note"], "1 model call.")
        self.assertIn("Live", snap["source"])

    def test_a_halted_run_names_the_call_in_flight_and_the_dropped_work(self):
        name = "model-stub-sandbox-20261128T010000Z"
        write_run(self.state, name, [
            start_line(name, source="stub"),
            ("called", {"call": "calendar_create", "target": "doris-b2", "attempt": 1}, "m2@b.example"),
            ("halted", halted_payload(2), "", why("halt.watch_button")),
        ])
        snap = self.look()
        current, calls, _, _ = snap["panes"]
        self.assertEqual(snap["state"], "halted")
        self.assertEqual(current["value"], "Halted: Stop pressed on the watch page.")
        self.assertIn("calendar_create doris-b2 (line 2) was in flight, unconfirmed", current["note"])
        self.assertIn("dropped 2: send_reply T4, message", current["note"])
        self.assertEqual(calls["note"], "Cut off by the stop: calendar_create doris-b2 (line 2).")
        halted = snap["rows"][-1]
        self.assertEqual((halted["step"], halted["tone"], halted["code"]), ("halted", "halted", "halt.watch_button"))
        self.assertIn("Worker dead 112 ms after the stop.", halted["what"])

    def test_a_hostile_display_name_shows_as_text(self):
        write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        snap = self.look()
        page = watch.render_page(snap, token="t", nonce="n")
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>" + THREAD, page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;&lt;img src=x onerror=&quot;alert(2)&quot;&gt;", page)
        self.assertIn("&lt;b&gt;" + THREAD, page)
        read = [r for r in snap["rows"] if r["step"] == "read"][0]
        self.assertIn("<script>alert(1)</script>", read["what"])         # the data stays as written: text
        self.assertNotIn("‮", page + json.dumps(snap, ensure_ascii=False))   # no right-to-left override

    def test_no_model_words_no_mail_body_and_no_error_text_reach_the_page(self):
        write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        snap = self.look()
        blob = watch.render_page(snap, token="t", nonce="n") + json.dumps(snap)
        self.assertNotIn(MODEL_WORDS, blob)
        self.assertNotIn(MAIL_WORDS, blob)
        error = [r for r in snap["rows"] if r["step"] == "error"][0]
        self.assertEqual(error["what"], "Error in file: MailError.")
        classified = [r for r in snap["rows"] if r["step"] == "classified"][0]
        self.assertEqual(classified["what"], "Classified as meeting_request (model replayed).")

    def test_a_staged_fault_is_labelled_on_the_message_it_slows(self):
        write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        read = [r for r in self.look()["rows"] if r["step"] == "read"][1]
        self.assertEqual(read["what"], "Read a message from Elena Voss <elena@harborline.example>. "
                                       "Staged fault: slow-calendar.")

    def test_a_changed_byte_is_said_loudly(self):
        path = write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(data.replace(b'"target":"inbox"', b'"target":"inbax"', 1))
        snap = self.look()
        self.assertFalse(snap["chain_ok"])
        self.assertIn("BROKEN at line 2", snap["chain"])
        self.assertIn('data-chain="broken"', watch.render_page(snap, token="t", nonce="n"))

    def test_a_line_still_being_written_is_not_an_alarm(self):
        path = write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with open(path, "ab") as fh:
            fh.write(b'{"hash":"12')
        snap = self.look()
        self.assertTrue(snap["chain_ok"], snap["chain"])
        self.assertIn("verified through line 18", snap["chain"])
        self.assertEqual(len(snap["rows"]), 18)

    def test_the_newest_run_started_is_the_one_watched(self):
        now = time.time()
        write_run(self.state, "model-stub-sandbox-20261128T010000Z", [start_line("a", "stub")], started=now - 60)
        write_run(self.state, "model-live-live-20261128T000000Z", [start_line("b", "live", "live")], mode="live",
                  started=now - 30)
        self.assertEqual(watch.newest_run().name, "model-live-live-20261128T000000Z")
        write_run(self.state, "model-stub-sandbox-20261128T020000Z", [start_line("c", "stub")], started=now)
        run = watch.newest_run()
        self.assertEqual((run.name, run.mode), ("model-stub-sandbox-20261128T020000Z", "sandbox"))

    def test_the_smokes_own_run_of_her_loop_is_watched_and_its_stop_is_said_plainly(self):
        now = time.time()
        older = "model-live-live-20261128T050000Z"
        write_run(self.state, older, [start_line(older, "live", "live")], mode="live", started=now - 60)
        stamp = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = "model-live-live-smoke-" + stamp
        folder = os.path.join(self.state, "live", "smoke", stamp, "runs", name)
        os.makedirs(folder)                 # where smoke.py runs her loop: no config.json, its own ledger
        writer = receipts.ReceiptWriter(os.path.join(folder, "receipts.jsonl"), name, "live", clock=lambda: T0)
        writer.write(*start_line(name, "live", "live"))
        run = watch.newest_run()
        self.assertEqual((run.name, run.mode, run.folder), (name, "live", folder))
        snap = self.look()
        self.assertEqual(snap["run"], name)
        # her loop under the smoke has no supervisor to kill it mid-call: the page must not imply one
        self.assertIn("The smoke's own run of her loop", snap["source"])
        self.assertIn("a Stop halts such a run at her next check, after any call in flight returns, not mid-call",
                      snap["source"])
        page = watch.render_page(snap, token="t", nonce="n", pressed=watch.pressed_words(
            [("sandbox", "watch_button"), ("live", "watch_button")], [], _dt.datetime(2026, 11, 28, tzinfo=_dt.timezone.utc)))
        self.assertNotRegex(page.replace("not mid-call", ""), r"(?<!kill switch is killed )mid-call")
        self.assertNotIn("smoke", self.look_at(older)["source"])

    def test_a_smoke_run_started_when_its_stamp_says_not_when_its_last_line_landed(self):
        now = time.time()
        stamp = _dt.datetime.fromtimestamp(now - 100, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        smoke = "model-live-live-smoke-" + stamp
        going = "model-stub-sandbox-20261128T060000Z"
        write_run(self.state, going, [start_line(going, "stub")], started=now - 50)
        folder = os.path.join(self.state, "live", "smoke", stamp, "runs", smoke)
        os.makedirs(folder)
        writer = receipts.ReceiptWriter(os.path.join(folder, "receipts.jsonl"), smoke, "live", clock=lambda: T0)
        writer.write(*start_line(smoke, "live", "live"))     # its closing line lands now, after the sandbox run began
        self.assertEqual(watch.newest_run().name, going)

    def test_a_yes_man_run_is_named_for_what_answers(self):
        folder = os.path.join(self.state, "sandbox", "runs", "model-yes-man-sandbox-20261128T030000Z")
        os.makedirs(folder)
        with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertIn("yes-man", self.look()["source"])

    def test_a_run_whose_receipts_are_not_written_yet_is_starting(self):
        folder = os.path.join(self.state, "sandbox", "runs", "model-stub-sandbox-20261128T030000Z")
        os.makedirs(folder)
        with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        snap = self.look()
        self.assertEqual((snap["run"], snap["state"], snap["rows"]),
                         ("model-stub-sandbox-20261128T030000Z", "starting", []))
        self.assertIn("stub", snap["source"])


    def test_a_line_that_is_not_json_shows_the_lines_before_it_never_a_fresh_run(self):
        path = write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with open(path, "rb") as fh:
            lines = fh.read().split(b"\n")
        lines[3] = b"{not json"
        with open(path, "wb") as fh:
            fh.write(b"\n".join(lines))
        snap = self.look()
        self.assertFalse(snap["chain_ok"])
        self.assertEqual(snap["state"], "broken")
        self.assertIn("BROKEN at line 4", snap["chain"])
        self.assertEqual([r["seq"] for r in snap["rows"]], [1, 2, 3])
        self.assertEqual(snap["status"], "Chain broken at line 4: only the 3 lines before it are shown and counted.")
        self.assertEqual(snap["panes"][1]["value"], "1")
        self.assertNotIn("Starting", json.dumps(snap))

    def test_a_first_line_that_cannot_be_trusted_is_never_starting(self):
        path = write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(data.replace(b'"step":"start"', b'"step":"stort"', 1))
        snap = self.look()
        self.assertEqual((snap["state"], snap["rows"]), ("broken", []))
        self.assertEqual(snap["status"], "Chain broken at line 1: no line can be trusted, so none is shown.")

    def test_odd_values_in_a_rechained_line_never_take_the_page_away(self):
        name = "model-replayed-sandbox-20261128T010000Z"
        path = write_run(self.state, name, [
            start_line(name),
            ("called", {"call": "model", "target": "claude-test", "source": "replayed"}),
        ])
        forge(path, "result", {"call_seq": [2], "ok": True, "cost_usd": 10 ** 400})
        forge(path, "result", {"call_seq": {}, "ok": [], "cost_usd": [0.5]})
        forge(path, "called", {"call": [], "target": {}, "attempt": [3], "to": {}})
        forge(path, "observed", {"step": "start", "mode": [], "model_source": {}})
        forge(path, "decided", {"outcome": [], "template": {}, "booking_id": []}, why_={"code": [], "text": {}})
        forge(path, "halted", {"cause": [], "in_flight": {"seq": [], "call": {}}, "dropped": [[], {"kind": []}],
                               "stop_to_dead_ms": 10 ** 400}, why_={"code": "halt.cli", "text": []})
        self.assertTrue(receipts.verify(path).ok)
        snap = self.look()
        self.assertEqual(len(snap["rows"]), 8)
        self.assertEqual(snap["panes"][3]["value"], "unknown")
        self.assertEqual(snap["panes"][3]["note"], "1 model call. The costs on lines 3, 4 can't be read.")
        page = watch.render_page(snap, token="t", nonce="n")
        self.assertIn('name="token"', page)
        json.dumps(snap)

    def test_a_refused_run_does_not_hide_the_run_that_is_going(self):
        now = time.time()
        going = "model-stub-sandbox-20261128T010000Z"
        write_run(self.state, going, [start_line(going, "stub")], started=now - 10)
        refused = os.path.join(self.state, "sandbox", "runs", "model-stub-sandbox-20261128T010005Z")
        os.makedirs(refused)
        with open(os.path.join(refused, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")              # a second demo refused while this one holds the ledger leaves this
        self.assertEqual(watch.newest_run().name, going)
        self.assertEqual(self.look()["state"], "running")

    def test_a_run_that_ended_on_read_failures_is_not_finished(self):
        name = "model-stub-sandbox-20261128T010000Z"
        write_run(self.state, name, [
            start_line(name, "stub"),
            ("called", {"call": "mail_fetch", "target": "inbox"}),
            ("result", {"call_seq": 2, "ok": False, "error": "MailError"}),
            ("error", {"step": "mail_fetch", "error": "MailError: %s" % MAIL_WORDS}),
            ("summary", {"handled": 0, "held": 0, "human_approvals": 0, "errors": 1, "quarantined": 0, "sends": 0,
                         "hold_notices": 0, "bookings": 0, "model_calls": 0, "cost_usd": 0.0, "halted": None,
                         "read_failed": True}),
        ])
        snap = self.look()
        self.assertEqual(snap["state"], "ended")
        self.assertEqual(snap["status"], "Ended: the inbox could not be read, so the run ended there "
                                         "(0 handled, 0 held, 0 human approvals before it).")
        self.assertEqual(snap["panes"][0]["value"], "Ended: the inbox could not be read.")
        self.assertIn("1 error", snap["rows"][-1]["what"])
        self.assertNotIn("Finished", json.dumps(snap))

    def test_a_cut_off_line_the_writer_moved_aside_is_named(self):
        name = "model-stub-sandbox-20261128T010000Z"
        write_run(self.state, name, [
            start_line(name, "stub"),
            ("error", {"torn_bytes": 11, "torn_sha256": "ab" * 32, "moved_to": "receipts.jsonl.torn-1",
                       "after_seq": 1}, "torn-tail"),
        ])
        self.assertEqual(self.look()["rows"][-1]["what"],
                         "A cut-off line (11 bytes after line 1) was moved aside to receipts.jsonl.torn-1.")

    def test_receipts_that_cannot_be_read_show_no_counts(self):
        path = write_run(self.state, "model-live-live-20261128T010000Z", [
            start_line("model-live-live-20261128T010000Z", source="live", mode="live"),
            ("called", {"call": "model", "target": "claude-test", "source": "live"}),
            ("result", {"call_seq": 2, "ok": True, "cost_usd": 4.2, "source": "live"}),
        ], mode="live")
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(data.replace(b'"step":"start"', b'"step":"stort"', 1))
        snap = self.look()
        self.assertEqual(snap["state"], "broken")
        self.assertEqual([p["value"] for p in snap["panes"][1:]], ["?", "?", "?"])
        self.assertNotIn("$0.0000", json.dumps(snap))

    def test_counts_before_a_break_say_so(self):
        path = write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with open(path, "rb") as fh:
            lines = fh.read().split(b"\n")
        lines[10] = b"{not json"
        with open(path, "wb") as fh:
            fh.write(b"\n".join(lines))
        snap = self.look()
        self.assertEqual(snap["panes"][3]["value"], "$0.0012 (recorded)")
        self.assertTrue(snap["panes"][3]["note"].endswith("From the 10 lines before the break only."))

    def test_bytes_after_the_closing_line_are_not_still_being_written(self):
        name = "model-stub-sandbox-20261128T010000Z"
        path = write_run(self.state, name, [
            start_line(name, "stub"),
            ("summary", {"handled": 0, "held": 0, "human_approvals": 0, "errors": 0, "quarantined": 0, "sends": 0,
                         "hold_notices": 0, "bookings": 0, "model_calls": 0, "cost_usd": 0.0, "halted": None,
                         "read_failed": False}),
        ])
        with open(path, "ab") as fh:
            fh.write(b'{"hash":"12')
        self.assertFalse(receipts.verify(path, finished=True).ok)
        snap = self.look()
        self.assertFalse(snap["chain_ok"])
        self.assertEqual(snap["state"], "broken")
        self.assertIn("after its closing line", snap["chain"])

    def test_a_run_folder_whose_name_is_not_text_still_shows_the_page(self):
        name = "model-replayed-sandbox-2026\udcff"
        try:
            write_run(self.state, name, busy_day())
        except (OSError, UnicodeError):
            self.skipTest("this file system will not hold that name")
        snap = self.look()
        self.assertEqual(snap["run"], "model-replayed-sandbox-2026\ufffd")
        page = watch.render_page(snap, token="t", nonce="n")
        page.encode("utf-8")
        json.dumps(snap, ensure_ascii=False).encode("utf-8")

    def test_a_long_run_shows_its_newest_lines_and_says_how_many_more(self):
        write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with mock.patch.object(watch, "MAX_ROWS", 5):
            snap = self.look()
            page = watch.render_page(snap, token="t", nonce="n")
        self.assertEqual(len(snap["rows"]), 18)
        self.assertEqual([int(s) for s in re.findall(r'<td class="seq">(\d+)<span', page)], [18, 17, 16, 15, 14])
        self.assertIn("Showing the newest 5 of 18 lines.", page)


# ---------------------------------------------------------------------------------------------------
# The server: 127.0.0.1 only, its own assets only, and a Stop only from its own page
# ---------------------------------------------------------------------------------------------------
class Server(TempState):
    def setUp(self):
        super().setUp()
        self.said = []
        self.server = serve(self, say=self.said.append)

    def nothing_halted(self):
        self.assertIsNone(supervisor.halt_cause("sandbox"))
        self.assertIsNone(supervisor.halt_cause("live"))

    def test_it_listens_on_127_0_0_1_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(watch.HOST, "127.0.0.1")
        self.assertTrue(self.server.url.startswith("http://127.0.0.1:"))

    def test_the_page_loads_nothing_from_anywhere_else_and_runs_only_its_own_script(self):
        status, headers, body = ask(self.server, "GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        csp = headers["content-security-policy"]
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
        for directive in ("default-src 'none'", "frame-ancestors 'none'", "form-action 'self'",
                          "connect-src 'self'", "base-uri 'none'"):
            self.assertIn(directive, csp)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("no-store", headers["cache-control"])
        # never no-referrer: under it a browser sends the Stop form's Origin as "null" and the page's own
        # button is refused (Chromium did, Nov 27)
        self.assertEqual(headers["referrer-policy"], "same-origin")
        self.assertNotIn("no-referrer", page)
        self.assertEqual(re.findall(r"<script\b[^>]*>", page), ['<script nonce="%s">' % nonce])
        self.assertNotRegex(page, r"(?i)\b(src|href)\s*=")
        self.assertNotRegex(page, r"(?i)https?://|//[a-z0-9]")
        self.assertNotRegex(page, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function")
        self.assertNotEqual(ask(self.server, "GET", "/")[1]["content-security-policy"], csp)   # a nonce each time

    def test_state_sends_only_the_lines_the_page_has_not_got(self):
        name = "model-replayed-sandbox-20261128T010000Z"
        write_run(self.state, name, busy_day())
        first = state_json(self.server)
        self.assertTrue(first["reset"])
        self.assertEqual([r["seq"] for r in first["rows"]], list(range(1, 19)))
        self.assertEqual((first["run"], first["after"]), (name, 18))
        later = state_json(self.server, run=name, after=15)
        self.assertFalse(later["reset"])
        self.assertEqual([r["seq"] for r in later["rows"]], [16, 17, 18])
        newer = "model-stub-sandbox-20261128T020000Z"
        write_run(self.state, newer, [start_line(newer, "stub")], started=time.time() + 5)
        switched = state_json(self.server, run=name, after=18)
        self.assertTrue(switched["reset"])
        self.assertEqual((switched["run"], [r["seq"] for r in switched["rows"]]), (newer, [1]))

    def test_reading_the_page_writes_nothing(self):
        write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        before = listing(self.state)
        ask(self.server, "GET", "/")
        state_json(self.server)
        self.assertEqual(listing(self.state), before)

    def test_the_stop_button_halts_sandbox_and_live_with_its_own_cause(self):
        status, headers, body = press(self.server)
        self.assertEqual(status, 303, body)
        self.assertEqual(headers["location"], "/")
        self.assertEqual(supervisor.halt_cause("sandbox"), "watch_button")
        self.assertEqual(supervisor.halt_cause("live"), "watch_button")
        page = ask(self.server, "GET", "/")[2].decode("utf-8")
        self.assertRegex(page, r"Stop pressed \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC")
        self.assertIn("python -m doris resume", page)
        self.assertEqual(len(self.said), 1)
        self.assertIn("Stop pressed", self.said[0])

    def test_the_press_is_not_said_once_a_newer_run_has_started(self):
        name = "model-stub-sandbox-20261128T010000Z"
        write_run(self.state, name, [start_line(name, "stub")], started=time.time() - 60)
        self.assertEqual(press(self.server)[0], 303)
        self.assertIn("Stop pressed", state_json(self.server)["pressed"])
        for mode in cli.STOP_MODES:
            self.assertTrue(supervisor.resume(mode).ok)
        newer = "model-stub-sandbox-20261128T020000Z"
        write_run(self.state, newer, [start_line(newer, "stub")], started=time.time() + 5)
        self.assertEqual(state_json(self.server)["pressed"], "")
        self.assertNotIn("Stop pressed", ask(self.server, "GET", "/")[2].decode("utf-8"))

    def test_a_press_that_failed_somewhere_is_said_until_watch_closes(self):
        name = "model-stub-live-20261128T010000Z"
        write_run(self.state, name, [start_line(name, "stub", "live")], mode="live", started=time.time() - 60)
        real = supervisor.request_stop

        def request_stop(mode, cause="cli", state_dir=None):
            if mode == "live":
                raise OSError("read-only folder")
            return real(mode, cause, state_dir)
        with mock.patch.object(supervisor, "request_stop", side_effect=request_stop):
            self.assertEqual(press(self.server)[0], 500)
        newer = "model-stub-live-20261128T020000Z"
        write_run(self.state, newer, [start_line(newer, "stub", "live")], mode="live", started=time.time() + 5)
        self.assertIn("NOT halted: live (OSError)", state_json(self.server)["pressed"])

    def test_a_press_is_dropped_once_a_newer_smoke_run_has_started(self):
        self.assertEqual(press(self.server)[0], 303)
        for mode in cli.STOP_MODES:
            self.assertTrue(supervisor.resume(mode).ok)
        stamp = _dt.datetime.fromtimestamp(time.time() + 5, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        smoke = "model-live-live-smoke-" + stamp
        folder = os.path.join(self.state, "live", "smoke", stamp, "runs", smoke)
        os.makedirs(folder)
        writer = receipts.ReceiptWriter(os.path.join(folder, "receipts.jsonl"), smoke, "live", clock=lambda: T0)
        writer.write(*start_line(smoke, "live", "live"))
        self.assertEqual(state_json(self.server)["pressed"], "")

    def test_a_press_is_not_dropped_for_a_run_with_no_config(self):
        self.assertEqual(press(self.server)[0], 303)
        folder = os.path.join(self.state, "sandbox", "runs", "model-stub-sandbox-20261128T020000Z")
        os.makedirs(folder)
        writer = receipts.ReceiptWriter(os.path.join(folder, "receipts.jsonl"), "r", "sandbox", clock=lambda: T0)
        writer.write("halted", halted_payload(1), why=why("halt.watch_button"))
        self.assertIn("Stop pressed", state_json(self.server)["pressed"])

    def test_a_file_rewritten_to_the_same_length_is_sent_afresh(self):
        name = "model-replayed-sandbox-20261128T010000Z"
        path = write_run(self.state, name, busy_day())
        first = state_json(self.server)
        tip = first["rows"][-1]["hash"]
        shutil.rmtree(os.path.dirname(path))
        write_run(self.state, name, [start_line(name)] + busy_day()[1:17] + [
            ("called", {"call": "calendar_query", "target": "span", "attempt": 2})])
        status, _, body = ask(self.server, "GET", "/state?run=%s&after=18&tip=%s" % (name, tip))
        again = json.loads(body.decode("ascii"))
        self.assertTrue(again["reset"])
        self.assertEqual(len(again["rows"]), 18)
        same = state_json_tip(self.server, name, 18, again["rows"][-1]["hash"])
        self.assertEqual((same["reset"], same["rows"]), (False, []))

    def test_a_page_left_open_across_a_restart_reloads_itself(self):
        earlier = serve(self)                               # an earlier start of watch, on another port
        old = ask(earlier, "GET", "/")[2].decode("utf-8")
        page = ask(self.server, "GET", "/")[2].decode("utf-8")
        state = state_json(self.server)
        self.assertNotEqual(boot_of(old), boot_of(page))
        self.assertEqual(state["boot"], boot_of(page))
        self.assertNotIn(token_of(page), json.dumps(state))
        self.assertNotIn(boot_of(page), token_of(page))
        self.assertRegex(page, r"s\.boot !== boot\) \{ window\.location\.reload\(\);")
        status, _, body = ask(self.server, "POST", "/stop", form_headers(self.server), "token=" + token_of(old))
        self.assertEqual(status, 403)
        self.assertIn("reload the page", body.decode("utf-8"))
        self.nothing_halted()

    def test_the_page_keeps_its_stop_button_when_the_receipts_cannot_be_shown(self):
        write_run(self.state, "model-replayed-sandbox-20261128T010000Z", busy_day())
        with mock.patch.object(watch, "snapshot", side_effect=TypeError("unhashable type: 'list'")):
            status, _, body = ask(self.server, "GET", "/")
            page = body.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn('name="token"', page)
            self.assertIn("could not be shown (TypeError)", page)
            self.assertEqual(state_json(self.server)["state"], "unreadable")
            self.assertEqual(ask(self.server, "POST", "/stop", form_headers(self.server),
                                 "token=" + token_of(page))[0], 303)
        self.assertEqual(supervisor.halt_cause("sandbox"), "watch_button")

    def test_an_odd_after_is_answered_from_the_start(self):
        status, _, body = ask(self.server, "GET", "/state?after=%C2%B2")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("ascii"))["reset"])

    def test_a_press_through_a_local_tls_proxy_is_the_pages_own(self):
        page = ask(self.server, "GET", "/")[2].decode("utf-8")
        headers = dict(form_headers(self.server), Origin="https://127.0.0.1:%d" % self.server.server_address[1])
        self.assertEqual(ask(self.server, "POST", "/stop", headers, "token=" + token_of(page))[0], 303)

    def test_the_stop_button_makes_the_calls_stop_makes(self):
        calls = []

        def request_stop(mode, cause="cli", state_dir=None):
            calls.append((mode, cause, state_dir))
            return cause
        with mock.patch.object(supervisor, "request_stop", side_effect=request_stop), \
                mock.patch.object(supervisor, "halt_cause", return_value="cli"):    # stop reads its halt back
            self.assertEqual(run_cli(["stop"])[0], 0)
            by_cli = list(calls)
            del calls[:]
            self.assertEqual(press(self.server)[0], 303)
        self.assertEqual(by_cli, [(m, "cli", None) for m in cli.STOP_MODES])
        self.assertEqual(calls, [(m, "watch_button", None) for m in cli.STOP_MODES])

    def test_a_stop_that_cannot_be_written_is_said_loudly_and_the_rest_still_halts(self):
        real = supervisor.request_stop

        def request_stop(mode, cause="cli", state_dir=None):
            if mode == "live":
                raise PermissionError("read-only folder")
            return real(mode, cause, state_dir)
        with mock.patch.object(supervisor, "request_stop", side_effect=request_stop):
            status, _, body = press(self.server)
        self.assertEqual(status, 500)
        text = body.decode("utf-8")
        self.assertIn("NOT halted: live (PermissionError)", text)
        self.assertIn("python -m doris stop", text)
        self.assertEqual(supervisor.halt_cause("sandbox"), "watch_button")
        self.assertIsNone(supervisor.halt_cause("live"))

    def test_a_stop_from_another_site_is_refused_and_stops_nothing(self):
        page = ask(self.server, "GET", "/")[2].decode("utf-8")
        good = "token=" + token_of(page)
        port = self.server.server_address[1]
        form = form_headers(self.server)
        tries = {
            "another site's Origin": (dict(form, Origin="http://evil.example"), good, None),
            "another port's Origin": (dict(form, Origin="http://127.0.0.1:%d" % (port + 1)), good, None),
            "an opaque Origin": (dict(form, Origin="null"), good, None),
            "a cross-site fetch": (dict(form, **{"Sec-Fetch-Site": "cross-site"}), good, None),
            "a same-site fetch": (dict(form, **{"Sec-Fetch-Site": "same-site"}), good, None),
            "no token": (form, "", None),
            "a wrong token": (form, "token=" + "x" * 43, None),
            "two tokens": (form, good + "&" + good, None),
            "not the page's form": (dict(form, **{"Content-Type": "text/plain"}), good, None),
            "a rebound name": (dict(form, Origin="http://evil.example:%d" % port), good, "evil.example:%d" % port),
        }
        for what, (headers, body, host) in tries.items():
            with self.subTest(what):
                status, _, reply = ask(self.server, "POST", "/stop", headers, body, host=host)
                self.assertIn(status, (400, 403, 415), reply)
                self.assertIn("Nothing was stopped", reply.decode("utf-8"))
                self.nothing_halted()
        self.assertEqual(len(self.said), len(tries))
        self.assertTrue(all("Refused" in s for s in self.said), self.said)

    def test_a_stop_by_get_is_refused(self):
        status, headers, _ = ask(self.server, "GET", "/stop?token=" + token_of(ask(self.server, "GET", "/")[2].decode()))
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "POST")
        self.nothing_halted()

    def test_a_rebound_name_cannot_read_the_page(self):
        port = self.server.server_address[1]
        for path in ("/", "/state"):
            for host in ("evil.example:%d" % port, "127.0.0.1.evil.example:%d" % port, ""):
                with self.subTest(path=path, host=host):
                    status, _, body = ask(self.server, "GET", path, host=host)
                    self.assertEqual(status, 403)
                    self.assertNotIn(b"token", body)
        self.assertEqual(ask(self.server, "GET", "/", host="localhost:%d" % port)[0], 200)

    def test_it_can_never_start_resume_or_release_anything(self):
        supervisor.request_stop("sandbox", "cli")
        page = ask(self.server, "GET", "/")[2].decode("utf-8")
        good = "token=" + token_of(page)
        for path in ("/resume", "/start", "/run", "/demo", "/release", "/stop/../resume"):
            with self.subTest(path):
                status, _, _ = ask(self.server, "POST", path, form_headers(self.server), good)
                self.assertEqual(status, 404)
        self.assertEqual(supervisor.halt_cause("sandbox"), "cli")
        # and nothing in the module could: no name that starts, resumes, locks or deletes
        with open(watch.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for forbidden in ("resume", "run_supervised", "acquire_lock", "release", "Popen", "subprocess", "run_cmd",
                          "demo", "remove", "unlink", "rmtree", "kill"):
            self.assertNotIn(forbidden, names)
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "supervisor"}
        self.assertEqual(used, {"request_stop", "CAUSE_WORDS"})


class Command(unittest.TestCase):
    def test_a_port_out_of_range_is_refused(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(watch.main(["--port", "70000"]), 2)
        self.assertIn("Not started", out.getvalue())

    def test_a_port_in_use_is_said_and_nothing_is_served(self):
        server = serve(self)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(watch.WatchServer, "allow_reuse_address", False):
            self.assertEqual(watch.main(["--port", str(server.server_address[1])]), 2)
        self.assertIn("can't be used", out.getvalue())


# ---------------------------------------------------------------------------------------------------
# The kill switch, end to end: the Stop button halts a real run mid-call, and its halted line is stop's
# ---------------------------------------------------------------------------------------------------
class StopMidCall(unittest.TestCase):
    """The real worker, on a sandbox holding one booking request whose calendar answers slowly (the staged
    slow-calendar fault), stopped while its calendar call waits: once from the page, once by `stop`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="doris-watch-stop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "DORIS_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)

    def stopped_mid_call(self, name, stop):
        root = mini_sandbox(os.path.join(self.tmp, name), ("01-priya-request.eml",))
        facts_path = os.path.join(root, "inbox", "facts.json")
        with open(facts_path, encoding="utf-8") as fh:
            facts = json.load(fh)
        facts["01-priya-request.eml"].update(fault="slow-calendar", fault_seconds="8")
        with open(facts_path, "w", encoding="utf-8") as fh:
            json.dump(facts, fh)
        state = os.path.join(self.tmp, name, "state")
        with mock.patch.object(paths, "SANDBOX", root), mock.patch.object(paths, "STATE", state):
            server = watch.WatchServer(0, say=lambda text: None)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                      name="doris-watch-test", daemon=True)
            thread.start()
            seen = {}

            def presser():
                deadline = time.monotonic() + WAIT_S
                while time.monotonic() < deadline:
                    snap = state_json(server)
                    # the model's own call waits too, for an instant; the stop is meant for the slow calendar
                    if snap["panes"][0]["value"] == "Tool call: calendar_query span, no answer yet.":
                        seen["waiting"] = snap["panes"][0]["value"]
                        break
                    time.sleep(0.05)
                stop(server)            # waiting or not: never leave her running
            helper = threading.Thread(target=presser, name="doris-watch-presser")
            helper.start()
            try:
                code, out = run_cli(["demo"])
            finally:
                helper.join(WAIT_S)
            after = state_json(server)
            server.shutdown()
            server.server_close()
            thread.join(WAIT_S)
            for mode in cli.STOP_MODES:
                self.assertTrue(supervisor.resume(mode).ok)
        self.assertEqual(seen.get("waiting"), "Tool call: calendar_query span, no answer yet.", out)
        self.assertEqual(code, supervisor.EXIT_HALTED, out)
        lines = receipts.read_lines(os.path.join(state, "sandbox", "runs", after["run"], "receipts.jsonl"))
        self.assertTrue(receipts.verify(os.path.join(state, "sandbox", "runs", after["run"], "receipts.jsonl"),
                                        finished=True).ok)
        self.assertEqual([n for n in os.listdir(os.path.join(root, "outbox")) if n != ".gitkeep"], [])
        self.assertEqual([n for n in os.listdir(os.path.join(root, "calendar")) if n.startswith("doris-")], [])
        return lines[-1], after

    def test_the_stop_button_halts_a_run_mid_call_and_its_halted_line_is_stops(self):
        by_page, page_after = self.stopped_mid_call("page", press)
        by_cli, _ = self.stopped_mid_call("cli", lambda server: cli.stop([]))
        self.assertEqual(by_page["kind"], "halted")
        self.assertEqual(by_page["why"], {"code": "halt.watch_button", "text": "Halted: Stop pressed on the watch page."})
        self.assertEqual(by_page["payload"]["cause"], "watch_button")
        self.assertEqual(by_cli["why"]["code"], "halt.cli")
        self.assertEqual(sorted(by_page["payload"]), sorted(by_cli["payload"]))
        for key in ("in_flight", "dropped", "ledger", "torn_tail", "worker_exit"):
            self.assertEqual(by_page["payload"][key], by_cli["payload"][key], key)
        self.assertEqual((by_page["payload"]["in_flight"]["call"], by_page["payload"]["in_flight"]["target"]),
                         ("calendar_query", "span"))
        self.assertEqual([d["kind"] for d in by_page["payload"]["dropped"]], ["book"])    # her queued booking
        self.assertEqual(page_after["state"], "halted")
        self.assertEqual(page_after["panes"][0]["value"], "Halted: Stop pressed on the watch page.")


if __name__ == "__main__":
    unittest.main()
