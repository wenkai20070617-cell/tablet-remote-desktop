import unittest

from app.security import (
    LoginLimiter,
    SessionCapacityError,
    SessionStore,
    effective_scheme,
    forwarded_client_ip,
    host_allowed,
    origin_matches,
)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class SessionStoreTests(unittest.TestCase):
    def test_idle_and_absolute_expiry(self):
        clock = Clock()
        store = SessionStore(10, 30, 3, 2, clock=clock)
        token = store.create()
        clock.value += 9
        self.assertIsNotNone(store.get(token))
        clock.value += 11
        self.assertIsNone(store.get(token))

    def test_connection_limit_is_released(self):
        store = SessionStore(10, 30, 3, 1)
        token = store.create()
        self.assertTrue(store.acquire_connection(token))
        self.assertFalse(store.acquire_connection(token))
        store.release_connection(token)
        self.assertTrue(store.acquire_connection(token))

    def test_active_session_is_not_evicted(self):
        removed = []
        store = SessionStore(10, 30, 1, 1, on_remove=removed.append)
        token = store.create()
        self.assertTrue(store.acquire_connection(token))
        with self.assertRaises(SessionCapacityError):
            store.create()
        self.assertIsNotNone(store.get(token, touch=False))
        store.release_connection(token)
        replacement = store.create()
        self.assertEqual(removed, [token])
        self.assertIsNotNone(store.get(replacement, touch=False))


class LoginLimiterTests(unittest.TestCase):
    def test_lockout_and_reset(self):
        clock = Clock()
        limiter = LoginLimiter(2, 60, 30, clock=clock)
        self.assertEqual(limiter.failure("client"), 0)
        self.assertGreater(limiter.failure("client"), 0)
        clock.value += 31
        self.assertEqual(limiter.retry_after("client"), 0)
        limiter.failure("client")
        limiter.success("client")
        self.assertEqual(limiter.retry_after("client"), 0)


class OriginTests(unittest.TestCase):
    def test_same_origin_and_explicit_allowlist(self):
        self.assertTrue(origin_matches("https://example.com", "https", "example.com", []))
        self.assertFalse(origin_matches("https://evil.example", "https", "example.com", []))
        self.assertTrue(
            origin_matches("https://allowed.example", "https", "example.com", ["https://allowed.example"])
        )

    def test_forwarded_scheme_only_from_loopback(self):
        self.assertEqual(effective_scheme("http", "https", "127.0.0.1"), "https")
        self.assertEqual(effective_scheme("http", "https", "192.0.2.2"), "http")

    def test_proxy_ip_and_host_validation(self):
        self.assertEqual(forwarded_client_ip("127.0.0.1", "203.0.113.9", None), "203.0.113.9")
        self.assertEqual(forwarded_client_ip("192.0.2.2", "203.0.113.9", None), "192.0.2.2")
        self.assertTrue(host_allowed("127.0.0.1:18765", []))
        self.assertTrue(host_allowed("demo.trycloudflare.com", []))
        self.assertTrue(host_allowed("remote.example", ["remote.example"]))
        self.assertFalse(host_allowed("attacker.example", []))


if __name__ == "__main__":
    unittest.main()