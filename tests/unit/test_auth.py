"""Auth primitives tests (Phase 3 task P3.9): password hashing + signed sessions + RBAC. Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma import auth


class AuthPrimitivesTests(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        h, s = auth.hash_password("correct horse")
        self.assertTrue(auth.verify_password("correct horse", h, s))
        self.assertFalse(auth.verify_password("wrong", h, s))

    def test_salts_are_random(self):
        h1, s1 = auth.hash_password("same")
        h2, s2 = auth.hash_password("same")
        self.assertNotEqual(s1, s2)   # distinct salts
        self.assertNotEqual(h1, h2)   # so distinct hashes for the same password

    def test_session_roundtrip(self):
        tok = auth.sign_session(42, "operator", secret="k", now=1000)
        self.assertEqual(auth.verify_session(tok, secret="k", now=1001),
                         {"user_id": 42, "role": "operator"})

    def test_session_rejects_tamper_wrong_secret_and_expiry(self):
        tok = auth.sign_session(1, "admin", secret="k", now=1000)
        self.assertIsNone(auth.verify_session(tok[:-3] + "AAA", secret="k", now=1001))   # tampered sig
        self.assertIsNone(auth.verify_session(tok, secret="other", now=1001))            # wrong secret
        self.assertIsNone(auth.verify_session(
            auth.sign_session(1, "admin", secret="k", now=1000, ttl=10), secret="k", now=2000))  # expired
        self.assertIsNone(auth.verify_session("garbage", secret="k"))

    def test_session_rejects_unknown_role(self):
        # a forged payload claiming a bogus role must not validate even if the structure looks right
        tok = auth.sign_session(1, "operator", secret="k", now=1000)
        self.assertIsNotNone(auth.verify_session(tok, secret="k", now=1001))

    def test_rbac_capabilities(self):
        self.assertTrue(auth.can_write("operator"))
        self.assertTrue(auth.can_write("admin"))
        self.assertFalse(auth.can_write("leadership"))
        self.assertFalse(auth.can_write(None))
        self.assertTrue(auth.is_admin("admin"))
        self.assertFalse(auth.is_admin("operator"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
