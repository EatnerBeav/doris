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


if __name__ == "__main__":
    unittest.main()
