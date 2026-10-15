# -*- coding: utf-8 -*-
"""多套 SmartStrm：各自通知、各自刷新对应的服务器、按夸克目录前缀分流。"""
from unittest import mock

from app import notify
from tests.test_layout_aliases import make_client


class Cli:
    def __init__(self): self.refreshed = []
    def find_series(self, n, t=None): return None, None
    def find_movie(self, n, t=None, y=None): return None, None
    def refresh(self, sid=None): self.refreshed.append(sid)


def setup(insts):
    store, eng, c, h = make_client()
    e, f = Cli(), Cli()
    eng.emby_only = lambda: e; eng.fn = lambda: f; eng.emby = lambda: e   # 旧行为路径用 emby()
    store.data["emby"]["refresh_delay"] = 0
    store.data["smartstrm"].update(insts[0])
    store.data["smartstrm_more"]["items"] = insts[1:]
    return store, eng, c, h, e, f


def run(eng, savepath):
    calls = []
    def fake(cfg, sp, force=False):
        calls.append((cfg["webhook"], sp)); return True, "ok"
    task = {"name": "剧", "savepath": savepath, "media": "tv", "season": 1}
    with mock.patch.object(notify, "trigger_smartstrm", fake):
        eng._after_save(task, [{"name": "a"}])
    return calls


def inst(wh, target="", prefix="", enabled=True):
    return {"enabled": enabled, "webhook": wh, "delay": 0, "target": target, "path_prefix": prefix, "event": "qas_strm"}


def test_same_folder_both_notified_and_refreshed():
    store, eng, c, h, e, f = setup([inst("http://a", "emby"), inst("http://b", "fn")])
    calls = run(eng, "/媒体库/剧集/x")
    assert sorted(w for w, _ in calls) == ["http://a", "http://b"]
    assert e.refreshed == [None] and f.refreshed == [None]


def test_different_folders_route_by_prefix():
    store, eng, c, h, e, f = setup([inst("http://a", "emby", "/emby目录"), inst("http://b", "fn", "/飞牛目录")])
    calls = run(eng, "/飞牛目录/剧")
    assert [w for w, _ in calls] == ["http://b"] and e.refreshed == [] and f.refreshed == [None]
    calls = run(eng, "/emby目录/剧")
    assert [w for w, _ in calls] == ["http://a"] and e.refreshed == [None]


def test_unbound_keeps_old_behaviour_and_disabled_skipped():
    store, eng, c, h, e, f = setup([inst("http://a"), inst("http://b", enabled=False)])
    calls = run(eng, "/x/y")
    assert [w for w, _ in calls] == ["http://a"] and e.refreshed == [None]


def test_extra_api_keeps_secret_and_validates():
    store, eng, c, h = make_client()
    r = c.put("/api/smartstrm/extra", json={"items": [{"name": "飞牛", "enabled": True, "webhook": "http://x/hook",
              "target": "fn", "path_prefix": "/a/，/b/", "delay": 999}]}, headers=h).get_json()["items"][0]
    assert r["webhook"] == "" and r["webhook_set"] and r["target"] == "fn" and r["delay"] == 60 and r["path_prefix"] == "/a,/b"
    r2 = c.put("/api/smartstrm/extra", json={"items": [dict(r, name="改名", target="bad")]}, headers=h).get_json()["items"][0]
    assert r2["webhook_set"] and r2["name"] == "改名" and r2["target"] == ""
    assert store.data["smartstrm_more"]["items"][0]["webhook"] == "http://x/hook"
