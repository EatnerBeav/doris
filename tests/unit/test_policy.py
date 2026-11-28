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
    # arrived: the message reached her an hour after every offer here went out (offer() sends at NOW)
    base = dict(sender=SENDER, branch="1", others_on_thread=0, thread=thread(), answer=answer(cls),
                parse=parse(), model_agrees=False, pick=None, money_code=None, reply_to_mismatch=False,
                arrived=NOW + dt.timedelta(hours=1))
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

    def test_a_derived_reading_is_never_one_of_the_senders_own_times(self):
        # D5's cross-pair (Fable review 2 of PR #3, finding A): it follows the sender's own times, never
        # among them, in an offer and in a move's fresh times
        mon = WED_11 + dt.timedelta(days=5)
        readings = [Reading(start_utc=WED_11, date=None, zone="Pacific", kept=True, rule="H1"),
                    Reading(start_utc=mon, date=None, zone="Pacific", kept=True, rule="H1", derived=True)]
        p = parse("offer", failed="B2")
        p.readings = readings
        offer_ = policy.decide(facts(parse=p))
        self.assertEqual((offer_.offer_family, offer_.first, offer_.derived), ("T2", (WED_11,), (mon,)))
        move = policy.decide(facts("move", parse=p, thread=thread(live_booking=booking())))
        self.assertEqual((move.offer_family, move.first, move.derived), ("T18", (WED_11,), (mon,)))

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


class APickBeforeHerNewestOffer(WhyIsListed):
    """A numbered pick or a yes names a slot only by its place in an offer: one that reached her before her
    newest offer to the sender went out cannot be answering it, so it gets that offer again (T12), never a
    booking (Fable review 2 of PR #3, finding B). A pick that names the time is read by its time."""
    BEFORE = NOW - dt.timedelta(minutes=6)

    def three(self):
        return [slot(0), slot(24), slot(48)]

    def on_thread(self, *offers):
        return thread(own_thread=True, offer_count=len(offers), open_offers=list(offers),
                      written_to=frozenset([SENDER]), offer_to=SENDER)

    def assertResent(self, p):
        self.assertEqual((p.action, p.template, p.slot, p.offer_id), ("resend", "T12", None, None))
        self.assertWhy(p, "policy.pick_before_offer")

    def test_a_numbered_pick_or_a_yes_from_before_her_nudge_gets_the_offer_again(self):
        # the nudge lists the offer's times again and numbers them anew when one was dropped: "2" written
        # before it went means the old list's second time, which is not the second one now
        nudged_at = NOW + dt.timedelta(days=2)
        two = dict(offer([slot(24), slot(48)]), nudged=nudged_at)
        between = nudged_at - dt.timedelta(minutes=5)          # after the offer, before the nudge
        pick = PickResult(kind="matched_slot", slot_index=1, by="number")
        self.assertResent(policy.decide(facts("pick", branch="2", thread=self.on_thread(two), pick=pick,
                                              arrived=between)))
        one = dict(offer([slot(24)]), nudged=nudged_at)
        self.assertResent(policy.decide(facts("yes", branch="2", thread=self.on_thread(one), arrived=between)))
        # after the nudge went out, the same pick and the same yes book
        after = nudged_at + dt.timedelta(hours=1)
        p = policy.decide(facts("pick", branch="2", thread=self.on_thread(two), pick=pick, arrived=after))
        self.assertEqual((p.action, p.slot), ("book", slot(48)))
        p = policy.decide(facts("yes", branch="2", thread=self.on_thread(one), arrived=after))
        self.assertEqual((p.action, p.slot), ("book", slot(24)))
        # and a pick that names its time is read by its time, nudge or no nudge
        named = PickResult(kind="matched_slot", slot_index=1, by="time")
        p = policy.decide(facts("pick", branch="2", thread=self.on_thread(two), pick=named, arrived=between))
        self.assertEqual((p.action, p.slot), ("book", slot(48)))

    def test_while_her_nudge_may_or_may_not_have_gone_no_number_and_no_yes_books(self):
        """Fable review of PR #42, finding 2: the nudge's send was cut off or its result is unknown, so
        the sender holds the offer's list or the nudge's, and a number or a bare yes could mean
        either. They get the open times again (T12); a time they write out still books."""
        nudged_at = NOW + dt.timedelta(days=2)
        after = nudged_at + dt.timedelta(hours=1)               # even one that reached her after the nudge's time
        two = dict(offer([slot(24), slot(48)]), nudged=nudged_at, nudge="in_doubt")
        one = dict(offer([slot(24)]), nudged=nudged_at, nudge="in_doubt")
        number = PickResult(kind="matched_slot", slot_index=1, by="number")
        for p in (policy.decide(facts("pick", branch="2", thread=self.on_thread(two), pick=number, arrived=after)),
                  policy.decide(facts("yes", branch="2", thread=self.on_thread(one), arrived=after))):
            self.assertEqual((p.action, p.template, p.slot, p.offer_id, p.pick_kind),
                             ("resend", "T12", None, None, "ambiguous"))
            self.assertWhy(p, "policy.pick_resend")
        named = PickResult(kind="matched_slot", slot_index=1, by="time")
        p = policy.decide(facts("pick", branch="2", thread=self.on_thread(two), pick=named, arrived=after))
        self.assertEqual((p.action, p.slot), ("book", slot(48)))
        # a nudge the mail server took is not in doubt: the same number and the same yes book
        for o, kind, kw, want in ((two, "pick", {"pick": number}, slot(48)), (one, "yes", {}, slot(24))):
            sent = dict(o, nudge="sent")
            p = policy.decide(facts(kind, branch="2", thread=self.on_thread(sent), arrived=after, **kw))
            self.assertEqual((p.action, p.slot), ("book", want))

    def test_an_offer_never_nudged_is_timed_by_its_send_alone(self):
        t = self.on_thread(dict(offer(self.three()), nudged=None))
        pick = PickResult(kind="matched_slot", slot_index=0, by="number")
        self.assertEqual(policy.decide(facts("pick", branch="2", thread=t, pick=pick)).action, "book")

    def test_a_numbered_pick_that_reached_her_before_the_offer_went_out_gets_it_again(self):
        t = self.on_thread(offer(self.three()))
        for by in ("number", None):          # a pick that does not say how it named the slot is read by number
            with self.subTest(by=by):
                self.assertResent(policy.decide(facts("pick", branch="2", thread=t, arrived=self.BEFORE,
                                                      pick=PickResult("matched_slot", 0, by))))
        self.assertResent(policy.decide(facts("meeting_request", branch="2", thread=t, arrived=NOW,
                                              pick=PickResult("matched_slot", 0, "number"))))
        p = policy.decide(facts("pick", branch="2", thread=t, pick=PickResult("matched_slot", 0, "number")))
        self.assertEqual((p.action, p.slot), ("book", slot(0)))

    def test_its_arrival_or_the_offers_send_time_unknown_is_never_a_booking(self):
        t = self.on_thread(offer(self.three()))
        self.assertResent(policy.decide(facts("pick", branch="2", thread=t, arrived=None,
                                              pick=PickResult("matched_slot", 0, "number"))))
        unsent = dict(offer(self.three()), created=None)
        self.assertResent(policy.decide(facts("pick", branch="2", thread=self.on_thread(unsent),
                                              pick=PickResult("matched_slot", 0, "number"))))

    def test_it_is_read_against_the_newest_offer(self):
        older = offer([slot(0)], oid=1)
        newer = dict(offer([slot(24), slot(48)], oid=2), created=NOW + dt.timedelta(minutes=70))
        between = NOW + dt.timedelta(minutes=30)
        self.assertResent(policy.decide(facts("pick", branch="2", thread=self.on_thread(older, newer), arrived=between,
                                              pick=PickResult("matched_slot", 1, "number"))))

    def test_a_pick_that_names_the_time_still_books(self):
        t = self.on_thread(offer(self.three()))
        p = policy.decide(facts("pick", branch="2", thread=t, arrived=self.BEFORE,
                                pick=PickResult("matched_slot", 1, "time")))
        self.assertEqual((p.action, p.slot, p.offer_id), ("book", slot(24), 1))

    def test_a_yes_that_reached_her_before_the_offer_went_out_gets_it_again(self):
        t = self.on_thread(offer([slot(0)]))
        self.assertResent(policy.decide(facts("yes", branch="2", thread=t, arrived=self.BEFORE)))
        self.assertResent(policy.decide(facts("yes", branch="2", thread=t, arrived=None)))
        p = policy.decide(facts("yes", branch="2", thread=t))
        self.assertEqual((p.action, p.slot), ("book", slot(0)))


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

    def test_a_cancel_or_move_for_a_booking_found_gone_is_named_on_the_held_thread(self):
        # ruling 17: the thread is already the owner's (its hold notice went when she found the booking
        # gone), so nothing is sent and no second notice goes; the digest says which booking they meant
        gone = thread(own_thread=True, held=True, gone_booking=True, written_to=frozenset([SENDER]))
        for cls in ("cancel", "move"):
            p = policy.decide(facts(cls, branch="2", thread=gone))
            self.assertEqual((p.action, p.outcome, p.hold_code), ("thread_held", "hold:not_in_ledger",
                                                                  "hold.not_in_ledger"), cls)
            self.assertWhy(p, "hold.not_in_ledger")
            self.assertIsNone(p.template)

    def test_anything_else_on_that_held_thread_is_only_a_held_thread(self):
        gone = thread(own_thread=True, held=True, gone_booking=True, written_to=frozenset([SENDER]))
        for cls in ("meeting_request", "pick", "yes", "decline", "fyi", "money", "unsure"):
            p = policy.decide(facts(cls, branch="2", thread=gone))
            self.assertEqual((p.action, p.hold_code), ("thread_held", "hold.thread_held"), cls)
        down = policy.decide(facts("cancel", branch="2", thread=gone, answer=answer("cancel", ok=False)))
        self.assertEqual(down.hold_code, "hold.thread_held")        # no model answer: nothing is read into it
        other = thread(own_thread=True, held=True, written_to=frozenset([SENDER]))
        self.assertEqual(policy.decide(facts("cancel", branch="2", thread=other)).hold_code, "hold.thread_held")


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

    def test_a_sender_on_a_thread_whose_open_offer_went_to_someone_else_is_held_before_any_offer(self):
        # Fable review 2 of PR #3, finding C: Bob names Lena's request, which holds her open offer, and has
        # none of his own: he is held (he would otherwise get an offer of his own on her thread)
        lena = "lena@harborline.example"
        t = thread(own_thread=False, offer_count=1, written_to=frozenset([lena]), offer_to=lena)
        for cls, pick in (("pick", PickResult("matched_slot", 0, "number")), ("meeting_request", None),
                          ("yes", None), ("decline", None), ("move", None)):
            with self.subTest(cls=cls):
                p = policy.decide(facts(cls, branch="1", thread=t, pick=pick,
                                        parse=parse("book", instant=WED_11), model_agrees=True))
                self.assertEqual((p.action, p.hold_code), ("hold", "hold.new_address"))
        self.assertEqual(policy.decide(facts("fyi", branch="1", thread=t)).action, "file")
        # his own open offer on the thread, or no open offer to anyone there: the table goes on as before
        mine = thread(own_thread=False, offer_count=2, open_offers=[offer([slot(0)])], written_to=frozenset([lena]),
                      offer_to=SENDER)
        self.assertEqual(policy.decide(facts("yes", branch="1", thread=mine)).action, "book")
        lapsed = thread(own_thread=False, offer_count=1, written_to=frozenset([lena]), offer_to=None)
        self.assertEqual(policy.decide(facts(branch="1", thread=lapsed)).offer_family, "T2")


class Resume(WhyIsListed):
    """Ruling 8 and A3: a message read again after a kill or a stop keeps what it already did,
    whatever the model or the thread now say (Fable re-review A, B, E)."""

    def test_its_own_hold_stands(self):
        p = policy.decide(facts("pick", own_hold="hold.money"))
        self.assertEqual((p.action, p.hold_code, p.outcome), ("hold", "hold.money", "hold:money"))
        self.assertWhy(p, "policy.resume_hold")

    def test_a_reply_already_in_sent_ends_it(self):
        for template, outcome in (("T1", "book"), ("T2", "offer"), ("T3", "reoffer"), ("T10", "cancel"),
                                  ("T18", "move"), ("T12", "offer")):
            p = policy.decide(facts("unsure", own_reply=template))
            self.assertEqual((p.action, p.template, p.outcome), ("replied", template, outcome))
            self.assertWhy(p, "policy.resume_sent")

    def test_a_reply_sent_could_not_be_asked_about_is_held(self):
        p = policy.decide(facts("pick", own_reply_unknown="T1", own_booking={
            "slot": slot(), "direct": True, "offer_id": None, "length_min": 30}))
        self.assertEqual((p.action, p.hold_code), ("hold", "hold.send_unconfirmed"))
        self.assertWhy(p, "hold.send_unconfirmed")

    def test_its_own_booking_is_picked_up_never_planned_again(self):
        mine = {"slot": slot(2), "direct": False, "offer_id": 4, "length_min": 30}
        p = policy.decide(facts("unsure", own_booking=mine, thread=thread(offer_count=1)))
        self.assertEqual((p.action, p.outcome, p.slot, p.direct, p.offer_id, p.length_min),
                         ("book", "book", slot(2), False, 4, 30))
        self.assertWhy(p, "policy.resume_booking")

    def test_the_hold_comes_first_then_the_reply_then_the_booking(self):
        mine = {"slot": slot(), "direct": True, "offer_id": None, "length_min": 30}
        self.assertEqual(policy.decide(facts(own_hold="hold.send_unconfirmed", own_reply="T1",
                                             own_booking=mine)).action, "hold")
        self.assertEqual(policy.decide(facts(own_reply="T1", own_booking=mine)).action, "replied")

    def test_its_own_effects_come_before_a_held_thread(self):
        # its own hold is what made the thread held
        p = policy.decide(facts(own_hold="hold.money", thread=thread(held=True)))
        self.assertEqual(p.outcome, "hold:money")


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
