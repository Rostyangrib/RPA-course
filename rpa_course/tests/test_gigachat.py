from io import BytesIO
import json
from time import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from uuid import UUID

from rpa_course.config import GigaChatConfig
from rpa_course.gigachat import GigaChatClient, GigaChatSetupError, OAUTH_URL


def json_response(value):
    return BytesIO(json.dumps(value).encode("utf-8"))


class GigaChatTests(unittest.TestCase):
    def setUp(self):
        self.client = GigaChatClient(GigaChatConfig())
        self.opener = Mock()
        self.enterContext(patch("rpa_course.gigachat.build_opener", return_value=self.opener))
        self.enterContext(patch("rpa_course.gigachat.read_credential", return_value="test-key"))
        self.sleep = self.enterContext(patch("rpa_course.gigachat.sleep"))

    def test_uses_oauth_and_reuses_token_within_cycle(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        self.opener.open.side_effect = [json_response({"access_token": "mock-token", "expires_at": int(time() + 600)}),
                                        json_response(response), json_response(response)]
        self.assertEqual(self.client.complete({"messages": []}), "{}")
        self.assertEqual(self.client.complete({"messages": []}), "{}")
        auth, chat, next_chat = self.opener.open.call_args_list
        auth_request, = auth.args
        self.assertEqual(auth_request.full_url, OAUTH_URL)
        self.assertEqual(auth_request.get_header("Authorization"), "Basic test-key")
        self.assertEqual(UUID(auth_request.get_header("Rquid")).version, 4)
        self.assertEqual(auth_request.data, b"scope=GIGACHAT_API_PERS")
        for call in (chat, next_chat):
            request, = call.args
            self.assertEqual(request.full_url, "https://api.giga.chat/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer mock-token")
            self.assertEqual(json.loads(request.data)["model"], "GigaChat-2-Pro")
            self.assertEqual(call.kwargs["timeout"], 180)

    def test_retries_network_error_and_stops_on_rejected_auth(self):
        self.opener.open.side_effect = [URLError("timeout"), HTTPError(OAUTH_URL, 401, "mock", {}, BytesIO())]
        with self.assertRaisesRegex(GigaChatSetupError, "HTTP 401"):
            self.client.complete({})
        self.assertEqual(self.opener.open.call_count, 2)
        self.sleep.assert_called_once_with(30)
        first, second = self.opener.open.call_args_list
        self.assertNotEqual(first.args[0].get_header("Rquid"), second.args[0].get_header("Rquid"))
