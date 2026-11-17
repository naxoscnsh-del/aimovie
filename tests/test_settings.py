"""模型与参数配置页：校验、保存、立即生效、恢复、持久化、权限。"""
import pytest
from starlette.testclient import TestClient

from test_portal import FakeHRBP, Resp
from test_web import factory, web_settings
from videogen import settings_schema as sch
from videogen.config import Settings
from web.app import create_app
from web.portal import PortalConfig


def app_for(tmp_path, **kw):
    return create_app(web_settings(), data_dir=tmp_path, factory=factory, workers=1, password="", **kw)


def test_parse_validation():
    P = lambda k, v: sch.parse(sch.BY_KEY[k], v)
    assert P("candidates_per_shot", "3") == 3 and P("budget_usd", "50.5") == 50.5
    assert P("seedance_durations", "10, 5，5") == [5, 10]
    assert P("seedance_generate_audio", "true") is True
    assert P("ark_base_url", "https://ark.example.com/api/v3/") == "https://ark.example.com/api/v3"
    for k, v in [("candidates_per_shot", 9), ("candidates_per_shot", "1.5"), ("budget_usd", "abc"), ("seedance_durations", "0,99"),
                 ("seedance_resolution", "4k"), ("ark_base_url", "http://evil.example.com"), ("ark_base_url", "ftp://x"),
                 ("seedance_model", "bad model!"), ("ark_api_key", "short"), ("ark_api_key", "has space 12345678")]:
        with pytest.raises(ValueError):
            P(k, v)


def test_settings_api_flow(tmp_path):
    c = TestClient(app_for(tmp_path))
    g = c.get("/api/settings").json()
    fields = {f["key"]: f for grp in g["groups"] for f in grp["fields"]}
    assert fields["budget_usd"]["value"] == 100.0 and not fields["budget_usd"]["overridden"]
    assert "value" in fields["ark_api_key"] and fields["ark_api_key"]["value"] == ""
    # 保存：立即生效；密钥只回显末 4 位，绝不回传明文
    r = c.put("/api/settings", json={"values": {"budget_usd": 60, "budget_warn_usd": 40, "candidates_per_shot": 3,
                                                  "ark_api_key": "ark-secret-key-1234", "pricing.seedance_per_second": 0.9}})
    assert r.status_code == 200
    f2 = {f["key"]: f for grp in r.json()["groups"] for f in grp["fields"]}
    assert f2["budget_usd"]["overridden"] and f2["ark_api_key"]["masked"] == "••••1234" and "ark-secret-key-1234" not in r.text
    cfg = c.get("/api/config").json()
    assert cfg["budget_usd"] == 60 and cfg["priced"] is True
    # 密钥留空 = 不修改；非法值整体拒绝且不落库
    assert c.put("/api/settings", json={"values": {"ark_api_key": ""}}).status_code == 200
    assert c.get("/api/settings").json()["groups"][0]["fields"][1]["masked"] == "••••1234"
    bad = c.put("/api/settings", json={"values": {"budget_usd": 70, "candidates_per_shot": 99}})
    assert bad.status_code == 400 and c.get("/api/config").json()["budget_usd"] == 60
    assert c.put("/api/settings", json={"values": {"budget_warn_usd": 500}}).status_code == 400  # 预警线 > 上限
    with pytest.raises(ValueError):  # 只填 Token 不填 AppID
        sch.cross_check({**sch.snapshot(Settings()), "tts_appid": "", "tts_token": "tok-12345678"})
    # 恢复：回到 .env / 默认
    r = c.put("/api/settings", json={"reset": ["budget_usd", "budget_warn_usd", "ark_api_key"]})
    f3 = {f["key"]: f for grp in r.json()["groups"] for f in grp["fields"]}
    assert not f3["budget_usd"]["overridden"] and c.get("/api/config").json()["budget_usd"] == 100.0
    assert not f3["ark_api_key"]["overridden"]


def test_settings_persist_across_restart(tmp_path):
    c = TestClient(app_for(tmp_path))
    assert c.put("/api/settings", json={"values": {"seedance_model": "ep-2025-abc", "seedance_durations": "5,10,15"}}).status_code == 200
    c2 = TestClient(app_for(tmp_path))  # 重启：从数据库恢复，叠在 .env 之上
    cfg = c2.get("/api/config").json()
    assert cfg["durations"] == [5, 10, 15]
    f = {f["key"]: f for grp in c2.get("/api/settings").json()["groups"] for f in grp["fields"]}
    assert f["seedance_model"]["value"] == "ep-2025-abc" and f["seedance_model"]["overridden"]


def test_settings_new_job_uses_latest(tmp_path):
    """改完参数后新任务立刻按新值运行：候选图数。"""
    import time
    with TestClient(app_for(tmp_path)) as c:
        assert c.put("/api/settings", json={"values": {"candidates_per_shot": 1}}).status_code == 200
        pid = c.post("/api/projects", data={"description": "社区咖啡店的清晨开门", "duration": "15"}).json()["id"]
        for _ in range(100):
            if c.get(f"/api/projects/{pid}").json()["script"]:
                break
            time.sleep(0.1)
        c.post(f"/api/projects/{pid}/jobs", json={"kind": "images"})
        for _ in range(100):
            d = c.get(f"/api/projects/{pid}").json()
            if not d["job"] and d["shots"] and d["shots"][0]["state"]["candidates"]:
                break
            time.sleep(0.1)
        assert all(len(sh["state"]["candidates"]) == 1 for sh in d["shots"])


def test_settings_admin_only(tmp_path):
    hr = FakeHRBP()
    hr.reply = Resp(200, {"name": "小林", "email": "lin@x", "systems": [{"key": "site-movie"}]})
    cfg = PortalConfig("https://hrbp.test", "https://portal.test", set(), 60)
    c = TestClient(app_for(tmp_path, base_path="/movie", portal_config=cfg, oidc_session=hr), follow_redirects=False)
    c.cookies.set("ni_portal", "opaque")
    assert c.get("/movie/api/settings").status_code == 403
    assert c.put("/movie/api/settings", json={"values": {"budget_usd": 1}}).status_code == 403
