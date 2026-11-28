"""A message that breaks something part way is held, and the run goes on (Contract 7).

Each test runs a copy of the demo sandbox holding only the inbox files it needs, on throwaway
state, and checks what the prospect, the owner and the calendar end up with.
"""

import contextlib
import io
import json
import os
import shutil
import unittest
from unittest import mock

from doris import __main__ as cli
from doris import ledger as ledger_mod
from doris import caps, loop, receipts, supervisor
from tests.unit.test_restart import DORIS, OWNER, Restart, add, edit

PRIYA = "priya@northwind.example"
MARCUS = "marcus@brightlane.example"


def unprocessed(state):
    led = ledger_mod.Ledger(os.path.join(state, "ledger.sqlite"))
    try:
        return led.unprocessed_messages()
    finally:
        led.close()


class HeaderBytes(Restart):
    """One byte outside ASCII in a header she copies into her reply (Fable #2)."""
    names = ("01-priya-request.eml",)

    def booked_and_told(self, s):
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])
        self.assertEqual(len(self.events()), 1, self.events())
        self.assertEqual(self.closing(s, "priya"), ["book"])
        self.assertEqual(unprocessed(self.state), [])

    def test_a_reference_outside_ascii_still_books_and_confirms(self):
        edit(self.root, "01-priya-request.eml", lambda r: r.replace(
            b"MIME-Version", "References: <café@x.example>\r\nMIME-Version".encode("utf-8"), 1))
        self.booked_and_told(self.run_once())

    def test_a_message_id_outside_ascii_still_books_and_confirms(self):
        edit(self.root, "01-priya-request.eml",
             lambda r: r.replace(b"quick-call@", "quick-café@".encode("utf-8"), 1))
        self.booked_and_told(self.run_once())

    def test_a_sender_address_outside_ascii_is_held_before_any_write(self):
        edit(self.root, "01-priya-request.eml",
             lambda r: r.replace(b"<priya@northwind", "<priyà@northwind".encode("utf-8"), 1))
        s = self.run_once()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.sent(PRIYA), [])
        (outcome,) = self.closing(s, "priy")
        self.assertTrue(outcome.startswith("hold:"), outcome)
        self.assertEqual([t for t in self.templates_sent(OWNER) if t == "S1"], ["S1"])   # Fable re-review F


class UnexpectedError(Restart):
    """A bug part way through one message holds that message; the next one still runs."""
    names = ("01-priya-request.eml", "02-marcus-request.eml")

    def test_an_unexpected_error_holds_that_message_and_the_run_goes_on(self):
        def broken(self_, e, f, plan):
            raise RuntimeError("a bug in the offer step")

        with mock.patch.object(loop._Run, "offer", broken):
            s = self.run_once()
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])
        self.assertEqual(self.sent(MARCUS), [])
        self.assertEqual(self.closing(s, "marcus"), ["hold:internal_error"])
        self.assertIn("S1", self.templates_sent(OWNER))
        errors = [l for l in receipts.read_lines(s.receipts_path) if l["kind"] == "error"]
        self.assertEqual([l["payload"]["step"] for l in errors], ["handle"])
        self.assertIn("RuntimeError", errors[0]["payload"]["error"])
        self.assertTrue(s.verify_ok)
        self.assertEqual(unprocessed(self.state), [])

    def test_a_halt_is_never_swallowed(self):
        def halted(self_, e, f, plan):
            raise loop.Halted("cli")

        with mock.patch.object(loop._Run, "offer", halted):
            s = self.run_once()
        self.assertEqual(s.halted, "cli")


class SameBatchFollowUp(Restart):
    """A follow-up that lands in the same ten-minute cycle as the request it answers (Fable #4)."""
    names = ("01-priya-request.eml",)
    FOLLOW_UP = (b"From: Priya Raman <priya@northwind.example>\r\n"
                 b"To: Shane Bornstein <whizz@fastmail.com>\r\n"
                 b"Subject: Re: Quick call this week?\r\n"
                 b"Date: Tue, 01 Dec 2026 08:05:00 -0800\r\n"
                 b"Message-ID: <20261201.0805.priya.scratch-that@northwind.example>\r\n"
                 b"In-Reply-To: <20261201.0802.priya.quick-call@northwind.example>\r\n"
                 b"References: <20261201.0802.priya.quick-call@northwind.example>\r\n"
                 b"MIME-Version: 1.0\r\n"
                 b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
                 b"Content-Transfer-Encoding: 7bit\r\n\r\n"
                 b"Scratch that: could Shane do Thursday, December 3 at 2:00 PM Central for 30 minutes?\r\n\r\n"
                 b"Priya\r\n")

    def test_a_follow_up_in_the_same_batch_supersedes_the_request(self):
        add(self.root, "01b-priya-follow-up.eml", self.FOLLOW_UP, {"forwarded": "yes"})
        s = self.run_once()
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])
        self.assertEqual(len(self.events()), 1, self.events())
        self.assertEqual(self.closing(s, "priya"), ["superseded", "book"])

    def test_a_stranger_naming_her_request_does_not_supersede_it(self):
        # only the sender's own newer message wins on a thread (Fable re-review C)
        stranger = self.FOLLOW_UP.replace(b"Priya Raman <priya@northwind.example>", b"Mal Ory <mal@elsewhere.example>")
        stranger = stranger.replace(b"0805.priya.scratch-that@northwind.example", b"0805.forged@elsewhere.example")
        add(self.root, "01b-stranger.eml", stranger, {"forwarded": "yes"})
        s = self.run_once()
        self.assertEqual(self.templates_sent(PRIYA), ["T1"])
        self.assertEqual(len(self.events()), 1, self.events())
        self.assertEqual(self.closing(s, "priya"), ["book"])
        self.assertEqual(self.sent("mal@elsewhere.example"), [])
        self.assertEqual(len(self.closing(s, "forged")), 1)

    def test_two_messages_that_name_each_other_still_end(self):
        # forged headers: each claims to answer the other; the batch still settles one thread
        first = self.FOLLOW_UP.replace(b"0805.priya.scratch-that", b"0803.priya.loop-a").replace(
            b"In-Reply-To: <20261201.0802.priya.quick-call@northwind.example>",
            b"In-Reply-To: <20261201.0804.priya.loop-b@northwind.example>")
        second = self.FOLLOW_UP.replace(b"0805.priya.scratch-that", b"0804.priya.loop-b").replace(
            b"In-Reply-To: <20261201.0802.priya.quick-call@northwind.example>",
            b"In-Reply-To: <20261201.0803.priya.loop-a@northwind.example>")
        add(self.root, "01b-loop-a.eml", first, {"forwarded": "yes"})
        add(self.root, "01c-loop-b.eml", second, {"forwarded": "yes"})
        s = self.run_once()
        self.assertEqual(len(self.closing(s, "priya")), 3)
        self.assertEqual(unprocessed(self.state), [])


class ForgedChain(Restart):
    """Hundreds of forged mails in one batch, each naming the next as the one it answers: threading
    them never runs out of stack, and every one reaches its closing line (Fable re-review D)."""
    names = ()

    def test_a_long_chain_of_forged_replies_ends(self):
        n = 400
        for i in range(n):
            raw = (b"From: Mal Ory <mal@elsewhere.example>\r\nTo: Shane Bornstein <whizz@fastmail.com>\r\n"
                   b"Subject: Re: chain\r\nDate: Tue, 01 Dec 2026 08:05:00 -0800\r\n"
                   b"Message-ID: <chain-%d@elsewhere.example>\r\nIn-Reply-To: <chain-%d@elsewhere.example>\r\n"
                   b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n\r\nHello.\r\n") % (i, i + 1)
            add(self.root, "c%04d.eml" % i, raw, {"forwarded": "yes"})
        s = self.run_once()
        self.assertEqual(unprocessed(self.state), [])
        self.assertEqual(len(self.closing(s, "chain-")), n)


class InboxReadFails(Restart):
    """The inbox can't be read: the run says so and ends badly, never as a clean empty run (Fable #6)."""
    names = ("01-priya-request.eml",)

    def test_the_summary_says_the_read_failed(self):
        shutil.rmtree(os.path.join(self.root, "inbox"))
        s = self.run_once()
        self.assertTrue(s.read_failed)
        (line,) = [l for l in receipts.read_lines(s.receipts_path) if l["kind"] == "summary"]
        self.assertTrue(line["payload"]["read_failed"])

    def test_the_worker_exits_non_zero_and_says_why(self):
        shutil.rmtree(os.path.join(self.root, "inbox"))
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"mode": "throwaway", "run_name": "run-worker", "state_dir": self.state,
                       "sandbox_root": self.root, "owner_addr": OWNER, "doris_addr": DORIS,
                       "model_source": "stub", "clock_mode": "advance", "retry_wait_s": 0}, fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.worker([config])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("inbox could not be read", out.getvalue())
        self.assertNotIn("Done:", out.getvalue())

    def test_under_the_stub_the_last_line_counts_stand_in_calls_not_model_calls(self):
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"mode": "throwaway", "run_name": "run-worker", "state_dir": self.state,
                       "sandbox_root": self.root, "owner_addr": OWNER, "doris_addr": DORIS,
                       "model_source": "stub", "clock_mode": "advance", "retry_wait_s": 0}, fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.worker([config])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(" 1 booking. Stand-in calls 1, cost none (stub). Receipts ", out.getvalue())
        self.assertNotIn("Model calls", out.getvalue())

    def test_a_run_until_stopped_reads_again_each_cycle_and_never_ends_on_failed_reads(self):
        """Review finding 4: one failed read no longer ends `run`; decision 89: nor do any number in a row.
        From caps.MAIL_READ_FAILURES_IN_A_ROW she tries every caps.MAIL_READ_RETRY_S until a stop."""
        shutil.rmtree(os.path.join(self.root, "inbox"))
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"mode": "throwaway", "run_name": "run-worker", "state_dir": self.state,
                       "sandbox_root": self.root, "owner_addr": OWNER, "doris_addr": DORIS,
                       "model_source": "stub", "clock_mode": "advance", "retry_wait_s": 0,
                       "cycles": None, "cycle_s": 1.0}, fh)
        clock = [1000.0]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] >= 2000.0:                  # tries at 1000 to 1005 (a second apart), 1200 and 1800
                supervisor.request_stop("throwaway", "cli", state_dir=self.state)

        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(loop, "_time", lambda: clock[0]), \
                mock.patch.object(loop, "_sleep", sleep):
            code = cli.worker([config])
        n = caps.MAIL_READ_FAILURES_IN_A_ROW
        said = out.getvalue()
        self.assertEqual(code, supervisor.WORKER_EXIT_HALTED, said)
        self.assertEqual(said.count("Inbox not read: MailError (error line written). She sends nothing until it "
                                    "reads again, and tries again at her next cycle"), n - 1)
        self.assertEqual(said.count("tries again every 10 minutes until it does or she is stopped"), 3)
        self.assertNotIn("Stopped: the inbox could not be read", said)
        self.assertNotIn("Done:", said)


if __name__ == "__main__":
    unittest.main()
