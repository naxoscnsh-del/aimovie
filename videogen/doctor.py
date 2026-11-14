"""系统自检：上线前检查配置、依赖和接口连通性。

命令行：python -m videogen.doctor [--ping]
--ping 会访问 DeepSeek 模型列表和方舟任务列表接口（只读，不产生生成费用）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .config import Settings

OK, WARN, FAIL = "ok", "warn", "fail"


def _item(name: str, level: str, detail: str) -> dict:
    return {"name": name, "level": level, "detail": detail}


def check(settings: Settings, data_dir: Path | None = None, ping: bool = False, session=None) -> list[dict]:
    s = settings
    out: list[dict] = []
    if s.demo_mode:
        out.append(_item("演示模式", WARN, "DEMO_MODE=1：不调用真实接口、不花钱，素材为占位图。正式使用前关闭"))
    # 依赖
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            out.append(_item(tool, FAIL, "未安装，合成成片需要它"))
            continue
        ver = subprocess.run([tool, "-version"], capture_output=True, text=True).stdout.split("\n")[0]
        out.append(_item(tool, OK, ver[:60]))
    # 密钥
    out.append(_item("DeepSeek Key", OK if s.deepseek_api_key else FAIL,
                     "已配置" if s.deepseek_api_key else "缺少 DEEPSEEK_API_KEY"))
    out.append(_item("火山方舟 Key", OK if s.ark_api_key else FAIL,
                     "已配置" if s.ark_api_key else "缺少 ARK_API_KEY"))
    out.append(_item("语音合成 TTS", OK if s.tts_enabled else WARN,
                     f"已配置，默认音色 {s.tts_voice}" if s.tts_enabled else "未配置，旁白只能作为字幕"))
    # 单价
    p = s.pricing
    missing = [n for n, v in (("PRICE_SEEDREAM_PER_IMAGE", p.seedream_per_image),
                              ("PRICE_SEEDANCE_PER_SECOND", p.seedance_per_second)) if v <= 0]
    if s.tts_enabled and p.tts_per_10k_chars <= 0:
        missing.append("PRICE_TTS_PER_10K_CHARS")
    out.append(_item("模型单价", FAIL if missing else OK,
                     "未配置：" + "、".join(missing) + "（不配则预估为 0，100 USD 熔断失效）" if missing
                     else f"图 ¥{p.seedream_per_image}/张，视频 ¥{p.seedance_per_second}/秒，汇率 {p.usd_cny}"))
    # 模型与参数
    out.append(_item("模型", OK, f"脚本 {s.deepseek_model} · 图 {s.seedream_model} · 视频 {s.seedance_model}"))
    durs = s.seedance_durations
    out.append(_item("单段时长", OK if durs and min(durs) >= 2 and max(durs) <= 30 else WARN,
                     f"{durs} 秒，请与所选 Seedance 版本允许的时长一致"))
    if "seedance-1" in s.seedance_model:
        out.append(_item("首尾帧过渡", WARN, "当前是 Seedance 1.x，首尾帧过渡可能不受支持，建议用 2.0 及以上"))
    out.append(_item("预算", OK, f"单片 ${s.budget_usd:.0f}，提醒线 ${s.budget_warn_usd:.0f}"))
    # 磁盘
    if data_dir is not None:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(data_dir).free / 1e9
        out.append(_item("磁盘空间", OK if free_gb > 20 else WARN if free_gb > 5 else FAIL,
                         f"{data_dir.resolve()} 剩余 {free_gb:.1f} GB（每部短片约 50–300 MB）"))
    # 连通性
    if ping:
        import requests
        sess = session or requests.Session()

        def probe(name: str, url: str, key: str) -> None:
            if not key:
                out.append(_item(name, WARN, "未配置 Key，跳过"))
                return
            try:
                r = sess.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
            except Exception as e:  # noqa: BLE001
                out.append(_item(name, FAIL, f"连不上：{e}"[:200]))
                return
            if r.status_code < 400:
                out.append(_item(name, OK, f"连通（HTTP {r.status_code}）"))
            elif r.status_code in (401, 403):
                out.append(_item(name, FAIL, f"Key 无效或无权限（HTTP {r.status_code}）"))
            else:
                out.append(_item(name, WARN, f"HTTP {r.status_code}：{r.text[:120]}"))

        probe("连通 DeepSeek", s.deepseek_base_url.rstrip("/") + "/models", s.deepseek_api_key)
        probe("连通 火山方舟", s.ark_base_url.rstrip("/") + "/contents/generations/tasks?page_size=1", s.ark_api_key)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    items = check(Settings.from_env(), Path("data"), ping="--ping" in argv)
    mark = {OK: "✓", WARN: "!", FAIL: "✗"}
    for it in items:
        print(f" {mark[it['level']]} {it['name']:<12} {it['detail']}")
    fails = sum(it["level"] == FAIL for it in items)
    print(f"\n{'有 ' + str(fails) + ' 项必须处理' if fails else '可以开始使用'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
