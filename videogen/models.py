"""火山方舟 Seedream（图）与 Seedance（视频）客户端。"""
from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path

from .config import Settings
from .http import ApiError, HttpClient

RATIO_SIZES = {  # Seedream 输出尺寸，与视频画幅一致，Seedance 用 adaptive 跟随首帧
    "9:16": "1440x2560",
    "16:9": "2560x1440",
    "1:1": "2048x2048",
    "3:4": "1728x2304",
    "4:3": "2304x1728",
}


def to_data_url(path: str | Path) -> str:
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


class Seedream:
    def __init__(self, settings: Settings, client: HttpClient | None = None):
        self.s = settings
        self.client = client or HttpClient(settings.ark_base_url, settings.ark_api_key)

    def generate(self, prompt: str, ratio: str, refs: list[str] | None = None) -> tuple[str, dict]:
        """生成一张图，返回 (图片 URL, usage)。refs 为本地参考图路径。"""
        body = {
            "model": self.s.seedream_model,
            "prompt": prompt,
            "size": RATIO_SIZES.get(ratio, self.s.seedream_size),
            "response_format": "url",
            "watermark": False,
            "sequential_image_generation": "disabled",
        }
        if refs:
            imgs = [to_data_url(r) for r in refs]
            body["image"] = imgs[0] if len(imgs) == 1 else imgs
        resp = self.client.post("images/generations", body)
        data = resp.get("data") or []
        if not data or not data[0].get("url"):
            raise ApiError(200, str(resp), "images/generations")
        return data[0]["url"], resp.get("usage") or {}


class VideoTaskFailed(RuntimeError):
    pass


class Seedance:
    TERMINAL_OK = {"succeeded"}
    TERMINAL_BAD = {"failed", "cancelled", "expired"}

    def __init__(self, settings: Settings, client: HttpClient | None = None, sleep=time.sleep):
        self.s = settings
        self.client = client or HttpClient(settings.ark_base_url, settings.ark_api_key)
        self._sleep = sleep

    def submit(self, prompt: str, first_frame: str | Path, duration: int,
               last_frame: str | Path | None = None) -> str:
        """首帧图生视频；传 last_frame 时为首尾帧模式（画面从首帧过渡到尾帧）。"""
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": to_data_url(first_frame)}, "role": "first_frame"},
        ]
        if last_frame:
            content.append({"type": "image_url", "image_url": {"url": to_data_url(last_frame)},
                            "role": "last_frame"})
        body = {
            "model": self.s.seedance_model,
            "content": content,
            "ratio": self.s.seedance_ratio,
            "duration": duration,
            "resolution": self.s.seedance_resolution,
            "watermark": False,
        }
        if self.s.seedance_generate_audio is not None:
            body["generate_audio"] = self.s.seedance_generate_audio
        resp = self.client.post("contents/generations/tasks", body)
        return resp["id"]

    def cancel(self, task_id: str) -> None:
        """尽量取消方舟侧任务（排队中的任务可取消；已在生成的可能无法取消）。"""
        try:
            self.client.request("DELETE", f"contents/generations/tasks/{task_id}")
        except Exception:  # noqa: BLE001
            pass

    def poll(self, task_id: str, should_stop=None) -> dict:
        """轮询到终态。成功返回任务详情（content.video_url 24 小时内有效）。"""
        from .pipeline import Cancelled
        deadline = time.monotonic() + self.s.poll_timeout_s
        while True:
            if should_stop and should_stop():
                self.cancel(task_id)
                raise Cancelled("任务已取消")
            task = self.client.get(f"contents/generations/tasks/{task_id}")
            status = task.get("status")
            if status in self.TERMINAL_OK:
                return task
            if status in self.TERMINAL_BAD:
                raise VideoTaskFailed(f"任务 {task_id} {status}: {task.get('error')}")
            if time.monotonic() > deadline:
                raise VideoTaskFailed(f"任务 {task_id} 轮询超时（最后状态 {status}）")
            self._sleep(self.s.poll_interval_s)


class TTSError(RuntimeError):
    pass


class VolcTTS:
    """火山引擎语音合成 HTTP 接口：POST /api/v1/tts，返回 base64 音频，code=3000 表示成功。"""

    def __init__(self, settings: Settings, session=None):
        import requests
        self.s = settings
        self.session = session or requests.Session()

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0) -> bytes:
        import uuid
        body = {
            "app": {"appid": self.s.tts_appid, "token": "access_token", "cluster": self.s.tts_cluster},
            "user": {"uid": "ai-video"},
            "audio": {"voice_type": voice or self.s.tts_voice, "encoding": "mp3", "speed_ratio": speed},
            "request": {"reqid": str(uuid.uuid4()), "text": text, "operation": "query"},
        }
        resp = self.session.post(self.s.tts_url, json=body, timeout=60,
                                 headers={"Authorization": f"Bearer;{self.s.tts_token}"})
        try:
            data = resp.json()
        except ValueError:
            raise TTSError(f"TTS 返回非 JSON（HTTP {resp.status_code}）")
        if data.get("code") != 3000 or not data.get("data"):
            raise TTSError(f"TTS 失败：code={data.get('code')} {data.get('message') or data.get('Message')}")
        return base64.b64decode(data["data"])
