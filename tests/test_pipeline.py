"""用假的 DeepSeek / Seedream / Seedance 跑完整流程；FFmpeg 合成是真实执行的。"""
import json
import subprocess
from pathlib import Path

import pytest

from videogen.config import Pricing, Settings
from videogen.cost import BudgetExceeded, Ledger, estimate
from videogen.models import Seedance
from videogen.pipeline import Pipeline, Project, StageError
from videogen.script import ScriptError, compose_image_prompt, snap_duration, validate_script

SCRIPT = {
    "title": "清晨咖啡",
    "style": "电影感写实摄影，暖色调",
    "characters": [{"name": "小林", "appearance": "25 岁女生，短发，米色毛衣"}],
    "shots": [
        {"description": "咖啡店外景", "characters": [], "image_prompt": "清晨街角咖啡店",
         "video_prompt": "镜头缓慢推进", "duration": 6, "narration": "早上七点"},
        {"description": "小林端起咖啡", "characters": ["小林", "路人"], "image_prompt": "小林坐在窗边",
         "video_prompt": "她端起杯子微笑", "duration": 9, "narration": ""},
    ],
}


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, path, body):
        self.calls.append(body)
        return {"choices": [{"message": {"content": self.replies.pop(0)}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 2000}}


class FakeSeedream:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, ratio, refs=None):
        self.prompts.append((prompt, ratio, list(refs or [])))
        return f"https://fake/img{len(self.prompts)}.png", {"generated_images": 1}


class FakeSeedanceClient:
    def __init__(self, fail_ids=()):
        self.n = 0
        self.fail_ids = set(fail_ids)
        self.bodies = []

    def post(self, path, body):
        self.n += 1
        self.bodies.append(body)
        return {"id": f"cgt-{self.n}"}

    def get(self, path):
        tid = path.rsplit("/", 1)[-1]
        if tid in self.fail_ids:
            return {"id": tid, "status": "failed", "error": {"message": "boom"}}
        return {"id": tid, "status": "succeeded", "content": {"video_url": f"https://fake/{tid}.mp4"},
                "usage": {"completion_tokens": 12345}}


def fake_download(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url.endswith(".png"):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=orange:s=144x256", "-frames:v", "1",
                        str(dest)], check=True, capture_output=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=360x640:r=24:d=1",
                        "-pix_fmt", "yuv420p", str(dest)], check=True, capture_output=True)


def settings(**kw):
    s = Settings(deepseek_api_key="x", ark_api_key="y", poll_interval_s=0, candidates_per_shot=2,
                 pricing=Pricing(usd_cny=7.0, seedream_per_image=0.2, seedance_per_second=1.0))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def make_pipeline(tmp_path, s, fail_ids=(), ref=True):
    refs = []
    if ref:
        r = tmp_path / "ref.png"
        fake_download("x.png", r)
        refs = [str(r)]
    proj = Project.create(tmp_path / "runs", "清晨咖啡店广告", 15, "9:16", refs=refs)
    client = FakeSeedanceClient(fail_ids)
    sd = Seedance(s, client=client, sleep=lambda _: None)
    logs = []
    pipe = Pipeline(s, proj, log=logs.append, seedream=FakeSeedream(), seedance=sd,
                    script_client=FakeChat([json.dumps(SCRIPT, ensure_ascii=False)]),
                    downloader=fake_download)
    return pipe, client, logs


def test_validate_script_snaps_and_filters():
    out = validate_script(SCRIPT, [5, 10])
    assert [s["duration"] for s in out["shots"]] == [5, 10]
    assert out["shots"][1]["characters"] == ["小林"]  # 未定义的“路人”被过滤
    assert snap_duration("abc", [5, 10]) == 5
    with pytest.raises(ScriptError):
        validate_script({"title": "t", "style": "s", "shots": []}, [5])


def test_image_prompt_includes_style_and_character():
    sc = validate_script(SCRIPT, [5, 10])
    p = compose_image_prompt(sc, sc["shots"][1])
    assert p.startswith("电影感写实摄影") and "短发，米色毛衣" in p and "小林坐在窗边" in p


def test_script_retries_on_bad_json(tmp_path):
    s = settings()
    pipe, _, _ = make_pipeline(tmp_path, s)
    pipe.script_client = FakeChat(["不是 JSON", json.dumps(SCRIPT, ensure_ascii=False)])
    pipe.run_script()
    assert len(pipe.script_client.calls) == 2
    assert "输出不合规" in pipe.script_client.calls[1]["messages"][-1]["content"]


def test_full_flow(tmp_path):
    s = settings()
    pipe, client, logs = make_pipeline(tmp_path, s)
    out = pipe.run_all()
    assert out.exists() and out.stat().st_size > 0
    assert out.with_suffix(".srt").read_text(encoding="utf-8").count("早上七点") == 1
    st = pipe.p.state
    assert st["status"] == "done"
    assert all(v["status"] == "succeeded" for v in st["shots"].values())
    # 参考图传给了 Seedream；首帧以 role=first_frame 传给 Seedance
    assert pipe.seedream.prompts[0][2]
    assert client.bodies[0]["content"][1]["role"] == "first_frame"
    assert client.bodies[0]["duration"] in (5, 10)
    # 花费：4 张图 × 0.2 + 15 秒 × 1.0 + 脚本
    kinds = pipe.ledger.summary()["by_kind_cny"]
    assert kinds["image"] == pytest.approx(0.8) and kinds["video"] == pytest.approx(15.0)


def test_failed_shot_then_rerun(tmp_path):
    s = settings(video_auto_retry=0)
    pipe, client, logs = make_pipeline(tmp_path, s, fail_ids={"cgt-1"})
    pipe.run_script(); pipe.run_images(); pipe.select(auto=True)
    pipe.run_videos()
    failed = [k for k, v in pipe.p.state["shots"].items() if v["status"] == "failed"]
    assert len(failed) == 1
    with pytest.raises(StageError):
        pipe.run_compose()
    pipe.run_videos()  # 默认只重跑没有视频的镜头
    assert client.n == 3
    assert pipe.run_compose().exists()


def test_reselect_invalidates_video(tmp_path):
    s = settings()
    pipe, _, _ = make_pipeline(tmp_path, s)
    pipe.run_all()
    pipe.select({1: 2})
    sh = pipe.p.state["shots"][pipe.p.script["shots"][0]["id"]]
    assert pipe.p.state["final"] is None
    assert sh["selected"] == 2 and sh["video"] is None and sh["status"] == "image_selected"


def test_budget_blocks_before_submit(tmp_path):
    # 上限 $2 = ¥14；图片 4×0.2，视频 15 秒 × 1.0 → 预估超限，整片提交被拒
    s = settings(budget_usd=2.0, budget_warn_usd=1.0)
    pipe, client, _ = make_pipeline(tmp_path, s)
    pipe.run_script()
    with pytest.raises(BudgetExceeded):
        pipe.run_images()


def test_budget_stops_videos_midway(tmp_path):
    # 只预留 margin=0 且上限刚好够第一个镜头
    s = settings(regen_margin=0.0, budget_usd=100.0)
    pipe, client, logs = make_pipeline(tmp_path, s)
    pipe.run_script(); pipe.run_images(); pipe.select(auto=True)
    s.budget_usd = (0.8 + 0.05 + 5 + 0.5) / 7.0 + 0.01  # 已花 + 第 1 镜 5 秒
    s.max_parallel_videos = 1
    with pytest.raises(BudgetExceeded):
        pipe.run_videos()  # 总需求超限，提交前即拒绝
    pipe.run_videos(shots=[1])
    assert client.n == 1


def test_ledger_warns_once():
    s = settings(budget_usd=10, budget_warn_usd=1)
    led = Ledger(s, [])
    assert led.record("image", "m", 3.0) == []
    msgs = led.record("image", "m", 5.0)
    assert msgs and "提醒线" in msgs[0]
    assert led.record("image", "m", 1.0) == []
    with pytest.raises(BudgetExceeded):
        led.ensure_room(100.0, "x")


def test_estimate_flags_unpriced():
    s = Settings()
    e = estimate(s, 6, 30)
    assert not e.priced and any("未配置" in line for line in e.lines())


def test_update_script_marks_stale_and_keeps_assets(tmp_path):
    s = settings()
    pipe, _, _ = make_pipeline(tmp_path, s)
    pipe.run_all()
    sc = pipe.p.script
    a, b = sc["shots"]
    b["image_prompt"] = "小林站在吧台后"
    a["video_prompt"] = "镜头拉远"
    sc["shots"] = [b, a]  # 调换顺序
    sc["shots"].append({"description": "新镜头", "image_prompt": "门口招牌", "video_prompt": "灯亮起",
                        "duration": 5, "narration": ""})
    out = pipe.update_script(sc)
    assert [x["id"] for x in out["shots"][:2]] == [b["id"], a["id"]]
    assert [x["index"] for x in out["shots"]] == [1, 2, 3]
    sa, sb = pipe.p.state["shots"][a["id"]], pipe.p.state["shots"][b["id"]]
    assert sb["image_stale"] and sb["video"]          # 画面变了：图过期，视频保留
    assert sa["video"] is None and sa["status"] == "image_selected"  # 运动变了：视频作废
    new_id = out["shots"][2]["id"]
    assert pipe.p.state["shots"][new_id]["status"] == "pending"
    # 默认出图只跑新镜头和过期镜头
    pipe.seedream.prompts.clear()
    pipe.run_images()
    assert len(pipe.seedream.prompts) == 2 * s.candidates_per_shot


def test_auto_retry_failed_video(tmp_path):
    s = settings(video_auto_retry=1)
    pipe, client, logs = make_pipeline(tmp_path, s, fail_ids={"cgt-1"})
    pipe.run_script(); pipe.run_images(); pipe.select(auto=True)
    pipe.run_videos()
    assert client.n == 3  # 2 个镜头 + 1 次自动重试
    assert all(v["status"] == "succeeded" for v in pipe.p.state["shots"].values())
    assert any("自动重试" in line for line in logs)
    assert pipe.ledger.summary()["by_kind_cny"]["video"] == pytest.approx(15.0)  # 失败的那次不记账


def test_link_next_uses_last_frame_and_invalidates(tmp_path):
    s = settings()
    pipe, client, logs = make_pipeline(tmp_path, s)
    pipe.run_script()
    sc = pipe.p.script
    sc["shots"][0]["link_next"] = True
    sc["shots"][1]["link_next"] = True  # 最后一镜会被强制为 False
    sc = pipe.update_script(sc)
    assert sc["shots"][0]["link_next"] and not sc["shots"][1]["link_next"]
    pipe.run_images()
    pipe.select({1: 1})
    with pytest.raises(StageError, match="请先给第 2 镜选图"):
        pipe.run_videos(shots=[1])
    pipe.select({2: 1})
    pipe.run_videos()
    roles = {b["content"][1]["role"]: len(b["content"]) for b in client.bodies}
    first_body = next(b for b in client.bodies if len(b["content"]) == 3)
    assert [c.get("role") for c in first_body["content"]] == [None, "first_frame", "last_frame"]
    assert sum(1 for b in client.bodies if len(b["content"]) == 2) == 1
    # 换第 2 镜的图 → 第 1 镜（过渡尾帧变了）和第 2 镜的视频都作废
    pipe.select({2: 2})
    st = pipe.p.state["shots"]
    assert st[sc["shots"][0]["id"]]["video"] is None and st[sc["shots"][1]["id"]]["video"] is None
    assert any("过渡尾帧已变化" in line for line in logs)
    # 关闭过渡 → 第 1 镜需要重做（尾帧从有变无）
    pipe.run_videos()
    sc = pipe.p.script
    sc["shots"][0]["link_next"] = False
    pipe.update_script(sc)
    assert pipe.p.state["shots"][sc["shots"][0]["id"]]["video"] is None


class FakeTTS:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice=None, speed=1.0):
        import subprocess, tempfile, os
        self.calls.append((text, voice))
        fd, path = tempfile.mkstemp(suffix=".mp3"); os.close(fd)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                        "-c:a", "libmp3lame", path], check=True, capture_output=True)
        data = Path(path).read_bytes(); os.unlink(path)
        return data


def test_compose_with_tts_and_bgm(tmp_path):
    import subprocess
    s = settings()
    s.pricing.tts_per_10k_chars = 10.0
    pipe, _, logs = make_pipeline(tmp_path, s)
    pipe.tts = FakeTTS()
    pipe.run_script(); pipe.run_images(); pipe.select(auto=True); pipe.run_videos()
    bgm = tmp_path / "bgm.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=0.5", str(bgm)],
                   check=True, capture_output=True)
    out = pipe.run_compose(str(bgm), tts=True, voice="v1")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.split()
    assert probe.count("video") == 1 and probe.count("audio") == 1 and "subtitle" in probe
    assert pipe.tts.calls == [("早上七点", "v1")]  # 只有第 1 镜有旁白
    tts_cost = sum(e["cost_cny"] for e in pipe.ledger.entries if e["kind"] == "tts")
    assert tts_cost == pytest.approx(4 / 10000 * 10)
    # 再合成一次：命中缓存，不重复花钱
    pipe.run_compose(None, tts=True, voice="v1")
    assert len(pipe.tts.calls) == 1
    assert pipe.p.state["final_meta"]["tts"]


def test_compose_tts_requires_config(tmp_path):
    s = settings()
    pipe, _, _ = make_pipeline(tmp_path, s)
    pipe.run_all()
    with pytest.raises(StageError, match="TTS"):
        pipe.run_compose(tts=True)
