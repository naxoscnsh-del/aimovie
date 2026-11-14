"""演示模式：不调用任何外部接口、不花钱，用本地 FFmpeg 生成占位素材，让团队先熟悉完整流程。

开启：环境变量 DEMO_MODE=1。
- 脚本：按描述和时长生成固定结构的分镜
- 场景图：纯色背景 + 镜头文字的占位图
- 视频：占位图做缓慢推镜
- 配音：与文字长度相当的静音
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from .compose import OUT_SIZES
from .config import Settings

FONT = "Noto Sans CJK SC"
DEMO_PREFIX = "demo://"


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"演示素材生成失败：{r.stderr[-800:]}")


def _color(seed: str) -> str:
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16) / 0xFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, 0.38, 0.45)
    return f"0x{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def _wrap(text: str, width: int) -> str:
    text = " ".join(text.split())
    return "\n".join(text[i:i + width] for i in range(0, len(text), width)) or " "


class DemoChat:
    """假装是 DeepSeek：按描述生成 OpenAI 兼容格式的回复。"""

    def __init__(self, settings: Settings):
        self.s = settings

    def post(self, path: str, body: dict) -> dict:
        user = body["messages"][1]["content"]
        desc = user.split("\n")[0].replace("场景描述：", "").strip()
        try:
            total = int(user.split("约 ")[1].split(" 秒")[0])
        except (IndexError, ValueError):
            total = 30
        durs = self.s.seedance_durations
        shots, acc = [], 0
        beats = ["开场全景，交代环境", "主角登场", "近景，动作细节", "情绪特写", "结尾，画面拉远"]
        while acc < total and len(shots) < 8:
            d = min(durs, key=lambda x: abs(x - min(max(durs), total - acc)))
            beat = beats[min(len(shots), len(beats) - 1)] if len(shots) < len(beats) - 1 else beats[-1]
            n = len(shots) + 1
            shots.append({
                "description": f"{beat}",
                "characters": ["主角"] if n > 1 else [],
                "image_prompt": f"{desc}，{beat}",
                "video_prompt": "镜头缓慢推近，画面轻微运动",
                "duration": d,
                "narration": f"第 {n} 镜：{beat}",
                "speaker": "",
                "link_next": False,
            })
            acc += d
        script = {"title": (desc[:12] or "演示短片") + "（演示）", "style": "演示模式占位画面",
                  "characters": [{"name": "主角", "appearance": "演示角色"}], "shots": shots}
        return {"choices": [{"message": {"content": json.dumps(script, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


class DemoSeedream:
    def __init__(self, settings: Settings):
        self.s = settings
        self.n = 0

    def generate(self, prompt: str, ratio: str, refs=None) -> tuple[str, dict]:
        self.n += 1
        payload = json.dumps({"kind": "image", "prompt": prompt[-120:], "ratio": ratio, "n": self.n},
                             ensure_ascii=False)
        return DEMO_PREFIX + payload, {"generated_images": 1}


class DemoArkClient:
    """假装是方舟视频任务接口：提交即完成。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}

    def post(self, path: str, body: dict) -> dict:
        tid = f"demo-{len(self.tasks) + 1:04d}"
        first = next(c["image_url"]["url"] for c in body["content"] if c.get("role") == "first_frame")
        self.tasks[tid] = {"duration": body["duration"], "first": first}
        return {"id": tid}

    def get(self, path: str) -> dict:
        tid = path.rsplit("/", 1)[-1]
        t = self.tasks[tid]
        payload = json.dumps({"kind": "video", "duration": t["duration"], "first": t["first"]})
        return {"id": tid, "status": "succeeded", "content": {"video_url": DEMO_PREFIX + payload}, "usage": {}}

    def request(self, method: str, path: str, json=None) -> dict:  # 取消任务
        return {}


class DemoTTS:
    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0) -> bytes:
        dur = max(0.8, len(text) / 4.5)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a.mp3"
            _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono", "-t", f"{dur:.2f}",
                  "-c:a", "libmp3lame", "-q:a", "9", str(out)])
            return out.read_bytes()


def demo_download(url: str, dest) -> None:
    """渲染 demo:// 地址对应的占位素材。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = json.loads(url[len(DEMO_PREFIX):])
    if info["kind"] == "image":
        w, h = OUT_SIZES.get(info["ratio"], (720, 1280))
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(_wrap(info["prompt"], max(8, w // 46)) + f"\n\n演示候选 #{info['n']}")
            txt = f.name
        try:
            _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={_color(info['prompt'] + str(info['n']))}:s={w}x{h}",
                  "-vf", f"drawtext=font='{FONT}':textfile={txt}:fontcolor=white:fontsize={w // 22}:"
                         f"line_spacing={w // 60}:x=(w-text_w)/2:y=(h-text_h)/2",
                  "-frames:v", "1", str(dest)])
        finally:
            Path(txt).unlink(missing_ok=True)
    else:
        first = info["first"]
        import base64
        img = dest.with_suffix(".first.png")
        img.write_bytes(base64.b64decode(first.split(",", 1)[1]))
        frames = int(info["duration"] * 24)
        dims = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", str(img)], capture_output=True, text=True)
        try:
            w, h = (int(x) // 2 * 2 for x in dims.stdout.strip().split(",")[:2])
        except ValueError:
            w, h = 720, 1280
        try:
            _run(["ffmpeg", "-y", "-loop", "1", "-i", str(img),
                  "-vf", f"zoompan=z='min(zoom+0.0012,1.25)':d={frames}"
                         f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=24",
                  "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                  str(dest)])
        finally:
            img.unlink(missing_ok=True)


def demo_pipeline(settings: Settings, project, log):
    """演示模式的流水线：所有单价视为 0。"""
    from copy import deepcopy

    from .config import Pricing
    from .models import Seedance
    from .pipeline import Pipeline
    s = deepcopy(settings)
    s.pricing = Pricing(usd_cny=settings.pricing.usd_cny)
    s.poll_interval_s = 0
    seedance = Seedance(s, client=DemoArkClient(), sleep=lambda _: None)
    return Pipeline(s, project, log=log, seedream=DemoSeedream(s), seedance=seedance,
                    script_client=DemoChat(s), downloader=demo_download, tts=DemoTTS())
