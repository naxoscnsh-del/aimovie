"""HTTP API 与静态页面（Starlette）。

鉴权：设置 APP_PASSWORD 后启用 HTTP Basic，用户名记为操作人，密码为团队共用口令。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from videogen import cost as costmod
from videogen.config import Settings
from videogen.models import RATIO_SIZES
from videogen.pipeline import Pipeline, Project, StageError
from videogen.script import ScriptError

from .jobs import JobStore, ProjectBusy, Runner, default_factory
from .team import ASSET_KINDS, TeamError, TeamStore

STATIC = Path(__file__).parent / "static"
PID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav"}
MAX_UPLOAD = 20 * 1024 * 1024


def err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_cross_site(request: Request) -> bool:
    """CSRF 防护：浏览器会自动附带已缓存的 Basic 凭据，所以要拒绝来自其他网站的写请求。
    浏览器跨站请求一定带 Origin / Sec-Fetch-Site；命令行工具不带这两个头，放行。"""
    site = request.headers.get("sec-fetch-site")
    if site in ("cross-site", "same-site"):
        return True
    origin = request.headers.get("origin")
    if origin and origin != "null":
        from urllib.parse import urlsplit
        netloc = urlsplit(origin).netloc
        extra = {h.strip() for h in os.environ.get("ALLOWED_ORIGIN_HOSTS", "").split(",") if h.strip()}
        return netloc != request.headers.get("host", "") and netloc not in extra
    return origin == "null"


IMAGE_MAGIC = [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"]


def looks_like_image(data: bytes) -> bool:
    return any(data.startswith(m) for m in IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


class Auth(BaseHTTPMiddleware):
    """三种模式：
    1. 已建成员账号 → 用户名 + 各自密码（HTTP Basic）
    2. 未建成员但设置了 APP_PASSWORD → 任意用户名 + 共用口令，均视为管理员（兼容 P1）
    3. 都没设置 → 不登录（本地开发）
    """

    def __init__(self, app, team, password: str):
        super().__init__(app)
        self.team = team
        self.password = password

    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS and is_cross_site(request):
            return JSONResponse({"error": "拒绝跨站请求"}, status_code=403)
        resp = await self._dispatch(request, call_next)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    async def _dispatch(self, request: Request, call_next):
        has_users = await run_in_threadpool(self.team.has_users)
        if not has_users and not self.password:
            request.state.user, request.state.role = "", "admin"
            return await call_next(request)
        user = role = None
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                name, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                name = pw = ""
            if name:
                if has_users:
                    role = await run_in_threadpool(self.team.authenticate, name, pw)
                elif secrets.compare_digest(pw.encode(), self.password.encode()):
                    role = "admin"
                user = name if role else None
        if not user:
            return Response("需要登录", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="ai-video", charset="UTF-8"'})
        request.state.user, request.state.role = user, role
        return await call_next(request)


def create_app(settings: Settings | None = None, data_dir: Path | None = None,
               factory=default_factory, workers: int | None = None,
               password: str | None = None, team_budget_usd: float | None = None) -> Starlette:
    s = settings or Settings.from_env()
    data = Path(data_dir or os.environ.get("DATA_DIR", "data"))
    runs = data / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    store = JobStore(data / "app.db")
    runner = Runner(store, s, runs, factory=factory,
                    workers=workers if workers is not None else int(os.environ.get("WORKERS", 2)))
    password = password if password is not None else os.environ.get("APP_PASSWORD", "")
    team = TeamStore(data / "app.db", data / "library")
    team_budget = float(os.environ.get("TEAM_MONTHLY_BUDGET_USD", 0) or 0) if team_budget_usd is None else team_budget_usd

    # ---------- 工具 ----------
    def user_of(req: Request) -> str:
        return getattr(req.state, "user", "") or ""

    def is_admin(req: Request) -> bool:
        return getattr(req.state, "role", "") == "admin"

    def can_edit(req: Request, p: Project) -> bool:
        owner = p.state.get("owner", "")
        return is_admin(req) or not owner or owner == user_of(req)

    def ensure_edit(req: Request, p: Project) -> None:
        if not can_edit(req, p):
            raise PermissionError(f"这是 {p.state.get('owner')} 的项目，只有本人或管理员可以修改")

    def iter_states():
        for d in runs.iterdir():
            if PID_RE.match(d.name) and (d / "state.json").exists():
                try:
                    yield json.loads((d / "state.json").read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue

    def month_spend_usd() -> float:
        month = time.strftime("%Y-%m")
        return s.pricing.cny_to_usd(sum(e["cost_cny"] for st in iter_states() for e in st["ledger"]
                                        if (e.get("at") or "").startswith(month)))

    def ensure_team_budget() -> None:
        if team_budget > 0:
            spent = month_spend_usd()
            if spent >= team_budget:
                raise ProjectBusy(f"本月团队预算 ${team_budget:.0f} 已用完（已花 ${spent:.2f}），请联系管理员")

    def load(pid: str) -> Project:
        if not PID_RE.match(pid) or not (runs / pid / "state.json").exists():
            raise LookupError
        return Project(runs / pid)

    def furl(p: Project, rel: str | None) -> str | None:
        return f"/files/{p.state['id']}/{rel}" if rel else None

    def ensure_idle(pid: str) -> None:
        if store.active(pid):
            raise ProjectBusy("该项目有任务正在执行，完成后再修改")

    def view(p: Project, full: bool = True, req: Request | None = None) -> dict:
        st = p.state
        ledger = costmod.Ledger(s, st["ledger"])
        out = {
            "id": st["id"], "title": st.get("title") or st["brief"]["description"][:30],
            "status": st["status"], "created_at": st["created_at"], "owner": st.get("owner", ""),
            "ratio": st["brief"]["ratio"], "cost": ledger.summary(),
            "final": furl(p, st.get("final")),
            "job": store.active(st["id"]),
            "can_edit": can_edit(req, p) if req is not None else True,
        }
        if not full:
            out["shots"] = len(st["shots"])
            return out
        out["brief"] = {**st["brief"], "refs": [furl(p, r) for r in st["brief"]["refs"]]}
        out["has_bgm"] = bool(st.get("bgm"))
        out["final_meta"] = st.get("final_meta")
        out["exports"] = {r: furl(p, f) for r, f in (st.get("exports") or {}).items()}
        last = store.latest(st["id"])
        out["last_job"] = last
        out["script"] = None
        out["shots"] = []
        if p.has_script:
            sc = p.script
            out["script"] = {k: sc[k] for k in ("title", "style", "characters")}
            for shot in sc["shots"]:
                ss = p.shot_state(shot["id"])
                out["shots"].append({**shot, "state": {
                    "status": ss["status"], "error": ss["error"], "image_stale": ss.get("image_stale", False),
                    "candidates": [furl(p, c) for c in ss["candidates"]], "selected": ss["selected"],
                    "refined": [(ss.get("refined") or {}).get(c, {}).get("instruction") for c in ss["candidates"]],
                    "video": furl(p, ss["video"]), "tasks": len(ss["tasks"])}})
            est = costmod.estimate(s, len(sc["shots"]), sum(x["duration"] for x in sc["shots"]))
            out["estimate"] = {"total_usd": round(est.total_usd, 2), "priced": est.priced,
                               "images_cny": round(est.images_cny, 2), "videos_cny": round(est.videos_cny, 2)}
        return out

    async def save_upload(upload, dest_dir: Path, allowed: set[str], stem: str) -> Path:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in allowed:
            raise ValueError(f"不支持的文件类型 {ext or '(无扩展名)'}")
        data_ = await upload.read()
        if len(data_) > MAX_UPLOAD:
            raise ValueError("文件超过 20MB")
        if allowed is IMAGE_EXT and not looks_like_image(data_):
            raise ValueError("文件内容不是 PNG / JPEG / WebP 图片")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{stem}{ext}"
        dest.write_bytes(data_)
        return dest

    # ---------- 路由 ----------
    async def index(_req):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def config(req):
        return JSONResponse({
            "user": user_of(req), "role": getattr(req.state, "role", ""), "accounts": team.has_users(),
            "asset_kinds": ASSET_KINDS, "team_budget_usd": team_budget, "budget_usd": s.budget_usd, "budget_warn_usd": s.budget_warn_usd,
            "ratios": list(RATIO_SIZES), "durations": s.seedance_durations,
            "candidates_per_shot": s.candidates_per_shot, "priced": s.pricing.configured,
            "keys": {"deepseek": bool(s.deepseek_api_key), "ark": bool(s.ark_api_key)},
            "tts": {"enabled": s.tts_enabled or s.demo_mode, "voice": s.tts_voice},
            "demo": s.demo_mode,
        })

    async def list_projects(_req):
        items = []
        for d in sorted(runs.iterdir(), reverse=True):
            if (d / "state.json").exists() and PID_RE.match(d.name):
                try:
                    items.append(view(Project(d), full=False))
                except (json.JSONDecodeError, KeyError):
                    continue
        return JSONResponse(items)

    async def create_project(req: Request):
        form = await req.form()
        desc = str(form.get("description", "")).strip()
        if len(desc) < 4:
            return err("请填写场景描述")
        try:
            duration = int(form.get("duration", 30))
        except ValueError:
            return err("时长必须是数字")
        if not 5 <= duration <= 180:
            return err("时长需在 5–180 秒之间")
        ratio = str(form.get("ratio", "9:16"))
        if ratio not in RATIO_SIZES:
            return err("不支持的画幅")
        refs = [f for f in form.getlist("refs") if getattr(f, "filename", None)]
        try:
            asset_ids = [int(x) for x in str(form.get("asset_ids", "")).split(",") if x.strip()]
        except ValueError:
            return err("素材 id 不正确")
        tpl = None
        if form.get("template_id"):
            try:
                tpl = team.get_template(int(form.get("template_id")))
            except ValueError:
                tpl = None
            if not tpl:
                return err("模板不存在")
            # 模板里引用的素材若已被删除则跳过
            asset_ids = list(dict.fromkeys(asset_ids + [i for i in tpl["asset_ids"] if team.get_asset(i)]))
        assets = []
        for aid in asset_ids:
            a_ = team.get_asset(aid)
            if not a_:
                return err(f"素材 {aid} 不存在")
            assets.append(a_)
        if len(refs) + len(assets) > 4:
            return err("参考图（上传 + 素材库）最多 4 张")
        try:
            ensure_team_budget()
        except ProjectBusy as e:
            return err(str(e), 409)
        style = str(form.get("style", "")).strip() or (tpl["style"] if tpl else "")
        with tempfile.TemporaryDirectory() as tmp:
            paths = [str(team.library / a_["file"]) for a_ in assets]
            try:
                for i, f in enumerate(refs, 1):
                    paths.append(str(await save_upload(f, Path(tmp), IMAGE_EXT, f"ref{i}")))
            except ValueError as e:
                return err(str(e))
            p = Project.create(runs, desc, duration, ratio, style, paths, owner=user_of(req))
        if tpl:
            p.state["brief"]["characters"] = tpl["characters"]
            p.state["brief"]["template"] = {"id": tpl["id"], "name": tpl["name"]}
        if assets:
            p.state["brief"]["assets"] = [{"id": a_["id"], "name": a_["name"], "kind": a_["kind"]} for a_ in assets]
        p.save()
        store.enqueue(p.state["id"], "script", user=user_of(req))
        return JSONResponse(view(p, req=req), status_code=201)

    async def get_project(req: Request):
        try:
            return JSONResponse(view(load(req.path_params["pid"]), req=req))
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)

    async def put_script(req: Request):
        pid = req.path_params["pid"]
        try:
            p = load(pid)
            ensure_edit(req, p)
            ensure_idle(pid)
            body = await req.json()
            pipe = Pipeline(s, p, log=lambda _l: None)
            await run_in_threadpool(pipe.update_script, body)
            if p.state["status"] in ("failed", "draft"):
                p.state["status"] = "script_review"
                p.save()
            return JSONResponse(view(p, req=req))
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)
        except ProjectBusy as e:
            return err(str(e), 409)
        except (ScriptError, StageError, ValueError) as e:
            return err(f"脚本不合规：{e}")

    async def select(req: Request):
        pid = req.path_params["pid"]
        try:
            p = load(pid)
            ensure_edit(req, p)
            ensure_idle(pid)
            body = await req.json()
            idx = next((x["index"] for x in p.script["shots"] if x["id"] == body.get("shot_id")), None)
            if idx is None:
                return err("镜头不存在")
            Pipeline(s, p, log=lambda _l: None).select({idx: int(body.get("candidate"))})
            if p.state["status"] == "image_review" and all(
                    p.shot_state(x["id"])["selected"] for x in p.script["shots"]):
                p.state["status"] = "image_selected"
                p.save()
            return JSONResponse(view(p, req=req))
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)
        except ProjectBusy as e:
            return err(str(e), 409)
        except (StageError, ValueError, TypeError) as e:
            return err(str(e))

    async def upload_bgm(req: Request):
        pid = req.path_params["pid"]
        try:
            p = load(pid)
            ensure_edit(req, p)
            form = await req.form()
            f = form.get("bgm")
            if not getattr(f, "filename", None):
                return err("请选择音频文件")
            dest = await save_upload(f, p.root, AUDIO_EXT, "bgm")
            p.state["bgm"] = dest.name
            p.save()
            return JSONResponse(view(p, req=req))
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)
        except ValueError as e:
            return err(str(e))

    async def create_job(req: Request):
        pid = req.path_params["pid"]
        try:
            p = load(pid)
            ensure_edit(req, p)
            ensure_team_budget()
            body = await req.json()
            kind = body.get("kind")
            args = {}
            shots = body.get("shots")
            if shots is not None:
                if not isinstance(shots, list) or not all(isinstance(x, int) for x in shots):
                    return err("shots 必须是镜头序号数组")
                args["shots"] = shots
            if kind != "script" and not p.has_script:
                return err("还没有脚本")
            if kind == "refine":
                try:
                    args = {"shot": int(body["shot"]), "candidate": int(body["candidate"]),
                            "instruction": str(body.get("instruction", "")).strip()}
                except (KeyError, TypeError, ValueError):
                    return err("缺少 shot / candidate")
                if not 2 <= len(args["instruction"]) <= 300:
                    return err("修改说明 2–300 字")
            if kind == "export":
                if body.get("ratio") not in RATIO_SIZES:
                    return err("不支持的画幅")
                if not p.state.get("final"):
                    return err("还没有成片，先合成")
                args = {"ratio": body["ratio"]}
            if kind == "compose":
                args["burn_subs"] = bool(body.get("burn_subs"))
                try:
                    args["transition"] = float(body.get("transition") or 0)
                except (TypeError, ValueError):
                    return err("转场时长必须是数字")
                if not 0 <= args["transition"] <= 1.5:
                    return err("转场时长需在 0–1.5 秒")
                if body.get("bgm") and p.state.get("bgm"):
                    args["bgm"] = p.state["bgm"]
                if body.get("tts"):
                    if not (s.tts_enabled or s.demo_mode):
                        return err("服务器未配置 TTS，无法配音")
                    args["tts"] = True
                    voice = str(body.get("voice") or "").strip()
                    if voice and not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", voice):
                        return err("音色 ID 格式不对")
                    args["voice"] = voice or None
            job = store.enqueue(pid, kind, args, user=user_of(req))
            return JSONResponse(job, status_code=202)
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)
        except ProjectBusy as e:
            return err(str(e), 409)
        except ValueError as e:
            return err(str(e))

    async def cancel_job(req: Request):
        job = store.get(int(req.path_params["jid"]))
        if not job:
            return err("任务不存在", 404)
        try:
            ensure_edit(req, load(job["project"]))
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)
        result = store.request_cancel(job["id"])
        if result not in ("cancelled", "cancelling"):
            return err("任务已经结束")
        return JSONResponse({"status": result})

    async def events(req: Request):
        """SSE：项目状态或任务日志变化时推送 update 事件，前端收到后重新拉取。"""
        pid = req.path_params["pid"]
        try:
            p = load(pid)
        except LookupError:
            return err("项目不存在", 404)
        state_path = p.state_path

        def signature():
            try:
                mtime = state_path.stat().st_mtime_ns
            except FileNotFoundError:
                mtime = 0
            j = store.latest(pid)
            return (mtime, j and j["id"], j and j["status"], j and len(j["log"]))

        async def stream():
            last, idle = None, 0
            started = time.monotonic()
            yield "retry: 3000\n\n"
            while time.monotonic() - started < 1800:
                if await req.is_disconnected():
                    break
                sig = await run_in_threadpool(signature)
                if sig != last:
                    if last is not None:
                        yield "event: update\ndata: {}\n\n"
                    last, idle = sig, 0
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": ping\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def get_job(req: Request):
        job = store.get(int(req.path_params["jid"]))
        return JSONResponse(job) if job else err("任务不存在", 404)

    # ---------- 项目复制 / 删除 ----------
    async def duplicate_project(req: Request):
        try:
            src = load(req.path_params["pid"])
            ensure_team_budget()
        except LookupError:
            return err("项目不存在", 404)
        except ProjectBusy as e:
            return err(str(e), 409)
        b = src.state["brief"]
        p = Project.create(runs, b["description"], b["duration"], b["ratio"], b.get("style", ""),
                           src.refs, owner=user_of(req))
        for k in ("characters", "template", "assets"):
            if k in b:
                p.state["brief"][k] = b[k]
        p.state["copied_from"] = src.state["id"]
        if src.has_script:
            sc = src.script
            sc["title"] = (sc["title"] + "（副本）")[:40]
            p.write_script(sc)
            p.state["shots"] = {x["id"]: {"status": "pending", "candidates": [], "selected": None, "video": None,
                                          "tasks": [], "error": None, "image_stale": False} for x in sc["shots"]}
            p.state["status"] = "script_review"
        p.save()
        return JSONResponse(view(p, req=req), status_code=201)

    async def delete_project(req: Request):
        pid = req.path_params["pid"]
        try:
            p = load(pid)
            ensure_edit(req, p)
            ensure_idle(pid)
        except LookupError:
            return err("项目不存在", 404)
        except PermissionError as e:
            return err(str(e), 403)
        except ProjectBusy as e:
            return err(str(e), 409)
        trash = data / "trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(p.root), str(trash / f"{pid}__{time.strftime('%Y%m%d%H%M%S')}__{user_of(req) or 'anon'}"))
        return JSONResponse({"ok": True, "trash": True})

    # ---------- 成员 ----------
    def admin_only(req: Request):
        if not is_admin(req):
            raise PermissionError("只有管理员可以操作")

    async def list_users(req: Request):
        try:
            admin_only(req)
        except PermissionError as e:
            return err(str(e), 403)
        return JSONResponse(await run_in_threadpool(team.list_users))

    async def add_user(req: Request):
        try:
            admin_only(req)
            body = await req.json()
            u = await run_in_threadpool(team.add_user, body.get("name", ""), body.get("password", ""),
                                        body.get("role", "member"))
            return JSONResponse(u, status_code=201)
        except PermissionError as e:
            return err(str(e), 403)
        except TeamError as e:
            return err(str(e))

    async def update_user(req: Request):
        name = req.path_params["name"]
        try:
            body = await req.json()
            if not is_admin(req):  # 成员只能改自己的密码
                if name != user_of(req) or set(body) - {"password"}:
                    raise PermissionError("只能修改自己的密码")
            u = await run_in_threadpool(lambda: team.update_user(
                name, password=body.get("password"), role=body.get("role"),
                disabled=body.get("disabled") if "disabled" in body else None))
            return JSONResponse(u)
        except PermissionError as e:
            return err(str(e), 403)
        except TeamError as e:
            return err(str(e))

    # ---------- 模板 ----------
    def tpl_view(tp: dict) -> dict:
        assets = [team.get_asset(i) for i in tp["asset_ids"]]
        return {**tp, "assets": [{"id": x["id"], "name": x["name"], "url": f"/library/{x['file']}"} for x in assets if x]}

    async def list_templates(_req):
        return JSONResponse([tpl_view(x) for x in team.list_templates()])

    async def add_template(req: Request):
        try:
            body = await req.json()
            if body.get("from_project"):
                p = load(str(body["from_project"]))
                sc = p.script
                b = p.state["brief"]
                body = {"name": body.get("name") or sc["title"], "description": b["description"],
                        "style": sc["style"], "ratio": b["ratio"], "duration": b["duration"],
                        "characters": sc["characters"], "asset_ids": [x["id"] for x in b.get("assets", [])]}
            tp = team.add_template(body, user_of(req))
            return JSONResponse(tpl_view(tp), status_code=201)
        except LookupError:
            return err("项目不存在", 404)
        except (StageError, TeamError) as e:
            return err(str(e))

    async def delete_template(req: Request):
        tp = team.get_template(req.path_params["tid"])
        if not tp:
            return err("模板不存在", 404)
        if not (is_admin(req) or tp["owner"] in ("", user_of(req))):
            return err("只能删除自己创建的模板", 403)
        team.delete_template(tp["id"])
        return JSONResponse({"ok": True})

    # ---------- 素材库 ----------
    def asset_view(x: dict) -> dict:
        return {**x, "url": f"/library/{x['file']}"}

    async def list_assets(_req):
        return JSONResponse([asset_view(x) for x in team.list_assets()])

    async def add_asset(req: Request):
        form = await req.form()
        f = form.get("file")
        if not getattr(f, "filename", None):
            return err("请选择图片")
        ext = Path(f.filename).suffix.lower()
        if ext not in IMAGE_EXT:
            return err(f"不支持的文件类型 {ext or '(无扩展名)'}")
        data_ = await f.read()
        if len(data_) > MAX_UPLOAD:
            return err("文件超过 20MB")
        if not looks_like_image(data_):
            return err("文件内容不是 PNG / JPEG / WebP 图片")
        try:
            x = team.add_asset(str(form.get("name", "")), str(form.get("kind", "")), data_, ext, user_of(req), "upload")
        except TeamError as e:
            return err(str(e))
        return JSONResponse(asset_view(x), status_code=201)

    async def save_shot_asset(req: Request):
        pid = req.path_params["pid"]
        try:
            p = load(pid)
            body = await req.json()
            shot = next((x for x in p.script["shots"] if x["id"] == body.get("shot_id")), None)
            if not shot:
                return err("镜头不存在")
            st = p.shot_state(shot["id"])
            n = int(body.get("candidate") or st["selected"] or 0)
            if not 1 <= n <= len(st["candidates"]):
                return err("该镜头还没有可保存的场景图")
            src = p.root / st["candidates"][n - 1]
            x = team.add_asset(str(body.get("name") or shot["description"] or f"第{shot['index']}镜")[:40],
                               str(body.get("kind") or "场景"), src.read_bytes(), src.suffix,
                               user_of(req), f"project:{pid}")
            return JSONResponse(asset_view(x), status_code=201)
        except LookupError:
            return err("项目不存在", 404)
        except (StageError, TeamError, ValueError) as e:
            return err(str(e))

    async def delete_asset(req: Request):
        x = team.get_asset(req.path_params["aid"])
        if not x:
            return err("素材不存在", 404)
        if not (is_admin(req) or x["owner"] in ("", user_of(req))):
            return err("只能删除自己上传的素材", 403)
        team.delete_asset(x["id"])
        return JSONResponse({"ok": True})

    async def library_file(req: Request):
        name = req.path_params["name"]
        target = (team.library / name).resolve()
        if team.library.resolve() not in target.parents or not target.is_file():
            return err("not found", 404)
        return FileResponse(target, headers={"Cache-Control": "private, max-age=86400"})

    async def system(req: Request):
        if not is_admin(req):
            return err("只有管理员可以查看", 403)
        from videogen.doctor import check
        ping = req.query_params.get("ping") == "1"
        items = await run_in_threadpool(check, s, data, ping)
        trash = data / "trash"
        n_trash = len(list(trash.iterdir())) if trash.exists() else 0
        return JSONResponse({"items": items, "trash": n_trash, "workers": runner.workers})

    async def stats(_req):
        def month(e):
            return (e.get("at") or "")[:7] or "未知"
        totals = {"spent_cny": 0.0, "projects": 0, "done": 0}
        by_month: dict[str, float] = {}
        by_user: dict[str, dict] = {}
        by_kind: dict[str, float] = {}
        projects = []
        videos = regen = 0
        for d in runs.iterdir():
            if not (PID_RE.match(d.name) and (d / "state.json").exists()):
                continue
            try:
                st = json.loads((d / "state.json").read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            spent = sum(e["cost_cny"] for e in st["ledger"])
            owner = st.get("owner") or "未登录"
            totals["projects"] += 1
            totals["done"] += st["status"] == "done"
            totals["spent_cny"] += spent
            u = by_user.setdefault(owner, {"user": owner, "projects": 0, "spent_cny": 0.0})
            u["projects"] += 1
            u["spent_cny"] += spent
            for e in st["ledger"]:
                by_month[month(e)] = by_month.get(month(e), 0) + e["cost_cny"]
                by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + e["cost_cny"]
            for sh in st["shots"].values():
                n = len(sh.get("tasks", []))
                if n:
                    videos += 1
                    regen += n - 1
            projects.append({"id": st["id"], "title": st.get("title") or st["brief"]["description"][:30],
                             "owner": owner, "status": st["status"], "spent_cny": round(spent, 2),
                             "spent_usd": round(s.pricing.cny_to_usd(spent), 2)})
        usd = s.pricing.cny_to_usd
        projects.sort(key=lambda x: -x["spent_cny"])
        return JSONResponse({
            "totals": {"projects": totals["projects"], "done": totals["done"],
                       "spent_usd": round(usd(totals["spent_cny"]), 2), "spent_cny": round(totals["spent_cny"], 2),
                       "avg_usd_per_done": round(usd(totals["spent_cny"]) / totals["done"], 2) if totals["done"] else None,
                       "video_shots": videos, "video_regens": regen},
            "by_month": [{"month": m, "spent_usd": round(usd(v), 2)} for m, v in sorted(by_month.items())],
            "by_user": sorted(({**u, "spent_usd": round(usd(u["spent_cny"]), 2), "spent_cny": round(u["spent_cny"], 2)}
                               for u in by_user.values()), key=lambda x: -x["spent_cny"]),
            "by_kind": [{"kind": k, "spent_usd": round(usd(v), 2)} for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])],
            "jobs": store.stats(),
            "top_projects": projects[:10],
            "budget_usd": s.budget_usd, "priced": s.pricing.configured,
            "month": {"month": time.strftime("%Y-%m"), "spent_usd": round(month_spend_usd(), 2),
                      "team_budget_usd": team_budget,
                      "warn": bool(team_budget) and month_spend_usd() >= team_budget * 0.8},
        })

    async def files(req: Request):
        pid, rel = req.path_params["pid"], req.path_params["path"]
        if not PID_RE.match(pid):
            return err("not found", 404)
        root = (runs / pid).resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file() or target.name.endswith(".json"):
            return err("not found", 404)
        return FileResponse(target, headers={"Cache-Control": "private, max-age=86400"})

    @asynccontextmanager
    async def lifespan(_app):
        team.ensure_admin(os.environ.get("ADMIN_USER", ""), os.environ.get("ADMIN_PASSWORD", ""))
        runner.start()
        yield
        runner.stop()

    routes = [
        Route("/", index),
        Route("/api/config", config),
        Route("/api/projects", list_projects, methods=["GET"]),
        Route("/api/projects", create_project, methods=["POST"]),
        Route("/api/projects/{pid}", get_project),
        Route("/api/projects/{pid}/script", put_script, methods=["PUT"]),
        Route("/api/projects/{pid}/select", select, methods=["POST"]),
        Route("/api/projects/{pid}/bgm", upload_bgm, methods=["POST"]),
        Route("/api/projects/{pid}/jobs", create_job, methods=["POST"]),
        Route("/api/jobs/{jid:int}", get_job),
        Route("/api/jobs/{jid:int}/cancel", cancel_job, methods=["POST"]),
        Route("/api/projects/{pid}/events", events),
        Route("/api/stats", stats),
        Route("/api/system", system),
        Route("/api/users", list_users, methods=["GET"]),
        Route("/api/users", add_user, methods=["POST"]),
        Route("/api/users/{name}", update_user, methods=["PATCH"]),
        Route("/api/templates", list_templates, methods=["GET"]),
        Route("/api/templates", add_template, methods=["POST"]),
        Route("/api/templates/{tid:int}", delete_template, methods=["DELETE"]),
        Route("/api/assets", list_assets, methods=["GET"]),
        Route("/api/assets", add_asset, methods=["POST"]),
        Route("/api/assets/{aid:int}", delete_asset, methods=["DELETE"]),
        Route("/api/projects/{pid}/save-asset", save_shot_asset, methods=["POST"]),
        Route("/api/projects/{pid}/duplicate", duplicate_project, methods=["POST"]),
        Route("/api/projects/{pid}", delete_project, methods=["DELETE"]),
        Route("/library/{name}", library_file),
        Route("/files/{pid}/{path:path}", files),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    middleware = [Middleware(Auth, team=team, password=password)]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner
    app.state.team = team
    return app
