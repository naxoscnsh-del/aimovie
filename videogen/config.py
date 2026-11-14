"""配置：全部从环境变量（或 .env 文件）读取。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """极简 .env 读取，不覆盖已存在的环境变量。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Pricing:
    """单价，单位为人民币（火山方舟/DeepSeek 按人民币计价），预算按 USD 管控。
    P0 阶段需用实测账单校准这些数字。"""
    usd_cny: float = 7.2
    deepseek_input_per_mtok: float = 2.0
    deepseek_output_per_mtok: float = 8.0
    seedream_per_image: float = 0.0
    seedance_per_second: float = 0.0
    tts_per_10k_chars: float = 0.0

    @property
    def configured(self) -> bool:
        return self.seedream_per_image > 0 and self.seedance_per_second > 0

    def cny_to_usd(self, cny: float) -> float:
        return cny / self.usd_cny


@dataclass
class Settings:
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    seedream_model: str = "doubao-seedream-4-0-250828"
    seedream_size: str = "2K"
    seedance_model: str = "doubao-seedance-2-0-260128"
    seedance_resolution: str = "720p"
    seedance_ratio: str = "adaptive"
    # 模型允许的单段时长（秒），分镜时长会被吸附到最近的允许值
    seedance_durations: list[int] = field(default_factory=lambda: [5, 10])
    # 是否让 Seedance 生成音频（部分版本默认开启）；None 表示不传该参数
    seedance_generate_audio: bool | None = None
    video_auto_retry: int = 1

    # 火山引擎语音合成（旁白配音）
    tts_appid: str = ""
    tts_token: str = ""
    tts_cluster: str = "volcano_tts"
    tts_voice: str = "zh_female_wanqudashu_moon_bigtts"
    tts_url: str = "https://openspeech.bytedance.com/api/v1/tts"

    @property
    def tts_enabled(self) -> bool:
        return bool(self.tts_appid and self.tts_token)

    candidates_per_shot: int = 2
    max_parallel_videos: int = 3
    poll_interval_s: float = 10.0
    poll_timeout_s: float = 900.0

    budget_usd: float = 100.0
    budget_warn_usd: float = 80.0
    # 预估时为重生成预留的余量（0.5 = 多预留 50%）
    regen_margin: float = 0.5

    pricing: Pricing = field(default_factory=Pricing)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ.get
        durations = env("SEEDANCE_DURATIONS", "5,10")
        return cls(
            deepseek_api_key=env("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=env("DEEPSEEK_BASE_URL", cls.deepseek_base_url),
            deepseek_model=env("DEEPSEEK_MODEL", cls.deepseek_model),
            ark_api_key=env("ARK_API_KEY", ""),
            ark_base_url=env("ARK_BASE_URL", cls.ark_base_url),
            seedream_model=env("SEEDREAM_MODEL", cls.seedream_model),
            seedream_size=env("SEEDREAM_SIZE", cls.seedream_size),
            seedance_model=env("SEEDANCE_MODEL", cls.seedance_model),
            seedance_resolution=env("SEEDANCE_RESOLUTION", cls.seedance_resolution),
            seedance_ratio=env("SEEDANCE_RATIO", cls.seedance_ratio),
            seedance_durations=sorted(int(x) for x in durations.split(",") if x.strip()),
            seedance_generate_audio=(None if env("SEEDANCE_GENERATE_AUDIO", "") == ""
                                     else env("SEEDANCE_GENERATE_AUDIO").lower() in ("1", "true", "yes")),
            video_auto_retry=_i("VIDEO_AUTO_RETRY", 1),
            tts_appid=env("TTS_APPID", ""),
            tts_token=env("TTS_TOKEN", ""),
            tts_cluster=env("TTS_CLUSTER", cls.tts_cluster),
            tts_voice=env("TTS_VOICE", cls.tts_voice),
            tts_url=env("TTS_URL", cls.tts_url),
            candidates_per_shot=_i("CANDIDATES_PER_SHOT", 2),
            max_parallel_videos=_i("MAX_PARALLEL_VIDEOS", 3),
            poll_interval_s=_f("POLL_INTERVAL_S", 10.0),
            poll_timeout_s=_f("POLL_TIMEOUT_S", 900.0),
            budget_usd=_f("BUDGET_USD", 100.0),
            budget_warn_usd=_f("BUDGET_WARN_USD", 80.0),
            regen_margin=_f("REGEN_MARGIN", 0.5),
            pricing=Pricing(
                usd_cny=_f("USD_CNY", 7.2),
                deepseek_input_per_mtok=_f("PRICE_DEEPSEEK_INPUT_PER_MTOK", 2.0),
                deepseek_output_per_mtok=_f("PRICE_DEEPSEEK_OUTPUT_PER_MTOK", 8.0),
                seedream_per_image=_f("PRICE_SEEDREAM_PER_IMAGE", 0.0),
                seedance_per_second=_f("PRICE_SEEDANCE_PER_SECOND", 0.0),
                tts_per_10k_chars=_f("PRICE_TTS_PER_10K_CHARS", 0.0),
            ),
        )
