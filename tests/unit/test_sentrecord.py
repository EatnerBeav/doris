"""sentrecord: state/live/smoke-sent.json, the smoke's sent record, in the one format the smoke
writes and a live run reads. Writer and reader are tested against each other here; test_smoke
and test_live_loop test the smoke's own record against the live preflight from each side.
"""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from doris import caps, paths, sentrecord

UTC = dt.timezone.utc
WHEN = dt.datetime(2026, 11, 28, 15, 4, 5, 678000, tzinfo=UTC)
GOOD = {"look": 1, "sent_copy_found": True, "ts": "2026-11-28T15:04:05Z", "waited_s": 0.4}


def as_written(record):
    """A record laid out as the smoke lays it out, whatever its values: so a test of one field
    is refused for that field, never for the layout."""
    return json.dumps(record, sort_keys=True).encode("ascii") + b"\n"


def but(**changes):
    record = dict(GOOD)
    for key, value in changes.items():
        if value is KeyError:
            del record[key]
        else:
            record[key] = value
    return as_written(record)


class SentRecordFormatTest(unittest.TestCase):

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="sentrecord-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.path = sentrecord.path(self.state)

    def put(self, data):
        with open(self.path, "wb") as fh:
            fh.write(data)

    def test_one_file_in_the_live_state_folder(self):
        self.assertEqual(sentrecord.NAME, "smoke-sent.json")
        self.assertEqual(sentrecord.path(paths.state_dir("live")),
                         os.path.join(paths.STATE, "live", "smoke-sent.json"))

    def test_the_smoke_writes_one_line_of_ascii_json_with_four_sorted_keys_and_no_value_of_hers(self):
        self.assertEqual(sentrecord.encode(1, 0.43, WHEN),
                         b'{"look": 1, "sent_copy_found": true, "ts": "2026-11-28T15:04:05Z", "waited_s": 0.4}\n')
        self.assertEqual(sentrecord.encode(2, 5.06, WHEN.astimezone(dt.timezone(dt.timedelta(hours=-8)))),
                         b'{"look": 2, "sent_copy_found": true, "ts": "2026-11-28T15:04:05Z", "waited_s": 5.1}\n')

    def test_every_record_the_smoke_can_write_is_one_a_live_run_takes(self):
        for look in (1, 2):
            for waited in (0, 0.0, 0.04, 0.05, 4.96, 5.0, 61.25, 3600.0):
                with self.subTest(look=look, waited=waited):
                    sentrecord.write(self.path, look, waited, WHEN)
                    self.assertIsNone(sentrecord.problem(self.state, WHEN))
                    self.assertEqual(sorted(os.listdir(self.state)), ["smoke-sent.json"])   # no temp file

    def test_the_smoke_never_writes_a_record_a_live_run_would_refuse(self):
        for look, waited in ((0, 0.4), (3, 0.4), (True, 0.4), (1, -0.4), (1, float("nan")), (1, float("inf"))):
            with self.subTest(look=look, waited=waited):
                with self.assertRaises(ValueError):
                    sentrecord.encode(look, waited, WHEN)
                with self.assertRaises(ValueError):
                    sentrecord.write(self.path, look, waited, WHEN)
                self.assertEqual(os.listdir(self.state), [])

    def test_a_record_is_fresh_from_when_the_smoke_wrote_it_until_a_day_after(self):
        self.assertEqual(caps.SMOKE_RECORD_MAX_AGE, dt.timedelta(hours=24))
        sentrecord.write(self.path, 1, 0.4, WHEN)                 # ts 15:04:05, to the second
        written = WHEN.replace(microsecond=0)
        for now in (written, WHEN, written + dt.timedelta(hours=23, minutes=59, seconds=59),
                    written + caps.SMOKE_RECORD_MAX_AGE):
            with self.subTest(now=now):
                self.assertIsNone(sentrecord.problem(self.state, now))
        for now in (written + caps.SMOKE_RECORD_MAX_AGE + dt.timedelta(seconds=1), written + dt.timedelta(days=30)):
            with self.subTest(now=now):
                self.assertEqual(sentrecord.problem(self.state, now),
                                 "the smoke check's record (smoke-sent.json) is more than 24 hours old")
        for now in (written - dt.timedelta(seconds=1), written - dt.timedelta(days=1)):
            with self.subTest(now=now):
                self.assertEqual(sentrecord.problem(self.state, now),
                                 "the smoke check's record (smoke-sent.json) is dated after now")

    def test_a_shorter_limit_reads_as_hours_and_minutes(self):
        sentrecord.write(self.path, 1, 0.4, WHEN)
        with mock.patch.object(caps, "SMOKE_RECORD_MAX_AGE", dt.timedelta(minutes=90)):
            self.assertEqual(sentrecord.problem(self.state, WHEN + dt.timedelta(hours=2)),
                             "the smoke check's record (smoke-sent.json) is more than 1.5 hours old")

    def test_no_record_is_refused_as_no_smoke_having_found_the_copy(self):
        self.assertEqual(sentrecord.problem(self.state, WHEN), sentrecord.MISSING)
        self.assertEqual(sentrecord.MISSING, "no smoke check has found her sent copy in Sent yet")

    def test_a_record_that_cannot_be_read_is_refused(self):
        os.makedirs(self.path)                   # a folder where the record goes
        self.assertRegex(sentrecord.problem(self.state, WHEN),
                         r"^the smoke check's record \(smoke-sent\.json\) cannot be read: \w+\Z")

    def test_anything_but_the_smokes_own_layout_is_refused(self):
        good = as_written(GOOD)
        cases = {
            "empty": b"",
            "not json": b"not json\n",
            "a list": b"[true]\n",
            "a string": b'"sent_copy_found"\n',
            "not ascii": good[:-2] + b', "x": "\xc3\xa9"}\n',
            "not utf-8": b"\xff\xfe\n",
            "too long": good[:-2] + b"," + b" " * sentrecord.MAX_BYTES + b"}\n",
            "no line end": good[:-1],
            "two lines": good + good,
            "other spacing": json.dumps(GOOD, sort_keys=True, indent=1).encode("ascii") + b"\n",
            "keys unsorted": json.dumps(dict(reversed(list(GOOD.items())))).encode("ascii") + b"\n",
            "a key twice": good[:-2] + b', "look": 1}\n',
            "waited_s spelled 0.40": good.replace(b'"waited_s": 0.4', b'"waited_s": 0.40'),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.put(data)
                self.assertTrue(sentrecord.problem(self.state, WHEN).startswith(
                    "the smoke check's record (smoke-sent.json) is not as the smoke writes it: "), name)

    def test_a_key_missing_or_extra_or_a_value_of_another_kind_is_refused_naming_the_field(self):
        cases = [
            (but(ts=KeyError), "its keys are not exactly look, sent_copy_found, ts, waited_s"),
            (but(sent_copy_found=KeyError), "its keys are"),
            (but(look=KeyError), "its keys are"),
            (but(waited_s=KeyError), "its keys are"),
            (but(to="whizz@fastmail.com"), "its keys are"),
            (but(sent_copy_found=False), "sent_copy_found is not true"),
            (but(sent_copy_found="true"), "sent_copy_found is not true"),
            (but(sent_copy_found=1), "sent_copy_found is not true"),
            (but(look=0), "look is not 1 or 2"),
            (but(look=3), "look is not 1 or 2"),
            (but(look=True), "look is not 1 or 2"),
            (but(look=1.0), "look is not 1 or 2"),
            (but(look="1"), "look is not 1 or 2"),
            (but(waited_s=0), "waited_s is not seconds to 0.1"),
            (but(waited_s=5), "waited_s is not seconds to 0.1"),
            (but(waited_s=-0.4), "waited_s is not seconds to 0.1"),
            (but(waited_s=0.05), "waited_s is not seconds to 0.1"),
            (but(waited_s=float("nan")), "waited_s is not seconds to 0.1"),
            (but(waited_s=float("inf")), "waited_s is not seconds to 0.1"),
            (but(waited_s="0.4"), "waited_s is not seconds to 0.1"),
            (but(waited_s=None), "waited_s is not seconds to 0.1"),
            (but(ts="2026-11-28 15:04:05Z"), "ts is not a UTC time to the second"),
            (but(ts="2026-11-28T15:04:05"), "ts is not a UTC time to the second"),
            (but(ts="2026-11-28T15:04:05+00:00"), "ts is not a UTC time to the second"),
            (but(ts="2026-11-28T15:04:05.678Z"), "ts is not a UTC time to the second"),
            (but(ts="2026-11-28T15:04Z"), "ts is not a UTC time to the second"),
            (but(ts="2026-02-30T15:04:05Z"), "ts is not a UTC time to the second"),
            (but(ts="2026-11-28T24:04:05Z"), "ts is not a UTC time to the second"),
            (but(ts="２026-11-28T15:04:05Z"), "ts is not a UTC time to the second"),
            (but(ts=1795878245), "ts is not a UTC time to the second"),
        ]
        for data, why in cases:
            with self.subTest(data=data):
                self.put(data)
                said = sentrecord.problem(self.state, WHEN)
                self.assertTrue(said.startswith(
                    "the smoke check's record (smoke-sent.json) is not as the smoke writes it: " + why), said)
                self.assertNotIn("whizz", said)


if __name__ == "__main__":
    unittest.main()
