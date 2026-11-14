"""门户 SSO：用 ni_portal Cookie + 假的 HRBP /api/portal/session。"""
import base64
import json

from starlette.testclient import TestClient

from test_web import factory, web_settings
from web.app import create_app
from web.portal import PortalConfig


def cookie(uid="u1"):
    p = base64.urlsafe_b64encode(json.dumps({"uid": uid, "exp": 9999999999999}).encode()).rstrip(b"=").decode()
    return f"{p}.sig"


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d = code, data or {}

    def json(self):
        return self._d


class FakeHRBP:
    def __init__(self):
        self.calls = 0
        self.reply = Resp(200, {"ok": True, "name": "小林", "pending": False})

    def get(self, url, headers=None, timeout=0):
        self.calls += 1
        assert url == "https://hrbp.test/api/portal/session" and headers["Cookie"].startswith("ni_portal=")
        return self.reply


def make(tmp_path, admins=()):
    hr = FakeHRBP()
    cfg = PortalConfig("https://hrbp.test", "https://portal.test", set(admins), 60)
    app = create_app(web_settings(), data_dir=tmp_path, factory=factory, workers=1, password="",
                     base_path="/movie", portal_config=cfg, oidc_session=hr)
    return TestClient(app, follow_redirects=False), hr


def test_portal_login_flow(tmp_path):
    c, hr = make(tmp_path)
    r = c.get("/movie/")
    assert r.status_code == 302 and r.headers["location"].startswith("/movie/auth/login")
    r = c.get("/movie/auth/login")
    assert r.status_code == 302 and r.headers["location"] == "https://portal.test/login"
    assert c.get("/movie/api/projects").status_code == 401
    c.cookies.set("ni_portal", cookie("u1"))
    assert c.get("/movie/api/projects").status_code == 200
    cfg = c.get("/movie/api/config").json()
    assert cfg["user"] == "小林" and cfg["role"] == "member" and cfg["auth"] == "oidc" and cfg["logout"] is None
    c.get("/movie/api/projects"); assert hr.calls == 1  # 缓存


def test_portal_rejects_and_admin(tmp_path):
    c, hr = make(tmp_path, admins={"u1"})
    hr.reply = Resp(401)
    c.cookies.set("ni_portal", cookie("u1"))
    assert c.get("/movie/api/projects").status_code == 401
    hr.reply = Resp(200, {"ok": True, "name": "小林", "pending": True})
    assert c.get("/movie/api/projects").status_code == 401
    hr.reply = Resp(200, {"ok": True, "name": "小林", "pending": False})
    assert c.get("/movie/api/config").json()["role"] == "admin"


def test_portal_upstream_down(tmp_path):
    c, hr = make(tmp_path)
    hr.reply = Resp(500)
    c.cookies.set("ni_portal", cookie("u2"))
    assert c.get("/movie/api/projects").status_code == 503
    assert c.get("/movie/healthz").status_code == 200
