"""全局账号登录：OIDC 授权码流程，对接 www.ainewimage.com 身份中心（与 PMS 相同）。

- 身份中心的 id_token 用 HS256 签名（密钥 = client secret），按此校验
- 登录成功后发一个签名 cookie（HMAC-SHA256，SESSION_SECRET），默认 12 小时
- 用户已在门户登录时，跳转到身份中心会立即带 code 回来，无需再输密码
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests

SESSION_COOKIE = "aimovie_session"
STATE_COOKIE = "aimovie_oidc"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class OIDCError(RuntimeError):
    pass


@dataclass
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    public_url: str              # 浏览器访问的站点根，如 https://www.ainewimage.com
    session_secret: str
    admins: set[str] = field(default_factory=set)   # sub 或用户名
    scope: str = "openid profile email"
    session_hours: float = 12

    @classmethod
    def from_env(cls) -> "OIDCConfig | None":
        if (os.environ.get("AUTH_MODE") or "").strip().lower() != "oidc":
            return None
        missing = [k for k in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "PUBLIC_URL", "SESSION_SECRET")
                   if not (os.environ.get(k) or "").strip()]
        if missing:
            raise OIDCError("AUTH_MODE=oidc 但缺少：" + "、".join(missing))
        return cls(
            issuer=os.environ["OIDC_ISSUER"].strip().rstrip("/"),
            client_id=os.environ["OIDC_CLIENT_ID"].strip(),
            client_secret=os.environ["OIDC_CLIENT_SECRET"].strip(),
            public_url=os.environ["PUBLIC_URL"].strip().rstrip("/"),
            session_secret=os.environ["SESSION_SECRET"].strip(),
            admins={x.strip() for x in (os.environ.get("OIDC_ADMINS") or "").split(",") if x.strip()},
            scope=(os.environ.get("OIDC_SCOPE") or "openid profile email").strip(),
            session_hours=float(os.environ.get("SESSION_HOURS") or 12),
        )


class OIDC:
    def __init__(self, cfg: OIDCConfig, base: str, session=None):
        self.cfg = cfg
        self.base = base
        self.http = session or requests.Session()
        self._discovery: dict | None = None
        self._discovered_at = 0.0

    # ---------- 身份中心 ----------
    def discovery(self) -> dict:
        if self._discovery is None or time.time() - self._discovered_at > 3600:
            r = self.http.get(f"{self.cfg.issuer}/.well-known/openid-configuration", timeout=10)
            if r.status_code != 200:
                raise OIDCError(f"身份中心发现文档获取失败：HTTP {r.status_code}")
            self._discovery = r.json()
            self._discovered_at = time.time()
        return self._discovery

    @property
    def redirect_uri(self) -> str:
        return f"{self.cfg.public_url}{self.base}/auth/callback"

    def authorize_url(self, state: str, nonce: str) -> str:
        q = {"response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.redirect_uri,
             "scope": self.cfg.scope, "state": state, "nonce": nonce}
        return f"{self.discovery()['authorization_endpoint']}?{urlencode(q)}"

    def exchange(self, code: str) -> dict:
        d = self.discovery()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                "client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret}
        r = self.http.post(d["token_endpoint"], data=data, timeout=15,
                           auth=(self.cfg.client_id, self.cfg.client_secret))
        if r.status_code != 200:
            raise OIDCError(f"换取令牌失败：HTTP {r.status_code} {r.text[:200]}")
        tok = r.json()
        if "id_token" not in tok:
            raise OIDCError("身份中心没有返回 id_token")
        return tok

    def verify_id_token(self, token: str, nonce: str) -> dict:
        try:
            h64, p64, s64 = token.split(".")
            header = json.loads(_unb64url(h64))
            claims = json.loads(_unb64url(p64))
        except (ValueError, json.JSONDecodeError):
            raise OIDCError("id_token 格式不正确") from None
        if header.get("alg") != "HS256":
            raise OIDCError(f"不支持的签名算法：{header.get('alg')}（身份中心应为 HS256）")
        expect = hmac.new(self.cfg.client_secret.encode(), f"{h64}.{p64}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _unb64url(s64)):
            raise OIDCError("id_token 签名校验失败（检查 OIDC_CLIENT_SECRET）")
        if claims.get("iss", "").rstrip("/") != self.cfg.issuer:
            raise OIDCError(f"id_token issuer 不符：{claims.get('iss')}")
        aud = claims.get("aud")
        if self.cfg.client_id not in (aud if isinstance(aud, list) else [aud]):
            raise OIDCError("id_token 不是发给本应用的")
        if claims.get("exp", 0) < time.time() - 60:
            raise OIDCError("id_token 已过期")
        if nonce and claims.get("nonce") and claims["nonce"] != nonce:
            raise OIDCError("nonce 不符")
        return claims

    def userinfo(self, access_token: str) -> dict:
        ep = self.discovery().get("userinfo_endpoint")
        if not ep or not access_token:
            return {}
        try:
            r = self.http.get(ep, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            return r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            return {}

    def logout_url(self) -> str:
        try:
            ep = self.discovery().get("end_session_endpoint")
        except OIDCError:
            ep = None
        back = f"{self.cfg.public_url}{self.base}/"
        return f"{ep}?{urlencode({'post_logout_redirect_uri': back, 'client_id': self.cfg.client_id})}" if ep \
            else f"{self.cfg.public_url}/"

    # ---------- 会话 ----------
    def _sign(self, payload: dict) -> str:
        body = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
        sig = _b64url(hmac.new(self.cfg.session_secret.encode(), body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _unsign(self, value: str) -> dict | None:
        try:
            body, sig = value.rsplit(".", 1)
        except ValueError:
            return None
        expect = _b64url(hmac.new(self.cfg.session_secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expect, sig):
            return None
        try:
            data = json.loads(_unb64url(body))
        except (ValueError, json.JSONDecodeError):
            return None
        return data if data.get("exp", 0) > time.time() else None

    def new_state(self, next_path: str) -> tuple[str, str, str]:
        state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        cookie = self._sign({"state": state, "nonce": nonce, "next": next_path, "exp": time.time() + 600})
        return state, nonce, cookie

    def read_state(self, cookie: str | None) -> dict | None:
        return self._unsign(cookie) if cookie else None

    def make_session(self, claims: dict, info: dict) -> tuple[str, dict]:
        merged = {**info, **claims}
        name = (merged.get("name") or merged.get("nickname") or merged.get("preferred_username")
                or merged.get("email") or merged["sub"])
        user = {"sub": merged["sub"], "name": str(name)[:40], "email": merged.get("email") or "",
                "employee_no": merged.get("employee_no") or ""}
        role = "admin" if (user["sub"] in self.cfg.admins or user["name"] in self.cfg.admins
                           or (user["email"] and user["email"] in self.cfg.admins)) else "member"
        user["role"] = role
        return self._sign({**user, "exp": time.time() + self.cfg.session_hours * 3600}), user

    def read_session(self, cookie: str | None) -> dict | None:
        return self._unsign(cookie) if cookie else None
