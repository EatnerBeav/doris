"""receipts.py: the hash-chained JSONL trail. Judges tamper with it, kill her mid-write and restart her.

Expected hashes and bytes are derived here with hashlib and json directly, never with receipts.py.
"""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from doris import receipts

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 27, 18, 0, 0, tzinfo=UTC)
WHY = {"code": "gate.pass", "text": "All 3 checks that apply passed."}
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")


def canon(obj):
    """The contract's canonical form, written out independently of receipts.canonical."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def ticking_clock(start=T0):
    state = {"t": start - dt.timedelta(seconds=1)}

    def clock():
        state["t"] = state["t"] + dt.timedelta(seconds=1)
        return state["t"]
    return clock


def build_chain(seqs, prev_override=None):
    """Hand-built chain bytes (independent hashing). prev_override: {seq: wrong prev}."""
    prev = "genesis"
    out = b""
    for s in seqs:
        line = {"ts": "2026-11-27T18:00:00Z", "run_id": "r", "mode": "sandbox", "seq": s,
                "kind": "observed", "subject": "", "payload": {"n": s}, "prev": prev}
        if prev_override and s in prev_override:
            line["prev"] = prev_override[s]
        h = hashlib.sha256(line["prev"].encode() + canon(line)).hexdigest()
        line["hash"] = h
        out += canon(line) + b"\n"
        prev = h
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "runs", "model-stub-sandbox-x", "receipts.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def writer(self, run_id="run-1", mode="sandbox", start=T0):
        return receipts.ReceiptWriter(self.path, run_id, mode, clock=ticking_clock(start))

    def data(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def put(self, data):
        with open(self.path, "wb") as fh:
            fh.write(data)

    def five_lines(self):
        """observed, called, decided (every field, why included), result, summary."""
        w = self.writer()
        w.write("observed", {"message_sha256": "ab" * 32, "now": "2026-11-27T18:00:00Z"}, subject="m1@northwind.example")
        w.write("called", {"call": "calendar_query", "target": "doris-1"}, subject="m1@northwind.example")
        w.write("decided", {"action": "offer", "slots": ["2026-12-01T17:00:00Z"]}, subject="m1@northwind.example", why=WHY)
        w.write("result", {"call_seq": 2, "events": []}, subject="m1@northwind.example")
        w.write("summary", {"handled": 1, "held": 0}, subject="run")
        return w


class CanonicalAndIso(unittest.TestCase):
    def test_canonical_sorts_keys_no_spaces_ascii_escapes(self):
        self.assertEqual(receipts.canonical({"b": 1, "a": "café", "c": [True, None]}),
                         b'{"a":"caf\\u00e9","b":1,"c":[true,null]}')

    def test_canonical_refuses_nan(self):
        with self.assertRaises(ValueError):
            receipts.canonical({"x": float("nan")})

    def test_iso_converts_aware_to_utc_z(self):
        pacific = dt.timezone(dt.timedelta(hours=-8))
        self.assertEqual(receipts.iso(dt.datetime(2026, 11, 27, 10, 0, 5, 999, tzinfo=pacific)), "2026-11-27T18:00:05Z")

    def test_iso_refuses_naive(self):
        with self.assertRaises(ValueError):
            receipts.iso(dt.datetime(2026, 11, 27, 10, 0))


class WriteLines(Tmp):
    def test_first_line_is_canonical_bytes_plus_lf_chained_from_genesis(self):
        w = self.writer()
        line = w.write("observed", {"mode": "sandbox"}, subject="startup")
        body = {"ts": "2026-11-27T18:00:00Z", "run_id": "run-1", "mode": "sandbox", "seq": 1, "kind": "observed",
                "subject": "startup", "payload": {"mode": "sandbox"}, "prev": "genesis"}
        expected = dict(body, hash=hashlib.sha256(b"genesis" + canon(body)).hexdigest())
        self.assertEqual(line, expected)
        self.assertEqual(self.data(), canon(expected) + b"\n")
        self.assertEqual((w.seq, w.last_hash, w.path), (1, expected["hash"], self.path))

    def test_second_line_chains_from_first_hash(self):
        w = self.writer()
        first = w.write("observed", {}, subject="a")
        second = w.write("observed", {"k": 2}, subject="b")
        body = dict(second)
        del body["hash"]
        self.assertEqual((second["seq"], second["prev"], second["ts"]), (2, first["hash"], "2026-11-27T18:00:01Z"))
        self.assertEqual(second["hash"], hashlib.sha256(first["hash"].encode() + canon(body)).hexdigest())
        self.assertEqual(self.data(), canon(first) + b"\n" + canon(second) + b"\n")

    def test_bytes_are_ascii_lf_only_with_non_ascii_escaped(self):
        w = self.writer()
        w.write("observed", {"name": "Zoë — café"}, subject="x")
        data = self.data()
        data.decode("ascii")
        self.assertNotIn(b"\r", data)
        self.assertIn(b'"name":"Zo\\u00eb \\u2014 caf\\u00e9"', data)

    def test_new_file_and_folders_are_created_empty(self):
        w = self.writer()
        self.assertEqual(self.data(), b"")
        self.assertEqual((w.seq, w.last_hash), (0, "genesis"))

    def test_unknown_mode_or_kind_refused(self):
        with self.assertRaises(ValueError):
            receipts.ReceiptWriter(self.path, "run-1", "demo")
        w = self.writer()
        with self.assertRaises(ValueError):
            w.write("decision", {}, why=WHY)
        self.assertEqual(self.data(), b"")


class WhyRequired(Tmp):
    def test_decision_kinds_refuse_a_missing_why_and_write_nothing(self):
        w = self.writer()
        for kind in ("decided", "checked", "held", "halted"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                w.write(kind, {"x": 1}, subject="m")
        self.assertEqual((self.data(), w.seq), (b"", 0))

    def test_malformed_why_refused(self):
        w = self.writer()
        for bad in ({"code": "gate.pass"}, "gate.pass", {"code": "", "text": "t"}, {"code": "gate.pass", "text": 3},
                    {"code": "gate.pass", "text": "t", "extra": "words"}):
            with self.subTest(why=bad), self.assertRaises(ValueError):
                w.write("checked", {}, why=bad)
        self.assertEqual(self.data(), b"")

    def test_why_written_when_given_and_absent_otherwise(self):
        w = self.writer()
        self.assertEqual(w.write("held", {"reason": "hold.money"}, why=WHY)["why"], WHY)
        self.assertNotIn("why", w.write("observed", {}))


class PayloadSafety(Tmp):
    def test_aware_datetimes_become_iso_z(self):
        pacific = dt.timezone(dt.timedelta(hours=-8))
        line = self.writer().write("observed", {"now": dt.datetime(2026, 11, 27, 10, 0, tzinfo=pacific),
                                                "slots": [(T0, dt.date(2026, 12, 1))]})
        self.assertEqual(line["payload"], {"now": "2026-11-27T18:00:00Z", "slots": [["2026-11-27T18:00:00Z", "2026-12-01"]]})

    def test_values_that_are_not_json_safe_are_refused(self):
        w = self.writer()
        for bad in ({"t": dt.datetime(2026, 11, 27, 10, 0)}, {"s": {1, 2}}, {"m": {2: "a", 10: "b"}},
                    {"f": float("inf")}, {"b": b"raw bytes"}):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                w.write("observed", bad)
        with self.assertRaises(ValueError):
            w.write("observed", ["not", "a", "dict"])
        self.assertEqual(self.data(), b"")


class CallPairing(Tmp):
    def test_called_needs_a_listed_call_and_a_target(self):
        w = self.writer()
        for bad in ({"target": "T1"}, {"call": "smtp", "target": "T1"}, {"call": "send"}):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                w.write("called", bad)
        self.assertEqual(w.write("called", {"call": "send", "target": "T1"})["seq"], 1)

    def test_a_send_carries_its_body_only_as_a_hash(self):
        w = self.writer()
        for key in ("body", "body_text", "raw"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                w.write("called", {"call": "send", "target": "T1", key: "Hi Priya, you're booked."})
        self.assertEqual(self.data(), b"")
        line = w.write("called", {"call": "send", "target": "T1", "body_sha256": "ab" * 32, "message_id": "<a1@fastmail.com>"})
        self.assertEqual(line["payload"]["body_sha256"], "ab" * 32)

    def test_result_must_name_an_earlier_called_seq_or_none(self):
        w = self.writer()
        w.write("observed", {})
        w.write("called", {"call": "calendar_create", "target": "doris-abc"})
        for bad in ({}, {"call_seq": 1}, {"call_seq": 9}, {"call_seq": "2"}):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                w.write("result", bad)
        self.assertEqual(w.write("result", {"call_seq": 2, "status": "written"})["seq"], 3)
        self.assertEqual(w.write("result", {"call_seq": None, "dropped_by_stop": "m9@northwind.example"})["seq"], 4)

    def test_result_may_answer_a_call_written_before_a_reopen(self):
        self.writer().write("called", {"call": "send", "target": "T1"})
        self.assertEqual(self.writer(run_id="run-2").write("result", {"call_seq": 1, "accepted": True})["seq"], 2)

    def test_in_flight_is_the_last_called_line_with_no_result(self):
        w = self.writer()
        w.write("called", {"call": "model", "target": "claude-x"})
        w.write("result", {"call_seq": 1})
        w.write("called", {"call": "calendar_query", "target": "doris-1"})
        w.write("called", {"call": "calendar_create", "target": "doris-2"})
        lines = receipts.read_lines(self.path)
        self.assertEqual(receipts.in_flight(lines), {"seq": 4, "call": "calendar_create", "target": "doris-2"})
        self.assertEqual([c["seq"] for c in receipts.unanswered_calls(lines)], [3, 4])
        w.write("result", {"call_seq": 4})
        w.write("result", {"call_seq": 3})
        self.assertIsNone(receipts.in_flight(receipts.read_lines(self.path)))


class Verify(Tmp):
    def lines(self):
        return self.data().split(b"\n")[:-1]

    def put_lines(self, lines):
        self.put(b"".join(l + b"\n" for l in lines))

    def assertBad(self, seq, word=None):
        res = receipts.verify(self.path)
        self.assertFalse(res.ok, res)
        self.assertEqual(res.first_bad_seq, seq, res)
        self.assertEqual(res.lines, seq - 1, res)
        if word:
            self.assertIn(word, res.reason)

    def test_a_rechained_line_holding_nan_or_an_infinity_names_its_seq(self):
        # json.loads takes NaN, Infinity and 1e999; canonical() refuses them. A hand re-chained line
        # holding one is a bad seq, never a ValueError out of verify.
        os.makedirs(os.path.dirname(self.path))
        for literal in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(literal=literal):
                first, second = build_chain([1, 2]).split(b"\n")[:2]
                line = json.loads(second.decode("ascii"))
                del line["hash"]
                line["payload"]["x"] = json.loads(literal)
                line["hash"] = hashlib.sha256(line["prev"].encode() + canon(line)).hexdigest()
                raw = canon(line).replace(b'"x":Infinity', b'"x":' + literal)
                self.assertIn(b'"x":' + literal, raw)
                self.put(first + b"\n" + raw + b"\n")
                try:
                    res = receipts.verify(self.path)
                except ValueError as exc:
                    self.fail("verify raised %r" % (exc,))
                self.assertEqual((res.ok, res.lines, res.first_bad_seq), (False, 1, 2), res)

    def test_untouched_file_passes(self):
        self.five_lines()
        self.assertEqual(tuple(receipts.verify(self.path)[:3]), (True, 5, None))

    def test_a_finished_run_ends_with_its_summary(self):
        self.five_lines()
        self.assertTrue(receipts.verify(self.path, finished=True).ok)

    def test_lines_cut_from_the_end_fail_the_finished_check(self):
        # whole lines cut from the end leave a chain that still links (Fable #7)
        self.five_lines()
        self.put_lines(self.lines()[:3])
        self.assertTrue(receipts.verify(self.path).ok)        # the chain so far is whole
        res = receipts.verify(self.path, finished=True)
        self.assertFalse(res.ok, res)
        self.assertEqual((res.lines, res.first_bad_seq), (3, 4))
        self.assertIn("no closing line", res.reason)

    def test_a_halted_line_also_ends_a_run(self):
        w = self.writer()
        w.write("observed", {"message_sha256": "ab" * 32}, subject="m1@northwind.example")
        w.write("halted", {"cause": "cli", "in_flight": "none", "dropped": []}, subject="run", why=WHY)
        self.assertTrue(receipts.verify(self.path, finished=True).ok)

    def test_a_halted_line_that_promises_a_summary_does_not_end_a_run(self):
        # an unsupervised run writes its own halted line, then its summary: cut the summary and the
        # run has not ended (Fable re-review, receipts.py:159)
        w = self.writer()
        w.write("observed", {"message_sha256": "ab" * 32}, subject="m1@northwind.example")
        w.write("halted", {"cause": "cli", "in_flight": "none", "dropped": [], "summary": "follows"},
                subject="run", why=WHY)
        res = receipts.verify(self.path, finished=True)
        self.assertFalse(res.ok, res)
        self.assertEqual((res.lines, res.first_bad_seq), (2, 3))
        self.assertIn("no closing line", res.reason)
        w.write("summary", {"handled": 0, "held": 0, "human_approvals": 0}, subject="run")
        self.assertTrue(receipts.verify(self.path, finished=True).ok)

    def test_an_empty_file_is_not_a_finished_run(self):
        os.makedirs(os.path.dirname(self.path))
        self.put(b"")
        self.assertTrue(receipts.verify(self.path).ok)
        self.assertFalse(receipts.verify(self.path, finished=True).ok)

    def test_hand_built_chain_passes(self):
        os.makedirs(os.path.dirname(self.path))
        self.put(build_chain([1, 2, 3]))
        self.assertTrue(receipts.verify(self.path).ok)

    def test_one_changed_byte_in_each_field_names_its_seq(self):
        self.five_lines()
        original = self.data()
        line3 = self.lines()[2]
        obj = json.loads(line3)
        edits = {
            "ts": (b'"ts":"2026-11-27T18:00:02Z"', b'"ts":"2026-11-27T18:00:03Z"'),
            "run_id": (b'"run_id":"run-1"', b'"run_id":"run-7"'),
            "mode": (b'"mode":"sandbox"', b'"mode":"sandbax"'),
            "seq": (b'"seq":3', b'"seq":4'),
            "kind": (b'"kind":"decided"', b'"kind":"decidad"'),
            "subject": (b'"subject":"m1@', b'"subject":"m2@'),
            "payload": (b'"action":"offer"', b'"action":"offes"'),
            "why": (b'"code":"gate.pass"', b'"code":"gate.pasS"'),
            "prev": (('"prev":"' + obj["prev"][:5]).encode(), ('"prev":"' + obj["prev"][:4] + "x").encode()),
            "hash": (('"hash":"' + obj["hash"][:5]).encode(), ('"hash":"' + obj["hash"][:4] + "x").encode()),
        }
        self.assertEqual(set(edits), set(obj))
        for field, (old, new) in edits.items():
            with self.subTest(field=field):
                self.assertEqual(line3.count(old), 1)
                self.put(original.replace(line3, line3.replace(old, new)))
                self.assertBad(3)

    def test_any_single_byte_flip_in_a_middle_or_last_line_is_caught(self):
        self.five_lines()
        original = self.data()
        for index in (2, 4):
            lines = self.lines()
            line = lines[index]
            for pos in range(len(line)):
                flipped = line[:pos] + bytes([line[pos] ^ 0x01]) + line[pos + 1:]
                self.put(original.replace(line, flipped))
                res = receipts.verify(self.path)
                self.assertEqual((res.ok, res.first_bad_seq), (False, index + 1), (index, pos, res))

    def test_inserted_whitespace_is_caught(self):
        self.five_lines()
        lines = self.lines()
        lines[1] = lines[1].replace(b'"kind":', b'"kind": ')
        self.put_lines(lines)
        self.assertBad(2)

    def test_deleted_line_names_its_seq(self):
        self.five_lines()
        lines = self.lines()
        del lines[2]
        self.put_lines(lines)
        self.assertBad(3)

    def test_reordered_lines_name_the_first_out_of_place(self):
        self.five_lines()
        lines = self.lines()
        lines[2], lines[3] = lines[3], lines[2]
        self.put_lines(lines)
        self.assertBad(3)

    def test_duplicated_line_names_the_copy(self):
        self.five_lines()
        lines = self.lines()
        lines.insert(3, lines[2])
        self.put_lines(lines)
        self.assertBad(4)

    def test_seq_gap_with_valid_hashes_is_caught(self):
        os.makedirs(os.path.dirname(self.path))
        self.put(build_chain([1, 2, 4]))
        self.assertBad(3, "seq")
        self.put(build_chain([2, 3]))
        self.assertBad(1, "seq")

    def test_prev_not_matching_previous_hash_is_caught(self):
        os.makedirs(os.path.dirname(self.path))
        self.put(build_chain([1, 2, 3], prev_override={2: "0" * 64}))
        self.assertBad(2, "prev")

    def test_torn_tail_without_newline_names_the_next_seq(self):
        self.five_lines()
        self.put(self.data() + b'{"ts":"2026-11')
        self.assertBad(6, "torn")

    def test_bad_json_last_line_is_a_torn_tail(self):
        self.five_lines()
        self.put(self.data() + b"not json\n")
        self.assertBad(6, "torn")

    def test_a_deeply_nested_line_is_reported_not_raised(self):
        self.five_lines()
        lines = self.lines()
        nested = b"[" * 200000 + b"]" * 200000
        self.put_lines(lines[:1] + [nested] + lines[2:])
        self.assertBad(2)
        self.put_lines(lines[:4] + [nested])
        self.assertBad(5, "torn")

    def test_crlf_line_endings_fail(self):
        self.five_lines()
        self.put(self.data().replace(b"\n", b"\r\n"))
        self.assertBad(1)

    # A re-save with CRLF line endings (PowerShell's Set-Content, some editors) changes every line at
    # once. The file never verifies, and the line that was really edited is still the one named.
    def put_crlf(self, lines):
        self.put(b"".join(l + b"\r\n" for l in lines))

    def test_a_crlf_resave_alone_is_named_as_line_endings_not_as_an_edited_line(self):
        self.five_lines()
        self.put_crlf(self.lines())
        for finished in (False, True):
            res = receipts.verify(self.path, finished=finished)
            self.assertEqual((res.ok, res.lines, res.first_bad_seq), (False, 0, 1), res)
            self.assertTrue(res.reason.startswith("broken at seq 1: the line endings were changed"), res)
            self.assertIn("5 of 5 lines now end CRLF where Doris wrote LF", res.reason)
            self.assertIn("every line still matches its hash", res.reason)
            self.assertNotIn("canonical", res.reason)

    def test_a_crlf_resave_with_one_changed_character_names_that_line(self):
        self.five_lines()
        lines = self.lines()
        self.assertIn(b'"action":"offer"', lines[2])
        lines[2] = lines[2].replace(b'"action":"offer"', b'"action":"0ffer"')
        self.put_crlf(lines)
        self.assertBad(3, "broken at seq 3: the hash does not match the line")
        self.assertIn("5 of 5 lines now end CRLF where Doris wrote LF", receipts.verify(self.path).reason)

    def test_a_crlf_resave_with_a_deleted_line_names_where_it_was(self):
        self.five_lines()
        lines = self.lines()
        self.put_crlf(lines[:1] + lines[2:])
        self.assertBad(2, "seq 3 found where seq 2 belongs")
        self.assertIn("CRLF", receipts.verify(self.path).reason)

    def test_a_crlf_resave_with_lines_cut_from_the_end_fails_the_finished_check(self):
        self.five_lines()
        self.put_crlf(self.lines()[:3])
        res = receipts.verify(self.path, finished=True)
        self.assertEqual((res.ok, res.lines, res.first_bad_seq), (False, 3, 4), res)
        self.assertTrue(res.reason.startswith("no closing line"), res)
        self.assertIn("3 of 3 lines now end CRLF where Doris wrote LF", res.reason)
        self.assertNotIn("every line still matches", res.reason)
        self.assertEqual(receipts.verify(self.path).first_bad_seq, 1)     # still being written: the endings alone

    def test_a_crlf_resave_with_a_torn_tail_names_the_tail(self):
        self.five_lines()
        self.put(b"".join(l + b"\r\n" for l in self.lines()) + b'{"ts":"2026')
        res = receipts.verify(self.path)
        self.assertEqual((res.ok, res.lines, res.first_bad_seq), (False, 5, 6), res)
        self.assertTrue(res.reason.startswith("torn tail"), res)
        self.assertIn("CRLF", res.reason)

    def test_crlf_on_some_lines_only_starts_at_the_first_of_them(self):
        self.five_lines()
        lines = self.lines()
        self.put(b"".join(l + b"\n" for l in lines[:2]) + b"".join(l + b"\r\n" for l in lines[2:]))
        self.assertBad(3, "broken at seq 3: the line endings were changed")
        self.assertIn("3 of 5 lines now end CRLF where Doris wrote LF", receipts.verify(self.path).reason)

    def test_an_edit_ahead_of_the_first_crlf_line_is_named_as_before(self):
        self.five_lines()
        lines = self.lines()
        lines[1] = lines[1].replace(b"calendar_query", b"calendar_qverY")
        self.put(b"".join(l + b"\n" for l in lines[:3]) + b"".join(l + b"\r\n" for l in lines[3:]))
        self.assertBad(2, "the hash does not match the line")

    def test_a_line_wrong_beyond_one_cr_is_named_itself(self):
        self.five_lines()
        lines = self.lines()
        for extra in (lines[3] + b"\r", lines[3] + b" ", b" " + lines[3], lines[3][:40] + b"\r" + lines[3][40:]):
            with self.subTest(extra=extra[-12:]):
                self.put_crlf(lines[:3] + [extra] + lines[4:])
                res = receipts.verify(self.path)
                self.assertEqual((res.ok, res.lines, res.first_bad_seq), (False, 3, 4), res)
                self.assertIn("CRLF", res.reason)
                self.assertNotIn("every line still matches", res.reason)

    def test_an_lf_file_with_an_edit_says_nothing_of_line_endings(self):
        self.five_lines()
        lines = self.lines()
        lines[2] = lines[2].replace(b'"action":"offer"', b'"action":"0ffer"')
        self.put_lines(lines)
        res = receipts.verify(self.path)
        self.assertEqual(res.reason, "broken at seq 3: the hash does not match the line")

    def test_a_crlf_file_is_never_ok_and_the_writer_refuses_it_untouched(self):
        self.five_lines()
        self.put_crlf(self.lines())
        resaved = self.data()
        with self.assertRaises(receipts.ChainError) as ctx:
            self.writer()
        self.assertIn("line endings were changed", str(ctx.exception))
        self.assertEqual(self.data(), resaved)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["receipts.jsonl"])

    def test_missing_file_fails_and_empty_file_passes(self):
        res = receipts.verify(self.path)
        self.assertFalse(res.ok)
        self.assertIsNone(res.first_bad_seq)
        self.writer()
        self.assertEqual(tuple(receipts.verify(self.path)[:3]), (True, 0, None))


class Reopen(Tmp):
    def test_second_writer_continues_the_chain(self):
        a = self.five_lines()
        b = receipts.ReceiptWriter(self.path, "run-1", "sandbox", clock=ticking_clock())
        self.assertEqual((b.seq, b.last_hash), (5, a.last_hash))
        halted = b.write("halted", {"cause": "cli", "in_flight": None, "dropped": []}, why=WHY)
        self.assertEqual((halted["seq"], halted["prev"]), (6, a.last_hash))
        self.assertEqual(tuple(receipts.verify(self.path)[:3]), (True, 6, None))

    def test_torn_tail_moved_aside_recorded_and_chain_continues(self):
        self.five_lines()
        whole = self.data()
        torn = b'{"ts":"2026-11-27T18:00:09Z","run'
        self.put(whole + torn)
        b = self.writer(run_id="run-2")
        with open(self.path + ".torn-1", "rb") as fh:
            self.assertEqual(fh.read(), torn)
        self.assertTrue(self.data().startswith(whole))
        error = receipts.read_lines(self.path)[5]
        self.assertEqual((error["seq"], error["kind"], error["subject"]), (6, "error", "torn-tail"))
        self.assertEqual(error["payload"]["torn_bytes"], len(torn))
        self.assertEqual(error["payload"]["torn_sha256"], hashlib.sha256(torn).hexdigest())
        self.assertEqual(error["payload"]["moved_to"], "receipts.jsonl.torn-1")
        self.assertEqual(b.torn["torn_sha256"], hashlib.sha256(torn).hexdigest())
        self.assertEqual(b.write("observed", {}, subject="startup")["seq"], 7)
        res = receipts.verify(self.path)
        self.assertEqual((res.ok, res.lines), (True, 7))
        self.assertIn("torn tail moved aside", res.reason)

    def test_bad_json_tail_moves_to_the_next_free_torn_name(self):
        self.five_lines()
        with open(self.path + ".torn-1", "wb") as fh:
            fh.write(b"older")
        self.put(self.data() + b"garbage\n")
        self.writer()
        with open(self.path + ".torn-1", "rb") as fh:
            self.assertEqual(fh.read(), b"older")
        with open(self.path + ".torn-2", "rb") as fh:
            self.assertEqual(fh.read(), b"garbage\n")
        self.assertTrue(receipts.verify(self.path).ok)

    def test_a_broken_chain_refuses_and_touches_nothing(self):
        self.five_lines()
        lines = self.data().split(b"\n")
        lines[1] = lines[1].replace(b"calendar_query", b"calendar_qverY")
        tampered = b"\n".join(lines) + b'{"torn'
        self.put(tampered)
        with self.assertRaises(receipts.ChainError) as ctx:
            self.writer()
        self.assertIn("seq 2", str(ctx.exception))
        self.assertEqual(self.data(), tampered)
        self.assertFalse(os.path.exists(self.path + ".torn-1"))

    def test_stale_writer_continues_after_another_writer(self):
        a = self.writer()
        a.write("observed", {}, subject="startup")
        b = self.writer(run_id="supervisor")
        b.write("observed", {"k": 1})
        b.write("observed", {"k": 2})
        line = a.write("observed", {"k": 3})
        self.assertEqual((line["seq"], line["prev"]), (4, b.last_hash))
        self.assertTrue(receipts.verify(self.path).ok)

    def test_stale_writer_moves_a_torn_tail_left_by_a_killed_writer(self):
        a = self.writer()
        a.write("called", {"call": "calendar_create", "target": "doris-1"})
        with open(self.path, "ab") as fh:
            fh.write(b'{"call_seq":1,')
        line = a.write("halted", {"cause": "cli"}, why={"code": "halt.cli", "text": "Halted: stop on the command line."})
        self.assertEqual(line["seq"], 3)
        kinds = [l["kind"] for l in receipts.read_lines(self.path)]
        self.assertEqual(kinds, ["called", "error", "halted"])
        self.assertEqual(receipts.in_flight(receipts.read_lines(self.path))["target"], "doris-1")
        self.assertTrue(receipts.verify(self.path).ok)

    def test_read_lines_leaves_out_a_torn_tail(self):
        w = self.writer()
        first = w.write("observed", {"a": 1})
        self.put(self.data() + b'{"ts"')
        self.assertEqual(receipts.read_lines(self.path), [first])


CHILD = r"""
import sys
sys.path.insert(0, sys.argv[2])
from doris import receipts
w = receipts.ReceiptWriter(sys.argv[1], "run-kill", "sandbox")
while True:
    w.write("observed", {"pad": "x" * 4000, "n": w.seq + 1}, subject="m")
    sys.stdout.write("%d\n" % w.seq)
    sys.stdout.flush()
"""


class HardKill(Tmp):
    def test_killed_writer_loses_no_reported_line_and_the_chain_resumes(self):
        proc = subprocess.Popen([sys.executable, "-c", CHILD, self.path, SRC],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        reported = 0
        try:
            while reported < 40:
                out = proc.stdout.readline()
                if not out:
                    break
                reported = int(out)
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            err = proc.stderr.read()
            proc.stderr.close()
        self.assertGreaterEqual(reported, 40, err)
        w = self.writer(run_id="supervisor")
        w.write("halted", {"cause": "cli"}, why={"code": "halt.cli", "text": "Halted: stop on the command line."})
        res = receipts.verify(self.path)
        self.assertTrue(res.ok, res)
        seqs = [l["seq"] for l in receipts.read_lines(self.path) if l["kind"] == "observed"]
        self.assertEqual(seqs[:reported], list(range(1, reported + 1)))


if __name__ == "__main__":
    unittest.main()
