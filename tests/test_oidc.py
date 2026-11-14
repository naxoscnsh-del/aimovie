"""全局账号（OIDC）登录：用假的身份中心走完整授权码流程。"""
import base64
import hashlib
import hmac
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import uvicorn

from test_web import factory, web_settings
from web.app import create_app
from web.oidc import OIDC, OIDCConfig, OIDCError

ISSUER = "https://www.ainewimage.com"
SECRET = "client-secret-xyz"


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def make_token(claims: dict, secret=SECRET, alg="HS256") -> str:
    h = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    p = b64(json.dumps(claims).encode())
    s = b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"


class FakeResp:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, json.dumps(data)

    def json(self):
        return self._d


class FakeIdP:
    """模拟身份中心：发现文档、令牌、用户信息。"""

    def __init__(self):
        self.codes = {}  # code -> (sub, nonce, name)
        self.token_calls = 0

    def get(self, url, headers=None, timeout=None):
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResp(200, {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                                  "token_endpoint": f"{ISSUER}/oauth/token",
                                  "userinfo_endpoint": f"{ISSUER}/oauth/userinfo",
                                  "end_session_endpoint": f"{ISSUER}/oauth/logout"})
        if url.endswith("/oauth/userinfo"):
            return FakeResp(200, {"preferred_username": "wangxiao", "email": "wx@ainewimage.com"})
        return FakeResp(404, {})

    def post(self, url, data=None, timeout=None, auth=None):
        self.token_calls += 1
        code = data["code"]
        if code not in self.codes or auth != ("aimovie", SECRET):
            return FakeResp(400, {"error": "invalid_grant"})
        sub, nonce, name = self.codes.pop(code)
        claims = {"iss": ISSUER, "aud": "aimovie", "sub": sub, "exp": time.time() + 300, "nonce": nonce,
                  "name": name, "employee_no": "E001"}
        return FakeResp(200, {"id_token": make_token(claims), "access_token": "at", "token_type": "Bearer"})


def cfg(**kw):
    c = OIDCConfig(issuer=ISSUER, client_id="aimovie", client_secret=SECRET,
                   public_url="https://www.ainewimage.com", session_secret="s" * 32, admins={"acc-boss"})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture()
def oidc_server(tmp_path):
    idp = FakeIdP()
    app = create_app(web_settings(), data_dir=tmp_path / "d", factory=factory, workers=1, password="",
                     base_path="/movie", oidc_config=cfg(), oidc_session=idp)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", idp, app
    srv.should_exit = True
    th.join(timeout=5)


def login(base, idp, sub, name, path="/movie/"):
    """模拟浏览器：访问 → 跳登录 → 跳身份中心（已登录，直接带 code 回来）→ 回调。"""
    s = requests.Session()
    r = s.get(base + path, allow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/movie/auth/login?next=")
    r = s.get(base + r.headers["location"], allow_redirects=False)
    auth_url = urlparse(r.headers["location"])
    assert f"{auth_url.scheme}://{auth_url.netloc}{auth_url.path}" == f"{ISSUER}/oauth/authorize"
    q = parse_qs(auth_url.query)
    assert q["redirect_uri"] == ["https://www.ainewimage.com/movie/auth/callback"]
    code = f"code-{sub}"
    idp.codes[code] = (sub, q["nonce"][0], name)
    r = s.get(f"{base}/movie/auth/callback", params={"code": code, "state": q["state"][0]}, allow_redirects=False)
    assert r.status_code == 302, r.text
    return s, r.headers["location"]


def test_full_oidc_login(oidc_server):
    base, idp, app = oidc_server
    # 未登录：API 返回 401 + 登录地址；页面跳转登录
    r = requests.get(f"{base}/movie/api/config")
    assert r.status_code == 401 and r.json()["login"] == "/movie/auth/login"
    assert requests.get(f"{base}/healthz").status_code == 200
    s, loc = login(base, idp, "acc-001", "王小")
    assert loc == "/movie/"
    cfg_ = s.get(f"{base}/movie/api/config").json()
    assert cfg_["user"] == "王小" and cfg_["role"] == "member" and cfg_["auth"] == "oidc"
    assert cfg_["logout"] == "/movie/auth/logout"
    # 建项目：owner 记为全局账号姓名
    pid = s.post(f"{base}/movie/api/projects", data={"description": "全局账号建的项目"}).json()["id"]
    assert s.get(f"{base}/movie/api/projects/{pid}").json()["owner"] == "王小"
    # 管理员（OIDC_ADMINS 里的 sub）
    boss, _ = login(base, idp, "acc-boss", "老板")
    assert boss.get(f"{base}/movie/api/config").json()["role"] == "admin"
    users = boss.get(f"{base}/movie/api/sso-users").json()
    assert {u["name"] for u in users} == {"王小", "老板"}
    assert next(u for u in users if u["name"] == "老板")["fixed_admin"]
    # 管理员把王小设为管理员，立即生效（不用重新登录）
    assert s.get(f"{base}/movie/api/sso-users").status_code == 403
    assert boss.patch(f"{base}/movie/api/sso-users/acc-001", json={"role": "admin"}).status_code == 200
    assert s.get(f"{base}/movie/api/config").json()["role"] == "admin"
    # 登出：清掉会话并跳到身份中心登出
    r = s.get(f"{base}/movie/auth/logout", allow_redirects=False)
    assert r.headers["location"].startswith(f"{ISSUER}/oauth/logout")
    s.cookies.clear()
    assert s.get(f"{base}/movie/api/config").status_code == 401


def test_deep_link_and_bad_state(oidc_server):
    base, idp, _ = oidc_server
    s, loc = login(base, idp, "acc-002", "李四", path="/movie/static/../")  # 普通页面
    assert loc.startswith("/movie/")
    # 伪造的 state：不会登录，重新走登录
    s2 = requests.Session()
    r = s2.get(f"{base}/movie/auth/callback", params={"code": "x", "state": "forged"}, allow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/movie/auth/login"
    assert s2.get(f"{base}/movie/api/config").status_code == 401
    # 开放重定向防护：next 只能是站内相对路径
    r = s2.get(f"{base}/movie/auth/login", params={"next": "//evil.com"}, allow_redirects=False)
    assert r.status_code == 302


def test_id_token_checks():
    o = OIDC(cfg(), "/movie", session=FakeIdP())
    good = {"iss": ISSUER, "aud": "aimovie", "sub": "a", "exp": time.time() + 60, "nonce": "n"}
    assert o.verify_id_token(make_token(good), "n")["sub"] == "a"
    with pytest.raises(OIDCError, match="签名"):
        o.verify_id_token(make_token(good, secret="wrong"), "n")
    with pytest.raises(OIDCError, match="issuer"):
        o.verify_id_token(make_token({**good, "iss": "https://evil"}), "n")
    with pytest.raises(OIDCError, match="本应用"):
        o.verify_id_token(make_token({**good, "aud": "pms"}), "n")
    with pytest.raises(OIDCError, match="过期"):
        o.verify_id_token(make_token({**good, "exp": time.time() - 3600}), "n")
    with pytest.raises(OIDCError, match="nonce"):
        o.verify_id_token(make_token(good), "other")
    with pytest.raises(OIDCError, match="算法"):
        o.verify_id_token(make_token(good, alg="none"), "n")
    # 会话 cookie 篡改无效
    cookie, user = o.make_session({"sub": "a", "name": "张三"}, {})
    assert o.read_session(cookie)["name"] == "张三"
    body, sig = cookie.rsplit(".", 1)
    assert o.read_session(body + "." + sig[::-1]) is None
