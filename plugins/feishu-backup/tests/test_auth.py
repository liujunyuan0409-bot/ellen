import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import auth, config  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402

CFG = config.Config(app_id="cli_fake", app_secret="fake_secret")


class AuthTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(config, "CONFIG_DIR", Path(self._tmp.name) / "fb")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_scopes_list(self):
        self.assertEqual(len(auth.SCOPES), 119)
        self.assertEqual(len(set(auth.SCOPES)), 119)
        self.assertNotIn("auth:user.id:read", auth.SCOPES)

    def test_authorize_url_contains_scope_and_state(self):
        url = auth.build_authorize_url("cli_fake", auth.REDIRECT_URI, ["a:b", "c:d"], "st123")
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["client_id"], ["cli_fake"])
        self.assertEqual(q["scope"], ["a:b c:d"])
        self.assertEqual(q["state"], ["st123"])
        self.assertEqual(q["redirect_uri"], [auth.REDIRECT_URI])

    def test_token_not_refreshed_when_fresh(self):
        config.save_token({"access_token": "A", "refresh_token": "R", "expires_in": 7200,
                           "obtained_at": int(time.time())})
        t = FakeTransport()
        self.assertEqual(auth.TokenManager(CFG, transport=t).access_token(), "A")
        self.assertEqual(t.calls, [])

    def test_token_refresh_when_near_expiry(self):
        config.save_token({"access_token": "old", "refresh_token": "R1", "expires_in": 7200,
                           "obtained_at": int(time.time()) - 7000})
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", body={
            "code": 0, "access_token": "new", "refresh_token": "R2", "expires_in": 7200})
        self.assertEqual(auth.TokenManager(CFG, transport=t).access_token(), "new")
        saved = config.load_token()
        self.assertEqual(saved["refresh_token"], "R2")
        self.assertGreater(saved["obtained_at"], int(time.time()) - 5)
        import json
        sent = json.loads(t.calls[0]["body"])
        self.assertEqual(sent["grant_type"], "refresh_token")
        self.assertEqual(sent["refresh_token"], "R1")

    def test_refresh_failure_raises_login_required(self):
        config.save_token({"access_token": "old", "refresh_token": "R1", "expires_in": 7200,
                           "obtained_at": int(time.time()) - 7000})
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", status=400, body={
            "code": 20037, "error": "invalid_grant"})
        with self.assertRaises(auth.LoginRequired):
            auth.TokenManager(CFG, transport=t).access_token()

    def test_no_token_raises_login_required(self):
        with self.assertRaises(auth.LoginRequired):
            auth.TokenManager(CFG, transport=FakeTransport()).access_token()

    def test_tenant_token(self):
        t = FakeTransport().queue("POST", "/auth/v3/tenant_access_token/internal",
                                  body={"code": 0, "tenant_access_token": "TT", "expire": 7200})
        self.assertEqual(auth.tenant_token(CFG, transport=t), "TT")


if __name__ == "__main__":
    unittest.main()
