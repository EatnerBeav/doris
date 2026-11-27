"""The policy table (decide): one row per test. Code decides; the model only classifies."""

import datetime as dt
import unittest

from doris import policy, why
from doris.shapes import ModelAnswer, ParseResult, PickResult, Reading, Slot

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 12, 1, 16, 0, tzinfo=UTC)          # Tue Dec 1, 8:00 AM Pacific
WED_11 = dt.datetime(2026, 12, 2, 19, 0, tzinfo=UTC)       # Wed Dec 2, 11:00 AM Pacific
SENDER = "priya@northwind.example"


def answer(cls="meeting_request", ok=True, side_question=False, side_text=None, injection=False):
    return ModelAnswer(cls=cls, date=None, time24=None, zone=None, length_min=None, pick=None,
                       injection_seen=injection, side_question=side_question, raw="{}", source="stub",
                       request_sha256="0" * 64, ok=ok, side_question_text=side_text)


def parse(verdict="offer", failed="B2", instant=None, length=30, length_rule=None):
    readings = [Reading(start_utc=instant, date=None, zone="Pacific", kept=True, rule="T1")] if instant else []
    return ParseResult(verdict=verdict, failed_condition=None if verdict == "book" else failed,
                       readings=readings, instant_utc=instant, zone="Pacific", length_min=length,
                       length_rule=length_rule)


def slot(hours_from_wed_11=0, minutes=30):
    start = WED_11 + dt.timedelta(hours=hours_from_wed_11)
    return Slot(start, start + dt.timedelta(minutes=minutes))


def offer(slots, sender=SENDER, oid=1):
    return {"id": oid, "slots": list(slots), "length_min": 30, "sender": sender,
            "created": NOW, "expires": NOW + dt.timedelta(days=7)}


def thread(**kw):
    base = dict(thread_key="t1", own_thread=False, held=False, offer_count=0, open_offers=[],
                live_booking=None, gone_booking=False, written_to=frozenset(), offer_to=None)
    base.update(kw)
    return policy.ThreadState(**base)


def facts(cls="meeting_request", **kw):
    base = dict(sender=SENDER, branch="1", others_on_thread=0, thread=thread(), answer=answer(cls),
                parse=parse(), model_agrees=False, pick=None, money_code=None, reply_to_mismatch=False)
    base.update(kw)
    return policy.Facts(**base)


def booking(sender=SENDER, booking_id="doris-abc"):
    return {"booking_id": booking_id, "slot": slot(), "sender": sender, "thread_key": "t1", "state": "confirmed"}


class WhyIsListed(unittest.TestCase):
    def assertWhy(self, plan, code):
        self.assertEqual(plan.why["code"], code)
        self.assertIn(plan.why["code"], why.CODES)


class MeetingRequest(WhyIsListed):
    def test_clear_time_parser_and_model_agree_books(self):
        p = policy.decide(facts(parse=parse("book", instant=WED_11), model_agrees=True))
        self.assertEqual(p.action, "book")
        self.assertEqual(p.slot, Slot(WED_11, WED_11 + dt.timedelta(minutes=30)))
        self.assertTrue(p.direct)
        self.assertWhy(p, "policy.book_clear_time")

    def test_written_length_sets_the_slot_end(self):
        p = policy.decide(facts(parse=parse("book", instant=WED_11, length=45), model_agrees=True))
        self.assertEqual(p.slot.end_utc - p.slot.start_utc, dt.timedelta(minutes=45))
        self.assertEqual(p.length_min, 45)

    def test_parser_says_offer_offers_with_the_failed_condition(self):
        p = policy.decide(facts(parse=parse("offer", failed="B3")))
        self.assertEqual((p.action, p.offer_family), ("offer", "T2"))
        self.assertWhy(p, "policy.offer_no_clear_time")
        self.assertIn("B3", p.why["text"])

    def test_model_disagreeing_offers_under_b11(self):
        p = policy.decide(facts(parse=parse("book", instant=WED_11), model_agrees=False))
        self.assertEqual((p.action, p.offer_family), ("offer", "T2"))
        self.assertIn("B11", p.why["text"])

    def test_parser_readings_go_first_in_the_offer(self):
        p = policy.decide(facts(parse=parse("offer", failed="B7", instant=WED_11)))
        self.assertEqual(p.first, (WED_11,))

    def test_longer_than_an_hour_holds(self):
        for rule in ("over_60", "open_ended"):
            p = policy.decide(facts(parse=parse(length_rule=rule)))
            self.assertEqual((p.action, p.hold_code), ("hold", "hold.longer_than_hour"), rule)

    def test_all_day_recurring_or_several_holds(self):
        for rule in ("all_day", "recurring", "multiple", "zero"):
            p = policy.decide(facts(parse=parse(length_rule=rule)))
            self.assertEqual(p.hold_code, "hold.all_day_recurring_multiple", rule)

    def test_a_third_offer_on_a_thread_holds(self):
        p = policy.decide(facts(thread=thread(offer_count=2)))
        self.assertEqual((p.action, p.hold_code), ("hold", "hold.third_offer"))

    def test_a_new_request_while_her_booking_stands_is_a_move(self):
        p = policy.decide(facts(thread=thread(live_booking=booking())))
        self.assertEqual((p.action, p.offer_family, p.booking_id), ("move", "T18", "doris-abc"))


class Picks(WhyIsListed):
    def three(self):
        return [slot(0), slot(24), slot(48)]

    def test_matched_pick_books_that_slot(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer(self.three())],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 1)))
        self.assertEqual((p.action, p.slot, p.offer_id), ("book", slot(24), 1))
        self.assertFalse(p.direct)
        self.assertWhy(p, "policy.pick_books")
        self.assertIn("2", p.why["text"])

    def test_unmatched_pick_resends_the_same_offer(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer(self.three())],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        for kind in ("none", "ambiguous", "near_miss"):
            p = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult(kind)))
            self.assertEqual((p.action, p.template), ("resend", "T12"), kind)
            self.assertWhy(p, "policy.pick_resend")

    def test_yes_on_a_one_slot_offer_books(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer([slot(0)])],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("yes", branch="2", thread=t))
        self.assertEqual((p.action, p.slot), ("book", slot(0)))
        self.assertWhy(p, "policy.yes_books")

    def test_yes_on_a_three_slot_offer_resends(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer(self.three())],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("yes", branch="2", thread=t))
        self.assertEqual((p.action, p.template), ("resend", "T12"))

    def test_pick_plus_question_books_and_keeps_the_copied_question(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer(self.three())],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        a = answer("pick", side_question=True, side_text="Is the call on video?")
        p = policy.decide(facts("pick", branch="2", thread=t, answer=a, pick=PickResult("matched_slot", 0)))
        self.assertEqual(p.action, "book")
        self.assertEqual(p.side_question, "Is the call on video?")
        self.assertWhy(p, "policy.pick_plus_question")

    def test_pick_with_no_open_offer_and_her_booking_standing_is_a_move(self):
        t = thread(own_thread=True, offer_count=1, live_booking=booking(), written_to=frozenset([SENDER]))
        p = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("none")))
        self.assertEqual(p.action, "move")

    def test_pick_with_no_open_offer_and_no_booking_is_a_new_request(self):
        t = thread(own_thread=True, offer_count=1, written_to=frozenset([SENDER]))
        p = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("none")))
        self.assertEqual((p.action, p.offer_family), ("offer", "T2"))

    def test_decline_of_the_first_offer_gets_t3_with_new_times(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer(self.three())],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("decline", branch="2", thread=t))
        self.assertEqual((p.action, p.offer_family, p.exclude_offered), ("offer", "T3", True))
        self.assertWhy(p, "policy.decline_second_offer")

    def test_decline_of_the_second_offer_holds(self):
        t = thread(own_thread=True, offer_count=2, open_offers=[offer(self.three(), oid=2)],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("decline", branch="2", thread=t))
        self.assertEqual((p.action, p.hold_code), ("hold", "hold.third_offer"))

    def test_a_plain_no_closes_the_offer_with_no_reply(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer(self.three())],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("no_thanks", branch="2", thread=t))
        self.assertEqual((p.action, p.template), ("close", None))
        self.assertWhy(p, "policy.no_thanks_close")


class CancelAndMove(WhyIsListed):
    def test_cancel_of_her_own_booking(self):
        t = thread(own_thread=True, live_booking=booking(), written_to=frozenset([SENDER]))
        p = policy.decide(facts("cancel", branch="2", thread=t))
        self.assertEqual((p.action, p.template, p.booking_id), ("cancel", "T10", "doris-abc"))
        self.assertWhy(p, "policy.cancel_own")

    def test_cancel_with_no_booking_holds(self):
        p = policy.decide(facts("cancel"))
        self.assertEqual((p.action, p.hold_code), ("hold", "hold.not_in_ledger"))

    def test_cancel_of_a_booking_marked_gone_holds(self):
        t = thread(own_thread=True, gone_booking=True, written_to=frozenset([SENDER]))
        p = policy.decide(facts("cancel", branch="2", thread=t))
        self.assertEqual(p.hold_code, "hold.not_in_ledger")

    def test_cancel_of_someone_elses_booking_holds(self):
        t = thread(own_thread=True, live_booking=booking(sender="other@else.example"),
                   written_to=frozenset([SENDER]))
        p = policy.decide(facts("cancel", branch="2", thread=t))
        self.assertEqual(p.hold_code, "hold.not_in_ledger")

    def test_move_cancels_and_offers_fresh_times(self):
        t = thread(own_thread=True, offer_count=1, live_booking=booking(), written_to=frozenset([SENDER]))
        p = policy.decide(facts("move", branch="2", thread=t))
        self.assertEqual((p.action, p.offer_family, p.booking_id), ("move", "T18", "doris-abc"))
        self.assertWhy(p, "policy.move_cancel_offer")

    def test_move_when_the_thread_has_used_its_two_offers_holds_and_cancels_nothing(self):
        t = thread(own_thread=True, offer_count=2, live_booking=booking(), written_to=frozenset([SENDER]))
        p = policy.decide(facts("move", branch="2", thread=t))
        self.assertEqual((p.action, p.hold_code), ("hold", "hold.third_offer"))

    def test_move_with_a_bookable_new_time_puts_it_first(self):
        t = thread(own_thread=True, offer_count=1, live_booking=booking(), written_to=frozenset([SENDER]))
        p = policy.decide(facts("move", branch="2", thread=t, parse=parse("book", instant=WED_11),
                                model_agrees=True))
        self.assertEqual(p.first, (WED_11,))

    def test_move_with_no_booking_holds(self):
        p = policy.decide(facts("move"))
        self.assertEqual(p.hold_code, "hold.not_in_ledger")

    def test_move_puts_the_new_time_first_without_model_agreement(self):
        # Offering needs no B11: the parser's kept reading goes first; find_slots checks it is available.
        t = thread(own_thread=True, offer_count=1, live_booking=booking(), written_to=frozenset([SENDER]))
        p = policy.decide(facts("move", branch="2", thread=t, parse=parse("book", instant=WED_11),
                                model_agrees=False))
        self.assertEqual(p.first, (WED_11,))


class FileSilentSuppress(WhyIsListed):
    def test_fyi_is_filed(self):
        p = policy.decide(facts("fyi"))
        self.assertEqual((p.action, p.outcome), ("file", "file"))
        self.assertWhy(p, "policy.file_fyi")

    def test_injection_only_is_filed_not_held(self):
        p = policy.decide(facts("injection_only", answer=answer("injection_only", injection=True)))
        self.assertEqual((p.action, p.outcome), ("file", "file"))
        self.assertWhy(p, "policy.file_injection")

    def test_auto_reply_gets_nothing(self):
        p = policy.decide(facts("auto_reply"))
        self.assertEqual((p.action, p.outcome), ("silent", "silent"))
        self.assertWhy(p, "policy.silent_auto")

    def test_the_models_opt_out_silences_the_sender(self):
        p = policy.decide(facts("opt_out"))
        self.assertEqual((p.action, p.outcome), ("suppress", "suppress"))
        self.assertWhy(p, "policy.suppress_opt_out")


class Holds(WhyIsListed):
    def test_each_held_class_maps_to_its_reason(self):
        expected = {"money": "hold.money", "legal": "hold.legal", "complaint": "hold.complaint",
                    "add_person": "hold.add_person", "unsure": "hold.unsure", "other": "hold.outside_classes"}
        for cls, code in expected.items():
            p = policy.decide(facts(cls))
            self.assertEqual((p.action, p.hold_code, p.outcome), ("hold", code, "hold:" + code[5:]), cls)
            self.assertWhy(p, code)

    def test_no_model_answer_holds(self):
        p = policy.decide(facts(answer=answer(ok=False)))
        self.assertEqual(p.hold_code, "hold.no_model")

    def test_a_held_thread_stays_held_and_sends_no_new_notice(self):
        p = policy.decide(facts(thread=thread(held=True)))
        self.assertEqual((p.action, p.hold_code), ("thread_held", "hold.thread_held"))


class CodeHoldsBeatTheModel(WhyIsListed):
    """Decision 59: a hold found by code beats every model class. Code can force a hold, never a send."""

    def test_money_found_by_code_holds_even_when_the_model_says_fyi(self):
        p = policy.decide(facts("fyi", money_code="word"))
        self.assertEqual(p.hold_code, "hold.money")

    def test_money_found_by_code_with_a_valid_pick_holds_and_books_nothing(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer([slot(0), slot(24)])],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 0), money_code="currency"))
        self.assertEqual((p.action, p.hold_code), ("hold", "hold.money_with_pick"))

    def test_reply_to_unlike_from_holds_even_when_the_model_says_injection_only(self):
        p = policy.decide(facts("injection_only", reply_to_mismatch=True))
        self.assertEqual(p.hold_code, "hold.reply_to_mismatch")

    def test_group_mail_she_would_act_on_is_held(self):
        p = policy.decide(facts("meeting_request", branch="else", others_on_thread=2))
        self.assertEqual(p.hold_code, "hold.group_mail")

    def test_group_mail_she_would_only_file_is_filed(self):
        p = policy.decide(facts("fyi", branch="else", others_on_thread=2))
        self.assertEqual(p.action, "file")

    def test_her_thread_from_an_address_she_never_wrote_to_is_held(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer([slot(0)])],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        p = policy.decide(facts("pick", sender="someone@else.example", branch="2", thread=t,
                                pick=PickResult("matched_slot", 0)))
        self.assertEqual(p.hold_code, "hold.new_address")

    def test_her_thread_from_a_new_address_she_would_only_file_is_filed(self):
        t = thread(own_thread=True, written_to=frozenset([SENDER]))
        p = policy.decide(facts("fyi", sender="someone@else.example", branch="2", thread=t))
        self.assertEqual(p.action, "file")

    def test_the_owner_copied_is_not_someone_else(self):
        # others_on_thread counts neither the owner nor Doris (ruling 18); 0 here means she answers
        p = policy.decide(facts(parse=parse("book", instant=WED_11), model_agrees=True))
        self.assertEqual(p.action, "book")


class OfferForm(unittest.TestCase):
    def test_no_available_slot_holds(self):
        self.assertEqual(policy.offer_form("T2", 0), (None, "hold.no_slot"))

    def test_one_slot_is_t13(self):
        self.assertEqual(policy.offer_form("T2", 1), ("T13", None))
        self.assertEqual(policy.offer_form("T3", 1), ("T13", None))

    def test_t3_with_two_slots_sends_t2(self):
        self.assertEqual(policy.offer_form("T3", 2), ("T2", None))

    def test_full_forms(self):
        for fam in ("T2", "T3", "T4", "T18"):
            self.assertEqual(policy.offer_form(fam, 3), (fam, None))


class Unavailable(WhyIsListed):
    def test_a_direct_booking_that_is_not_available_becomes_t4(self):
        plan = policy.decide(facts(parse=parse("book", instant=WED_11), model_agrees=True))
        p = policy.on_unavailable(plan, "overlap", thread())
        self.assertEqual((p.action, p.offer_family), ("offer", "T4"))
        self.assertWhy(p, "policy.offer_conflict")
        self.assertIn("overlap", p.why["text"])

    def test_a_stale_pick_gets_fresh_times_as_a_new_offer(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer([slot(0)])],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        plan = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 0)))
        p = policy.on_unavailable(plan, "under_24h", t)
        self.assertEqual((p.action, p.offer_family, p.exclude_offered), ("offer", "T4", True))
        self.assertWhy(p, "policy.stale_pick_reoffer")

    def test_a_collision_after_a_direct_write_offers_t4_naming_the_time_asked(self):
        plan = policy.decide(facts(parse=parse("book", instant=WED_11), model_agrees=True))
        p = policy.on_collision(plan, thread())
        self.assertEqual((p.action, p.outcome, p.offer_family, p.asked), ("offer", "offer", "T4", WED_11))
        self.assertWhy(p, "policy.collision_reoffer")

    def test_a_collision_after_a_picked_write_reoffers_without_the_old_times(self):
        t = thread(own_thread=True, offer_count=1, open_offers=[offer([slot(0)])],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        plan = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 0)))
        p = policy.on_collision(plan, t)
        self.assertEqual((p.outcome, p.offer_family, p.exclude_offered), ("reoffer", "T4", True))
        self.assertWhy(p, "policy.collision_reoffer")

    def test_a_collision_when_the_thread_has_two_offers_holds(self):
        t = thread(own_thread=True, offer_count=2, open_offers=[offer([slot(0)], oid=2)],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        plan = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 0)))
        self.assertEqual(policy.on_collision(plan, t).hold_code, "hold.third_offer")

    def test_a_stale_pick_when_the_thread_has_two_offers_holds(self):
        t = thread(own_thread=True, offer_count=2, open_offers=[offer([slot(0)], oid=2)],
                   written_to=frozenset([SENDER]), offer_to=SENDER)
        plan = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 0)))
        p = policy.on_unavailable(plan, "overlap", t)
        self.assertEqual(p.hold_code, "hold.third_offer")


class MoneyCheck(unittest.TestCase):
    def test_a_currency_sign_next_to_a_number(self):
        for text in ("It's $500 a month", "£18.40 each", "costs 40€", "about ¥2000"):
            self.assertEqual(policy.money_check(text), "currency", text)

    def test_a_currency_code_next_to_a_number(self):
        for text in ("USD 200 up front", "18.40 GBP", "a 300eur deposit"):
            self.assertEqual(policy.money_check(text), "currency", text)

    def test_the_listed_words(self):
        for text in ("Please see the attached Invoice", "I want a refund", "a chargeback", "our lawsuit",
                     "my attorney says", "a subpoena", "the invoices"):
            self.assertEqual(policy.money_check(text), "word", text)

    def test_ordinary_meeting_mail_is_clean(self):
        for text in ("Can we meet Tuesday at 10am Pacific?", "Room 5, floor 2", "Is $ the symbol?",
                     "The Euro team", "refundable is not a listed word"):
            self.assertIsNone(policy.money_check(text), text)


if __name__ == "__main__":
    unittest.main()
