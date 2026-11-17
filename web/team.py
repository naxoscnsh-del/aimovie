"""团队数据：成员、风格模板、共享素材库（SQLite，与任务队列同库）。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

NAME_RE = re.compile(r"^[\w一-鿿.-]{1,32}$")
ROLES = {"admin", "member"}
ASSET_KINDS = ["角色", "产品", "场景", "风格"]


class TeamError(ValueError):
    pass


def hash_password(pw: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


class TeamStore:
    def __init__(self, db_path: Path, library_dir: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.library = library_dir
        self.library.mkdir(parents=True, exist_ok=True)
        self._auth_cache: dict[str, tuple[str, float]] = {}
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                name TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, role TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT '', ratio TEXT NOT NULL DEFAULT '9:16', duration INTEGER NOT NULL DEFAULT 30,
                characters TEXT NOT NULL DEFAULT '[]', asset_ids TEXT NOT NULL DEFAULT '[]',
                owner TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sso_users (
                sub TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member', first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
                builtin INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, prompt_id INTEGER NOT NULL, content TEXT NOT NULL,
                by TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
                file TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL);
            """)
            if "prompt_id" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(templates)")}:
                self.conn.execute("ALTER TABLE templates ADD COLUMN prompt_id INTEGER")  # 风格组合绑定的写作提示词
            self.conn.commit()

    # ---------- 成员 ----------
    def has_users(self) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def list_users(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT name, role, disabled, created FROM users ORDER BY created").fetchall()
        return [{**dict(r), "disabled": bool(r["disabled"])} for r in rows]

    def get_user(self, name: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT name, role, disabled FROM users WHERE name=?", (name,)).fetchone()
        return {**dict(r), "disabled": bool(r["disabled"])} if r else None

    def add_user(self, name: str, password: str, role: str = "member") -> dict:
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise TeamError("用户名 1–32 个字，可用中文、字母、数字、点、横线、下划线")
        if len(password or "") < 8:
            raise TeamError("密码至少 8 位")
        if role not in ROLES:
            raise TeamError("角色只能是 admin 或 member")
        with self.lock:
            if self.conn.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
                raise TeamError("该用户名已存在")
            self.conn.execute("INSERT INTO users(name, pw_hash, role, created) VALUES (?,?,?,?)",
                              (name, hash_password(password), role, time.time()))
            self.conn.commit()
        return self.get_user(name)

    def update_user(self, name: str, password: str | None = None, role: str | None = None,
                    disabled: bool | None = None) -> dict:
        user = self.get_user(name)
        if not user:
            raise TeamError("用户不存在")
        if role is not None and role not in ROLES:
            raise TeamError("角色只能是 admin 或 member")
        if (role == "member" or disabled) and user["role"] == "admin" and not user["disabled"]:
            admins = [u for u in self.list_users() if u["role"] == "admin" and not u["disabled"]]
            if len(admins) <= 1:
                raise TeamError("至少要保留一名可用的管理员")
        with self.lock:
            if password is not None:
                if len(password) < 8:
                    raise TeamError("密码至少 8 位")
                self.conn.execute("UPDATE users SET pw_hash=? WHERE name=?", (hash_password(password), name))
            if role is not None:
                self.conn.execute("UPDATE users SET role=? WHERE name=?", (role, name))
            if disabled is not None:
                self.conn.execute("UPDATE users SET disabled=? WHERE name=?", (int(disabled), name))
            self.conn.commit()
            self._auth_cache.clear()
        return self.get_user(name)

    def authenticate(self, name: str, password: str) -> str | None:
        """校验成功返回角色。成功结果缓存 10 分钟，避免每个图片请求都做一次慢哈希。"""
        key = hashlib.sha256(f"{name}\0{password}".encode("utf-8")).hexdigest()
        hit = self._auth_cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        with self.lock:
            r = self.conn.execute("SELECT pw_hash, role, disabled FROM users WHERE name=?", (name,)).fetchone()
        if not r or r["disabled"] or not verify_password(password, r["pw_hash"]):
            return None
        self._auth_cache[key] = (r["role"], time.time() + 600)
        return r["role"]

    def ensure_admin(self, name: str, password: str) -> None:
        """启动时用 ADMIN_USER / ADMIN_PASSWORD 创建首个管理员（已存在则跳过）。"""
        if name and password and not self.get_user(name):
            self.add_user(name, password, "admin")

    # ---------- 全局账号（OIDC）成员 ----------
    def sso_touch(self, sub: str, name: str, email: str = "") -> str:
        """登录时记录成员，返回库里保存的角色。"""
        now = time.time()
        with self.lock:
            # 账号标识从邮箱换成稳定 ID（HRBP 会话接口开始返回 id）时，把原来按邮箱记的成员和角色并过来
            if email and sub != email and self.conn.execute("SELECT 1 FROM sso_users WHERE sub=?", (email,)).fetchone() \
                    and not self.conn.execute("SELECT 1 FROM sso_users WHERE sub=?", (sub,)).fetchone():
                self.conn.execute("UPDATE sso_users SET sub=? WHERE sub=?", (sub, email))
            self.conn.execute(
                "INSERT INTO sso_users(sub, name, email, first_seen, last_seen) VALUES (?,?,?,?,?) "
                "ON CONFLICT(sub) DO UPDATE SET name=excluded.name, email=excluded.email, last_seen=excluded.last_seen",
                (sub, name, email, now, now))
            self.conn.commit()
            r = self.conn.execute("SELECT role FROM sso_users WHERE sub=?", (sub,)).fetchone()
        return r["role"] if r else "member"

    def sso_role(self, sub: str) -> str:
        with self.lock:
            r = self.conn.execute("SELECT role FROM sso_users WHERE sub=?", (sub,)).fetchone()
        return r["role"] if r else "member"

    def sso_list(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT sub, name, email, role, first_seen, last_seen FROM sso_users ORDER BY first_seen").fetchall()]

    def sso_set_role(self, sub: str, role: str) -> None:
        if role not in ROLES:
            raise TeamError("角色只能是 admin 或 member")
        with self.lock:
            cur = self.conn.execute("UPDATE sso_users SET role=? WHERE sub=?", (role, sub))
            self.conn.commit()
        if not cur.rowcount:
            raise TeamError("该成员还没登录过")

    # ---------- 模板 ----------
    def _tpl(self, r) -> dict:
        d = dict(r)
        d["characters"] = json.loads(d["characters"])
        d["asset_ids"] = json.loads(d["asset_ids"])
        return d

    def list_templates(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM templates ORDER BY id DESC").fetchall()
        return [self._tpl(r) for r in rows]

    def get_template(self, tid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        return self._tpl(r) if r else None

    def add_template(self, data: dict, owner: str) -> dict:
        name = str(data.get("name", "")).strip()
        if not 1 <= len(name) <= 40:
            raise TeamError("模板名称 1–40 个字")
        chars = data.get("characters") or []
        if not isinstance(chars, list) or not all(isinstance(c, dict) and c.get("name") and c.get("appearance")
                                                   for c in chars):
            raise TeamError("角色需要名字和外观描述")
        asset_ids = [int(x) for x in (data.get("asset_ids") or [])][:4]
        for aid in asset_ids:
            if not self.get_asset(aid):
                raise TeamError(f"素材 {aid} 不存在")
        try:
            duration = int(data.get("duration", 30))
        except (TypeError, ValueError):
            raise TeamError("时长必须是数字")
        prompt_id = None
        if data.get("prompt_id"):
            row = self.get_prompt(int(data["prompt_id"]))
            prompt_id = row["id"] if row else None
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO templates(name, description, style, ratio, duration, characters, asset_ids, owner, created, prompt_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name, str(data.get("description", "")).strip()[:500], str(data.get("style", "")).strip()[:300],
                 str(data.get("ratio", "9:16")), duration,
                 json.dumps([{"name": c["name"][:20], "appearance": c["appearance"][:300],
                              **({"voice": c["voice"]} if re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", str(c.get("voice") or ""))
                                 else {})} for c in chars], ensure_ascii=False),
                 json.dumps(asset_ids), owner, time.time(), prompt_id))
            self.conn.commit()
        return self.get_template(cur.lastrowid)

    def delete_template(self, tid: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM templates WHERE id=?", (tid,))
            self.conn.commit()

    # ---------- 素材 ----------
    def list_assets(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM assets ORDER BY id DESC").fetchall()]

    def get_asset(self, aid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def add_asset(self, name: str, kind: str, data: bytes, ext: str, owner: str, source: str = "") -> dict:
        name = (name or "").strip()
        if not 1 <= len(name) <= 40:
            raise TeamError("素材名称 1–40 个字")
        if kind not in ASSET_KINDS:
            raise TeamError(f"类型只能是：{'、'.join(ASSET_KINDS)}")
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO assets(name, kind, file, owner, source, created) VALUES (?,?,?,?,?,?)",
                (name, kind, "", owner, source, time.time()))
            aid = cur.lastrowid
            fname = f"{aid}_{os.urandom(3).hex()}{ext}"
            (self.library / fname).write_bytes(data)
            self.conn.execute("UPDATE assets SET file=? WHERE id=?", (fname, aid))
            self.conn.commit()
        return self.get_asset(aid)

    def delete_asset(self, aid: int) -> None:
        a = self.get_asset(aid)
        if not a:
            return
        with self.lock:
            self.conn.execute("DELETE FROM assets WHERE id=?", (aid,))
            self.conn.commit()
        f = self.library / a["file"]
        if f.exists():
            f.unlink()

    # ---------- 写作提示词 ----------
    DEFAULT_PROMPT_NAME = "默认"

    def ensure_default_prompt(self) -> None:
        from videogen.script import DEFAULT_GUIDE
        with self.lock:
            if not self.conn.execute("SELECT 1 FROM prompts WHERE builtin=1").fetchone():
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO prompts(name, content, builtin, owner, updated_by, updated) VALUES (?,?,1,'系统','系统',?)",
                    (self.DEFAULT_PROMPT_NAME, DEFAULT_GUIDE, time.time()))
                if cur.rowcount:
                    self.conn.execute("INSERT INTO prompt_versions(prompt_id, content, by, at) VALUES (?,?,?,?)",
                                      (cur.lastrowid, DEFAULT_GUIDE, "系统", time.time()))
                self.conn.commit()

    def _prompt_row(self, r) -> dict:
        return {"id": r["id"], "name": r["name"], "content": r["content"], "builtin": bool(r["builtin"]),
                "owner": r["owner"], "updated_by": r["updated_by"], "updated": r["updated"]}

    def list_prompts(self) -> list[dict]:
        self.ensure_default_prompt()
        with self.lock:
            rows = self.conn.execute("SELECT * FROM prompts ORDER BY builtin DESC, id").fetchall()
        return [self._prompt_row(r) for r in rows]

    def get_prompt(self, pid: int) -> dict | None:
        self.ensure_default_prompt()
        with self.lock:
            r = self.conn.execute("SELECT * FROM prompts WHERE id=?", (pid,)).fetchone()
        return self._prompt_row(r) if r else None

    def _check_prompt(self, name: str, content: str) -> tuple[str, str]:
        from videogen.script import GUIDE_MAX
        name, content = name.strip(), content.strip()
        if not 1 <= len(name) <= 30:
            raise TeamError("名称 1–30 字")
        if not 20 <= len(content) <= GUIDE_MAX:
            raise TeamError(f"提示词内容 20–{GUIDE_MAX} 字")
        return name, content

    def _add_version(self, pid: int, content: str, by: str) -> None:
        self.conn.execute("INSERT INTO prompt_versions(prompt_id, content, by, at) VALUES (?,?,?,?)",
                          (pid, content, by, time.time()))
        self.conn.execute("DELETE FROM prompt_versions WHERE prompt_id=? AND id NOT IN "
                          "(SELECT id FROM prompt_versions WHERE prompt_id=? ORDER BY id DESC LIMIT 20)", (pid, pid))

    def add_prompt(self, name: str, content: str, user: str) -> dict:
        self.ensure_default_prompt()
        name, content = self._check_prompt(name, content)
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO prompts(name, content, owner, updated_by, updated) VALUES (?,?,?,?,?)",
                    (name, content, user, user, time.time()))
            except sqlite3.IntegrityError:
                raise TeamError("已有同名提示词") from None
            self._add_version(cur.lastrowid, content, user)
            self.conn.commit()
        return self.get_prompt(cur.lastrowid)

    def update_prompt(self, pid: int, name: str | None, content: str, user: str) -> dict:
        cur_row = self.get_prompt(pid)
        if not cur_row:
            raise TeamError("提示词不存在")
        name, content = self._check_prompt(name if name is not None else cur_row["name"], content)
        if cur_row["builtin"]:
            name = cur_row["name"]  # 默认模板不改名
        with self.lock:
            try:
                self.conn.execute("UPDATE prompts SET name=?, content=?, updated_by=?, updated=? WHERE id=?",
                                  (name, content, user, time.time(), pid))
            except sqlite3.IntegrityError:
                raise TeamError("已有同名提示词") from None
            if content != cur_row["content"]:
                self._add_version(pid, content, user)
            self.conn.commit()
        return self.get_prompt(pid)

    def delete_prompt(self, pid: int) -> None:
        row = self.get_prompt(pid)
        if not row:
            raise TeamError("提示词不存在")
        if row["builtin"]:
            raise TeamError("默认提示词不能删除，可以恢复为系统默认内容")
        with self.lock:
            self.conn.execute("DELETE FROM prompts WHERE id=?", (pid,))
            self.conn.execute("DELETE FROM prompt_versions WHERE prompt_id=?", (pid,))
            self.conn.commit()

    def prompt_versions(self, pid: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, content, by, at FROM prompt_versions WHERE prompt_id=? ORDER BY id DESC",
                                     (pid,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 页面设置（覆盖 .env）----------
    def settings_all(self) -> dict[str, dict]:
        with self.lock:
            rows = self.conn.execute("SELECT key, value, updated_by, updated FROM app_settings").fetchall()
        return {r["key"]: {"value": json.loads(r["value"]), "by": r["updated_by"], "at": r["updated"]} for r in rows}

    def settings_save(self, values: dict, reset: list[str], user: str) -> None:
        now = time.time()
        with self.lock:
            for k, v in values.items():
                self.conn.execute(
                    "INSERT INTO app_settings(key, value, updated_by, updated) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated=excluded.updated",
                    (k, json.dumps(v, ensure_ascii=False), user, now))
            for k in reset:
                self.conn.execute("DELETE FROM app_settings WHERE key=?", (k,))
            self.conn.commit()
