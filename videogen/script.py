"""DeepSeek 脚本生成：场景描述 → 结构化分镜（含场景图提示词与视频运动提示词）。"""
from __future__ import annotations

import json
import re
import uuid

from .config import Settings
from .http import HttpClient

SYSTEM_PROMPT = """你是短视频编剧兼分镜师。根据用户的场景描述，写一个可直接用于 AI 生成的分镜脚本。
生成流程是：先用文生图模型为每个镜头画一张"场景图"（视频首帧），再用图生视频模型让这张图动起来。

只输出 JSON，格式如下：
{
  "title": "短片标题",
  "style": "全片统一的画风与色调描述，会拼接到每一张场景图提示词前面，例如：电影感写实摄影，暖色调，浅景深，35mm",
  "characters": [
    {"name": "角色名", "appearance": "固定外观描述：年龄、发型、服装、体型等，越具体越好"}
  ],
  "shots": [
    {
      "description": "镜头内容的中文简述",
      "characters": ["出现的角色名"],
      "image_prompt": "场景图提示词：只描述这一帧静态画面——主体、动作姿态、环境、构图、光线。不要写镜头运动",
      "video_prompt": "视频提示词：描述画面如何动起来——人物动作、物体变化、镜头运动（推/拉/摇/移/跟），一句到两句",
      "duration": 5,
      "narration": "这一镜的旁白或字幕，可为空字符串",
      "link_next": false
    }
  ]
}

要求：
- 镜头时长只能从这些值中选：{durations} 秒；所有镜头时长之和尽量接近目标总时长。
- 画幅：{ratio}。构图要适合这个画幅。
- 同一角色在每个镜头的外观必须一致，外观只在 characters 里定义，image_prompt 里用角色名指代。
- 每个镜头只表达一个清晰的动作，避免复杂多人互动和画面内文字。
- 用户提供了参考图时，提示词要与参考图的主体保持一致。
- link_next：这一镜和下一镜是同一场景里连续的动作（例如人物从门口走到吧台）时填 true，视频会从本镜画面平滑过渡到下一镜画面；换场景、跳切时填 false。最后一镜必须是 false。
- 旁白每镜不超过（时长 × 4）个字，保证念得完。
"""


class ScriptError(ValueError):
    pass


def build_user_prompt(brief: dict) -> str:
    parts = [f"场景描述：{brief['description']}",
             f"目标总时长：约 {brief['duration']} 秒"]
    if brief.get("style"):
        parts.append(f"风格要求：{brief['style']}")
    chars = brief.get("characters") or []
    if chars:
        parts.append("固定角色设定（必须原样使用这些角色名和外观描述，可只用其中一部分，不要改写外观）：\n"
                     + "\n".join(f"- {c['name']}：{c['appearance']}" for c in chars))
    refs = brief.get("refs") or []
    if refs:
        parts.append(f"用户提供了 {len(refs)} 张参考图（角色/产品/风格参考），会在生成场景图时一并传给图像模型。")
    return "\n".join(parts)


def snap_duration(value, allowed: list[int]) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = allowed[0]
    return min(allowed, key=lambda a: (abs(a - v), a))


def validate_script(data: dict, allowed: list[int]) -> dict:
    if not isinstance(data, dict):
        raise ScriptError("脚本不是 JSON 对象")
    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ScriptError("缺少 shots 或为空")
    for key in ("title", "style"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ScriptError(f"缺少 {key}")
    chars = data.get("characters") or []
    if not isinstance(chars, list):
        raise ScriptError("characters 必须是数组")
    names = set()
    for c in chars:
        if not isinstance(c, dict) or not c.get("name") or not c.get("appearance"):
            raise ScriptError("characters 每项需要 name 和 appearance")
        names.add(c["name"])
    clean = []
    seen_ids: set[str] = set()
    for i, s in enumerate(shots, 1):
        if not isinstance(s, dict):
            raise ScriptError(f"第 {i} 镜不是对象")
        for key in ("image_prompt", "video_prompt"):
            if not isinstance(s.get(key), str) or not s[key].strip():
                raise ScriptError(f"第 {i} 镜缺少 {key}")
        sc = [n for n in (s.get("characters") or []) if n in names]
        sid = s.get("id")
        if not (isinstance(sid, str) and re.fullmatch(r"[a-z0-9]{4,16}", sid)) or sid in seen_ids:
            sid = uuid.uuid4().hex[:8]
        seen_ids.add(sid)
        clean.append({
            "id": sid,
            "index": i,
            "description": str(s.get("description", "")),
            "characters": sc,
            "image_prompt": s["image_prompt"].strip(),
            "video_prompt": s["video_prompt"].strip(),
            "duration": snap_duration(s.get("duration"), allowed),
            "narration": str(s.get("narration", "") or ""),
            "link_next": bool(s.get("link_next", False)) and i < len(shots),
        })
    return {"title": data["title"].strip(), "style": data["style"].strip(),
            "characters": [{"name": c["name"], "appearance": c["appearance"]} for c in chars],
            "shots": clean}


def generate_script(settings: Settings, brief: dict, client: HttpClient | None = None,
                    max_attempts: int = 3) -> tuple[dict, dict]:
    """返回 (脚本, usage)。JSON 不合规时把错误反馈给模型重试。"""
    client = client or HttpClient(settings.deepseek_base_url, settings.deepseek_api_key)
    system = SYSTEM_PROMPT.replace("{durations}", "、".join(map(str, settings.seedance_durations))) \
                          .replace("{ratio}", brief.get("ratio", "9:16"))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": build_user_prompt(brief)}]
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    last_err = ""
    for _ in range(max_attempts):
        resp = client.post("chat/completions", {
            "model": settings.deepseek_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.8,
        })
        u = resp.get("usage") or {}
        usage_total["prompt_tokens"] += u.get("prompt_tokens", 0)
        usage_total["completion_tokens"] += u.get("completion_tokens", 0)
        content = resp["choices"][0]["message"]["content"]
        try:
            return validate_script(json.loads(content), settings.seedance_durations), usage_total
        except (json.JSONDecodeError, ScriptError) as e:
            last_err = str(e)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"输出不合规：{last_err}。请修正后重新输出完整 JSON。"}]
    raise ScriptError(f"DeepSeek 连续 {max_attempts} 次输出不合规：{last_err}")


def script_cost_cny(settings: Settings, usage: dict) -> float:
    p = settings.pricing
    return (usage.get("prompt_tokens", 0) * p.deepseek_input_per_mtok
            + usage.get("completion_tokens", 0) * p.deepseek_output_per_mtok) / 1e6


def compose_image_prompt(script: dict, shot: dict) -> str:
    """全局风格 + 出场角色外观 + 本镜画面，保证各镜头一致。"""
    looks = [f"{c['name']}：{c['appearance']}" for c in script["characters"]
             if c["name"] in shot.get("characters", [])]
    parts = [script["style"]]
    if looks:
        parts.append("人物设定——" + "；".join(looks))
    parts.append(shot["image_prompt"])
    return "。".join(p.rstrip("。") for p in parts) + "。"
