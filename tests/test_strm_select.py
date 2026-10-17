# -*- coding: utf-8 -*-
"""创建任务时勾选 SmartStrm 任务：只触发选中的；SmartStrm 可限定适用网盘；目录浏览按网盘。"""
from unittest import mock

from app import notify
from tests.test_layout_aliases import make_client
from tests.test_smartstrm_multi import Cli, inst


def setup(insts):
    store, eng, c, h = make_client()
    e, f = Cli(), Cli()
    eng.emby_only = lambda: e; eng.fn = lambda: f; eng.emby = lambda: e
    store.data["emby"]["refresh_delay"] = 0
    store.data["smartstrm"].update(insts[0])
    store.data["smartstrm_more"]["items"] = [dict(i, id="i%d" % n) for n, i in enumerate(insts[1:], 1)]
    return store, eng, c, h


def run(eng, **task):
    calls = []
    def fake(cfg, sp, force=False, **kw):
        calls.append((cfg["webhook"], cfg.get("strmtask", ""))); return True, "ok"
    t = {"name": "剧", "savepath": "/x/剧", "media": "tv", "season": 1, **task}
    with mock.patch.object(notify, "trigger_smartstrm", fake):
        eng._after_save(t, [{"name": "a"}])
    return calls


def test_items_split_by_task_name_and_filter_by_drive():
    store, eng, c, h = setup([dict(inst("http://a"), strmtask="电影,剧集", name="夸克那台", drive="quark"),
                              dict(inst("http://b"), name="迅雷那台", drive="xunlei"),
                              dict(inst("http://c"), name="通用", strmtask="t1")])
    allx = notify.strm_items(store.data)
    assert [i["key"] for i in allx] == ["main|电影", "main|剧集", "i1", "i2|t1"] and allx[0]["label"] == "夸克那台：电影"
    assert [i["key"] for i in notify.strm_items(store.data, "xunlei")] == ["i1", "i2|t1"]      # 夸克专用的不出现
    assert [i["key"] for i in notify.strm_items(store.data, "quark")] == ["main|电影", "main|剧集", "i2|t1"]
    assert c.get("/api/smartstrm/items?drive=xunlei", headers=h).get_json()["items"][0]["label"] == "迅雷那台"


def test_only_selected_tasks_are_triggered():
    store, eng, c, h = setup([dict(inst("http://a"), strmtask="电影,剧集"), inst("http://b"), dict(inst("http://c"), strmtask="t1")])
    assert sorted(run(eng, strm=["main|剧集", "i2|t1"])) == [("http://a", "剧集"), ("http://c", "t1")]      # 只触发选中的任务，不是整套
    assert run(eng, strm=["i1"]) == [("http://b", "")]
    assert run(eng, strm=[]) == []                                                                            # 一个都没勾：不通知 SmartStrm


def test_legacy_task_uses_drive_filter_and_none_mode_wins():
    store, eng, c, h = setup([dict(inst("http://a"), drive="quark"), dict(inst("http://b"), drive="xunlei")])
    assert [w for w, _ in run(eng, drive="xunlei")] == ["http://b"]          # 没勾选过的旧任务：迅雷任务只通知迅雷那套
    assert [w for w, _ in run(eng)] == ["http://a"]
    assert run(eng, strm=["main"], after="none") == []


def test_strm_field_kept_on_edit_and_clean_ss_drive():
    store, eng, c, h = setup([inst("http://a")])
    t = eng.create_task({"kind": "monitor", "name": "A", "savepath": "/x/a", "links_text": "", "strm": ["main"]})
    assert t["strm"] == ["main"]
    eng.update_task(t["id"], {"interval": 120})
    assert t["strm"] == ["main"]
    eng.update_task(t["id"], {"strm": None})
    assert t["strm"] is None
    assert notify.clean_ss({"drive": "xunlei", "webhook": "http://x"})["drive"] == "xunlei" and notify.clean_ss({"drive": "bad"})["drive"] == ""


def test_browse_uses_the_chosen_drive():
    store, eng, c, h = make_client()
    class Q:
        def ls_dir(self, fid): return [{"fid": "q1", "file_name": "夸克目录", "dir": True}]
        def ensure_dir(self, p): return "qq"
    class X:
        def ls_dir(self, fid): return [{"fid": "x1", "file_name": "迅雷目录", "dir": True}, {"fid": "f", "file_name": "a.mp4", "dir": False}]
        def ensure_dir(self, p): return "xx"
    eng.quark = lambda: Q(); eng.xunlei = lambda: X()
    assert c.get("/api/browse?fid=0", headers=h).get_json()["dirs"][0]["name"] == "夸克目录"
    assert c.get("/api/browse?fid=0&drive=xunlei", headers=h).get_json()["dirs"] == [{"fid": "x1", "name": "迅雷目录"}]
    assert c.post("/api/browse/mkdir", json={"path": "/a/b", "drive": "xunlei"}, headers=h).get_json()["fid"] == "xx"


def test_xunlei_no_savepath():
    from unittest import mock as _m
    sent = []
    class R:
        status_code = 200
        text = '{"success":true}'
        def raise_for_status(self): pass
        def json(self): return {"success": True}
    with _m.patch.object(notify.requests, "post", lambda url, json=None, timeout=0: (sent.append(json), R())[1]):
        notify.trigger_smartstrm({"enabled": True, "webhook": "http://x", "strmtask": "ceshi xunlei, b"}, "/a/b", drive="xunlei")
        notify.trigger_smartstrm({"enabled": True, "webhook": "http://x", "strmtask": "ceshi kuake"}, "/a/b", drive="quark")
    assert sent[0] == {"event": "a_task", "task": {"name": "ceshi xunlei", "storage_path": "/a/b"}} and sent[1]["task"]["name"] == "b"
    assert sent[2]["savepath"] == "/a/b" and sent[2]["strmtask"] == "ceshi kuake" and sent[2]["event"] == "qas_strm"
