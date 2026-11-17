"""可在页面里调整的参数：定义、校验、读写。

页面保存的值存数据库，启动时叠在 .env 之上；运行时原地更新共享的 Settings 对象，
下一个任务立即生效（已在跑的任务不受影响）。「恢复」= 删除页面设置，回到 .env/默认值。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Settings

RATIOS = ["9:16", "16:9", "1:1", "3:4", "4:3"]


@dataclass(frozen=True)
class Field_:
    key: str                 # Settings 属性路径，如 "pricing.usd_cny"
    label: str
    group: str
    kind: str                # text / url / secret / choice / int / float / bool / ints
    help: str = ""
    choices: tuple = ()
    lo: float | None = None
    hi: float | None = None
    pattern: str = ""
    unit: str = ""


F = Field_
FIELDS: list[Field_] = [
    # ---- 接口与密钥 ----
    F("deepseek_api_key", "DeepSeek API Key", "接口与密钥", "secret", "写脚本用。保存后只显示末 4 位，留空表示不修改"),
    F("ark_api_key", "方舟 API Key", "接口与密钥", "secret", "Seedream 出图和 Seedance 生成视频共用"),
    F("tts_appid", "语音合成 AppID", "接口与密钥", "secret", "火山引擎语音合成；不配则旁白只作字幕", pattern=r"[A-Za-z0-9_-]{4,40}"),
    F("tts_token", "语音合成 Token", "接口与密钥", "secret", "与 AppID 一起配置才会启用配音"),
    F("deepseek_base_url", "DeepSeek 接口地址", "接口与密钥", "url"),
    F("ark_base_url", "方舟接口地址", "接口与密钥", "url", "Key 会发往这个地址，只改成可信的官方或自建网关"),
    F("tts_url", "语音合成接口地址", "接口与密钥", "url"),
    # ---- 模型 ----
    F("deepseek_model", "脚本模型", "模型", "text", "例如 deepseek-chat", pattern=r"[A-Za-z0-9_.:/-]{2,100}"),
    F("seedream_model", "出图模型", "模型", "text", "模型名或接入点 ID（ep- 开头）", pattern=r"[A-Za-z0-9_.:/-]{2,100}"),
    F("seedance_model", "视频模型", "模型", "text", "模型名或接入点 ID（ep- 开头）", pattern=r"[A-Za-z0-9_.:/-]{2,100}"),
    F("seedance_resolution", "视频分辨率", "模型", "choice", "越高越贵、越慢", choices=("480p", "720p", "1080p")),
    F("seedance_ratio", "视频画幅", "模型", "choice", "adaptive = 跟随首帧（推荐，与场景图画幅一致）",
      choices=("adaptive", "9:16", "16:9", "1:1", "3:4", "4:3", "21:9")),
    F("seedance_durations", "视频单段时长", "模型", "ints", "模型允许的秒数，逗号分隔；分镜时长会吸附到最近的值。Seedance 2.0 支持 4–15 秒", unit="秒"),
    F("seedance_generate_audio", "视频自带音轨", "模型", "bool", "成片合成时会丢掉模型自带音轨，默认关闭更省时间和费用"),
    F("tts_voice", "默认旁白音色", "模型", "text", "voice_type", pattern=r"[A-Za-z0-9_.-]{2,64}"),
    F("tts_cluster", "语音合成 cluster", "模型", "text", pattern=r"[A-Za-z0-9_.-]{2,64}"),
    # ---- 生成行为 ----
    F("candidates_per_shot", "每镜候选图数", "生成行为", "int", "越多越能挑，但出图费用线性增加", lo=1, hi=4),
    F("max_parallel_videos", "视频并发数", "生成行为", "int", "同时提交给方舟的视频任务数，受账号并发限额约束", lo=1, hi=8),
    F("video_auto_retry", "视频自动重试次数", "生成行为", "int", "视频任务失败后自动重提的次数（重提会再次计费）", lo=0, hi=3),
    F("poll_interval_s", "视频轮询间隔", "生成行为", "float", lo=2, hi=60, unit="秒"),
    F("poll_timeout_s", "视频等待上限", "生成行为", "float", "超过则判定失败", lo=60, hi=7200, unit="秒"),
    # ---- 预算 ----
    F("budget_usd", "单片花费上限", "预算", "float", "预估或累计超过会拒绝提交新任务", lo=1, hi=100000, unit="USD"),
    F("budget_warn_usd", "单片预警线", "预算", "float", "达到后页面提示", lo=0, hi=100000, unit="USD"),
    F("regen_margin", "预估重生成余量", "预算", "float", "0.5 = 预估时多预留 50%，用于重出图、重生成视频", lo=0, hi=3),
    # ---- 单价 ----
    F("pricing.usd_cny", "汇率（人民币/美元）", "单价", "float", lo=1, hi=20),
    F("pricing.deepseek_input_per_mtok", "DeepSeek 输入", "单价", "float", lo=0, hi=10000, unit="元 / 百万 tokens"),
    F("pricing.deepseek_output_per_mtok", "DeepSeek 输出", "单价", "float", lo=0, hi=10000, unit="元 / 百万 tokens"),
    F("pricing.seedream_per_image", "Seedream 出图", "单价", "float", "没配单价时 100 USD 熔断和花费预估都不生效", lo=0, hi=1000, unit="元 / 张"),
    F("pricing.seedance_per_second", "Seedance 视频", "单价", "float", "按你选的分辨率填，到方舟控制台核对", lo=0, hi=1000, unit="元 / 秒"),
    F("pricing.tts_per_10k_chars", "语音合成", "单价", "float", lo=0, hi=10000, unit="元 / 万字"),
    # ---- 运行模式 ----
    F("demo_mode", "演示模式", "运行模式", "bool", "开启后不调用任何真实接口、不花钱，图片和视频是占位素材；正式使用请关闭"),
]
BY_KEY = {f.key: f for f in FIELDS}
GROUPS = list(dict.fromkeys(f.group for f in FIELDS))
SECRET_KEYS = {f.key for f in FIELDS if f.kind == "secret"}


def get_value(s: Settings, key: str) -> Any:
    obj = s
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


def set_value(s: Settings, key: str, value: Any) -> None:
    *path, last = key.split(".")
    obj = s
    for part in path:
        obj = getattr(obj, part)
    setattr(obj, last, value)


def snapshot(s: Settings) -> dict[str, Any]:
    """取出所有可调参数的当前值（深拷贝），用作「恢复」时的 .env/默认基线。"""
    return {f.key: copy.deepcopy(get_value(s, f.key)) for f in FIELDS}


def mask(v: str) -> str:
    return "" if not v else ("••••" + v[-4:] if len(v) > 8 else "••••")


def parse(f: Field_, raw: Any) -> Any:
    """校验并转成 Settings 里的类型；不合法抛 ValueError（中文提示）。"""
    name = f.label
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name}：请选择开或关")
    if f.kind in ("int", "float"):
        try:
            v = int(raw) if f.kind == "int" and str(raw).strip().lstrip("-").isdigit() else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name}：请填数字") from None
        if f.kind == "int" and v != int(v):
            raise ValueError(f"{name}：请填整数")
        v = int(v) if f.kind == "int" else float(v)
        if (f.lo is not None and v < f.lo) or (f.hi is not None and v > f.hi):
            raise ValueError(f"{name}：范围 {f.lo:g}–{f.hi:g}")
        return v
    if f.kind == "ints":
        try:
            parts = raw if isinstance(raw, (list, tuple)) else re.split(r"[,，\s]+", str(raw).strip())
            vals = sorted({int(x) for x in parts if str(x).strip()})
        except ValueError:
            raise ValueError(f"{name}：请填整数秒数，逗号分隔，如 5,10") from None
        if not vals or not all(1 <= x <= 30 for x in vals):
            raise ValueError(f"{name}：至少一个值，每个 1–30 秒")
        return vals
    v = str(raw if raw is not None else "").strip()
    if f.kind == "choice":
        if v not in f.choices:
            raise ValueError(f"{name}：只能是 {' / '.join(f.choices)}")
        return v
    if f.kind == "url":
        v = v.rstrip("/")
        m = re.fullmatch(r"(https?)://([^\s/:?#]+)(:\d+)?(/[^\s?#]*)?", v)
        if not m or (m.group(1) == "http" and m.group(2) not in ("localhost", "127.0.0.1")):
            raise ValueError(f"{name}：必须是 https:// 开头的地址")
        return v
    if f.kind == "secret":
        pat = f.pattern or r"\S{8,300}"
        if not re.fullmatch(pat, v):
            raise ValueError(f"{name}：格式不对（不能含空格或换行）")
        return v
    if not re.fullmatch(f.pattern or r".{1,100}", v):
        raise ValueError(f"{name}：格式不对")
    return v


def cross_check(values: dict[str, Any]) -> None:
    """合并后整体校验（字段之间的关系）。"""
    if values["budget_warn_usd"] > values["budget_usd"]:
        raise ValueError("单片预警线不能高于单片花费上限")
    if bool(values["tts_appid"]) != bool(values["tts_token"]):
        raise ValueError("语音合成的 AppID 和 Token 要同时填写，或同时留空")


def apply(s: Settings, values: dict[str, Any]) -> None:
    for k, v in values.items():
        set_value(s, k, copy.deepcopy(v))
