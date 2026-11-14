"""P3：成员账号与权限、模板库、素材库、团队月度预算。"""
import json
import socket
import threading
import time

import pytest
import requests
import uvicorn

from test_pipeline import fake_download
from test_web import factory, wait_idle, web_settings
from web.app import create_app
from web.team import TeamError, TeamStore, hash_password, verify_password


def u(name, pw):
    return (name.encode("utf-8"), pw.encode("utf-8"))


ADMIN = u("老板", "admin-pass-1")
ALICE = u("小王", "alice-pass-1")
BOB = u("小李", "bob-pass-12")


def start(tmp_path, **kw):
    app = create_app(web_settings(), data_dir=tmp_path / "data", factory=factory, workers=1, password="", **kw)
    app.state.team.add_user("老板", "admin-pass-1", "admin")
    app.state.team.add_user("小王", "alice-pass-1")
    app.state.team.add_user("小李", "bob-pass-12")
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    while not srv.started:
        time.sleep(0.05)
    return srv, th, f"http://127.0.0.1:{port}", app


@pytest.fixture()
def team_server(tmp_path):
    srv, th, base, app = start(tmp_path)
    yield base, app
    srv.should_exit = True
    th.join(timeout=5)


def wait(base, pid, auth):
    end = time.time() + 60
    while time.time() < end:
        p = requests.get(f"{base}/api/projects/{pid}", auth=auth).json()
        if not p["job"]:
            return p
        time.sleep(0.2)
    raise TimeoutError


def test_password_hashing():
    h = hash_password("secret-123", iterations=1000)
    assert verify_password("secret-123", h) and not verify_password("nope", h)
    assert not verify_password("x", "garbage")


def test_team_store_rules(tmp_path):
    t = TeamStore(tmp_path / "db", tmp_path / "lib")
    t.add_user("管理员", "12345678", "admin")
    with pytest.raises(TeamError):
        t.add_user("管理员", "12345678")  # 重名
    with pytest.raises(TeamError):
        t.add_user("bad name!", "12345678")
    with pytest.raises(TeamError):
        t.add_user("短密码", "123")
    with pytest.raises(TeamError, match="至少要保留"):
        t.update_user("管理员", role="member")  # 最后一个管理员不能降级
    assert t.authenticate("管理员", "12345678") == "admin"
    t.add_user("员工", "abcdefgh")
    t.update_user("员工", disabled=True)
    assert t.authenticate("员工", "abcdefgh") is None


def test_login_and_member_management(team_server):
    base, _ = team_server
    assert requests.get(f"{base}/api/config").status_code == 401
    assert requests.get(f"{base}/api/config", auth=u("小王", "wrong-pass")).status_code == 401
    cfg = requests.get(f"{base}/api/config", auth=ALICE).json()
    assert cfg["role"] == "member" and cfg["accounts"]
    # 成员不能管理成员
    assert requests.get(f"{base}/api/users", auth=ALICE).status_code == 403
    assert requests.post(f"{base}/api/users", json={"name": "x", "password": "12345678"}, auth=ALICE).status_code == 403
    # 管理员加人
    r = requests.post(f"{base}/api/users", json={"name": "小张", "password": "zhang-pass-1"}, auth=ADMIN)
    assert r.status_code == 201
    assert len(requests.get(f"{base}/api/users", auth=ADMIN).json()) == 4
    # 成员改自己的密码可以，改角色/改别人不行
    assert requests.patch(f"{base}/api/users/小王", json={"role": "admin"}, auth=ALICE).status_code == 403
    assert requests.patch(f"{base}/api/users/小李", json={"password": "hacked-123"}, auth=ALICE).status_code == 403
    assert requests.patch(f"{base}/api/users/小王", json={"password": "new-pass-99"}, auth=ALICE).status_code == 200
    assert requests.get(f"{base}/api/config", auth=u("小王", "new-pass-99")).status_code == 200
    # 停用后无法登录
    requests.patch(f"{base}/api/users/小李", json={"disabled": True}, auth=ADMIN)
    assert requests.get(f"{base}/api/config", auth=BOB).status_code == 401


def test_only_owner_or_admin_can_edit(team_server):
    base, _ = team_server
    pid = requests.post(f"{base}/api/projects", data={"description": "小王的项目描述"}, auth=ALICE).json()["id"]
    p = wait(base, pid, ALICE)
    assert p["owner"] == "小王" and p["can_edit"]
    pb = requests.get(f"{base}/api/projects/{pid}", auth=BOB).json()
    assert pb["can_edit"] is False  # 其他成员能看不能改
    r = requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "images"}, auth=BOB)
    assert r.status_code == 403 and "小王" in r.json()["error"]
    shots = [{k: v for k, v in s.items() if k not in ("state", "index")} for s in p["shots"]]
    assert requests.put(f"{base}/api/projects/{pid}/script", json={**p["script"], "shots": shots}, auth=BOB).status_code == 403
    assert requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "images"}, auth=ADMIN).status_code == 202


def test_templates_and_assets(team_server, tmp_path):
    base, app = team_server
    img = tmp_path / "role.png"
    fake_download("x.png", img)
    # 上传素材
    with open(img, "rb") as f:
        r = requests.post(f"{base}/api/assets", data={"name": "店主小林", "kind": "角色"},
                          files={"file": ("role.png", f, "image/png")}, auth=ALICE)
    assert r.status_code == 201, r.text
    asset = r.json()
    assert requests.get(base + asset["url"], auth=BOB).content[:4] == b"\x89PNG"
    with open(img, "rb") as f:
        assert requests.post(f"{base}/api/assets", data={"name": "x", "kind": "别的"},
                             files={"file": ("a.png", f, "image/png")}, auth=ALICE).status_code == 400
    # 用素材新建项目，再另存为模板
    pid = requests.post(f"{base}/api/projects", data={"description": "用素材的项目", "asset_ids": str(asset["id"])},
                        auth=ALICE).json()["id"]
    p = wait(base, pid, ALICE)
    assert len(p["brief"]["refs"]) == 1 and p["brief"]["assets"][0]["name"] == "店主小林"
    r = requests.post(f"{base}/api/templates", json={"from_project": pid, "name": "咖啡店系列"}, auth=ALICE)
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert tpl["characters"][0]["name"] == "小林" and tpl["asset_ids"] == [asset["id"]]
    assert tpl["assets"][0]["name"] == "店主小林"
    # 改模板角色外观后套用：脚本里的同名角色外观被强制为模板版本
    team = app.state.team
    tpl2 = team.add_template({**tpl, "name": "咖啡店系列 v2",
                              "characters": [{"name": "小林", "appearance": "30 岁，长卷发，墨绿围裙"}]}, "小王")
    pid2 = requests.post(f"{base}/api/projects", data={"description": "第二集：开门营业", "template_id": str(tpl2["id"])},
                         auth=BOB).json()["id"]
    p2 = wait(base, pid2, BOB)
    assert p2["brief"]["template"]["name"] == "咖啡店系列 v2"
    assert p2["brief"]["style"] == tpl["style"]  # 未填风格时沿用模板
    assert p2["script"]["characters"][0]["appearance"] == "30 岁，长卷发，墨绿围裙"
    assert len(p2["brief"]["refs"]) == 1  # 模板带的素材作为参考图
    # 场景图存入素材库
    requests.post(f"{base}/api/projects/{pid2}/jobs", json={"kind": "images"}, auth=BOB)
    p2 = wait(base, pid2, BOB)
    r = requests.post(f"{base}/api/projects/{pid2}/save-asset",
                      json={"shot_id": p2["shots"][0]["id"], "candidate": 2, "kind": "场景"}, auth=BOB)
    assert r.status_code == 201 and r.json()["source"] == f"project:{pid2}"
    # 删除权限
    assert requests.delete(f"{base}/api/assets/{asset['id']}", auth=BOB).status_code == 403
    assert requests.delete(f"{base}/api/templates/{tpl['id']}", auth=BOB).status_code == 403
    assert requests.delete(f"{base}/api/assets/{asset['id']}", auth=ALICE).status_code == 200
    assert requests.get(base + asset["url"], auth=ALICE).status_code == 404
    assert requests.delete(f"{base}/api/templates/{tpl['id']}", auth=ADMIN).status_code == 200
    # 参考图上限
    files = [("refs", (f"r{i}.png", img.read_bytes(), "image/png")) for i in range(5)]
    r = requests.post(f"{base}/api/projects", data={"description": "太多参考图了", "template_id": str(tpl2["id"])},
                      files=files, auth=ALICE)
    assert r.status_code == 400 and "最多 4 张" in r.json()["error"]


def test_team_monthly_budget(tmp_path):
    srv, th, base, app = start(tmp_path, team_budget_usd=0.001)
    try:
        pid = requests.post(f"{base}/api/projects", data={"description": "第一部短片描述"}, auth=ALICE).json()["id"]
        wait(base, pid, ALICE)  # 脚本花了一点钱，超过 0.001 美元
        d = requests.get(f"{base}/api/stats", auth=ALICE).json()
        assert d["month"]["team_budget_usd"] == 0.001 and d["month"]["warn"]
        r = requests.post(f"{base}/api/projects/{pid}/jobs", json={"kind": "images"}, auth=ALICE)
        assert r.status_code == 409 and "本月团队预算" in r.json()["error"]
        r = requests.post(f"{base}/api/projects", data={"description": "第二部短片描述"}, auth=ALICE)
        assert r.status_code == 409
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def test_shared_password_mode_still_works(tmp_path):
    app = create_app(web_settings(), data_dir=tmp_path / "d", factory=factory, workers=0, password="pw")
    assert not app.state.team.has_users()
