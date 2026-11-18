import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation import BedrockConversation, ConversationError
from relay import Store


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ".test-temp"
        self.root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.root, prefix="conversation-")
        store = Store(Path(self.temp.name))
        self.settings = {
            "provider": "amazon-bedrock",
            "region": "us-west-2",
            "model": "test-model",
            "reasoning_effort": "low",
            "max_output_tokens": 4096,
            "max_conversations_per_hour": 60,
            "request_timeout_seconds": 30,
        }
        self.brain = BedrockConversation(store, self.settings)
        self.brain.token_path.write_bytes(b"protected-test-fixture")

    def tearDown(self):
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(self.root))
        self.temp.cleanup()

    def response(self, result, *, status="completed"):
        return {"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}

    def reply(self, response):
        with patch("conversation.protect", return_value=b"test-only-credential"), patch(
            "conversation.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())
        ) as transport:
            result = self.brain.reply("A question", [], {})
        return result, transport.call_args.args[0]

    def test_selected_model_region_and_reasoning_remain_exact(self):
        result, request = self.reply(self.response({"reply": "Hello", "intent": "chat", "app": None, "task": None, "instruction": None}))
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], self.settings["model"])
        self.assertEqual(payload["reasoning"]["effort"], self.settings["reasoning_effort"])
        self.assertIn(self.settings["region"], request.full_url)
        self.assertEqual(result["reply"], "Hello")

    def test_incomplete_response_never_becomes_an_action(self):
        with self.assertRaises(ConversationError):
            self.reply(self.response({"reply": "Opened", "intent": "open_app", "app": "notepad"}, status="incomplete"))

    def test_malformed_output_or_action_fields_fail_closed(self):
        candidates = [
            {"status": "completed", "output": "invalid"},
            {"status": "completed", "output": [{"type": "message", "content": [None]}]},
            self.response({"reply": "Opened", "intent": "open_app", "app": ["notepad"]}),
            self.response({"reply": "", "intent": "chat", "app": None}),
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ConversationError):
                self.reply(candidate)

    def test_model_operation_schema_is_callable_only_with_current_host_capability(self):
        context = {"owner_message_permissions": {"intents": ["chat", "project_operation"]},
                   "project_operations": {"operations": ["project_reply"], "project": "sample-project"}}
        response = self.response({"reply": "", "intent": "project_operation", "operation": "project_reply"})
        with patch("conversation.protect", return_value=b"test-only-credential"), patch(
                "conversation.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as transport:
            result = self.brain.reply("Check the sample project thread", [], context)
        schema = json.loads(transport.call_args.args[0].data)["text"]["format"]["schema"]
        self.assertEqual(schema["properties"]["operation"]["enum"], [None, "project_reply"])
        self.assertEqual(result["operation"], "project_reply")
        with self.assertRaises(ConversationError):
            self.reply(response)


if __name__ == "__main__":
    unittest.main()
