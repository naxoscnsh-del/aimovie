"""启动真实 HTTP 服务（uvicorn），用假模型走完 Web 端全流程。"""
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

from test_pipeline import SCRIPT, FakeChat, FakeSeedanceClient, FakeSeedream, FakeTTS, fake_download, settings
from videogen.models import Seedance
from videogen.pipeline import Pipeline
from web.app import create_app
from web.jobs import JobStore, ProjectBusy

AUTH = ("小王".encode("utf-8"), b"pw")  # 浏览器按 UTF-8 发送中文用户名


def factory(s, project, log):
    return Pipeline(s, project, log=log, seedream=FakeSeedream(),
                    seedance=Seedance(s, client=FakeSeedanceClient(), sleep=lambda _: None),
                    script_client=FakeChat([json.dumps(SCRIPT, ensure_ascii=False)]),
                    downloader=fake_download, tts=FakeTTS())


def web_settings():
    s = settings()
    s.tts_appid, s.tts_token = "app", "tok"
    return s


@pytest.fixture()
def server(tmp_path):
    app = create_app(web_settings(), data_dir=tmp_path / "data", factory=factory, workers=1, password="pw")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def wait_idle(base, pid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        p = requests.get(f"{base}/api/projects/{pid}", auth=AUTH).json()
        if not p["job"]:
            return p
        time.sleep(0.2)
    raise TimeoutError(p)


def job(base, pid, kind, **extra):
    r = requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": kind, **extra}, auth=AUTH)
    assert r.status_code == 202, r.text
    return wait_idle(base, pid)


def test_auth_required(server):
    assert requests.get(f"{server}/api/config").status_code == 401
    assert requests.get(f"{server}/api/config", auth=("x", "wrong")).status_code == 401
    cfg = requests.get(f"{server}/api/config", auth=AUTH).json()
    assert cfg["user"] == "小王" and cfg["budget_usd"] == 100


def test_full_web_flow(server, tmp_path):
    ref = tmp_path / "ref.png"
    fake_download("x.png", ref)
    with open(ref, "rb") as f:
        r = requests.post(f"{server}/api/projects", auth=AUTH,
                          data={"description": "清晨咖啡店广告", "duration": "15", "ratio": "9:16"},
                          files=[("refs", ("ref.png", f, "image/png"))])
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    p = wait_idle(server, pid)
    assert p["status"] == "script_review" and len(p["shots"]) == 2
    assert p["owner"] == "小王" and p["brief"]["refs"][0].startswith(f"/files/{pid}/refs/")
    assert p["last_job"]["status"] == "done" and "DeepSeek" in p["last_job"]["log"]

    # 编辑脚本：不合规 → 400；合规 → 保存
    bad = {**p["script"], "shots": []}
    assert requests.put(f"{server}/api/projects/{pid}/script", json=bad, auth=AUTH).status_code == 400
    shots = [{k: v for k, v in s.items() if k not in ("state", "index")} for s in p["shots"]]
    shots[1]["narration"] = "改过的旁白"
    r = requests.put(f"{server}/api/projects/{pid}/script", json={**p["script"], "shots": shots}, auth=AUTH)
    assert r.status_code == 200 and r.json()["shots"][1]["narration"] == "改过的旁白"

    p = job(server, pid, "images")
    assert p["status"] == "image_review"
    assert all(len(s["state"]["candidates"]) == 2 for s in p["shots"])
    img = requests.get(server + p["shots"][0]["state"]["candidates"][0], auth=AUTH)
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"

    # 视频前必须选图
    r = requests.post(f"{server}/api/projects/{pid}/jobs", json={"kind": "videos"}, auth=AUTH)
    assert r.status_code == 202
    p = wait_idle(server, pid)
    assert p["last_job"]["status"] == "failed" and "还没选场景图" in p["last_job"]["error"]
    assert p["status"] == "image_review"  # 状态退回可操作

    for s in p["shots"]:
        r = requests.post(f"{server}/api/projects/{pid}/select", json={"shot_id": s["id"], "candidate": 2}, auth=AUTH)
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "image_selected"

    p = job(server, pid, "videos")
    assert p["status"] == "shot_review" and all(s["state"]["video"] for s in p["shots"])
    assert p["cost"]["by_kind_cny"]["video"] == pytest.approx(15.0)

    r = requests.post(f"{server}/api/projects/{pid}/jobs", json={"kind": "compose", "tts": True, "voice": "bad voice!"}, auth=AUTH)
    assert r.status_code == 400
    p = job(server, pid, "compose", tts=True, voice="zh_test_voice")
    assert p["status"] == "done" and p["final"]
    assert p["final_meta"] == {"tts": True, "bgm": False, "voice": "zh_test_voice", "burn_subs": False, "transition": 0.0}
    v = requests.get(server + p["final"], auth=AUTH, headers={"Range": "bytes=0-99"})
    assert v.status_code == 206 and len(v.content) == 100
    srt = requests.get(server + p["final"].replace(".mp4", ".srt"), auth=AUTH)
    assert "改过的旁白" in srt.content.decode("utf-8")

    pk = requests.get(f"{server}/api/projects/{pid}/package", auth=AUTH)  # 打包下载
    assert pk.status_code == 200 and pk.headers["content-type"] == "application/zip"
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(pk.content)).namelist()
    assert any(n.endswith(".mp4") for n in names) and any(n.endswith(".srt") for n in names) and any(n.endswith(".json") for n in names)

    lst = requests.get(f"{server}/api/projects", auth=AUTH).json()
    assert lst[0]["id"] == pid and lst[0]["title"] == "清晨咖啡"


def test_files_are_sandboxed(server):
    r = requests.post(f"{server}/api/projects", auth=AUTH, data={"description": "测试项目描述", "duration": "15"})
    pid = r.json()["id"]
    wait_idle(server, pid)
    for path in ["state.json", "../app.db", "%2e%2e/app.db", "script.json"]:
        assert requests.get(f"{server}/files/{pid}/{path}", auth=AUTH).status_code == 404
    assert requests.get(f"{server}/files/nope/x.png", auth=AUTH).status_code == 404
    assert requests.get(f"{server}/api/projects/../etc", auth=AUTH).status_code == 404


def test_validation(server):
    r = requests.post(f"{server}/api/projects", auth=AUTH, data={"description": "x"})
    assert r.status_code == 400
    r = requests.post(f"{server}/api/projects", auth=AUTH, data={"description": "正常描述", "ratio": "2:1"})
    assert r.status_code == 400
    r = requests.post(f"{server}/api/projects", auth=AUTH, data={"description": "正常描述"},
                      files=[("refs", ("a.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 400 and "不支持" in r.json()["error"]


def test_one_job_per_project(tmp_path):
    store = JobStore(tmp_path / "db.sqlite")
    store.enqueue("p1", "images")
    with pytest.raises(ProjectBusy):
        store.enqueue("p1", "videos")
    store.enqueue("p2", "images")  # 其他项目不受影响
    j = store.claim()
    assert j["project"] == "p1" and j["status"] == "running"
    assert store.recover() == 2  # 重启时中断的任务被标记失败
    assert store.get(j["id"])["status"] == "failed"
    store.enqueue("p1", "videos")  # 失败后可重新提交


def test_stats(server):
    r = requests.post(f"{server}/api/projects", auth=AUTH, data={"description": "统计用的项目", "duration": "15"})
    pid = r.json()["id"]
    wait_idle(server, pid)
    job(server, pid, "images")
    d = requests.get(f"{server}/api/stats", auth=AUTH).json()
    assert d["totals"]["projects"] == 1 and d["totals"]["spent_usd"] > 0
    assert d["by_user"][0]["user"] == "小王"
    kinds = {k["kind"] for k in d["by_kind"]}
    assert {"script", "image"} <= kinds
    jobs = {j["kind"]: j for j in d["jobs"]}
    assert jobs["images"]["done"] == 1 and jobs["script"]["total"] == 1
    assert d["top_projects"][0]["id"] == pid


def test_export_via_api(server):
    r = requests.post(f"{server}/api/projects", auth=AUTH, data={"description": "导出画幅测试", "duration": "15"})
    pid = r.json()["id"]
    wait_idle(server, pid)
    bad = requests.post(f"{server}/api/projects/{pid}/jobs", json={"kind": "export", "ratio": "16:9"}, auth=AUTH)
    assert bad.status_code == 400  # 还没有成片
    job(server, pid, "images")
    p = wait_idle(server, pid)
    for s in p["shots"]:
        requests.post(f"{server}/api/projects/{pid}/select", json={"shot_id": s["id"], "candidate": 1}, auth=AUTH)
    job(server, pid, "videos")
    p = job(server, pid, "compose", burn_subs=True)
    assert p["final_meta"]["burn_subs"]
    assert requests.post(f"{server}/api/projects/{pid}/jobs", json={"kind": "export", "ratio": "2:1"},
                         auth=AUTH).status_code == 400
    p = job(server, pid, "export", ratio="1:1")
    assert p["status"] == "done" and p["exports"]["1:1"].startswith(f"/files/{pid}/export_1x1_")
    assert requests.get(server + p["exports"]["1:1"], auth=AUTH).status_code == 200
