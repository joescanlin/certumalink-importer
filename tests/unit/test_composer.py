"""StubComposeProvider + composer constants (Studio AI compose). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma.composer import (BANNED_CLAIMS, DEFAULT_MODEL, MESSAGE_TYPE_KEYS, MODEL_IDS,
                              REQUIRED_TOKENS, AnthropicComposeProvider, ComposeRefused,
                              ComposeRequest, StubComposeProvider)


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class _FakeClient:
    """Stands in for anthropic.Anthropic; records the create() kwargs and returns a canned response."""
    def __init__(self, resp):
        self._resp = resp
        self.captured = {}
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.captured = kw
                return outer._resp
        self.messages = _Messages()


class ComposerStubTests(unittest.TestCase):
    def setUp(self):
        self.p = StubComposeProvider()

    def test_every_message_type_is_compliant(self):
        for mt in MESSAGE_TYPE_KEYS:
            out = self.p.compose(ComposeRequest(message_type=mt, specialty="Dermatology"))
            self.assertTrue(out.subject.strip(), mt)
            for tok in REQUIRED_TOKENS:
                self.assertIn(tok, out.body, f"{mt} missing {tok}")
            low = (out.subject + "\n" + out.body).lower()
            for banned in BANNED_CLAIMS:
                self.assertNotIn(banned, low, f"{mt} contains banned claim {banned}")

    def test_specialty_is_substituted_not_left_as_a_token(self):
        out = self.p.compose(ComposeRequest(message_type="first_touch", specialty="Cardiology"))
        self.assertIn("Cardiology", out.subject)
        self.assertNotIn("{specialty}", out.body)

    def test_brief_is_folded_in(self):
        out = self.p.compose(ComposeRequest(message_type="first_touch",
                                            brief="we serve same-day appointments"))
        self.assertIn("same-day appointments", out.body)

    def test_unknown_type_falls_back_to_first_touch(self):
        out = self.p.compose(ComposeRequest(message_type="nonsense"))
        for tok in REQUIRED_TOKENS:
            self.assertIn(tok, out.body)

    def test_personalization_token_preserved_for_the_copywriter(self):
        out = self.p.compose(ComposeRequest(message_type="follow_up_1"))
        self.assertIn("{last_name}", out.body)

    def test_constants(self):
        self.assertIn(DEFAULT_MODEL, MODEL_IDS)
        self.assertEqual(DEFAULT_MODEL, "claude-opus-4-8")
        self.assertIn("claude-fable-5", MODEL_IDS)


class AnthropicComposeProviderTests(unittest.TestCase):
    """The real provider via the injectable client seam - no SDK / API key needed."""

    def test_structured_output_is_parsed_and_model_is_honored(self):
        client = _FakeClient(_Resp('{"subject": "Hi", "body": "Body {claim_url}"}'))
        prov = AnthropicComposeProvider("sk-test", client=client)
        out = prov.compose(ComposeRequest(message_type="first_touch", model="claude-fable-5"))
        self.assertEqual(out.subject, "Hi")
        self.assertEqual(out.body, "Body {claim_url}")
        # the dropdown model flows into the API call, and structured output is requested
        self.assertEqual(client.captured["model"], "claude-fable-5")
        self.assertEqual(client.captured["output_config"]["format"]["type"], "json_schema")

    def test_refusal_raises_compose_refused(self):
        client = _FakeClient(_Resp("", stop_reason="refusal"))
        prov = AnthropicComposeProvider("sk-test", client=client)
        with self.assertRaises(ComposeRefused):
            prov.compose(ComposeRequest(message_type="first_touch"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
