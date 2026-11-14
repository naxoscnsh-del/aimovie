"""命令行入口：python -m videogen <命令>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .cost import BudgetExceeded
from .pipeline import Pipeline, Project, StageError

RUNS = Path("runs")


def _shots(text: str | None) -> list[int] | None:
    return [int(x) for x in text.split(",")] if text else None


def _latest() -> Path:
    runs = sorted(p for p in RUNS.glob("*") if (p / "state.json").exists())
    if not runs:
        raise StageError("还没有项目，先运行 new 或 run")
    return runs[-1]


def _check_keys(s: Settings, need_deepseek=False, need_ark=False) -> None:
    if need_deepseek and not s.deepseek_api_key:
        raise StageError("缺少 DEEPSEEK_API_KEY（写在 .env 里）")
    if need_ark and not s.ark_api_key:
        raise StageError("缺少 ARK_API_KEY（写在 .env 里）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="videogen", description="AI 视频生成 P0 原型")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def brief_args(p):
        p.add_argument("description", help="场景描述")
        p.add_argument("--duration", type=int, default=30, help="目标总时长（秒）")
        p.add_argument("--ratio", default="9:16", choices=["9:16", "16:9", "1:1", "3:4", "4:3"])
        p.add_argument("--style", default="", help="风格要求")
        p.add_argument("--ref", action="append", default=[], help="参考图，可多次传入")

    p_new = sub.add_parser("new", help="新建项目并生成脚本")
    brief_args(p_new)
    p_run = sub.add_parser("run", help="一键跑完全流程（自动选第 1 张图）")
    brief_args(p_run)
    p_run.add_argument("--bgm")

    for name, helptext in [("script", "重新生成脚本"), ("images", "生成场景图"),
                           ("videos", "生成镜头视频"), ("compose", "合成成片"),
                           ("select", "选图"), ("status", "查看进度与花费")]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--project", help="项目目录，默认最新一个")
        if name in ("images", "videos"):
            p.add_argument("--shots", help="只跑指定镜头，如 2,5")
        if name == "compose":
            p.add_argument("--bgm")
        if name == "select":
            p.add_argument("picks", nargs="*", help="镜头=候选，如 1=2 3=1")
            p.add_argument("--auto", action="store_true", help="未选的镜头自动选第 1 张")

    args = ap.parse_args(argv)
    s = Settings.from_env()
    try:
        if args.cmd in ("new", "run"):
            _check_keys(s, need_deepseek=True, need_ark=args.cmd == "run")
            proj = Project.create(RUNS, args.description, args.duration, args.ratio, args.style, args.ref)
            print(f"项目目录：{proj.root}")
            pipe = Pipeline(s, proj)
            if args.cmd == "new":
                pipe.run_script()
            else:
                pipe.run_all(args.bgm)
            return 0

        proj = Project(Path(args.project) if args.project else _latest())
        pipe = Pipeline(s, proj)
        if args.cmd == "script":
            _check_keys(s, need_deepseek=True)
            pipe.run_script()
        elif args.cmd == "images":
            _check_keys(s, need_ark=True)
            pipe.run_images(_shots(args.shots))
        elif args.cmd == "select":
            picks = {}
            for item in args.picks:
                k, v = item.split("=")
                picks[int(k)] = int(v)
            pipe.select(picks, auto=args.auto)
        elif args.cmd == "videos":
            _check_keys(s, need_ark=True)
            pipe.run_videos(_shots(args.shots))
        elif args.cmd == "compose":
            pipe.run_compose(args.bgm)
        elif args.cmd == "status":
            print("\n".join(pipe.status_lines()))
        return 0
    except (StageError, BudgetExceeded) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
