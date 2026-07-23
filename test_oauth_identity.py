import base64
import json
import os
import unittest
from unittest import mock

from utils import oauth


class OAuthIdentityContractTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("GOOGLE_SA_JSON", None)
        os.environ.pop("GOOGLE_USER_TOKEN_JSON", None)
        os.environ.pop("USE_SA_CREDS", None)

    def test_load_creds_always_uses_service_account(self):
        os.environ["GOOGLE_USER_TOKEN_JSON"] = '{"legacy":true}'
        os.environ["USE_SA_CREDS"] = "0"
        with mock.patch.object(oauth, "load_sa_creds", return_value=object()) as load_sa:
            result = oauth.load_creds()
        self.assertIsNotNone(result)
        load_sa.assert_called_once_with()

    def test_missing_service_account_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "GOOGLE_SA_JSON"):
            oauth.load_sa_creds()

    def test_raw_json_and_base64_share_one_parser(self):
        info = {"type": "service_account", "client_email": "test@example.invalid"}
        encoded = base64.b64encode(
            json.dumps(info, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        with mock.patch.object(
            oauth.service_account.Credentials,
            "from_service_account_info",
            return_value=object(),
        ) as create:
            oauth.load_sa_creds(json.dumps(info))
            oauth.load_sa_creds(encoded)
        self.assertEqual(create.call_count, 2)
        for call in create.call_args_list:
            self.assertEqual(call.args[0], info)
            self.assertEqual(call.kwargs["scopes"], oauth.SCOPES)

    def test_runtime_sources_do_not_reference_user_oauth_switch(self):
        root = os.path.dirname(os.path.abspath(__file__))
        paths = [
            "main.py",
            "utils/oauth.py",
            "scripts/backfill_drive_gone.py",
            "README.md",
            "requirements.txt",
        ]
        forbidden = (
            "GOOGLE_USER_TOKEN_JSON",
            "USE_SA_CREDS",
            "load_user_creds",
            "google.oauth2.credentials",
            "google-auth-oauthlib",
        )
        for relative in paths:
            with open(os.path.join(root, relative), encoding="utf-8") as handle:
                content = handle.read()
            for marker in forbidden:
                self.assertNotIn(marker, content, f"{relative}: {marker}")


if __name__ == "__main__":
    unittest.main()
