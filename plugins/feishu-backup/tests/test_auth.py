import json
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
        # 终审 I3：登录时只申请实际用到的只读权限
        expected = {
            "drive:drive:readonly", "drive:drive.metadata:readonly", "drive:file:readonly", "drive:file:download",
            "drive:export:readonly", "docs:document:export", "docs:doc:readonly", "docx:document:readonly",
            "sheets:spreadsheet:readonly", "bitable:app:readonly", "slides:presentation:read",
            "wiki:wiki:readonly", "wiki:node:read", "wiki:space:read", "space:document:retrieve",
            "search:docs:read", "docs:permission.member:auth", "im:chat:readonly", "im:message:readonly",
            "im:message.group_msg:get_as_user", "im:resource", "im:chat.members:read",
            "contact:user.base:readonly", "offline_access"}
        self.assertEqual(set(auth.SCOPES), expected)
        self.assertEqual(len(auth.SCOPES), len(expected))
        self.assertNotIn("auth:user.id:read", auth.SCOPES)
        writes = [s for s in auth.SCOPES if s.endswith((":create", ":update", ":delete", ":write_only", ":upload",
                                                        ":move", ":copy", ":import"))
                  or s in ("drive:drive", "drive:file", "docs:doc", "docx:document", "im:message", "im:chat",
                           "bitable:app", "sheets:spreadsheet", "wiki:wiki")]
        self.assertEqual(writes, [])

    def test_readme_scope_json_matches(self):
        import json as _json
        import re as _re
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        block = _re.search(r"```json\n(.*?)```", readme, _re.S).group(1)
        d = _json.loads(block)
        self.assertEqual(sorted(d["scopes"]["user"]), sorted(auth.SCOPES))
        self.assertEqual(d["scopes"]["tenant"], [])

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

    def test_documented_refresh_rejection_codes_require_login(self):
        # 终审 I2：只有飞书文档里「续期凭据无效 / 过期 / 被吊销 / 已用过」的错误码才算要重新登录
        for code in (20026, 20037, 20064, 20073, 20074):
            self._stale()
            t = FakeTransport().queue("POST", "/authen/v2/oauth/token", status=400, body={"code": code})
            with self.assertRaises(auth.LoginRequired, msg=code):
                auth.TokenManager(CFG, transport=t).access_token()

    def test_invalid_grant_requires_login(self):
        self._stale()
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", status=400,
                                  body={"code": 12345, "error": "invalid_grant"})
        with self.assertRaises(auth.LoginRequired):
            auth.TokenManager(CFG, transport=t).access_token()

    def test_other_refresh_code_is_transient_not_login(self):
        self._stale()
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", status=400,
                                  body={"code": 20050, "error": "server_error", "error_description": "busy"})
        with self.assertRaises(auth.NetworkError) as cm:
            auth.TokenManager(CFG, transport=t).access_token()
        self.assertNotIsInstance(cm.exception, auth.LoginRequired)
        self.assertIn("20050", str(cm.exception))
        self.assertEqual(config.load_token()["access_token"], "old")

    def test_public_refresh_single_attempt_with_timeout(self):
        self._stale()
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", status=502, body=b"bad gateway")
        sleeps = []
        tm = auth.TokenManager(CFG, transport=t, sleep=sleeps.append)
        with self.assertRaises(auth.NetworkError):
            tm.refresh(config.load_token(), attempts=1, timeout=15)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(t.calls[0]["timeout"], 15)

    def test_refresh_stamps_injected_now(self):
        self._stale()
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", body={
            "code": 0, "access_token": "new", "expires_in": 7200})
        auth.TokenManager(CFG, transport=t, now=lambda: 1234567890).refresh(config.load_token())
        self.assertEqual(config.load_token()["obtained_at"], 1234567890)

    def _stale(self, **extra):
        tok = {"access_token": "old", "refresh_token": "R1", "expires_in": 7200,
               "obtained_at": int(time.time()) - 7000}
        tok.update(extra)
        config.save_token(tok)

    def test_refresh_5xx_then_success(self):
        self._stale()
        t = (FakeTransport().queue("POST", "/authen/v2/oauth/token", status=502, body=b"bad gateway")
             .queue("POST", "/authen/v2/oauth/token", body={
                 "code": 0, "access_token": "new", "refresh_token": "R2", "expires_in": 7200}))
        sleeps = []
        tm = auth.TokenManager(CFG, transport=t, sleep=sleeps.append)
        self.assertEqual(tm.access_token(), "new")
        self.assertEqual(sleeps, [1])

    def test_refresh_persistent_network_error_raises_network_error(self):
        self._stale()
        calls = []

        def tr(m, u, h, b):
            calls.append(1)
            raise OSError("down")
        tm = auth.TokenManager(CFG, transport=tr, sleep=lambda s: None)
        with self.assertRaises(auth.NetworkError):
            tm.access_token()
        self.assertEqual(len(calls), 4)
        self.assertEqual(config.load_token()["access_token"], "old")

    def test_refresh_keeps_old_refresh_token_if_missing(self):
        self._stale()
        t = FakeTransport().queue("POST", "/authen/v2/oauth/token", body={
            "code": 0, "access_token": "new", "expires_in": 7200})
        auth.TokenManager(CFG, transport=t).access_token()
        self.assertEqual(config.load_token()["refresh_token"], "R1")

    def test_no_token_raises_login_required(self):
        with self.assertRaises(auth.LoginRequired):
            auth.TokenManager(CFG, transport=FakeTransport()).access_token()

    def test_tenant_token(self):
        t = FakeTransport().queue("POST", "/auth/v3/tenant_access_token/internal",
                                  body={"code": 0, "tenant_access_token": "TT", "expire": 7200})
        self.assertEqual(auth.tenant_token(CFG, transport=t), "TT")


if __name__ == "__main__":
    unittest.main()
