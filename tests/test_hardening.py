"""上线前加固：CSRF、上传校验、改图、复制/删除、系统自检、中间文件清理。"""
import time

import pytest
import requests

from test_pipeline import fake_download, make_pipeline, settings
from test_team import ADMIN, ALICE, BOB, start, wait
from videogen.doctor import check
from videogen.pipeline import StageError


@pytest.fixture()
def srv(tmp_path):
    s, th, base, app = start(tmp_path)
    yield base, app, tmp_path
    s.should_exit = True
    th.join(timeout=5)


def test_csrf_and_headers(srv):
    base, _, _ = srv
    host = base.split("//")[1]
    evil = {"Origin": "https://evil.example"}
    r = requests.post(f"{base}/api/projects", data={"description": "跨站伪造请求"}, headers=evil, auth=ALICE)
    assert r.status_code == 403
    r = requests.post(f"{base}/api/projects", data={"description": "跨站伪造请求"},
                      headers={"Sec-Fetch-Site": "cross-site"}, auth=ALICE)
    assert r.status_code == 403
    r = requests.post(f"{base}/api/projects", data={"description": "同源正常请求"},
                      headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}, auth=ALICE)
    assert r.status_code == 201
    assert requests.get(f"{base}/api/config", headers=evil, auth=ALICE).status_code == 200  # 读请求不拦
    h = requests.get(f"{base}/", auth=ALICE).headers
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
    wait(base, r.json()["id"], ALICE)


def test_upload_content_checked(srv):
    base, _, _ = srv
    fake = b"<html>not an image</html>"
    r = requests.post(f"{base}/api/assets", data={"name": "假图", "kind": "角色"},
                      files={"file": ("x.png", fake, "image/png")}, auth=ALICE)
    assert r.status_code == 400 and "不是" in r.json()["error"]
    r = requests.post(f"{base}/api/projects", data={"description": "带假图的项目"},
                      files=[("refs", ("x.jpg", fake, "image/jpeg"))], auth=ALICE)
    assert r.status_code == 400


def test_refine_appends_candidate(srv):
    base, _, _ = srv
    pid = requests.post(f"{base}/api/projects", data={"description": "改图测试项目"}, auth=ALICE).json()["id"]
    wait(base, pid, ALICE)
    requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "images"}, auth=ALICE)
    p = wait(base, pid, ALICE)
    s1 = p["shots"][0]
    requests.post(f"{base}/api/projects/{pid}/select", json={"shot_id": s1["id"], "candidate": 2}, auth=ALICE)
    assert requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "refine", "shot": 1, "candidate": 2,
                                                                    "instruction": "x"}, auth=ALICE).status_code == 400
    r = requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "refine", "shot": 1, "candidate": 2,
                                                                 "instruction": "背景加一盏暖色吊灯"}, auth=ALICE)
    assert r.status_code == 202
    p = wait(base, pid, ALICE)
    st = p["shots"][0]["state"]
    assert len(st["candidates"]) == 3 and st["selected"] == 2  # 原选择不变
    assert st["refined"] == [None, None, "背景加一盏暖色吊灯"]
    assert p["last_job"]["status"] == "done" and p["status"] == "image_review"
    assert p["cost"]["by_kind_cny"]["image"] == pytest.approx(0.2 * 5)


def test_refine_uses_base_image_as_first_reference(tmp_path):
    s = settings()
    pipe, _, _ = make_pipeline(tmp_path, s)
    pipe.run_script(); pipe.run_images()
    with pytest.raises(StageError):
        pipe.refine_image(1, 5, "改一下")
    pipe.refine_image(1, 2, "换成侧脸")
    prompt, ratio, refs = pipe.seedream.prompts[-1]
    st = pipe.p.shot_state(pipe.p.script["shots"][0]["id"])
    assert refs[0].endswith(st["candidates"][1]) and "换成侧脸" in prompt and len(refs) == 2


def test_duplicate_and_delete(srv):
    base, app, tmp = srv
    pid = requests.post(f"{base}/api/projects", data={"description": "要被复制的项目"}, auth=ALICE).json()["id"]
    wait(base, pid, ALICE)
    r = requests.post(f"{base}/api/projects/{pid}/duplicate", auth=BOB)
    assert r.status_code == 201
    dup = r.json()
    assert dup["owner"] == "小李" and dup["status"] == "script_review" and dup["title"].endswith("（副本）")
    assert len(dup["shots"]) == 2 and not any(s["state"]["candidates"] for s in dup["shots"])
    # 删除：别人不行，本人可以，进回收站
    assert requests.delete(f"{base}/api/projects/{pid}", auth=BOB).status_code == 403
    assert requests.delete(f"{base}/api/projects/{pid}", auth=ALICE).json()["trash"]
    assert requests.get(f"{base}/api/projects/{pid}", auth=ALICE).status_code == 404
    trash = list((tmp / "data" / "trash").iterdir())
    assert len(trash) == 1 and trash[0].name.startswith(pid) and (trash[0] / "state.json").exists()
    assert requests.delete(f"{base}/api/projects/{dup['id']}", auth=ADMIN).status_code == 200


def test_system_check(srv, tmp_path):
    base, _, _ = srv
    assert requests.get(f"{base}/api/system", auth=ALICE).status_code == 403
    d = requests.get(f"{base}/api/system", auth=ADMIN).json()
    names = {i["name"]: i for i in d["items"]}
    assert names["ffmpeg"]["level"] == "ok" and "磁盘空间" in names
    # 测试配置开了 TTS 但没配 TTS 单价 → 必须提示
    assert names["模型单价"]["level"] == "fail" and "PRICE_TTS_PER_10K_CHARS" in names["模型单价"]["detail"]
    items = check(settings(), tmp_path, ping=False)
    assert all(i["level"] in ("ok", "warn", "fail") for i in items)


def test_doctor_ping_reports_auth_errors(tmp_path):
    class Resp:
        def __init__(self, code): self.status_code, self.text = code, ""

    class Sess:
        def get(self, url, headers, timeout):
            return Resp(401 if "deepseek" in url else 200)

    items = {i["name"]: i for i in check(settings(), None, ping=True, session=Sess())}
    assert items["连通 DeepSeek"]["level"] == "fail" and "Key" in items["连通 DeepSeek"]["detail"]
    assert items["连通 火山方舟"]["level"] == "ok"


def test_compose_cleans_temp(tmp_path):
    pipe, _, _ = make_pipeline(tmp_path, settings())
    pipe.run_all()
    assert not (pipe.p.root / "_compose").exists()
