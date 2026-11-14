"""演示模式、取消任务、实时推送、转场。"""
import subprocess
import threading
import time

import pytest
import requests

from test_pipeline import FakeSeedanceClient, make_pipeline, settings
from test_team import ALICE, BOB, start, wait
from videogen.compose import effective_lengths, segment_overlap
from videogen.demo import demo_pipeline
from videogen.pipeline import Cancelled, Project
from web.app import create_app
from web.jobs import JobStore


def test_demo_pipeline_end_to_end(tmp_path):
    s = settings()
    p = Project.create(tmp_path / "runs", "社区咖啡店的清晨", 20, "16:9")
    logs = []
    pipe = demo_pipeline(s, p, logs.append)
    out = pipe.run_all()
    assert out.exists()
    dims = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert dims.stdout.strip() == "1280,720"
    assert pipe.ledger.spent_cny == 0  # 演示模式不计费
    out = pipe.run_compose(tts=True)
    assert len(list((p.root / "audio").glob("*.mp3"))) >= 1
    assert pipe.p.script["title"].endswith("（演示）")


def test_transition_math():
    assert segment_overlap([5, 10], 0.5) == 0.5
    assert segment_overlap([0.6, 10], 0.5) == 0.3  # 不超过最短镜头一半
    assert segment_overlap([5], 0.5) == 0
    assert effective_lengths([5, 10], 0.5) == [4.5, 10]


def test_compose_with_transition_shortens_and_syncs(tmp_path):
    pipe, _, _ = make_pipeline(tmp_path, settings())
    pipe.run_all()  # 每个假视频 1 秒
    out = pipe.run_compose(transition=0.4)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout)
    assert abs(dur - 1.6) < 0.15  # 2 秒 − 0.4 重叠
    srt = out.with_suffix(".srt").read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,600" in srt  # 第 1 镜字幕时长扣掉重叠
    assert pipe.p.state["final_meta"]["transition"] == 0.4


class SlowSeedanceClient(FakeSeedanceClient):
    def __init__(self):
        super().__init__()
        self.deleted = []

    def get(self, path):
        return {"id": path.rsplit("/", 1)[-1], "status": "running"}

    def request(self, method, path, json=None):
        self.deleted.append(path)
        return {}


def test_cancel_running_videos(tmp_path):
    s = settings()
    pipe, _, _ = make_pipeline(tmp_path, s)
    pipe.run_script(); pipe.run_images(); pipe.select(auto=True)
    slow = SlowSeedanceClient()
    pipe.seedance.client = slow
    stop = threading.Event()
    pipe.should_stop = stop.is_set
    threading.Timer(0.3, stop.set).start()
    with pytest.raises(Cancelled):
        pipe.run_videos()
    assert slow.deleted  # 尝试取消了方舟侧任务
    assert all(st["status"] == "image_selected" and not st["video"] for st in pipe.p.state["shots"].values())
    assert pipe.ledger.summary()["by_kind_cny"].get("video", 0) == 0


def test_job_store_cancel(tmp_path):
    store = JobStore(tmp_path / "db")
    j = store.enqueue("p1", "images")
    assert store.request_cancel(j["id"]) == "cancelled"
    assert store.get(j["id"])["status"] == "cancelled" and store.claim() is None
    j2 = store.enqueue("p1", "videos")  # 取消后可再提交
    store.claim()
    assert store.request_cancel(j2["id"]) == "cancelling" and store.cancel_requested(j2["id"])
    store.finish(j2["id"], "任务已取消", status="cancelled")
    assert store.request_cancel(j2["id"]) == "finished"  # 已结束，状态不变


def test_cancel_api_and_events(tmp_path):
    srv, th, base, app = start(tmp_path)
    try:
        pid = requests.post(f"{base}/api/projects", data={"description": "取消测试项目"}, auth=ALICE).json()["id"]
        wait(base, pid, ALICE)
        # 占住 worker：先停掉 runner，让任务停在排队状态
        app.state.runner.stop()
        job = requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "images"}, auth=ALICE).json()
        assert requests.post(f"{base}/api/jobs/{job['id']}/cancel", auth=BOB).status_code == 403
        r = requests.post(f"{base}/api/jobs/{job['id']}/cancel", auth=ALICE)
        assert r.json()["status"] == "cancelled"
        assert requests.post(f"{base}/api/jobs/{job['id']}/cancel", auth=ALICE).status_code == 400
        p = requests.get(f"{base}/api/projects/{pid}", auth=ALICE).json()
        assert p["job"] is None and p["last_job"]["status"] == "cancelled"
        # SSE：状态变化时收到 update
        got = []

        def listen():
            with requests.get(f"{base}/api/projects/{pid}/events", auth=ALICE, stream=True, timeout=10) as resp:
                assert resp.headers["content-type"].startswith("text/event-stream")
                for line in resp.iter_lines(decode_unicode=True):
                    if line == "event: update":
                        got.append(line)
                        return

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(1.5)
        p = requests.get(f"{base}/api/projects/{pid}", auth=ALICE).json()
        requests.post(f"{base}/api/projects/{pid}/select", json={"shot_id": p["shots"][0]["id"], "candidate": 1},
                      auth=ALICE)  # 无候选图会 400，但下面用改脚本触发状态变化
        shots = [{k: v for k, v in s.items() if k not in ("state", "index")} for s in p["shots"]]
        shots[0]["narration"] = "改一下旁白"
        assert requests.put(f"{base}/api/projects/{pid}/script", json={**p["script"], "shots": shots},
                            auth=ALICE).status_code == 200
        t.join(timeout=6)
        assert got == ["event: update"]
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def test_demo_mode_config(tmp_path):
    s = settings()
    s.demo_mode = True
    s.tts_appid = s.tts_token = ""
    app = create_app(s, data_dir=tmp_path / "d", workers=0, password="")
    import socket, uvicorn
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        cfg = requests.get(f"http://127.0.0.1:{port}/api/config").json()
        assert cfg["demo"] and cfg["tts"]["enabled"]
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def test_healthz_without_login(tmp_path):
    srv, th, base, app = start(tmp_path)
    try:
        r = requests.get(f"{base}/healthz")
        assert r.status_code == 200 and r.json()["ok"]
        assert requests.get(f"{base}/api/config").status_code == 401  # 其他接口仍需登录
    finally:
        srv.should_exit = True
        th.join(timeout=5)
