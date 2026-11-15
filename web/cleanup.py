"""回收站定期清理：data/trash 里超过 TRASH_KEEP_DAYS（默认 30）天的已删除项目会被彻底删除。"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path


def _deleted_at(d: Path) -> float:
    """目录名形如 <pid>__<YYYYMMDDHHMMSS>__<user>，取其中的删除时间；解析不出来就用目录修改时间。"""
    parts = d.name.split("__")
    if len(parts) >= 2:
        try:
            return time.mktime(time.strptime(parts[1], "%Y%m%d%H%M%S"))
        except ValueError:
            pass
    return d.stat().st_mtime


def purge_trash(trash: Path, keep_days: float, now: float | None = None) -> int:
    if keep_days <= 0 or not trash.exists():
        return 0
    cutoff = (now or time.time()) - keep_days * 86400
    n = 0
    for d in trash.iterdir():
        try:
            if _deleted_at(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
                n += 1
        except OSError:
            continue
    return n


def start_daily(trash: Path, stop: threading.Event) -> threading.Thread:
    days = float(os.environ.get("TRASH_KEEP_DAYS") or 30)

    def loop():
        while not stop.is_set():
            purge_trash(trash, days)
            stop.wait(24 * 3600)

    t = threading.Thread(target=loop, name="trash-purge", daemon=True)
    t.start()
    return t
