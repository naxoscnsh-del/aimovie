"""P7：打包下载、回收站清理。"""
import time
import zipfile
import io

from starlette.testclient import TestClient

from test_web import factory, web_settings
from web.app import create_app
from web.cleanup import purge_trash


def make(tmp_path):
    app = create_app(web_settings(), data_dir=tmp_path, factory=factory, workers=1, password="")
    return TestClient(app)


def test_purge_trash(tmp_path):
    t = tmp_path / "trash"; t.mkdir()
    old = time.strftime("%Y%m%d%H%M%S", time.localtime(time.time() - 40 * 86400))
    new = time.strftime("%Y%m%d%H%M%S", time.localtime(time.time() - 2 * 86400))
    for n in (f"a__{old}__x", f"b__{new}__x"):
        (t / n).mkdir(); (t / n / "state.json").write_text("{}")
    assert purge_trash(t, 30) == 1
    assert [d.name for d in t.iterdir()] == [f"b__{new}__x"]
    assert purge_trash(t, 0) == 0  # 0 = 关闭


def test_package_requires_final(tmp_path):
    c = make(tmp_path)
    r = c.post("/api/projects", data={"description": "社区咖啡店的清晨开门", "ratio": "9:16", "duration": "15"})
    pid = r.json()["id"]
    assert c.get(f"/api/projects/{pid}/package").status_code == 400
    assert c.get("/api/projects/nope/package").status_code in (404,)
