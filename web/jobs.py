"""任务队列：SQLite 持久化 + 后台线程执行。

5 人内部工具的规模下，用它代替 Celery + Redis：
- 每个项目同一时间只允许一个排队/运行中的任务，避免并发改写 state.json
- 任务日志写入数据库，前端轮询读取
- 服务重启时，把中断的任务标记为失败（素材已落盘，可直接重跑）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from videogen.config import Settings
from videogen.cost import BudgetExceeded
from videogen.pipeline import Cancelled, Pipeline, Project, StageError

KINDS = {"script", "images", "videos", "compose", "refine", "export"}
KIND_LABEL = {"script": "生成脚本", "images": "生成场景图", "videos": "生成镜头视频", "compose": "合成成片",
              "refine": "修改场景图", "export": "导出其他画幅"}


class ProjectBusy(RuntimeError):
    pass


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL, kind TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL, error TEXT, log TEXT NOT NULL DEFAULT '',
                user TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, started REAL, finished REAL)""")
            self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project, id)")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
            if "cancel" not in cols:  # 旧库升级
                self.conn.execute("ALTER TABLE jobs ADD COLUMN cancel INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    def _row(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["args"] = json.loads(d["args"])
        d["label"] = KIND_LABEL.get(d["kind"], d["kind"])
        return d

    def enqueue(self, project: str, kind: str, args: dict | None = None, user: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError(f"未知任务类型 {kind}")
        with self.lock:
            busy = self.conn.execute(
                "SELECT id FROM jobs WHERE project=? AND status IN ('queued','running')", (project,)).fetchone()
            if busy:
                raise ProjectBusy("该项目已有任务在执行，请等待完成")
            cur = self.conn.execute(
                "INSERT INTO jobs(project, kind, args, status, user, created) VALUES (?,?,?,?,?,?)",
                (project, kind, json.dumps(args or {}, ensure_ascii=False), "queued", user, time.time()))
            self.conn.commit()
            return self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone())

    def active(self, project: str) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute(
                "SELECT * FROM jobs WHERE project=? AND status IN ('queued','running') ORDER BY id LIMIT 1",
                (project,)).fetchone())

    def latest(self, project: str) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute(
                "SELECT * FROM jobs WHERE project=? ORDER BY id DESC LIMIT 1", (project,)).fetchone())

    def get(self, job_id: int) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def claim(self) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not r:
                return None
            self.conn.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), r["id"]))
            self.conn.commit()
            return self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone())

    def log(self, job_id: int, line: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE jobs SET log = log || ? WHERE id=?",
                              (f"[{time.strftime('%H:%M:%S')}] {line}\n", job_id))
            self.conn.commit()

    def finish(self, job_id: int, error: str | None = None, status: str | None = None) -> None:
        with self.lock:
            self.conn.execute("UPDATE jobs SET status=?, error=?, finished=? WHERE id=?",
                              (status or ("failed" if error else "done"), error, time.time(), job_id))
            self.conn.commit()

    def request_cancel(self, job_id: int) -> str:
        """排队中的直接取消；运行中的打标记，由流水线在检查点停下。返回取消后的状态。"""
        with self.lock:
            r = self.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r or r["status"] not in ("queued", "running"):
                return "finished" if r else "missing"
            if r["status"] == "queued":
                self.conn.execute("UPDATE jobs SET status='cancelled', error='已取消', finished=? WHERE id=?",
                                  (time.time(), job_id))
                self.conn.commit()
                return "cancelled"
            self.conn.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
            self.conn.commit()
            return "cancelling"

    def cancel_requested(self, job_id: int) -> bool:
        with self.lock:
            r = self.conn.execute("SELECT cancel FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(r and r["cancel"])

    def stats(self, since: float = 0) -> list[dict]:
        """按任务类型统计成功率与平均耗时。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT kind, COUNT(*) total, SUM(status='done') done, SUM(status='failed') failed, "
                "AVG(CASE WHEN status='done' THEN finished-started END) avg_s "
                "FROM jobs WHERE created >= ? GROUP BY kind", (since,)).fetchall()
        return [{"kind": r["kind"], "label": KIND_LABEL.get(r["kind"], r["kind"]), "total": r["total"],
                 "done": r["done"] or 0, "failed": r["failed"] or 0,
                 "avg_s": round(r["avg_s"], 1) if r["avg_s"] is not None else None} for r in rows]

    def recover(self) -> int:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status='failed', error='服务重启，任务中断，可直接重试', finished=? "
                "WHERE status IN ('running','queued')", (time.time(),))
            self.conn.commit()
            return cur.rowcount


PipelineFactory = Callable[[Settings, Project, Callable[[str], None]], Pipeline]


def default_factory(settings: Settings, project: Project, log) -> Pipeline:
    if settings.demo_mode:
        from videogen.demo import demo_pipeline
        return demo_pipeline(settings, project, log)
    return Pipeline(settings, project, log=log)


class Runner:
    def __init__(self, store: JobStore, settings: Settings, runs_dir: Path,
                 factory: PipelineFactory = default_factory, workers: int = 2, idle_sleep: float = 0.5):
        self.store = store
        self.s = settings
        self.runs_dir = runs_dir
        self.factory = factory
        self.workers = workers
        self.idle_sleep = idle_sleep
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.store.recover()
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.claim()
            if not job:
                self._stop.wait(self.idle_sleep)
                continue
            self.run_job(job)

    def run_job(self, job: dict) -> None:
        jid = job["id"]
        log = lambda line: self.store.log(jid, line)  # noqa: E731
        try:
            project = Project(self.runs_dir / job["project"])
            pipe = self.factory(self.s, project, log)
            pipe.should_stop = lambda: self.store.cancel_requested(jid)
            args = job["args"]
            if job["kind"] == "script":
                pipe.run_script()
            elif job["kind"] == "images":
                pipe.run_images(args.get("shots"))
            elif job["kind"] == "videos":
                pipe.run_videos(args.get("shots"))
            elif job["kind"] == "refine":
                pipe.refine_image(int(args["shot"]), int(args["candidate"]), str(args["instruction"]))
            elif job["kind"] == "compose":
                bgm = project.root / args["bgm"] if args.get("bgm") else None
                pipe.run_compose(str(bgm) if bgm else None, tts=bool(args.get("tts")), voice=args.get("voice"),
                                 burn_subs=bool(args.get("burn_subs")), transition=float(args.get("transition") or 0))
            elif job["kind"] == "export":
                pipe.export_ratio(str(args["ratio"]))
        except Cancelled as e:
            log(f"■ {e}")
            self._restore_status(job)
            self.store.finish(jid, str(e), status="cancelled")
        except (StageError, BudgetExceeded) as e:
            log(f"✗ {e}")
            self._restore_status(job)
            self.store.finish(jid, str(e))
        except Exception as e:  # noqa: BLE001
            log(f"✗ 未预期的错误：{e}")
            log(traceback.format_exc()[-1500:])
            self._restore_status(job)
            self.store.finish(jid, f"{type(e).__name__}: {e}")
        else:
            self.store.finish(jid)

    def _restore_status(self, job: dict) -> None:
        """失败后把项目状态退回可操作的审核态，避免卡在“生成中”。"""
        try:
            p = Project(self.runs_dir / job["project"])
        except Exception:  # noqa: BLE001
            return
        back = {"script": "failed" if not p.has_script else "script_review",
                "images": "image_review", "videos": "shot_review", "compose": "shot_review",
                "refine": "image_review", "export": "done"}
        if p.state["status"] in {"scripting", "imaging", "generating", "rendering", "draft"}:
            p.state["status"] = back[job["kind"]]
            p.save()
