"""The gate, G1 to G17: a pass and a block for every check, plus the fail-closed paths.

SAFETY.md cites these by name (test_g<N>_...). The helper modules the gate looks up at the
moment of a check (templates, compose, slots, ics, sensitive, parser) are faked through
sys.modules, so these tests never depend on another builder's half-written file.

Run: cd /home/user/doris && python3 -m unittest tests.unit.test_gate -v
"""
import datetime as dt
import json
import sys
import types
import unittest
from unittest import mock

from doris import caps, gate
from doris.shapes import (Action, Attachment, CalendarError, Event, Message, ModelAnswer, ParseResult,
                          Reading, Slot)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 12, 1, 17, 0, tzinfo=UTC)               # Tue Dec 1, 9:00 AM Pacific
SLOT = Slot(dt.datetime(2026, 12, 3, 18, 0, tzinfo=UTC),         # Thu Dec 3, 10:00 AM Pacific
            dt.datetime(2026, 12, 3, 18, 30, tzinfo=UTC))
SLOT2 = Slot(dt.datetime(2026, 12, 4, 19, 0, tzinfo=UTC), dt.datetime(2026, 12, 4, 19, 30, tzinfo=UTC))
SLOT3 = Slot(dt.datetime(2026, 12, 7, 20, 0, tzinfo=UTC), dt.datetime(2026, 12, 7, 20, 30, tzinfo=UTC))
SLOT4 = Slot(dt.datetime(2026, 12, 8, 18, 0, tzinfo=UTC), dt.datetime(2026, 12, 8, 18, 30, tzinfo=UTC))

OWNER = "whizz@fastmail.com"
DORIS = "doris.htm@fastmail.com"
RAE = "rae@landscaping.example"
PAT = "pat@elsewhere.example"
KEY = "req-1@landscaping.example"
THREAD = KEY
BOOKING = "doris-3f9a1c"
HER_OFFER = "<5d41402abc4b2a76b9719d911017c592@fastmail.com>"
SIGNATURE = "Doris, AI assistant to Shane Bornstein, Hire the Machines.\nI'm an AI, not a person. Reply \"stop\" to opt out."
CARD = "4111 1111 1111 1111"
ALL = ["G%d" % n for n in range(1, 18)]
OFFER_TEMPLATES = ("T2", "T3", "T4", "T9", "T12", "T13", "T18")
TIMES = [SLOT.start_utc, SLOT2.start_utc, SLOT3.start_utc]      # an offer's times, as the template takes them
ROOM = "https://meet.example/test-room"                         # the meeting link from .env (faked)
FAKE_APPROVED = ("T1", "T2", "T3", "T4", "T9", "T10", "T12", "T13", "T18", "S1", "S2")


# --- fake helper modules (the real ones belong to other builders) ------------------------
class FakeTemplateError(Exception):
    pass


def fake_render(template_id, slots):
    if template_id not in FAKE_APPROVED or slots.get("bad_slot"):
        raise FakeTemplateError(template_id)
    return template_id + ": " + "; ".join("%s=%s" % (k, slots[k]) for k in sorted(slots))


def body(template_id, slots):
    return fake_render(template_id, slots) + "\n\n" + SIGNATURE


def ics_bytes(uid, slot, status="CONFIRMED", extra=""):
    stamp = "%Y%m%dT%H%M%SZ"
    return ("BEGIN:VEVENT\r\nUID:%s\r\nDTSTART:%s\r\nDTEND:%s\r\nSTATUS:%s\r\n%sEND:VEVENT\r\n"
            % (uid, slot.start_utc.strftime(stamp), slot.end_utc.strftime(stamp), status, extra)).encode("ascii")


def fake_read_event(data):
    fields = dict(line.split(":", 1) for line in data.decode("ascii").splitlines() if ":" in line)

    def when(text):
        return dt.datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)

    return Event(uid=fields["UID"], status=fields["STATUS"], transp="", kind="timed",
                 start_utc=when(fields["DTSTART"]), end_utc=when(fields["DTEND"]))


def fake_modules():
    templates = types.ModuleType("doris.templates")
    templates.APPROVED = FAKE_APPROVED
    templates.TemplateError = FakeTemplateError
    templates.render = fake_render
    templates.meeting_link = lambda: ROOM
    compose = types.ModuleType("doris.compose")
    compose.SIGNATURE = SIGNATURE
    compose.signature_ok = lambda text: text.endswith(SIGNATURE)
    slots = types.ModuleType("doris.slots")
    slots.bookable = mock.Mock(return_value=(True, None))
    ics = types.ModuleType("doris.ics")
    ics.fields_ok = mock.Mock(return_value=True)
    ics.read_event = fake_read_event
    sensitive = types.ModuleType("doris.sensitive")
    sensitive.scan_text = lambda text: "card-number" if CARD in text else None
    sensitive.scan_message = (
        lambda msg: "card-number" if CARD in (msg.subject + msg.own_text + msg.quoted_text) else None)
    parser = types.ModuleType("doris.parser")
    parser.model_agrees = mock.Mock(return_value=(True, "same instant"))
    return {"doris.templates": templates, "doris.compose": compose, "doris.slots": slots,
            "doris.ics": ics, "doris.sensitive": sensitive, "doris.parser": parser}


# --- what the loop hands the gate -----------------------------------------------------------
def message(**over):
    text = "Could Shane do Thursday, December 3 at 10:00 AM Pacific?"
    fields = dict(key=KEY, raw_sha256="ab" * 32, raw_size=812, message_id="<" + KEY + ">", from_addr=RAE,
                  from_name="Rae Wolfe", reply_to=[], to=[OWNER], cc=[], subject="Quick call?",
                  date_utc=NOW - dt.timedelta(hours=1), in_reply_to=[], references=[], headers={},
                  text_plain=text, html=None, visible_text=text, own_text=text, below_closing="Thanks,\nRae",
                  quoted_text="", attachments=[], is_report=False)
    fields.update(over)
    return Message(**fields)


def answer(**over):
    fields = dict(cls="meeting_request", date="2026-12-03", time24="10:00", zone="PT", length_min=None,
                  pick=None, injection_seen=False, side_question=False, raw='{"class": "meeting_request"}',
                  source="replayed", request_sha256="cd" * 32, ok=True)
    fields.update(over)
    return ModelAnswer(**fields)


def parse(**over):
    fields = dict(verdict="book", failed_condition=None,
                  readings=[Reading(SLOT.start_utc, dt.date(2026, 12, 3), "Pacific", True, "S1")],
                  instant_utc=SLOT.start_utc, zone="Pacific",
                  conditions={"B%d" % n: True for n in range(1, 10)})
    fields.update(over)
    return ParseResult(**fields)


def offer_slots(template):
    """An offer's slots as templates.render takes them: UTC start times, the length, the zone.
    T13 lists one time; T12 re-sends the open offer and states no length (the loop puts the open
    offer's length_min in the action's evidence)."""
    if template == "T13":
        return {"slot": SLOT.start_utc, "duration": 30, "zone": None}
    if template == "T12":
        return {"slots": list(TIMES), "zone": None}
    slots = {"slots": list(TIMES), "duration": 30, "zone": None}
    if template == "T4":
        slots["asked"] = SLOT4.start_utc
    if template == "T18":
        slots["start"] = SLOT4.start_utc
    return slots


def reply(template="T2", **over):
    """A template to the sender on their thread, BCC the owner. Offers list three times."""
    slots = offer_slots(template) if template in OFFER_TEMPLATES else {"duration": 30}
    slots.update(over.pop("slots", {}))
    fields = dict(kind="send_reply", message_key=KEY, thread_key=THREAD, template=template, slots=slots,
                  to=[RAE], bcc=[OWNER], recipient_source="sender",
                  evidence={"length_min": 30} if template == "T12" else {})
    fields.update(over)
    action = Action(**fields)
    if "body_text" not in over:
        action.body_text = body(action.template, action.slots)
    return action


def confirm(**over):
    """T1 after a clean booking: the booking ID, its span and the .ics ride along."""
    over.setdefault("booking_id", BOOKING)
    over.setdefault("span", SLOT)
    over.setdefault("ics", ics_bytes(BOOKING, SLOT))
    return reply("T1", **over)


def cancel_done(**over):
    """T10 after a confirmed delete: the cancel .ics rides along."""
    over.setdefault("booking_id", BOOKING)
    over.setdefault("span", SLOT)
    over.setdefault("ics", ics_bytes(BOOKING, SLOT, status="CANCELLED"))
    return reply("T10", **over)


def hold_notice(**over):
    slots = {"reason": "money", "quote": "Can you send over the invoice first?"}
    slots.update(over.pop("slots", {}))
    fields = dict(kind="send_hold_notice", message_key=KEY, thread_key=THREAD, template="S1", slots=slots,
                  to=[OWNER], bcc=[], recipient_source="owner")
    fields.update(over)
    action = Action(**fields)
    if "body_text" not in over:
        action.body_text = body(action.template, action.slots)
    return action


def digest(**over):
    slots = {"handled": 9, "held": 1, "approvals": 0}
    fields = dict(kind="send_digest", message_key="", thread_key="", template="S2", slots=slots,
                  to=[OWNER], bcc=[], recipient_source="owner")
    fields.update(over)
    action = Action(**fields)
    if "body_text" not in over:
        action.body_text = body(action.template, action.slots)
    return action


def booking(direct=True, **over):
    fields = dict(kind="calendar_create", message_key=KEY, thread_key=THREAD, booking_id=BOOKING, span=SLOT,
                  direct=direct, ics=ics_bytes(BOOKING, SLOT))
    fields.update(over)
    return Action(**fields)


def delete(**over):
    fields = dict(kind="calendar_delete", message_key=KEY, thread_key=THREAD, booking_id=BOOKING, span=SLOT)
    fields.update(over)
    return Action(**fields)


class FakeCtx:
    """The loop's ctx, with every answer the safe one by default. Tests change one thing."""

    def __init__(self, **over):
        self.now = NOW
        self.run_id = "model-stub-sandbox-20261201T1700Z"
        self.owner_addr = OWNER
        self.doris_addr = DORIS
        self.message = message()
        self.screen_facts = {"branch": 1, "own_thread": False, "others_on_thread": 0, "offer_to": None}
        self.model_answer = answer()
        self.parse = parse()
        self.halt = None
        self.lock = True
        self.spend = 0.25
        self.stopped = set()
        self.count_values = dict(sends_hour=0, sends_run=0, sends_thread_day=0, sends_thread_ever=0,
                                 offers_thread=0, calendar_writes_run=0, hold_notices_hour=0)
        self.events = []
        self.state = "intent"
        self.ledger = None
        self.live = None
        self.clean = True
        self.model_up = True
        self.offered = set()               # the slots of the open offers on the thread
        self.calls = []
        for name, value in over.items():
            setattr(self, name, value)
        if self.ledger is None:
            self.ledger = {BOOKING: {"booking_id": BOOKING, "message_key": KEY, "thread_key": THREAD,
                                     "sender": RAE, "state": self.state}}

    def halt_cause(self):
        self.calls.append(("halt_cause",))
        return self.halt

    def holds_lock(self):
        return self.lock

    def spend_usd(self):
        return self.spend

    def silenced(self, addr):
        self.calls.append(("silenced", addr))
        return addr in self.stopped

    def counts(self, thread_key):
        self.calls.append(("counts", thread_key))
        return dict(self.count_values)

    def fresh_read(self, slot):
        self.calls.append(("fresh_read", slot))
        return list(self.events)

    def booking(self, booking_id):
        return self.ledger.get(booking_id)

    def live_booking_for_thread(self, thread_key):
        return self.live

    def readback_clean(self, booking_id):
        self.calls.append(("readback_clean", booking_id))
        return self.clean

    def model_ok(self):
        return self.model_up

    def open_offer_slots(self, thread_key):
        self.calls.append(("open_offer_slots", thread_key))
        return set(self.offered)


def raises(exc):
    def call(*args):
        raise exc
    return call


def on_her_thread(**facts):
    """Ctx for a reply sent to Doris alone on her own thread (ruling 18, branch 2)."""
    over = {"message": message(to=[DORIS], in_reply_to=[HER_OFFER], references=[HER_OFFER])}
    over["screen_facts"] = dict({"branch": 2, "own_thread": True, "others_on_thread": 0, "offer_to": RAE}, **facts)
    return over


class GateTest(unittest.TestCase):
    def setUp(self):
        self.fakes = fake_modules()
        patcher = mock.patch.dict(sys.modules, self.fakes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, action, **ctx_over):
        return gate.check(action, FakeCtx(**ctx_over))

    def one(self, result, check_id):
        return [r for r in result.results if r.check == check_id][0]

    def assertPasses(self, result):
        self.assertTrue(result.passed, (result.first_fail, result.why,
                                        [(r.check, r.status, r.detail) for r in result.results]))
        self.assertIsNone(result.first_fail)
        self.assertEqual(result.why["code"], "gate.pass")

    def assertBlocked(self, result, check_id, status="fail"):
        self.assertFalse(result.passed)
        self.assertEqual(result.first_fail, check_id, (result.why, [(r.check, r.status) for r in result.results]))
        self.assertEqual(self.one(result, check_id).status, status)


# --- the shape of every answer ---------------------------------------------------------------
class TableTest(GateTest):
    def test_check_records_all_17_in_table_order(self):
        result = self.check(reply("T2"))
        self.assertEqual([r.check for r in result.results], ALL)
        for r in result.results:
            self.assertIn(r.status, ("pass", "fail", "n/a", "error"))

    def test_pass_why_counts_the_checks_that_applied(self):
        result = self.check(reply("T2"))
        self.assertPasses(result)
        applied = [r.check for r in result.results if r.status != "n/a"]
        self.assertEqual(applied, ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G16", "G17"])
        self.assertEqual(result.why, {"code": "gate.pass", "text": "All 11 checks that apply passed."})

    def test_every_check_runs_after_a_failure_and_why_is_the_first(self):
        ctx = dict(stopped={RAE})
        result = self.check(reply("T2", body_text="Sure, Shane is free whenever.\n\n" + SIGNATURE), **ctx)
        self.assertBlocked(result, "G5")
        self.assertEqual(self.one(result, "G8").status, "fail")
        self.assertEqual(self.one(result, "G17").status, "pass")
        self.assertEqual(result.why["code"], "gate.g5_silenced")
        self.assertIsNone(result.halt_cause)

    def test_receipt_payload_is_json_safe_and_lists_all_17(self):
        result = self.check(booking())
        payload = gate.receipt_payload(result)
        self.assertEqual(list(payload["checks"]), ALL)
        self.assertEqual({k: v["status"] for k, v in payload["checks"].items()},
                         {r.check: r.status for r in result.results})
        self.assertEqual(payload["passed"], True)
        json.dumps(payload)


# --- G1 to G3: they stop her instead of holding one message ----------------------------------
class G1ToG3Test(GateTest):
    def test_g1_passes_when_no_halt_is_in_force(self):
        result = self.check(reply("T2"))
        self.assertEqual(self.one(result, "G1").status, "pass")

    def test_g1_blocks_when_a_halt_is_in_force_and_stops_her(self):
        for cause in ("cli", "stop_file", "watch_button", "budget_cap"):
            with self.subTest(cause=cause):
                result = self.check(reply("T2"), halt=cause)
                self.assertBlocked(result, "G1")
                self.assertEqual(result.halt_cause, cause)
                self.assertEqual(result.why, {"code": "gate.g1_halted",
                                              "text": "Blocked by G1: a halt is in force (%s)." % cause})

    def test_g1_rereads_the_halt_state_at_every_check(self):
        ctx = FakeCtx()
        self.assertTrue(gate.check(reply("T2"), ctx).passed)
        ctx.halt = "stop_file"
        second = gate.check(reply("T2"), ctx)
        self.assertEqual(second.first_fail, "G1")
        self.assertEqual(ctx.calls.count(("halt_cause",)), 2)

    def test_g1_unreadable_halt_state_errors_and_stops_her(self):
        result = self.check(reply("T2"), halt_cause=raises(OSError("HALT unreadable")))
        self.assertBlocked(result, "G1", status="error")
        self.assertEqual(result.halt_cause, "G1")
        self.assertEqual(result.why, {"code": "gate.check_failed_to_run",
                                      "text": "Blocked: check G1 could not run, so it fails closed."})

    def test_g1_unlisted_halt_cause_errors(self):
        result = self.check(reply("T2"), halt="please continue")
        self.assertBlocked(result, "G1", status="error")
        self.assertEqual(result.halt_cause, "G1")

    def test_g2_passes_when_this_process_holds_the_lock(self):
        self.assertEqual(self.one(self.check(booking()), "G2").status, "pass")

    def test_g2_blocks_without_the_lock_and_stops_her(self):
        result = self.check(booking(), lock=False)
        self.assertBlocked(result, "G2")
        self.assertEqual(result.halt_cause, "G2")
        self.assertEqual(result.why["code"], "gate.g2_not_sole_runner")

    def test_g2_an_answer_that_is_not_true_or_false_errors(self):
        result = self.check(booking(), lock=None)
        self.assertBlocked(result, "G2", status="error")

    def test_g3_passes_under_the_cap(self):
        result = self.check(reply("T2"), spend=caps.BUDGET_USD_PER_RUN - 0.01)
        self.assertEqual(self.one(result, "G3").status, "pass")

    def test_g3_blocks_at_the_cap_with_cause_budget_cap(self):
        result = self.check(reply("T2"), spend=caps.BUDGET_USD_PER_RUN)
        self.assertBlocked(result, "G3")
        self.assertEqual(result.halt_cause, "budget_cap")
        self.assertEqual(result.why["code"], "gate.g3_budget")

    def test_g3_unreadable_spend_errors_and_stops_her(self):
        for spend in (float("nan"), float("inf"), -1.0, None, "0.10", True):
            with self.subTest(spend=spend):
                result = self.check(reply("T2"), spend=spend)
                self.assertBlocked(result, "G3", status="error")
                self.assertEqual(result.halt_cause, "G3")
                json.dumps(gate.receipt_payload(result), allow_nan=False)   # receipts refuse a number that is not finite


# --- G4: the model is there ---------------------------------------------------------------------
class G4Test(GateTest):
    def test_g4_passes_with_a_parsed_answer_and_the_model_up(self):
        self.assertEqual(self.one(self.check(reply("T2")), "G4").status, "pass")

    def test_g4_blocks_a_reply_when_the_answer_is_missing(self):
        result = self.check(reply("T2"), model_answer=None)
        self.assertBlocked(result, "G4")
        self.assertEqual(result.why["code"], "gate.g4_model_missing")

    def test_g4_blocks_a_booking_whose_answer_is_not_usable(self):
        for bad in (answer(ok=False), answer(cls="book_it_now"), answer(source="someone-else")):
            with self.subTest(answer=bad):
                self.assertBlocked(self.check(booking(), model_answer=bad), "G4")

    def test_g4_blocks_every_prospect_send_while_the_model_is_down(self):
        self.assertBlocked(self.check(reply("T2"), model_up=False), "G4")

    def test_g4_blocks_a_nudge_while_the_model_is_down(self):
        self.assertBlocked(self.check(reply("T9"), model_up=False, model_answer=None), "G4")
        self.assertEqual(self.one(self.check(reply("T9"), model_answer=None), "G4").status, "pass")

    def test_g4_hold_notices_and_the_digest_still_reach_the_owner_while_the_model_is_down(self):
        self.assertPasses(self.check(hold_notice(), model_up=False, model_answer=None))
        self.assertPasses(self.check(digest(), model_up=False, model_answer=None, message=None))

    def test_g4_a_model_state_that_is_not_true_or_false_errors(self):
        self.assertBlocked(self.check(reply("T2"), model_up="probably"), "G4", status="error")


# --- G5: not silenced ---------------------------------------------------------------------------
class G5Test(GateTest):
    def test_g5_passes_when_nobody_is_silenced_and_asks_for_every_recipient(self):
        ctx = FakeCtx()
        result = gate.check(reply("T2"), ctx)
        self.assertEqual(self.one(result, "G5").status, "pass")
        self.assertIn(("silenced", RAE), ctx.calls)
        self.assertIn(("silenced", OWNER), ctx.calls)

    def test_g5_blocks_a_silenced_sender(self):
        result = self.check(reply("T2"), stopped={RAE})
        self.assertBlocked(result, "G5")
        self.assertEqual(result.why["code"], "gate.g5_silenced")

    def test_g5_reads_the_stop_list_fresh_at_the_check(self):
        ctx = FakeCtx()
        self.assertEqual(self.one(gate.check(reply("T2"), ctx), "G5").status, "pass")
        ctx.stopped.add(RAE)
        self.assertEqual(gate.check(reply("T2"), ctx).first_fail, "G5")

    def test_g5_an_unreadable_stop_list_blocks(self):
        result = self.check(reply("T2"), silenced=raises(RuntimeError("ledger locked")))
        self.assertBlocked(result, "G5", status="error")

    def test_g5_does_not_apply_to_calendar_writes(self):
        self.assertEqual(self.one(self.check(booking(), stopped={RAE}), "G5").status, "n/a")


# --- G6: right recipient ---------------------------------------------------------------------------
class G6Test(GateTest):
    def g6(self, action, **ctx_over):
        return self.one(self.check(action, **ctx_over), "G6")

    def assertG6Fails(self, action, rule, **ctx_over):
        result = self.check(action, **ctx_over)
        self.assertBlocked(result, "G6")
        self.assertEqual(result.why, {"code": "gate.g6_recipient",
                                      "text": "Blocked by G6: the recipient or the addressing is not allowed (%s)." % rule})

    def test_g6_passes_branch_1_the_owner_in_to_with_only_the_sender(self):
        result = self.g6(reply("T2"))
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.detail["branch"], 1)
        self.assertEqual(result.detail["from"], RAE)

    def test_g6_passes_branch_2_doris_in_to_on_her_own_thread(self):
        result = self.g6(reply("T2"), **on_her_thread())
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.detail["branch"], 2)

    def test_g6_passes_a_pick_that_copies_the_owner(self):
        over = on_her_thread()
        over["message"] = message(to=[DORIS], cc=[OWNER], in_reply_to=[HER_OFFER])
        self.assertEqual(self.g6(booking(direct=False), **over).status, "pass")

    def test_g6_blocks_a_to_that_is_not_the_original_sender(self):
        self.assertG6Fails(reply("T2", to=[PAT]), "sender")

    def test_g6_blocks_a_second_to_address(self):
        self.assertG6Fails(reply("T2", to=[RAE, PAT]), "sender")

    def test_g6_blocks_a_bcc_that_is_not_the_owner_from_config(self):
        self.assertG6Fails(reply("T2", bcc=[PAT]), "owner")
        self.assertG6Fails(reply("T2", bcc=[OWNER, PAT]), "owner")

    def test_g6_blocks_a_reply_without_the_owner_bcc(self):
        self.assertG6Fails(reply("T2", bcc=[]), "owner")

    def test_g6_blocks_a_sender_address_that_could_carry_a_header(self):
        forged = RAE + "\r\nbcc: " + PAT
        self.assertG6Fails(reply("T2", to=[forged]), "sender", message=message(from_addr=forged))

    def test_g6_blocks_group_mail_even_when_the_screens_said_branch_1(self):
        self.assertG6Fails(reply("T2"), "multiple", message=message(cc=[PAT]))
        self.assertG6Fails(reply("T2"), "multiple", message=message(to=[OWNER, PAT]))

    def test_g6_blocks_a_reply_all_on_her_thread(self):
        over = on_her_thread()
        over["message"] = message(to=[DORIS, PAT], in_reply_to=[HER_OFFER])
        self.assertG6Fails(booking(direct=False), "multiple", **over)

    def test_g6_blocks_copied_only_and_blind_copied_mail(self):
        self.assertG6Fails(reply("T2"), "cc_only", message=message(to=[PAT], cc=[OWNER]))
        self.assertG6Fails(reply("T2"), "bcc", message=message(to=[PAT]))

    def test_g6_blocks_a_first_mail_sent_straight_to_her(self):
        self.assertG6Fails(reply("T2"), "direct_new_thread", message=message(to=[DORIS]),
                           screen_facts={"branch": 2, "own_thread": False, "others_on_thread": 0, "offer_to": None})

    def test_g6_blocks_mail_that_claims_a_thread_she_never_wrote_on(self):
        self.assertG6Fails(reply("T2"), "forged_thread", message=message(to=[DORIS], in_reply_to=["<x@never.example>"]),
                           screen_facts={"branch": 2, "own_thread": False, "others_on_thread": 0, "offer_to": None})

    def test_g6_blocks_when_the_screens_did_not_clear_the_message(self):
        self.assertG6Fails(reply("T2"), "none",
                           screen_facts={"branch": "else", "own_thread": False, "others_on_thread": 0, "offer_to": None})

    def test_g6_blocks_a_reply_to_unlike_from(self):
        self.assertG6Fails(reply("T2"), "sender", message=message(reply_to=[PAT]))
        self.assertEqual(self.g6(reply("T2"), message=message(reply_to=[RAE])).status, "pass")

    def test_g6_blocks_a_pick_from_a_new_address_on_her_thread(self):
        over = on_her_thread(offer_to=RAE)
        over["message"] = message(from_addr=PAT, to=[DORIS], in_reply_to=[HER_OFFER])
        self.assertG6Fails(reply("T12", to=[PAT]), "sender", **over)
        self.assertG6Fails(booking(direct=False), "sender", **over)

    def test_g6_a_booking_record_made_from_this_message_is_not_evidence_of_who_she_wrote_to(self):
        over = on_her_thread(offer_to=None)
        over["message"] = message(from_addr=PAT, to=[DORIS], in_reply_to=[HER_OFFER])
        intent = {"booking_id": BOOKING, "message_key": KEY, "thread_key": THREAD, "sender": PAT, "state": "intent"}
        result = self.check(booking(), ledger={BOOKING: intent}, live=intent, **over)
        self.assertBlocked(result, "G6")
        self.assertEqual(self.one(result, "G6").detail["wrote_to"], "unknown")

    def test_g6_a_booking_never_written_is_not_evidence_of_who_she_wrote_to(self):
        over = on_her_thread(offer_to=None)
        over["message"] = message(from_addr=PAT, to=[DORIS], in_reply_to=[HER_OFFER])
        blocked_pick = {"booking_id": BOOKING, "message_key": "<earlier@elsewhere.example>", "thread_key": THREAD,
                        "sender": PAT, "state": "intent"}
        result = self.check(reply("T2", to=[PAT]), live=blocked_pick, **over)
        self.assertBlocked(result, "G6")
        self.assertEqual(self.one(result, "G6").detail["wrote_to"], "unknown")

    def test_g6_blocks_on_her_thread_when_the_address_she_wrote_to_is_unknown(self):
        result = self.check(reply("T2"), **on_her_thread(offer_to=None))
        self.assertBlocked(result, "G6")
        self.assertEqual(self.one(result, "G6").detail["wrote_to"], "unknown")

    def test_g6_on_her_thread_the_booking_names_the_address_she_wrote_to(self):
        result = self.check(cancel_done(), state="cancelled", **on_her_thread(offer_to=None))
        self.assertEqual(self.one(result, "G6").status, "pass")
        self.assertEqual(self.one(self.check(cancel_done(), state="cancelled", ledger={BOOKING: {
            "booking_id": BOOKING, "message_key": KEY, "thread_key": THREAD, "sender": PAT, "state": "cancelled"}},
            **on_her_thread(offer_to=None)), "G6").status, "fail")

    def test_g6_an_offer_address_off_the_contract_errors(self):
        facts = {"branch": 1, "own_thread": False, "others_on_thread": 0, "offer_to": [RAE]}
        self.assertBlocked(self.check(reply("T2"), screen_facts=facts), "G6", status="error")

    def test_g6_blocks_her_own_mail_and_the_owners_mail(self):
        self.assertG6Fails(reply("T2", to=[DORIS]), "self", message=message(from_addr=DORIS))
        self.assertG6Fails(reply("T2", to=[OWNER]), "owner", message=message(from_addr=OWNER))

    def test_g6_blocks_list_bounce_and_auto_mail(self):
        cases = [({"list-id": "<news.example>"}, False, "list"), ({"precedence": "bulk"}, False, "list"),
                 ({"list-unsubscribe": "<mailto:x@news.example>"}, False, "list"),
                 ({"auto-submitted": "auto-replied"}, False, "auto"), ({"x-autoreply": "yes"}, False, "auto"),
                 ({}, True, "bounce"), ({"return-path": "<>"}, False, "bounce")]
        for headers, report, rule in cases:
            with self.subTest(rule=rule, headers=headers):
                self.assertG6Fails(reply("T2"), rule, message=message(headers=headers, is_report=report))
        self.assertEqual(self.g6(reply("T2"), message=message(headers={"auto-submitted": "no"})).status, "pass")

    def test_g6_hold_notices_and_the_digest_go_to_the_owner_only(self):
        self.assertEqual(self.g6(hold_notice()).status, "pass")
        self.assertEqual(self.g6(digest(), message=None).status, "pass")
        self.assertG6Fails(hold_notice(to=[RAE]), "owner")
        self.assertG6Fails(hold_notice(bcc=[OWNER]), "owner")
        self.assertG6Fails(digest(to=[OWNER, PAT]), "owner", message=None)
        self.assertG6Fails(hold_notice(recipient_source="sender"), "owner")

    def test_g6_never_sends_to_her_own_address(self):
        self.assertG6Fails(hold_notice(to=[DORIS]), "self", owner_addr=DORIS)

    def test_g6_checks_a_booking_and_a_cancel_before_the_calendar_write(self):
        self.assertG6Fails(booking(), "multiple", message=message(cc=[PAT]))
        self.assertG6Fails(delete(), "sender", state="confirmed", message=message(reply_to=[PAT]))

    def test_g6_blocks_an_action_for_another_message(self):
        self.assertG6Fails(reply("T2", message_key="other@landscaping.example"), "sender")


# --- G7: under caps ----------------------------------------------------------------------------------
class G7Test(GateTest):
    SEND_CAPS = (("sends_hour", caps.SENDS_PER_HOUR), ("sends_run", caps.SENDS_PER_RUN),
                 ("sends_thread_day", caps.SENDS_PER_THREAD_PER_DAY),
                 ("sends_thread_ever", caps.SENDS_PER_THREAD_EVER))

    def counts(self, **over):
        values = dict(sends_hour=0, sends_run=0, sends_thread_day=0, sends_thread_ever=0, offers_thread=0,
                      calendar_writes_run=0, hold_notices_hour=0)
        values.update(over)
        return values

    def test_g7_passes_one_under_every_send_cap(self):
        over = {name: limit - 1 for name, limit in self.SEND_CAPS}
        result = self.check(reply("T2"), count_values=self.counts(offers_thread=1, **over))
        self.assertEqual(self.one(result, "G7").status, "pass")

    def test_g7_blocks_a_send_at_each_cap(self):
        for name, limit in self.SEND_CAPS:
            with self.subTest(cap=name):
                result = self.check(reply("T1", booking_id=BOOKING), count_values=self.counts(**{name: limit}),
                                    state="written")
                self.assertBlocked(result, "G7")
                self.assertEqual(self.one(result, "G7").detail["cap"], name)
                self.assertEqual(result.why["text"], "Blocked by G7: a cap is reached (send).")

    def test_g7_blocks_a_third_offer_on_a_thread(self):
        for template in ("T2", "T3", "T4", "T13", "T18"):
            with self.subTest(template=template):
                result = self.check(reply(template), count_values=self.counts(offers_thread=caps.OFFERS_PER_THREAD_EVER))
                self.assertBlocked(result, "G7")
                self.assertEqual(self.one(result, "G7").detail["cap"], "offers_thread")

    def test_g7_a_resend_or_nudge_is_not_a_new_offer(self):
        for template in ("T9", "T12"):
            with self.subTest(template=template):
                result = self.check(reply(template), count_values=self.counts(offers_thread=caps.OFFERS_PER_THREAD_EVER))
                self.assertEqual(self.one(result, "G7").status, "pass")

    def test_g7_blocks_a_booking_at_the_calendar_write_cap(self):
        result = self.check(booking(), count_values=self.counts(calendar_writes_run=caps.CALENDAR_WRITES_PER_RUN))
        self.assertBlocked(result, "G7")
        self.assertEqual(result.why["text"], "Blocked by G7: a cap is reached (booking).")

    def test_g7_blocks_hold_notices_past_the_hourly_cap_but_not_on_prospect_caps(self):
        result = self.check(hold_notice(), count_values=self.counts(hold_notices_hour=caps.HOLD_NOTICES_PER_HOUR))
        self.assertBlocked(result, "G7")
        self.assertEqual(result.why["text"], "Blocked by G7: a cap is reached (hold_notice).")
        busy = self.counts(sends_hour=caps.SENDS_PER_HOUR, sends_thread_ever=caps.SENDS_PER_THREAD_EVER)
        self.assertPasses(self.check(hold_notice(), count_values=busy))

    def test_g7_asks_for_the_counts_of_this_thread(self):
        ctx = FakeCtx()
        gate.check(reply("T2"), ctx)
        self.assertIn(("counts", THREAD), ctx.calls)

    def test_g7_a_missing_or_bad_count_errors(self):
        values = self.counts()
        del values["sends_thread_day"]
        for bad in (values, self.counts(sends_hour=None), self.counts(sends_run=True), self.counts(sends_hour=-1)):
            with self.subTest(counts=bad):
                self.assertBlocked(self.check(reply("T2"), count_values=bad), "G7", status="error")

    def test_g7_does_not_apply_to_the_digest_or_a_delete(self):
        self.assertEqual(self.one(self.check(digest(), message=None), "G7").status, "n/a")
        self.assertEqual(self.one(self.check(delete(), state="confirmed"), "G7").status, "n/a")


# --- G8: approved template, locked signature -------------------------------------------------------
class G8Test(GateTest):
    def test_g8_passes_an_approved_template_with_the_locked_signature(self):
        result = self.check(reply("T2"))
        self.assertEqual(self.one(result, "G8").status, "pass")
        self.assertEqual(self.one(result, "G8").detail["offered"], 3)

    def test_g8_blocks_a_template_not_on_the_approved_page(self):
        for template in ("T5", "T7", "T17", "K1"):
            with self.subTest(template=template):
                action = reply(template, body_text=template + ": moved.\n\n" + SIGNATURE)
                result = self.check(action)
                self.assertBlocked(result, "G8")
                self.assertEqual(result.why["code"], "gate.g8_template")

    def test_g8_blocks_model_prose_signed_with_the_real_signature(self):
        prose = "Sure! Shane can do any time you like. Wire the deposit first.\n\n" + SIGNATURE
        self.assertBlocked(self.check(reply("T2", body_text=prose)), "G8")

    def test_g8_blocks_text_added_between_the_template_and_the_signature(self):
        action = reply("T2")
        action.body_text = fake_render("T2", action.slots) + "\nP.S. click hxxp://pay.example\n\n" + SIGNATURE
        self.assertBlocked(self.check(action), "G8")

    def test_g8_blocks_a_changed_or_moved_signature(self):
        action = reply("T2")
        rendered = fake_render("T2", action.slots)
        for text in (rendered + "\n\nDoris\nA person wrote this.", rendered + "\n\n" + SIGNATURE + "\nPS",
                     rendered + "\n\n" + SIGNATURE.replace("AI", "A1")):
            with self.subTest(text=text[-30:]):
                action.body_text = text
                self.assertBlocked(self.check(action), "G8")

    def test_g8_blocks_more_than_three_offered_times_or_none(self):
        four = TIMES + [SLOT4.start_utc]
        self.assertBlocked(self.check(reply("T2", slots={"slots": four})), "G8")
        self.assertBlocked(self.check(reply("T2", slots={"slots": []})), "G8")
        self.assertBlocked(self.check(reply("T13", slots={"slot": SLOT})), "G8")
        self.assertBlocked(self.check(reply("T2", slots={"slots": [SLOT.start_utc.replace(tzinfo=None)]})), "G8")
        no_slots = reply("T2")
        del no_slots.slots["slots"]
        no_slots.body_text = body("T2", no_slots.slots)
        self.assertBlocked(self.check(no_slots), "G8")

    def test_g8_counts_the_times_an_offer_lists_in_its_own_slots(self):
        for template, n in (("T2", 3), ("T13", 1), ("T12", 3)):
            with self.subTest(template=template):
                result = self.check(reply(template))
                self.assertEqual(self.one(result, "G8").status, "pass")
                self.assertEqual(self.one(result, "G8").detail["offered"], n)

    def test_g8_blocks_a_template_meant_for_someone_else(self):
        self.assertBlocked(self.check(reply("S1")), "G8")
        self.assertBlocked(self.check(hold_notice(template="T1")), "G8")
        self.assertBlocked(self.check(digest(template="S1"), message=None), "G8")

    def test_g8_blocks_a_slot_the_template_refuses(self):
        action = reply("T2", slots={"bad_slot": "Call with Pat about the merger"},
                       body_text="T2: Call with Pat about the merger\n\n" + SIGNATURE)
        self.assertBlocked(self.check(action), "G8")

    def test_g8_a_missing_templates_module_errors_and_blocks(self):
        with mock.patch.dict(sys.modules, {"doris.templates": None}):
            result = self.check(reply("T2"))
        self.assertBlocked(result, "G8", status="error")
        self.assertEqual(result.why["text"], "Blocked: check G8 could not run, so it fails closed.")
        self.assertEqual(self.one(result, "G9").status, "pass")

    def test_g8_a_missing_body_errors(self):
        self.assertBlocked(self.check(reply("T2", body_text=None)), "G8", status="error")


# --- G9: bookable interval --------------------------------------------------------------------------
class G9Test(GateTest):
    """A time the sender wrote may start on any quarter hour; a slot she offers on her own starts on
    the hour or half hour. Offering their written time back keeps it a time the sender wrote (ruling 6
    offers their candidates first). A pick books only a slot of an open offer on its thread, exactly
    as offered, and keeps the grid it was offered on."""

    def bookable(self):
        return self.fakes["doris.slots"].bookable

    def test_g9_passes_a_direct_booking_checked_as_a_time_the_sender_wrote(self):
        self.assertEqual(self.one(self.check(booking()), "G9").status, "pass")
        self.bookable().assert_called_once_with(SLOT, NOW, True)

    def test_g9_a_pick_books_a_slot_of_an_open_offer_on_its_thread(self):
        ctx = FakeCtx(parse=parse(readings=[]), offered={SLOT2, SLOT})
        result = gate.check(booking(direct=False), ctx)
        self.assertEqual(self.one(result, "G9").status, "pass")
        self.assertIn(("open_offer_slots", THREAD), ctx.calls)
        # it passed G9 when it was offered, so it keeps that grid (a quarter hour if the sender wrote it)
        self.bookable().assert_called_once_with(SLOT, NOW, True)

    def test_g9_blocks_a_pick_of_a_slot_no_open_offer_holds(self):
        longer = Slot(SLOT.start_utc, SLOT.end_utc + dt.timedelta(minutes=30))
        for offered in (set(), {SLOT2}, {longer}):
            with self.subTest(offered=offered):
                # the default parse holds the sender's own reading of this very time: a pick still needs the offer
                result = self.check(booking(direct=False), offered=offered)
                self.assertBlocked(result, "G9")
                self.assertEqual(self.one(result, "G9").detail["rule"], "not_offered")
                self.assertEqual(result.why["text"], "Blocked by G9: the interval is not bookable (not_offered).")
        self.bookable().assert_not_called()

    def test_g9_a_booking_not_marked_direct_is_held_to_a_picks_rule(self):
        for direct in (None, "yes", 1):
            with self.subTest(direct=direct):
                self.assertBlocked(self.check(booking(direct=direct)), "G9")

    def test_g9_blocks_a_booking_outside_the_bookable_interval(self):
        for rule in ("outside_hours", "under_24h", "over_30d", "holiday", "not_quarter_hour", "length"):
            with self.subTest(rule=rule):
                self.bookable().return_value = (False, rule)
                result = self.check(booking())
                self.assertBlocked(result, "G9")
                self.assertEqual(result.why["text"], "Blocked by G9: the interval is not bookable (%s)." % rule)

    def test_g9_checks_every_offered_slot(self):
        self.bookable().side_effect = lambda slot, now, written: (False, "holiday") if slot == SLOT3 else (True, None)
        result = self.check(reply("T2"))
        self.assertBlocked(result, "G9")
        # the first is the time the sender wrote (offered first, ruling 6); the others are hers
        self.assertEqual([c.args for c in self.bookable().call_args_list],
                         [(SLOT, NOW, True), (SLOT2, NOW, False), (SLOT3, NOW, False)])

    def test_g9_a_reading_set_aside_is_not_a_time_the_sender_wrote(self):
        set_aside = [Reading(SLOT.start_utc, dt.date(2026, 12, 3), "Pacific", False, "H12")]
        self.check(reply("T2"), parse=parse(readings=set_aside))
        self.assertEqual([c.args[2] for c in self.bookable().call_args_list], [False, False, False])

    def test_g9_checks_each_offered_time_at_the_length_the_offer_states(self):
        self.check(reply("T2", slots={"duration": 60}))
        self.assertEqual([c.args[0] for c in self.bookable().call_args_list],
                         [Slot(t, t + dt.timedelta(minutes=60)) for t in TIMES])

    def test_g9_checks_the_one_time_t13_offers(self):
        self.assertEqual(self.one(self.check(reply("T13"), parse=parse(readings=[])), "G9").status, "pass")
        self.bookable().assert_called_once_with(SLOT, NOW, False)

    def test_g9_checks_a_resent_offer_at_the_open_offers_length(self):
        self.assertEqual(self.one(self.check(reply("T12"), parse=parse(readings=[])), "G9").status, "pass")
        self.assertEqual([c.args for c in self.bookable().call_args_list],
                         [(SLOT, NOW, False), (SLOT2, NOW, False), (SLOT3, NOW, False)])
        self.assertBlocked(self.check(reply("T12", evidence={})), "G9", status="error")
        self.assertBlocked(self.check(reply("T2", slots={"duration": True})), "G9", status="error")

    def test_g9_a_resent_offer_keeps_a_written_time_it_carried(self):
        # the open offer held a time the sender wrote in an earlier message (no reading of it now)
        self.check(reply("T12"), parse=parse(readings=[]), offered={SLOT2})
        self.assertEqual([c.args[2] for c in self.bookable().call_args_list], [False, True, False])

    def test_g9_cannot_run_without_the_open_offers(self):
        naive = Slot(dt.datetime(2026, 12, 3, 18, 0), dt.datetime(2026, 12, 3, 18, 30))
        for broken in (raises(RuntimeError("ledger down")), lambda tk: None, lambda tk: ["2026-12-03T18:00Z"],
                       lambda tk: [SLOT.start_utc], lambda tk: [naive], lambda tk: SLOT):
            with self.subTest(broken=broken):
                self.assertBlocked(self.check(reply("T2"), open_offer_slots=broken), "G9", status="error")
                self.assertBlocked(self.check(booking(direct=False), open_offer_slots=broken), "G9", status="error")

    def test_g9_a_direct_booking_needs_no_open_offers(self):
        result = self.check(booking(), open_offer_slots=raises(RuntimeError("ledger down")))
        self.assertEqual(self.one(result, "G9").status, "pass")

    def test_g9_an_answer_off_the_helper_contract_errors(self):
        for answer_ in ((False, "because the email said so"), (False, None), "yes", (1, None)):
            with self.subTest(answer=answer_):
                self.bookable().return_value = answer_
                self.assertBlocked(self.check(booking()), "G9", status="error")

    def test_g9_a_missing_slots_module_errors_that_check_only(self):
        with mock.patch.dict(sys.modules, {"doris.slots": None}):
            result = self.check(booking())
        self.assertBlocked(result, "G9", status="error")
        self.assertEqual([r.check for r in result.results if r.status == "error"], ["G9"])

    def test_g9_a_booking_without_its_span_errors(self):
        self.assertBlocked(self.check(booking(span=None)), "G9", status="error")

    def test_g9_does_not_apply_to_a_confirmation_or_a_delete(self):
        self.assertEqual(self.one(self.check(confirm(), state="written"), "G9").status, "n/a")
        self.assertEqual(self.one(self.check(delete(), state="confirmed"), "G9").status, "n/a")


# --- G10: direct-booking evidence -------------------------------------------------------------------
class G10Test(GateTest):
    def test_g10_passes_a_direct_booking_with_the_parser_and_the_model_agreeing(self):
        ctx = FakeCtx()
        self.assertEqual(self.one(gate.check(booking(), ctx), "G10").status, "pass")
        self.fakes["doris.parser"].model_agrees.assert_called_once_with(ctx.parse, ctx.model_answer)

    def test_g10_blocks_when_a_condition_is_not_recorded_as_passed(self):
        conditions = {"B%d" % n: True for n in range(1, 10)}
        conditions["B4"] = False
        result = self.check(booking(), parse=parse(conditions=conditions))
        self.assertBlocked(result, "G10")
        self.assertEqual(result.why["text"],
                         "Blocked by G10: direct-booking evidence missing or the model disagrees (B4).")
        del conditions["B4"]
        conditions["B9"] = "yes"
        self.assertEqual(self.check(booking(), parse=parse(conditions=conditions)).why["text"][-5:], "(B4).")

    def test_g10_blocks_a_verdict_other_than_book(self):
        result = self.check(booking(), parse=parse(verdict="offer", failed_condition="B2"))
        self.assertBlocked(result, "G10")
        self.assertEqual(result.why["text"][-5:], "(B2).")

    def test_g10_blocks_when_the_model_disagrees(self):
        self.fakes["doris.parser"].model_agrees.return_value = (False, "time differs")
        result = self.check(booking())
        self.assertBlocked(result, "G10")
        self.assertEqual(result.why["text"][-6:], "(B11).")

    def test_g10_blocks_a_booking_at_a_time_other_than_the_parsed_instant(self):
        self.assertBlocked(self.check(booking(span=SLOT2, ics=ics_bytes(BOOKING, SLOT2))), "G10")

    def test_g10_does_not_apply_to_a_pick(self):
        self.assertEqual(self.one(self.check(booking(direct=False), parse=None), "G10").status, "n/a")

    def test_g10_missing_parse_or_parser_errors(self):
        self.assertBlocked(self.check(booking(), parse=None), "G10", status="error")
        with mock.patch.dict(sys.modules, {"doris.parser": None}):
            self.assertBlocked(self.check(booking()), "G10", status="error")


# --- G11: fresh re-check --------------------------------------------------------------------------
def timed(start, end, status="CONFIRMED", transp="OPAQUE"):
    return Event(uid="owner-evt", status=status, transp=transp, kind="timed", start_utc=start, end_utc=end)


def whole_days(first, after_last, kind="all_day"):
    return Event(uid="owner-day", status="", transp="", kind=kind, start_utc=None, end_utc=None,
                 start_date=first, end_date=after_last)


class G11Test(GateTest):
    def test_g11_passes_when_a_read_made_now_shows_nothing_overlapping(self):
        ctx = FakeCtx()
        result = gate.check(booking(), ctx)
        self.assertEqual(self.one(result, "G11").status, "pass")
        self.assertIn(("fresh_read", SLOT), ctx.calls)

    def test_g11_blocks_an_overlapping_event(self):
        one_minute = timed(SLOT.end_utc - dt.timedelta(minutes=1), SLOT.end_utc + dt.timedelta(hours=1))
        result = self.check(booking(), events=[one_minute])
        self.assertBlocked(result, "G11")
        self.assertEqual(result.why["code"], "gate.g11_overlap")

    def test_g11_marked_free_and_tentative_events_are_busy(self):
        for event in (timed(SLOT.start_utc, SLOT.end_utc, transp="TRANSPARENT"),
                      timed(SLOT.start_utc, SLOT.end_utc, status="TENTATIVE"),
                      timed(SLOT.start_utc, SLOT.end_utc, status="")):
            with self.subTest(event=event):
                self.assertBlocked(self.check(booking(), events=[event]), "G11")

    def test_g11_a_cancelled_event_is_not_busy(self):
        result = self.check(booking(), events=[timed(SLOT.start_utc, SLOT.end_utc, status="CANCELLED")])
        self.assertEqual(self.one(result, "G11").status, "pass")

    def test_g11_back_to_back_is_not_an_overlap(self):
        before = timed(SLOT.start_utc - dt.timedelta(hours=1), SLOT.start_utc)
        after = timed(SLOT.end_utc, SLOT.end_utc + dt.timedelta(hours=1))
        self.assertEqual(self.one(self.check(booking(), events=[before, after]), "G11").status, "pass")

    def test_g11_an_all_day_event_is_busy_its_whole_pacific_day(self):
        self.assertBlocked(self.check(booking(), events=[whole_days(dt.date(2026, 12, 3), dt.date(2026, 12, 4))]), "G11")
        for other in (whole_days(dt.date(2026, 12, 2), dt.date(2026, 12, 3)),
                      whole_days(dt.date(2026, 12, 4), dt.date(2026, 12, 5))):
            with self.subTest(other=other):
                self.assertEqual(self.one(self.check(booking(), events=[other]), "G11").status, "pass")

    def test_g11_an_unknown_zone_event_is_busy_its_whole_pacific_day(self):
        event = whole_days(dt.date(2026, 12, 3), dt.date(2026, 12, 4), kind="unknown_zone")
        self.assertBlocked(self.check(booking(), events=[event]), "G11")

    def test_g11_an_event_she_cannot_place_is_busy(self):
        for event in (timed(None, SLOT.end_utc), Event("x", "", "", "floating", None, None),
                      whole_days(None, None)):
            with self.subTest(event=event):
                self.assertBlocked(self.check(booking(), events=[event]), "G11")

    def test_g11_a_failed_read_is_never_free(self):
        result = self.check(booking(), fresh_read=raises(CalendarError("timed out twice")))
        self.assertBlocked(result, "G11", status="error")
        self.assertEqual(result.why["text"], "Blocked: check G11 could not run, so it fails closed.")
        self.assertBlocked(self.check(booking(), fresh_read=lambda slot: None), "G11", status="error")


# --- G12: ledger first -------------------------------------------------------------------------------
class G12Test(GateTest):
    def record(self, **over):
        row = {"booking_id": BOOKING, "message_key": KEY, "thread_key": THREAD, "sender": RAE, "state": "intent"}
        row.update(over)
        return {row["booking_id"]: row}

    def test_g12_passes_with_the_booking_id_in_the_ledger_before_the_write(self):
        self.assertEqual(self.one(self.check(booking()), "G12").status, "pass")

    def test_g12_blocks_a_booking_not_in_the_ledger(self):
        result = self.check(booking(), ledger={})
        self.assertBlocked(result, "G12")
        self.assertEqual(result.why["code"], "gate.g12_ledger")

    def test_g12_blocks_a_booking_recorded_for_another_message_or_thread(self):
        self.assertBlocked(self.check(booking(), ledger=self.record(message_key="other@x.example")), "G12")
        self.assertBlocked(self.check(booking(), ledger=self.record(thread_key="other@x.example")), "G12")

    def test_g12_blocks_a_second_write_of_the_same_booking(self):
        for state in ("written", "confirmed", "cancelled", "collision_unresolved"):
            with self.subTest(state=state):
                self.assertBlocked(self.check(booking(), state=state), "G12")

    def test_g12_blocks_when_the_thread_has_another_live_booking(self):
        other = {"booking_id": "doris-77aa", "message_key": "old@x.example", "thread_key": THREAD, "sender": RAE,
                 "state": "confirmed"}
        self.assertBlocked(self.check(booking(), live=other), "G12")
        same = self.record()[BOOKING]
        self.assertEqual(self.one(self.check(booking(), live=same), "G12").status, "pass")

    def test_g12_blocks_a_booking_id_that_is_not_hers(self):
        result = self.check(booking(booking_id="evt-123"), ledger=self.record(booking_id="evt-123"))
        self.assertBlocked(result, "G12")

    def test_g12_an_unreadable_ledger_errors(self):
        self.assertBlocked(self.check(booking(), booking=raises(RuntimeError("db locked"))), "G12", status="error")


# --- G13: fixed .ics -------------------------------------------------------------------------------
class G13Test(GateTest):
    def test_g13_checks_the_meeting_link_from_env_as_the_location(self):
        self.check(booking())
        self.fakes["doris.ics"].fields_ok.assert_called_once_with(ics_bytes(BOOKING, SLOT), meeting_link=ROOM)

    def test_g13_passes_a_fixed_field_ics_for_this_booking(self):
        result = self.check(booking())
        self.assertEqual(self.one(result, "G13").status, "pass")
        self.assertEqual(self.one(result, "G13").detail["ics_status"], "confirmed")

    def test_g13_blocks_an_ics_with_a_field_outside_the_list(self):
        self.fakes["doris.ics"].fields_ok.return_value = False
        result = self.check(booking())
        self.assertBlocked(result, "G13")
        self.assertEqual(result.why["code"], "gate.g13_ics")

    def test_g13_blocks_an_attendee_or_organizer_even_if_the_helper_missed_it(self):
        for extra in ("ATTENDEE;CN=Pat:mailto:" + PAT + "\r\n", "ORGA\r\n NIZER:mailto:" + PAT + "\r\n",
                      "attendee:mailto:" + PAT + "\r\n"):
            with self.subTest(extra=extra):
                self.assertBlocked(self.check(booking(ics=ics_bytes(BOOKING, SLOT, extra=extra))), "G13")

    def test_g13_blocks_an_ics_for_another_booking_or_another_time(self):
        self.assertBlocked(self.check(booking(ics=ics_bytes("doris-other", SLOT))), "G13")
        self.assertBlocked(self.check(booking(direct=False, ics=ics_bytes(BOOKING, SLOT2)), offered={SLOT}), "G13")

    def test_g13_checks_the_cancel_ics_on_a_cancel_reply(self):
        self.assertEqual(self.one(self.check(cancel_done(), state="cancelled"), "G13").status, "pass")
        wrong = cancel_done(ics=ics_bytes(BOOKING, SLOT, status="CONFIRMED"))
        self.assertBlocked(self.check(wrong, state="cancelled"), "G13")

    def test_g13_a_booking_without_its_ics_errors(self):
        self.assertBlocked(self.check(booking(ics=None)), "G13", status="error")

    def test_g13_a_missing_ics_module_errors(self):
        with mock.patch.dict(sys.modules, {"doris.ics": None}):
            self.assertBlocked(self.check(booking()), "G13", status="error")

    def test_g13_does_not_apply_without_an_attachment(self):
        self.assertEqual(self.one(self.check(reply("T2")), "G13").status, "n/a")
        self.assertEqual(self.one(self.check(delete(), state="confirmed"), "G13").status, "n/a")


# --- G14: her own event ---------------------------------------------------------------------------
class G14Test(GateTest):
    def record(self, **over):
        row = {"booking_id": BOOKING, "message_key": KEY, "thread_key": THREAD, "sender": RAE, "state": "confirmed"}
        row.update(over)
        return {row["booking_id"]: row}

    def test_g14_passes_her_own_booking_for_this_sender_on_this_thread(self):
        self.assertEqual(self.one(self.check(delete(), ledger=self.record()), "G14").status, "pass")

    def test_g14_blocks_a_booking_she_does_not_have(self):
        result = self.check(delete(), ledger={})
        self.assertBlocked(result, "G14")
        self.assertEqual(result.why["code"], "gate.g14_not_hers")

    def test_g14_blocks_another_senders_or_another_threads_booking(self):
        self.assertBlocked(self.check(delete(), ledger=self.record(sender=PAT)), "G14")
        self.assertBlocked(self.check(delete(), ledger=self.record(thread_key="other@x.example")), "G14")

    def test_g14_blocks_a_booking_marked_gone_cancelled_or_removed(self):
        for state in ("gone", "cancelled", "removed"):
            with self.subTest(state=state):
                self.assertBlocked(self.check(delete(), ledger=self.record(state=state)), "G14")

    def test_g14_blocks_an_event_id_that_is_not_hers(self):
        result = self.check(delete(booking_id="owner-evt-1"), ledger=self.record(booking_id="owner-evt-1"))
        self.assertBlocked(result, "G14")


# --- G15: clean read-back ---------------------------------------------------------------------------
class G15Test(GateTest):
    def test_g15_passes_a_confirmation_after_a_clean_readback(self):
        ctx = FakeCtx(state="written")
        self.assertPasses(gate.check(confirm(), ctx))
        self.assertIn(("readback_clean", BOOKING), ctx.calls)

    def test_g15_blocks_a_confirmation_without_a_clean_readback(self):
        result = self.check(confirm(), state="written", clean=False)
        self.assertBlocked(result, "G15")
        self.assertEqual(result.why["code"], "gate.g15_readback")

    def test_g15_never_confirms_a_collision_unresolved_or_unwritten_booking(self):
        for state in ("collision_unresolved", "intent", "cancelled", "gone", "removed"):
            with self.subTest(state=state):
                self.assertBlocked(self.check(confirm(), state=state), "G15")

    def test_g15_an_unreadable_readback_errors(self):
        result = self.check(confirm(), state="written", readback_clean=raises(OSError("receipts unreadable")))
        self.assertBlocked(result, "G15", status="error")

    def test_g15_applies_to_the_confirmation_only(self):
        self.assertEqual(self.one(self.check(reply("T2")), "G15").status, "n/a")
        self.assertEqual(self.one(self.check(cancel_done(), state="cancelled"), "G15").status, "n/a")


# --- G16: nothing opened, nobody new -------------------------------------------------------------------
class G16Test(GateTest):
    PDF = Attachment(name="invoice.pdf", declared_type="application/pdf", size=48213)

    def test_g16_passes_when_nothing_was_opened_and_the_recipient_is_the_sender(self):
        result = self.check(reply("T2"), message=message(attachments=[self.PDF], links_seen=2))
        g16 = self.one(result, "G16")
        self.assertEqual(g16.status, "pass")
        self.assertEqual(g16.detail, {"opened": 0, "attachments": 1, "links_seen": 2, "host": "mail",
                                      "recipient_source": "sender"})

    def test_g16_a_link_count_off_the_contract_errors(self):
        self.assertBlocked(self.check(reply("T2"), message=message(links_seen="3")), "G16", status="error")

    def test_g16_blocks_when_an_attachment_was_opened(self):
        opened = Attachment(name="qr.png", declared_type="image/png", size=900, inspected=True)
        result = self.check(reply("T2"), message=message(attachments=[self.PDF, opened]))
        self.assertBlocked(result, "G16")
        self.assertEqual(result.why["code"], "gate.g16_opened")

    def test_g16_blocks_an_action_that_is_not_one_of_the_fixed_kinds(self):
        forward = Action(kind="forward", message_key=KEY, thread_key=THREAD, to=[PAT])
        result = self.check(forward)
        self.assertBlocked(result, "G16")
        self.assertEqual([r.check for r in result.results if r.status != "n/a"], ["G1", "G2", "G3", "G16"])

    def test_g16_blocks_a_recipient_who_is_neither_the_sender_nor_the_owner(self):
        result = self.check(reply("T2", bcc=[OWNER, PAT]))
        self.assertEqual(self.one(result, "G16").status, "fail")
        result = self.check(reply("T2", recipient_source="body"))
        self.assertEqual(self.one(result, "G16").status, "fail")

    def test_g16_blocks_attendees_on_a_calendar_write(self):
        self.assertBlocked(self.check(booking(to=[PAT])), "G16")

    def test_g16_passes_the_digest_to_the_owner(self):
        g16 = self.one(self.check(digest(), message=None), "G16")
        self.assertEqual((g16.status, g16.detail["host"], g16.detail["recipient_source"]), ("pass", "mail", "owner"))


# --- G17: nothing sensitive -------------------------------------------------------------------------
class G17Test(GateTest):
    def test_g17_passes_clean_mail_and_clean_outgoing_text(self):
        self.assertEqual(self.one(self.check(reply("T2")), "G17").status, "pass")

    def test_g17_blocks_a_hold_notice_whose_quote_carries_a_card_number(self):
        result = self.check(hold_notice(slots={"quote": "Please charge my card " + CARD + " today."}))
        self.assertBlocked(result, "G17")
        self.assertEqual(result.why["code"], "gate.g17_sensitive")
        self.assertEqual(self.one(result, "G17").detail["outgoing"], "card-number")
        self.assertNotIn(CARD, json.dumps(gate.receipt_payload(result)))

    def test_g17_blocks_when_the_message_itself_matches_the_screen(self):
        sensitive = message(quoted_text="> card " + CARD)
        self.assertBlocked(self.check(reply("T2"), message=sensitive), "G17")
        self.assertBlocked(self.check(booking(), message=sensitive), "G17")

    def test_g17_scans_every_template_slot(self):
        action = hold_notice(slots={"lines": ["Offered times to: Rae", ["nested", "card " + CARD]]})
        action.body_text = body("S1", {"reason": "money", "quote": "clean"})
        self.assertEqual(self.one(self.check(action), "G17").status, "fail")

    def test_g17_a_missing_sensitive_module_errors(self):
        with mock.patch.dict(sys.modules, {"doris.sensitive": None}):
            self.assertBlocked(self.check(hold_notice()), "G17", status="error")

    def test_g17_does_not_apply_to_a_delete(self):
        self.assertEqual(self.one(self.check(delete(), state="confirmed"), "G17").status, "n/a")


# --- which checks apply to which action, and fail closed everywhere -----------------------------------
class AppliesAndFailClosedTest(GateTest):
    def test_each_kind_gets_exactly_its_checks_and_passes_when_all_is_well(self):
        rows = [
            ("T2 offer", reply("T2"), {},
             ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G16", "G17"]),
            ("T1 confirmation", confirm(), {"state": "written"},
             ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G13", "G15", "G16", "G17"]),
            ("T10 cancel done", cancel_done(), {"state": "cancelled"},
             ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G13", "G16", "G17"]),
            ("T18 move", reply("T18", booking_id=BOOKING, span=SLOT, ics=ics_bytes(BOOKING, SLOT, "CANCELLED")),
             {"state": "cancelled"}, ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G13", "G16", "G17"]),
            ("S1 hold notice", hold_notice(), {"model_answer": None},
             ["G1", "G2", "G3", "G5", "G6", "G7", "G8", "G16", "G17"]),
            ("S2 digest", digest(), {"message": None, "model_answer": None},
             ["G1", "G2", "G3", "G5", "G6", "G8", "G16", "G17"]),
            ("direct booking", booking(), {},
             ["G1", "G2", "G3", "G4", "G6", "G7", "G9", "G10", "G11", "G12", "G13", "G16", "G17"]),
            ("pick booking", booking(direct=False), {"offered": {SLOT}},
             ["G1", "G2", "G3", "G4", "G6", "G7", "G9", "G11", "G12", "G13", "G16", "G17"]),
            ("cancel (delete)", delete(), {"state": "confirmed"}, ["G1", "G2", "G3", "G6", "G14", "G16"]),
        ]
        for name, action, ctx_over, expected in rows:
            with self.subTest(action=name):
                result = self.check(action, **ctx_over)
                self.assertPasses(result)
                self.assertEqual([r.check for r in result.results if r.status != "n/a"], expected)
                self.assertEqual(result.why["text"], "All %d checks that apply passed." % len(expected))

    def test_a_missing_ctx_field_errors_and_blocks(self):
        full = FakeCtx()
        ctx = types.SimpleNamespace(**{name: getattr(full, name) for name in dir(full)
                                       if not name.startswith("_") and name != "counts"})
        result = gate.check(reply("T2"), ctx)
        self.assertBlocked(result, "G7", status="error")
        self.assertEqual(self.one(result, "G7").detail, {"missing": "counts"})
        self.assertEqual(result.why["text"], "Blocked: check G7 could not run, so it fails closed.")

    def test_a_required_ctx_field_set_to_none_errors(self):
        self.assertBlocked(self.check(booking(), now=None), "G9", status="error")
        self.assertBlocked(self.check(reply("T2"), owner_addr=None), "G6", status="error")
        self.assertBlocked(self.check(booking(), now=dt.datetime(2026, 12, 1, 9, 0)), "G9", status="error")

    def test_a_helper_that_raises_errors_only_its_own_check(self):
        self.fakes["doris.slots"].bookable.side_effect = RuntimeError("bug in slots")
        result = self.check(booking())
        self.assertBlocked(result, "G9", status="error")
        self.assertEqual([r.check for r in result.results if r.status == "error"], ["G9"])

    def test_an_error_detail_names_the_exception_class_never_its_text(self):
        result = self.check(booking(), fresh_read=raises(CalendarError("rae@landscaping.example said hi")))
        self.assertEqual(self.one(result, "G11").detail, {"error": "CalendarError"})
        self.assertNotIn("said hi", json.dumps(gate.receipt_payload(result)))

    def test_no_path_passes_by_default(self):
        nothing = gate.check(None, FakeCtx())
        self.assertFalse(nothing.passed)
        self.assertEqual(len(nothing.results), 17)
        no_ctx = gate.check(reply("T2"), None)
        self.assertFalse(no_ctx.passed)
        self.assertEqual((no_ctx.first_fail, no_ctx.halt_cause), ("G1", "G1"))
        bare = Action(kind="send_reply", message_key=KEY, thread_key=THREAD)
        self.assertFalse(self.check(bare).passed)


if __name__ == "__main__":
    unittest.main()
