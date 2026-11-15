"""写作提示词管理 + 门户成员登记 + 账号标识迁移。"""
import json

import pytest
from starlette.testclient import TestClient

from test_pipeline import FakeChat, SCRIPT
from test_portal import FakeHRBP, Resp, cookie
from test_web import factory, web_settings
from videogen.script import DEFAULT_GUIDE, FORMAT_PROMPT, generate_script, system_prompt
from videogen.config import Settings
from web.app import create_app
from web.portal import PortalConfig
from web.team import TeamError, TeamStore


def test_system_prompt_keeps_fixed_format():
    p = system_prompt("你是美食短视频编剧，语气俏皮。" * 3, [5, 10], "16:9")
    assert p.startswith("你是美食") and "只输出 JSON" in p and "5、10 秒" in p and "16:9" in p
    assert system_prompt("", [5], "9:16").startswith(DEFAULT_GUIDE)  # 空 = 默认
    assert "{durations}" not in p and "{ratio}" not in p


def test_generate_script_uses_guide():
    chat = FakeChat([json.dumps(SCRIPT, ensure_ascii=False)])
    s = Settings(); s.seedance_durations = [5, 10]
    brief = {"description": "咖啡店的早晨", "duration": 15, "ratio": "9:16", "guide": "你是治愈系绘本编剧，请多用留白镜头。"}
    generate_script(s, brief, client=chat)
    sys_msg = chat.calls[0]["messages"][0]["content"]
    assert sys_msg.startswith("你是治愈系绘本编剧") and "只输出 JSON" in sys_msg


def test_prompt_store(tmp_path):
    t = TeamStore(tmp_path / "app.db", tmp_path / "library")
    items = t.list_prompts()
    assert len(items) == 1 and items[0]["builtin"] and items[0]["content"] == DEFAULT_GUIDE
    a = t.add_prompt("搞笑短剧", "你是搞笑短剧编剧，每个镜头都要有一个反转或包袱，节奏快。", "小林")
    with pytest.raises(TeamError):
        t.add_prompt("搞笑短剧", "你是搞笑短剧编剧，每个镜头都要有一个反转或包袱，节奏快。", "小林")
    with pytest.raises(TeamError):
        t.add_prompt("太短", "短", "小林")
    t.update_prompt(a["id"], None, "你是搞笑短剧编剧，包袱要在最后一镜揭晓，前面层层铺垫。", "小王")
    vs = t.prompt_versions(a["id"])
    assert len(vs) == 2 and vs[0]["by"] == "小王"
    for i in range(25):
        t.update_prompt(a["id"], None, f"你是搞笑短剧编剧，第 {i} 版：包袱要在最后一镜揭晓。", "小王")
    assert len(t.prompt_versions(a["id"])) == 20  # 只留最近 20 版
    builtin = items[0]["id"]
    with pytest.raises(TeamError):
        t.delete_prompt(builtin)
    assert t.update_prompt(builtin, "改名无效", "你是短视频编剧兼分镜师，写得越具体越好，镜头要连贯。", "x")["name"] == "默认"
    t.delete_prompt(a["id"])
    assert t.get_prompt(a["id"]) is None


def test_prompt_api_and_project(tmp_path):
    app = create_app(web_settings(), data_dir=tmp_path, factory=factory, workers=1, password="")
    c = TestClient(app)
    r = c.get("/api/prompts").json()
    assert r["items"][0]["builtin"] and "只输出 JSON" in r["fixed"] and r["default_content"] == DEFAULT_GUIDE
    r = c.post("/api/prompts", json={"name": "治愈绘本", "content": "你是治愈系绘本编剧，请多用留白镜头，节奏舒缓。"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert c.post("/api/prompts", json={"name": "x", "content": "短"}).status_code == 400
    r = c.put(f"/api/prompts/{pid}", json={"content": "你是治愈系绘本编剧，请多用留白镜头，节奏舒缓，结尾温暖。"})
    assert r.status_code == 200 and c.get(f"/api/prompts/{pid}/versions").json()[0]["content"].endswith("结尾温暖。")
    # 用它新建短片：brief 里记录提示词，写作内容随之带入
    r = c.post("/api/projects", data={"description": "社区咖啡店的清晨开门", "ratio": "9:16", "duration": "15", "prompt_id": str(pid)})
    assert r.status_code == 201
    b = r.json()["brief"]
    assert b["prompt_id"] == pid and b["prompt_name"] == "治愈绘本" and b["guide"].endswith("结尾温暖。")
    assert c.post("/api/projects", data={"description": "社区咖啡店的清晨开门", "prompt_id": "999"}).status_code == 400
    assert c.delete(f"/api/prompts/{pid}").status_code == 200
    assert c.delete(f"/api/prompts/{c.get('/api/prompts').json()['items'][0]['id']}").status_code == 400


def make_portal(tmp_path, admins=()):
    hr = FakeHRBP()
    cfg = PortalConfig("https://hrbp.test", "https://portal.test", set(admins), 60)
    app = create_app(web_settings(), data_dir=tmp_path, factory=factory, workers=1, password="",
                     base_path="/movie", portal_config=cfg, oidc_session=hr)
    return TestClient(app, follow_redirects=False), hr


def test_portal_records_members_and_migrates_id(tmp_path):
    c, hr = make_portal(tmp_path, admins={"小林"})
    c.cookies.set("ni_portal", "opaque")
    hr.reply = Resp(200, {"name": "小林", "email": "lin@x", "systems": [{"key": "site-movie"}]})
    assert c.get("/movie/api/config").json()["role"] == "admin"
    users = c.get("/movie/api/sso-users").json()
    assert [(u["sub"], u["name"]) for u in users] == [("lin@x", "小林")]
    # HRBP 开始返回稳定 ID：同一个人并到新 ID 下，不出现重复成员
    store = TeamStore(tmp_path / "again.db", tmp_path / "lib2")
    store.sso_touch("lin@x", "小林", "lin@x")
    store.sso_set_role("lin@x", "admin")
    store.sso_touch("cuid123", "小林", "lin@x")
    rows = store.sso_list()
    assert [(r["sub"], r["role"]) for r in rows] == [("cuid123", "admin")]


def test_style_combo_binds_prompt(tmp_path):
    """风格组合 = 提示词 + 风格 + 角色 + 画幅时长 + 素材：从项目另存后，套用时自动带上提示词。"""
    app = create_app(web_settings(), data_dir=tmp_path, factory=factory, workers=1, password="")
    with TestClient(app) as c:
        _combo(c)


def _combo(c):
    pid = c.post("/api/prompts", json={"name": "治愈绘本", "content": "你是治愈系绘本编剧，请多用留白镜头，节奏舒缓。"}).json()["id"]
    r = c.post("/api/projects", data={"description": "社区咖啡店的清晨开门", "duration": "15", "prompt_id": str(pid)})
    proj = r.json()["id"]
    import time
    for _ in range(100):  # 等脚本任务跑完才能另存
        if c.get(f"/api/projects/{proj}").json()["script"]:
            break
        time.sleep(0.1)
    t = c.post("/api/templates", json={"from_project": proj, "name": "绘本风"})
    assert t.status_code == 201 and t.json()["prompt_id"] == pid and t.json()["prompt_name"] == "治愈绘本"
    # 只选组合、不单独指定提示词：自动用组合绑定的
    r = c.post("/api/projects", data={"description": "雨天的书店老板", "duration": "15", "template_id": str(t.json()["id"])})
    assert r.status_code == 201 and r.json()["brief"]["prompt_id"] == pid and r.json()["brief"]["guide"].startswith("你是治愈系绘本编剧")
    # 提示词被删除：组合仍可用，退回默认
    c.delete(f"/api/prompts/{pid}")
    r = c.post("/api/projects", data={"description": "雨天的书店老板", "duration": "15", "template_id": str(t.json()["id"])})
    assert r.status_code == 201 and "guide" not in r.json()["brief"]
