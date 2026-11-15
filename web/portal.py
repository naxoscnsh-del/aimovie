"""门户 SSO：直接用门户登录后写在 .ainewimage.com 的 ni_portal Cookie，不再单独登录。

- 浏览器访问 www.ainewimage.com/movie/ 时会带上 ni_portal（域 .ainewimage.com）
- 应用把该 Cookie 原样转发给 HRBP `GET {HRBP_BASE_URL}/api/portal/session`，由 HRBP 验签、查账号是否启用
- 通过后账号 ID = Cookie 载荷里的 uid（HRBP User.id，与 PMS 的 sub 一致），姓名取自 HRBP 返回
- 结果缓存 PORTAL_CACHE_SECONDS 秒（默认 60），避免每个请求都打到 HRBP
- 未登录：跳到门户登录页 PORTAL_URL/login
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field

import logging

import requests

log = logging.getLogger("aimovie.portal")

PORTAL_COOKIE = "ni_portal"


class PortalError(RuntimeError):
    pass


class PortalDenied(PortalError):
    """门户登录有效，但没有开通本系统。"""


SYSTEM_KEY = "site-movie"


@dataclass
class PortalConfig:
    hrbp_base_url: str
    portal_url: str
    admins: set[str] = field(default_factory=set)   # account_id 或姓名
    cache_seconds: float = 60

    @classmethod
    def from_env(cls) -> "PortalConfig | None":
        if (os.environ.get("AUTH_MODE") or "").strip().lower() != "portal":
            return None
        return cls(
            hrbp_base_url=(os.environ.get("HRBP_BASE_URL") or "https://hrbp.ainewimage.com").strip().rstrip("/"),
            portal_url=(os.environ.get("PORTAL_URL") or "https://www.ainewimage.com").strip().rstrip("/"),
            admins={x.strip() for x in (os.environ.get("PORTAL_ADMINS") or os.environ.get("OIDC_ADMINS") or "").split(",") if x.strip()},
            cache_seconds=float(os.environ.get("PORTAL_CACHE_SECONDS") or 60),
        )


def _uid_of(cookie: str) -> str:
    try:
        payload = cookie.split(".")[0]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("uid") or "")
    except (ValueError, IndexError):
        return ""


class PortalAuth:
    """与 OIDC 类同接口（cfg.admins / identify），app.py 里按鸭子类型使用。"""
    is_portal = True

    def __init__(self, cfg: PortalConfig, base: str = "", session=None):
        self.cfg, self.base = cfg, base
        self.http = session or requests.Session()
        self._cache: dict[str, tuple[float, dict]] = {}

    @property
    def login_url(self) -> str:
        return f"{self.cfg.portal_url}/login"

    def diagnose(self, cookies) -> dict:
        """只返回状态，不返回 Cookie 内容：给 /auth/debug 排障用。"""
        token = cookies.get(PORTAL_COOKIE)
        out = {"has_cookie": bool(token), "cookie_names": sorted(cookies.keys()), "uid_in_cookie": bool(token and _uid_of(token)),
               "hrbp": self.cfg.hrbp_base_url}
        if token:
            try:
                r = self.http.get(f"{self.cfg.hrbp_base_url}/api/portal/session",
                                  headers={"Cookie": f"{PORTAL_COOKIE}={token}"}, timeout=8)
                out["hrbp_status"] = r.status_code
                try:
                    d = r.json()
                    out["name"], out["has_movie"] = d.get("name"), any(isinstance(x, dict) and x.get("key") == SYSTEM_KEY for x in d.get("systems") or [])
                except ValueError:
                    out["hrbp_body"] = r.text[:120]
            except requests.RequestException as e:
                out["hrbp_error"] = str(e)[:200]
        return out

    def identify(self, cookies) -> dict | None:
        token = cookies.get(PORTAL_COOKIE)
        if not token:
            log.warning("未登录：请求里没有 %s Cookie（已有：%s）", PORTAL_COOKIE, sorted(cookies.keys()))
            return None
        hit = self._cache.get(token)
        if hit and hit[0] > time.time():
            return hit[1]
        uid = _uid_of(token)  # 可能为空：生产 Cookie 载荷里没有 uid
        try:
            r = self.http.get(f"{self.cfg.hrbp_base_url}/api/portal/session",
                              headers={"Cookie": f"{PORTAL_COOKIE}={token}"}, timeout=8)
        except requests.RequestException as e:
            raise PortalError(f"无法连接 HRBP：{e}") from None
        if r.status_code == 401:
            log.warning("HRBP 会话接口返回 401（Cookie 无效或已过期）")
            return None
        if r.status_code != 200:
            raise PortalError(f"HRBP 返回 HTTP {r.status_code}")
        data = r.json()
        # 生产的会话接口形如 {name, email, systems:[{key,...}]}，没有 ok / 账号 ID；有 ok 或 pending 字段时照旧判断
        if data.get("ok") is False or data.get("pending") or not data.get("name"):
            log.warning("HRBP 会话不可用：ok=%s pending=%s name=%s", data.get("ok"), data.get("pending"), bool(data.get("name")))
            return None
        systems = data.get("systems")
        if isinstance(systems, list) and not any(isinstance(x, dict) and x.get("key") == SYSTEM_KEY for x in systems):
            raise PortalDenied("你的账号还没有开通「AI视频」，请联系管理员在用户权限里开通")
        sub = str(data.get("id") or data.get("userId") or data.get("uid") or uid or data.get("email") or data["name"])
        user = {"sub": sub, "name": str(data["name"])[:40], "email": str(data.get("email") or "")}
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[token] = (time.time() + self.cfg.cache_seconds, user)
        return user
