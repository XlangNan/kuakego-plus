# -*- coding: utf-8 -*-
import base64, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from tests.test_engine import env


def mk(eng, name, path, **kw):
    return eng.create_task({"name": name, "savepath": path, "links_text": "", "kind": "subscription", **kw})


def test_recent_dirs_ordering_dedupe_cap_and_root():
    store, eng = env()
    mk(eng, "A", "/影视/剧集/A"); mk(eng, "B", "/影视/剧集/B")
    assert [d["path"] for d in store.data["recent_dirs"]] == ["/影视/剧集/B", "/影视/剧集/A"]
    mk(eng, "A2", "影视//剧集/A/")                                         # 同一目录换个写法：去重并提到最前
    assert [d["path"] for d in store.data["recent_dirs"]][:2] == ["/影视/剧集/A", "/影视/剧集/B"] and len(store.data["recent_dirs"]) == 2
    assert store.data["recent_dirs"][0]["name"] == "A2"
    eng.create_task({"name": "根", "savepath": "", "links_text": ""})        # 根目录不记
    assert all(d["path"] != "/" for d in store.data["recent_dirs"])
    for i in range(20): mk(eng, f"X{i}", f"/d/{i}")
    assert len(store.data["recent_dirs"]) == 12 and store.data["recent_dirs"][0]["path"] == "/d/19"


def test_editing_savepath_records_new_dir_only_when_changed():
    store, eng = env()
    t = mk(eng, "A", "/a")
    eng.update_task(t["id"], {"name": "A", "savepath": "/a"})                # 没改目录：不动
    assert [d["path"] for d in store.data["recent_dirs"]] == ["/a"]
    eng.update_task(t["id"], {"savepath": "/b"})
    assert [d["path"] for d in store.data["recent_dirs"]] == ["/b", "/a"]


def test_two_different_shows_cannot_share_a_directory():
    store, eng = env()
    mk(eng, "庆余年", "/影视/庆余年", tmdb_id=1, season=1)
    try: mk(eng, "繁花", "/影视/庆余年", tmdb_id=2); assert False
    except ValueError as e: assert "《庆余年》" in str(e) and "混在一起" in str(e)
    mk(eng, "庆余年", "/影视/庆余年", tmdb_id=1, season=2)                    # 同一部剧的不同季，放一起没问题
    t = mk(eng, "繁花", "/影视/繁花", tmdb_id=2)
    try: eng.update_task(t["id"], {"savepath": "/影视/庆余年"}); assert False   # 改目录时同样拦截
    except ValueError: pass
    assert t["savepath"] == "/影视/繁花"
    m = eng.create_task({"name": "随便", "savepath": "/影视/庆余年", "links_text": ""})   # 分享监控不知道是哪部剧：不拦
    assert m["kind"] == "monitor"


def test_api_recent_dirs_and_clash():
    os.environ["WEBUI_PASSWORD"] = "pw"
    from unittest import mock
    from app.main import create_app
    from app.tmdb import Tmdb
    store, eng = env()
    store.data["tmdb"]["api_key"] = "k"
    c = create_app(store, eng).test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    mk(eng, "A", "/x/A"); mk(eng, "B", "/x/B")
    assert [d["path"] for d in c.get("/api/recent-dirs", headers=h).get_json()["dirs"]] == ["/x/B", "/x/A"]
    c.delete("/api/recent-dirs?path=/x/B", headers=h)
    assert [d["path"] for d in c.get("/api/recent-dirs", headers=h).get_json()["dirs"]] == ["/x/A"]
    c.delete("/api/recent-dirs", headers=h)
    assert c.get("/api/recent-dirs", headers=h).get_json()["dirs"] == [] and c.get("/api/recent-dirs").status_code == 401
    mk(eng, "老剧", "/x/同目录", tmdb_id=5)
    detail = {"id": 7, "name": "新剧", "poster_url": "", "overview": "", "first_air_date": "", "vote_average": 0, "seasons": []}
    with mock.patch.object(Tmdb, "detail", return_value=detail), mock.patch.object(eng, "auto_find"), mock.patch.object(eng, "run_task"):
        r = c.post("/api/subscriptions", json={"tmdb_id": 7, "season": 1, "savepath": "/x/同目录", "links_text": ""}, headers=h)
    assert r.status_code == 400 and "《老剧》" in r.get_json()["error"]
    assert not any(t.get("tmdb_id") == 7 for t in store.data["tasks"])


def test_existing_tasks_seed_recent_dirs_once():
    """升级前就有订阅的老用户：第一次启动时把已有任务的目录补进去（新建的排前面），清除后不再冒出来。"""
    import tempfile
    from app.engine import Engine
    from app.store import Store
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    base = {"links": [], "kind": "subscription", "season": 1}
    store.data["tasks"] = [
        {**base, "id": "a", "name": "兰香如故", "savepath": "/测试媒体库/剧集/兰香如故", "created": 100},
        {**base, "id": "b", "name": "长相思", "savepath": "/测试媒体库/剧集/长相思/", "created": 300},
        {**base, "id": "c", "name": "同目录", "savepath": "/测试媒体库//剧集/兰香如故", "created": 200},     # 重复目录：只记一次
        {**base, "id": "d", "name": "根", "savepath": "", "created": 400}]                                   # 根目录不记
    assert store.data["recent_dirs"] == [] and not store.data["recent_dirs_seeded"]
    eng = Engine(store)
    assert [d["path"] for d in store.data["recent_dirs"]] == ["/测试媒体库/剧集/长相思", "/测试媒体库/剧集/兰香如故"]
    assert store.data["recent_dirs"][0]["name"] == "长相思" and store.data["recent_dirs_seeded"]
    store.data["recent_dirs"] = []                                                    # 用户点了「清除记录」
    Engine(store)
    assert store.data["recent_dirs"] == []                                            # 重启后不会再冒出来
    reloaded = Store(store.path)                                                      # 配置文件里持久化了
    assert reloaded.data["recent_dirs_seeded"] is True
