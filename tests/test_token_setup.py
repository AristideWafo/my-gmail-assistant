import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.gmail import token_setup

CONFIG = {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "GOCSPX-x"}}


class LoadClientConfigTests(unittest.TestCase):
    def _write(self, content):
        path = Path(tempfile.mkdtemp()) / "client_secret.json"
        path.write_text(json.dumps(content))
        return path

    def test_loads_desktop_client_file(self):
        self.assertEqual(token_setup.load_client_config(self._write(CONFIG)), CONFIG)

    def test_rejects_non_desktop_client_file(self):
        with self.assertRaises(ValueError):
            token_setup.load_client_config(self._write({"web": {}}))


class FormatEnvLinesTests(unittest.TestCase):
    def test_outputs_the_three_gmail_variables(self):
        lines = token_setup.format_env_lines(CONFIG, "1//refresh").splitlines()

        self.assertEqual(
            lines,
            [
                "GOOGLE_CLIENT_ID=id.apps.googleusercontent.com",
                "GOOGLE_CLIENT_SECRET=GOCSPX-x",
                "GMAIL_REFRESH_TOKEN=1//refresh",
            ],
        )


class ObtainRefreshTokenTests(unittest.TestCase):
    def _run_with_creds(self, refresh_token):
        flow = MagicMock()
        flow.run_local_server.return_value = MagicMock(refresh_token=refresh_token)
        module = MagicMock()
        module.InstalledAppFlow.from_client_config.return_value = flow
        with patch.dict("sys.modules", {"google_auth_oauthlib": MagicMock(), "google_auth_oauthlib.flow": module}):
            return token_setup.obtain_refresh_token(CONFIG), flow, module

    def test_requests_offline_access_with_consent_and_gmail_scope(self):
        token, flow, module = self._run_with_creds("1//refresh")

        self.assertEqual(token, "1//refresh")
        flow.run_local_server.assert_called_once_with(port=0, access_type="offline", prompt="consent")
        scopes = module.InstalledAppFlow.from_client_config.call_args.kwargs["scopes"]
        self.assertEqual(scopes, token_setup.GmailClient.SCOPES)

    def test_raises_when_google_returns_no_refresh_token(self):
        with self.assertRaises(RuntimeError):
            self._run_with_creds(None)


if __name__ == "__main__":
    unittest.main()
