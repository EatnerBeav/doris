"""netguard: refuse and record every connect outside the allowed hosts; restore on uninstall."""
import socket
import threading
import unittest

from doris import netguard

TEST_NET = "192.0.2.1"   # RFC 5737 documentation address: never routable


class RefusesTest(unittest.TestCase):
    def setUp(self):
        self.guard = netguard.install(())

    def tearDown(self):
        self.guard.uninstall()

    def test_create_connection_to_a_name_is_refused_and_recorded(self):
        with self.assertRaises(netguard.NetworkRefused):
            socket.create_connection(("attacker.example.invalid", 443), timeout=1)
        self.assertEqual(len(self.guard.attempts), 1)
        a = self.guard.attempts[0]
        self.assertEqual((a["host"], a["port"], a["call"], a["allowed"]),
                         ("attacker.example.invalid", 443, "create_connection", False))

    def test_raw_connect_is_refused_and_recorded(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(netguard.NetworkRefused):
                s.connect((TEST_NET, 9))
        finally:
            s.close()
        self.assertEqual([(a["host"], a["port"], a["call"]) for a in self.guard.attempts],
                         [(TEST_NET, 9, "connect")])

    def test_connect_ex_is_refused_and_recorded(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(netguard.NetworkRefused):
                s.connect_ex((TEST_NET, 25))
        finally:
            s.close()
        self.assertEqual(self.guard.attempts[0]["call"], "connect_ex")
        self.assertFalse(self.guard.attempts[0]["allowed"])

    def test_name_lookup_of_a_refused_host_never_happens(self):
        with self.assertRaises(netguard.NetworkRefused):
            socket.getaddrinfo("exfil.example.invalid", 53)
        self.assertEqual(self.guard.attempts[0]["call"], "getaddrinfo")

    def test_refusals_are_counted(self):
        for port in (1, 2):
            with self.assertRaises(netguard.NetworkRefused):
                socket.create_connection(("x.invalid", port), timeout=1)
        self.assertEqual(self.guard.refused_count(), 2)

    def test_refused_is_an_oserror_so_callers_fail_closed(self):
        self.assertTrue(issubclass(netguard.NetworkRefused, OSError))


class AllowsTest(unittest.TestCase):
    def test_allowed_host_connects_and_is_recorded_as_allowed(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted = []
        t = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
        t.start()
        guard = netguard.install(("localhost", "127.0.0.1"))
        try:
            conn = socket.create_connection(("localhost", port), timeout=2)
            conn.close()
        finally:
            guard.uninstall()
            t.join(2)
            for c in accepted:
                c.close()
            server.close()
        self.assertTrue(guard.attempts)
        self.assertTrue(all(a["allowed"] for a in guard.attempts))
        self.assertEqual(guard.refused_count(), 0)

    def test_allowed_names_are_case_insensitive(self):
        guard = netguard.install(("API.Example.Invalid",))
        try:
            self.assertTrue(guard.allows("api.example.invalid"))
            self.assertFalse(guard.allows("api.example.invalid.evil.test"))
        finally:
            guard.uninstall()


class RestoreTest(unittest.TestCase):
    def test_uninstall_restores_every_patch(self):
        before = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
                  socket.getaddrinfo, "connect" in socket.socket.__dict__)
        guard = netguard.install(())
        self.assertIsNot(socket.create_connection, before[2])
        guard.uninstall()
        after = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
                 socket.getaddrinfo, "connect" in socket.socket.__dict__)
        self.assertEqual(before, after)

    def test_context_manager_uninstalls_on_error(self):
        original = socket.create_connection
        with self.assertRaises(ZeroDivisionError):
            with netguard.install(()):
                1 / 0
        self.assertIs(socket.create_connection, original)

    def test_a_nested_guard_only_narrows_and_both_record(self):
        # The traps runner holds a guard while loop.run may install its own: nesting must not crash,
        # an inner guard can never widen what the outer one allows, and the outer one sees everything.
        outer = netguard.install(())
        try:
            inner = netguard.install(("allowed.example.invalid",))
            with self.assertRaises(netguard.NetworkRefused):
                socket.getaddrinfo("allowed.example.invalid", 443)
            inner.uninstall()
            self.assertEqual(len(inner.attempts), 1)
            self.assertEqual(len(outer.attempts), 1)
            self.assertFalse(outer.attempts[0]["allowed"])
            self.assertIs(netguard.active(), outer)
            with self.assertRaises(netguard.NetworkRefused):
                socket.create_connection(("x.invalid", 1), timeout=1)
            self.assertEqual(len(outer.attempts), 2)
        finally:
            outer.uninstall()
        self.assertIsNone(netguard.active())
        self.assertFalse("connect" in socket.socket.__dict__)

    def test_outer_uninstalled_first_keeps_the_inner_guard_working(self):
        outer = netguard.install(())
        inner = netguard.install(())
        outer.uninstall()
        try:
            with self.assertRaises(netguard.NetworkRefused):
                socket.create_connection(("x.invalid", 1), timeout=1)
            self.assertEqual(len(inner.attempts), 1)
        finally:
            inner.uninstall()
        self.assertIsNone(netguard.active())

    def test_uninstall_twice_is_harmless(self):
        guard = netguard.install(())
        guard.uninstall()
        guard.uninstall()
        self.assertFalse("connect" in socket.socket.__dict__)


class AdmitTest(unittest.TestCase):
    """admit(host, under): a host a service names, checked by the caller, is allowed on every installed
    guard that allows that service's own host `under` (the JMAP session on api.fastmail.com names
    apiUrl on phl.api.fastmail.com): only a whole-label name below `under`, never on a guard that
    does not allow `under`, and only for the life of the guards it was added to."""

    def tearDown(self):
        while netguard.active() is not None:
            netguard.active().uninstall()

    def test_a_host_below_an_allowed_one_is_allowed_once_admitted(self):
        guard = netguard.install(("api.fastmail.com",))
        self.assertFalse(guard.allows("phl.api.fastmail.com"))
        self.assertTrue(netguard.admit("phl.api.fastmail.com", "api.fastmail.com"))
        self.assertTrue(guard.allows("phl.api.fastmail.com"))
        self.assertTrue(guard.allows("PHL.Api.FastMail.com."))
        self.assertEqual(guard.allowed, frozenset(["api.fastmail.com"]))      # what it was installed with
        self.assertEqual(guard.admitted, {"phl.api.fastmail.com"})
        self.assertTrue(netguard.admit("a.b-2.api.fastmail.com", "API.FastMail.com"))

    def test_only_a_whole_label_name_below_it_is_admitted(self):
        guard = netguard.install(("api.fastmail.com",))
        for bad in ("xapi.fastmail.com", "evil-api.fastmail.com.attacker", "api.fastmail.com.attacker",
                    "phl.api.fastmail.com.attacker", "fastmail.com", "attacker.example", ".api.fastmail.com",
                    "a..api.fastmail.com", "-a.api.fastmail.com", "a-.api.fastmail.com", "a_b.api.fastmail.com",
                    "a b.api.fastmail.com", "a%2e.api.fastmail.com", "\u0430.api.fastmail.com", "", None, 7):
            self.assertFalse(netguard.admit(bad, "api.fastmail.com"), bad)
        self.assertFalse(netguard.admit("phl.api.fastmail.com", ""))
        self.assertEqual(guard.admitted, set())
        self.assertFalse(guard.allows("xapi.fastmail.com"))

    def test_only_a_guard_that_allows_the_service_is_told_and_any_other_still_refuses(self):
        outer = netguard.install(("api.fastmail.com", "127.0.0.1"))
        inner = netguard.install(("127.0.0.1",))                # as a traps guard: it allows no Fastmail host
        self.assertFalse(netguard.admit("phl.api.fastmail.com", "api.fastmail.com"))
        self.assertTrue(outer.allows("phl.api.fastmail.com"))
        self.assertFalse(inner.allows("phl.api.fastmail.com"))
        with self.assertRaises(netguard.NetworkRefused):
            socket.create_connection(("phl.api.fastmail.com", 443), timeout=1)      # refused before any lookup
        self.assertFalse(outer.attempts[-1]["allowed"])

    def test_nested_guards_that_both_allow_the_service_are_both_told(self):
        outer = netguard.install(("api.fastmail.com",))
        inner = netguard.install(("api.fastmail.com", "api.anthropic.com"))
        self.assertTrue(netguard.admit("phl.api.fastmail.com", "api.fastmail.com"))
        self.assertTrue(outer.allows("phl.api.fastmail.com") and inner.allows("phl.api.fastmail.com"))

    def test_an_admitted_host_goes_with_its_guard_and_without_a_guard_nothing_is_admitted(self):
        self.assertFalse(netguard.admit("phl.api.fastmail.com", "api.fastmail.com"))
        guard = netguard.install(("api.fastmail.com",))
        netguard.admit("phl.api.fastmail.com", "api.fastmail.com")
        guard.uninstall()
        again = netguard.install(("api.fastmail.com",))
        self.assertFalse(again.allows("phl.api.fastmail.com"))

    def test_a_host_admitted_by_a_name_it_was_admitted_under_is_not_a_parent_itself(self):
        """Only what a guard was installed with can be a parent: an admitted host admits nothing below it."""
        guard = netguard.install(("api.fastmail.com",))
        netguard.admit("phl.api.fastmail.com", "api.fastmail.com")
        self.assertFalse(netguard.admit("x.phl.api.fastmail.com", "phl.api.fastmail.com"))
        self.assertFalse(guard.allows("x.phl.api.fastmail.com"))


if __name__ == "__main__":
    unittest.main()
