from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.desktop_app import DesktopControl
from sira.desktop_chat import (
    DesktopChatSettingsStore,
    desktop_chat_cost_guard,
    desktop_chat_status,
    run_desktop_model_chat,
)
from sira.providers.gemini_chat import (
    DesktopChatBatch,
    GeminiDesktopChatModel,
)
from sira.provider_catalog import get_provider
from sira.runtime import RuntimeStateStore
from sira.self_modification import PROTECTED_PATHS


class FakeChatModel:
    def __init__(self):
        self.calls = 0

    def generate(self, payload):
        self.calls += 1
        self.last_payload = payload
        return DesktopChatBatch(
            {
                "reply": "Model-backed SIRA reply.",
                "intent": "conversation",
                "needs_research": False,
            },
            api_requests=1,
            input_tokens=30,
            output_tokens=12,
            attempt_count=1,
            retry_delays=(),
        )


class DesktopChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        RuntimeStateStore(self.root).save({
            "desired_state": "off",
            "worker_state": "stopped",
            "pid": None,
            "started_at": "2026-09-21T10:00:00+00:00",
            "heartbeat_at": "2026-09-21T10:00:00+00:00",
            "generation": 8,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def configure_key(self):
        (self.root / ".env").write_text(
            "GEMINI_API_KEY=fixture-key\n",
            encoding="utf-8",
        )

    def confirm_free(self):
        DesktopChatSettingsStore(
            self.root
        ).set_free_tier_confirmed(True)

    def test_protected_chat_boundary_is_registered(self):
        self.assertIn(
            "src/sira/desktop_chat.py",
            PROTECTED_PATHS,
        )
        self.assertIn(
            "src/sira/providers/gemini_chat.py",
            PROTECTED_PATHS,
        )

    def test_provider_catalog_formally_exposes_desktop_chat(self):
        gemini = get_provider("gemini")
        self.assertIn("desktop_chat", gemini.capabilities)

    def test_model_chat_blocks_before_model_without_confirmation(self):
        self.configure_key()
        model = FakeChatModel()
        report = run_desktop_model_chat(
            self.root,
            "Explain evidence synthesis.",
            model=model,
        )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(model.calls, 0)
        self.assertEqual(
            report["cost_guard"]["reason"],
            "free_tier_confirmation_missing",
        )
        self.assertEqual(report["metrics"]["api_requests"], 0)
        self.assertFalse(report["paid_spending"])

    def test_missing_key_after_confirmation_creates_access_request(self):
        self.confirm_free()
        report = run_desktop_model_chat(
            self.root,
            "Explain SIRA architecture.",
            model=FakeChatModel(),
        )
        self.assertEqual(report["status"], "blocked")
        self.assertIsInstance(
            report["access_request_id"],
            str,
        )
        self.assertIn(
            "GEMINI_API_KEY",
            report["reply"],
        )

    def test_confirmed_model_chat_uses_memory_runtime_context(self):
        self.configure_key()
        self.confirm_free()
        model = FakeChatModel()
        report = run_desktop_model_chat(
            self.root,
            "Explain what SIRA can do.",
            history=[
                {"role": "user", "text": "hello"},
                {"role": "assistant", "text": "hi"},
            ],
            model=model,
        )
        self.assertEqual(report["status"], "completed")
        self.assertEqual(
            report["reply"],
            "Model-backed SIRA reply.",
        )
        self.assertEqual(model.calls, 1)
        self.assertEqual(
            model.last_payload["runtime"]["effective_state"],
            "stopped",
        )
        self.assertFalse(
            model.last_payload["authority"]["runtime_control"]
        )
        self.assertEqual(
            report["metrics"]["api_requests"],
            1,
        )
        self.assertEqual(
            desktop_chat_cost_guard(
                self.root
            )["local_requests_this_month"],
            1,
        )

    def test_operational_desktop_chat_stays_local_zero_model(self):
        control = DesktopControl(self.root)
        with patch(
            "sira.desktop_app.run_desktop_model_chat"
        ) as model_chat:
            reply = control.chat_send(
                "what is your status?"
            )
        self.assertEqual(
            reply["mode"],
            "local_operational",
        )
        model_chat.assert_not_called()

    def test_general_desktop_chat_uses_guarded_model_boundary(self):
        control = DesktopControl(self.root)
        fake = {
            "status": "completed",
            "reply": "Hello from the guarded model.",
            "intent": "conversation",
            "needs_research": False,
            "access_request_id": None,
            "metrics": {"api_requests": 1},
            "artifact": "/tmp/audit.json",
        }
        with patch(
            "sira.desktop_app.run_desktop_model_chat",
            return_value=fake,
        ) as model_chat:
            reply = control.chat_send(
                "Tell me something useful about Python."
            )
        self.assertEqual(reply["mode"], "model")
        self.assertEqual(
            reply["assistant"]["text"],
            "Hello from the guarded model.",
        )
        model_chat.assert_called_once()

    def test_status_never_claims_paid_or_runtime_authority(self):
        self.configure_key()
        status = desktop_chat_status(self.root)
        self.assertFalse(
            status["paid_spending_authority"]
        )
        self.assertFalse(
            status["runtime_control_authority"]
        )
        self.assertFalse(
            status["promotion_authority"]
        )


class GeminiDesktopChatModelTests(unittest.TestCase):
    def test_request_is_structured_tool_free_and_secret_not_in_body(self):
        captured = {}

        def fake_request_json(
            request,
            timeout,
            opener,
            max_bytes,
        ):
            captured["request"] = request
            captured["timeout"] = timeout
            captured["max_bytes"] = max_bytes
            return {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": json.dumps({
                                "reply": "Safe reply",
                                "intent": "conversation",
                                "needs_research": False,
                            })
                        }]
                    }
                }],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                },
            }

        model = GeminiDesktopChatModel(
            "super-secret-fixture",
            sleep_fn=lambda _: None,
            jitter_fn=lambda _a, _b: 0.0,
        )
        with patch(
            "sira.providers.gemini_chat.request_json",
            side_effect=fake_request_json,
        ):
            result = model.generate({
                "message": "hello",
                "runtime": {"effective_state": "stopped"},
                "relevant_memories": [],
                "conversation_history": [],
                "authority": {"runtime_control": False},
            })

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        encoded = json.dumps(body)
        self.assertNotIn("tools", body)
        self.assertNotIn("super-secret-fixture", encoded)
        self.assertIn(
            "gemini-3.1-flash-lite:generateContent",
            request.full_url,
        )
        self.assertEqual(result.api_requests, 1)
        self.assertEqual(result.data["reply"], "Safe reply")


if __name__ == "__main__":
    unittest.main()
