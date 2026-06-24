"""EmailProvider tests (Phase 1 task P1.2).

Pure assembly tests run anywhere; the real Mailpit roundtrip skips unless the isolated
certuma-mailpit (SMTP 11026 / API 18026) is reachable (`make db-up` brings it up too).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import unittest
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma.config import Settings  # noqa: E402
from certuma.email import EspProvider, MailpitProvider, build_outbound, get_provider, to_mime  # noqa: E402

MAILPIT_SMTP_PORT = int(os.environ.get("CERTUMA_MAILPIT_SMTP_PORT", "11026"))
MAILPIT_API = os.environ.get("CERTUMA_MAILPIT_API", "http://127.0.0.1:18026")


def _outbound(subject="Hi", to_addr="dr@example.com"):
    return build_outbound(
        to_addr=to_addr, from_addr="jordan@getcertuma.com", from_name="Jordan Avery",
        subject=subject, html_body="<p>hello {body}</p>", text_body="hello",
        reply_to="reply+tok123@getcertuma.com",
        unsubscribe_url="https://getcertuma.com/u/abc", unsubscribe_mailto="mailto:unsub@getcertuma.com",
    )


class AssemblyTests(unittest.TestCase):
    def test_build_outbound_sets_one_click_headers(self):
        o = _outbound()
        self.assertIn("List-Unsubscribe", o.headers)
        self.assertIn("https://getcertuma.com/u/abc", o.headers["List-Unsubscribe"])
        self.assertIn("mailto:unsub@getcertuma.com", o.headers["List-Unsubscribe"])
        self.assertEqual(o.headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_build_outbound_requires_unsubscribe(self):
        with self.assertRaises(ValueError):
            build_outbound(to_addr="a@b.com", from_addr="f@g.com", from_name="F", subject="s",
                           html_body="", text_body="", reply_to="r@g.com",
                           unsubscribe_url="", unsubscribe_mailto="mailto:x@y.com")

    def test_to_mime_headers_and_multipart(self):
        msg = to_mime(_outbound(subject="Subj"))
        self.assertEqual(msg["Subject"], "Subj")
        self.assertIn("Jordan Avery", msg["From"])
        self.assertIn("jordan@getcertuma.com", msg["From"])
        self.assertEqual(msg["Reply-To"], "reply+tok123@getcertuma.com")
        self.assertTrue(msg["Message-ID"])
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertTrue(msg.is_multipart())  # text + html

    def test_mailpit_provider_uses_injected_transport(self):
        captured = {}
        prov = MailpitProvider("127.0.0.1", 11026, transport=lambda m: captured.update(msg=m))
        result = prov.send(_outbound(subject="Captured"))
        self.assertTrue(result.accepted)
        self.assertEqual(result.provider_message_id, captured["msg"]["Message-ID"])
        self.assertEqual(captured["msg"]["Subject"], "Captured")

    def test_factory_selects_provider(self):
        self.assertIsInstance(get_provider(Settings(email_provider="mailpit")), MailpitProvider)
        esp = get_provider(Settings(email_provider="esp"))
        self.assertIsInstance(esp, EspProvider)
        with self.assertRaises(NotImplementedError):
            esp.send(_outbound())
        with self.assertRaises(ValueError):
            get_provider(Settings(email_provider="bogus"))


def _mailpit_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", MAILPIT_SMTP_PORT), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(_mailpit_up(), "isolated Mailpit not reachable (run `make db-up`)")
class MailpitRoundtripTests(unittest.TestCase):
    def test_send_is_captured_by_mailpit(self):
        subject = f"Certuma test {uuid.uuid4().hex[:8]}"
        prov = MailpitProvider("127.0.0.1", MAILPIT_SMTP_PORT)
        result = prov.send(_outbound(subject=subject, to_addr="roundtrip@example.com"))
        self.assertTrue(result.accepted)
        with urllib.request.urlopen(f"{MAILPIT_API}/api/v1/search?query={urllib.parse.quote(subject)}", timeout=5) as r:
            data = json.load(r)
        subjects = [m.get("Subject") for m in data.get("messages", [])]
        self.assertIn(subject, subjects)


class _FakeResp:
    def __init__(self, body, status=200, headers=None):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class EspProviderTests(unittest.TestCase):
    """The cold ESP send seam (P3.10), exercised with an injected opener (no network)."""

    def test_send_via_injected_opener(self):
        captured = {}

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            return _FakeResp(json.dumps({"id": "esp-123"}))

        provider = EspProvider(Settings(esp_api_key="k3y", esp_base_url="https://esp.example.com"),
                               opener=opener)
        res = provider.send(_outbound())
        self.assertTrue(res.accepted)
        self.assertEqual(res.provider_message_id, "esp-123")
        self.assertIn("/v1/messages", captured["url"])
        self.assertEqual(captured["auth"], "Bearer k3y")

    def test_http_error_is_not_accepted(self):
        import urllib.error

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

        res = EspProvider(Settings(esp_api_key="k", esp_base_url="https://esp.example.com"),
                          opener=opener).send(_outbound())
        self.assertFalse(res.accepted)
        self.assertIn("500", res.detail or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
