# -*- coding: utf-8 -*-
"""同步记录 / 来源标注 / 回收站 / 分类 / 批量修改 / 失败退避 / 自动备份 / 健康状态。"""
import copy, json, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp())

import pytest
from app.engine import Engine, BACKOFF
from app.backups import Backups
from app.store import Store
from tests.test_mirror import FakeQuark as MirrorQuark, DEST, TREE, mk, run
from tests.test_layout_aliases import make_client


class RecycleQuark(MirrorQuark):
    """在镜像用的内存夸克上加一个回收站。"""
    def __init__(self):
        super().__init__()
        self.bin = []          # [{record_id, fid, file_name, size}]
        self.removed = []

    def delete(self, fids):
        for f in fids:
            p = f[2:]
            if p in self.files:
                self.bin.append({"record_id": f"r{len(self.bin)}", "fid": f, "file_name": p.rsplit("/", 1)[1], "size": self.files[p]})
        super().delete(fids)

    def recycle_list(self, page=1, size=50):
        items = self.bin[(page - 1) * size: page * size]
        return items, len(self.bin)

    def recycle_remove(self, ids):
        self.removed += ids
        self.bin = [b for b in self.bin if b["record_id"] not in ids]


@pytest.fixture
def env():
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    eng = Engine(store)
    fq = RecycleQuark()
    eng.quark = lambda: fq
    return fq, eng


# ---------------------------------------------------------------- 同步记录 / 来源
def test_log_records_save_delete_and_source_link(env):
    fq, eng = env
    fq.shares["bbb"] = {"tree": {"x.mp4": 5}}
    t = run(eng, mk(env, {"a.mp4": 1, "b.mp4": 2, "c.mp4": 3}, links=("aaa", "bbb")))
    kinds = {(x["kind"], x["rel"], x["src"]) for x in eng.mirror.log_view(t)}
    assert ("save", "a.mp4", 1) in kinds and ("save", "x.mp4", 2) in kinds        # 来源：第 1 / 第 2 个链接
    del fq.shares["aaa"]["tree"]["a.mp4"]
    run(eng, t); run(eng, t)
    ev = eng.mirror.log_view(t)
    assert ev[0]["kind"] == "delete" and ev[0]["rel"] == "a.mp4" and ev[0]["src"] == 1 and ev[0]["size"] == 1


def test_pending_and_resolve_are_logged_and_pending_has_source(env):
    fq, eng = env
    t = run(eng, mk(env, {"a.mp4": 1, "b.mp4": 2, "c.mp4": 3, "d.mp4": 4}, {"dry_run": True}))
    del fq.shares["aaa"]["tree"]["a.mp4"]
    run(eng, t); run(eng, t); run(eng, t)
    assert [x["kind"] for x in eng.mirror.log_view(t)].count("pending") == 1          # 不重复刷屏
    assert eng.mirror.info(t)["pending"][0]["src"] == 1
    eng.mirror.resolve(t, "confirm")
    assert eng.mirror.log_view(t)[0]["kind"] == "delete" and eng.mirror.log_view(t)[0]["note"] == "你确认删除"


def test_errors_logged_once_and_log_is_capped(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.shares["aaa"]["status"], fq.shares["aaa"]["msg"] = 500, "timeout"
    run(eng, t); run(eng, t); run(eng, t)
    assert [x["kind"] for x in eng.mirror.log_view(t)].count("error") == 1
    st = eng.mirror.state(t)
    for i in range(400):
        eng.mirror._ev(st, "save", f"f{i}")
    assert len(st["log"]) == 300


# ---------------------------------------------------------------- 回收站
def test_trash_tracked_hint_and_clean_only_our_files(env):
    fq, eng = env
    t = run(eng, mk(env, {"a.mp4": 1 << 30, "b.mp4": 2, "c.mp4": 3, "d.mp4": 4, "e.mp4": 5}))
    del fq.shares["aaa"]["tree"]["a.mp4"]
    run(eng, t); run(eng, t)
    assert len(t["mstate"]["trash"]) == 1 and "1.0 GB" in eng.mirror.trash_hint()
    fq.bin.append({"record_id": "other", "fid": "P:/别的/文件", "file_name": "别人的", "size": 9})      # 回收站里别的文件
    assert eng.mirror.clean_trash(7) == 0                       # 还没放满 7 天
    t["mstate"]["trash"][0]["ts"] -= 8 * 86400
    assert eng.mirror.clean_trash(7) == 1
    assert [b["record_id"] for b in fq.bin] == ["other"]        # 只清了自己的，别人的没动
    assert t["mstate"]["trash"] == []


def test_trash_entry_dropped_when_already_gone_from_bin(env):
    fq, eng = env
    t = run(eng, mk(env, {"a.mp4": 1, "b.mp4": 2, "c.mp4": 3, "d.mp4": 4, "e.mp4": 5}))
    del fq.shares["aaa"]["tree"]["a.mp4"]
    run(eng, t); run(eng, t)
    fq.bin.clear()                                               # 你自己把回收站清了
    t["mstate"]["trash"][0]["ts"] -= 10 * 86400
    assert eng.mirror.clean_trash(7) == 0 and t["mstate"]["trash"] == []


def test_space_alert_mentions_trash(env):
    fq, eng = env
    t = run(eng, mk(env, {"a.mp4": 1 << 30, "b.mp4": 2, "c.mp4": 3, "d.mp4": 4, "e.mp4": 5}))
    del fq.shares["aaa"]["tree"]["a.mp4"]
    run(eng, t); run(eng, t)
    sent = []
    eng.maint._dingtalk = lambda title, text, kind="on_error": sent.append(text)
    eng.maint.alert_space("夸克网盘仅剩 1.0 GB")
    assert "回收站" in sent[0]


# ---------------------------------------------------------------- 分类 / 批量 / 退避
def test_category_and_batch_update(env):
    fq, eng = env
    a = eng.create_task({"kind": "monitor", "name": "A", "savepath": "/x/a", "links_text": "", "category": "  工具  "})
    b = eng.create_task({"kind": "monitor", "name": "B", "savepath": "/x/b", "links_text": ""})
    assert a["category"] == "工具" and b["category"] == ""
    ok, bad = eng.batch_update([a["id"], b["id"], "nope"], {"interval": 240, "after": "none", "category": "资料", "enabled": False,
                                                            "mirror": {"dry_run": False, "evil": 1}, "savepath": "/hack"})
    assert ok == 2 and not bad
    for t in (a, b):
        assert t["interval"] == 240 and t["after"] == "none" and t["category"] == "资料" and t["enabled"] is False
        assert t["savepath"] in ("/x/a", "/x/b")                 # 路径之类的不在批量修改范围内
    with pytest.raises(ValueError):
        eng.batch_update([a["id"]], {"savepath": "/hack"})


def test_batch_update_endpoint_and_validation():
    store, eng, c, h = make_client()
    t = eng.create_task({"kind": "monitor", "name": "A", "savepath": "/x/a", "links_text": ""})
    assert c.post("/api/tasks/batch-update", json={"ids": [], "changes": {"after": "none"}}, headers=h).status_code == 400
    r = c.post("/api/tasks/batch-update", json={"ids": [t["id"]], "changes": {"after": "none", "interval": 1}}, headers=h)
    assert r.status_code == 200 and r.get_json()["updated"] == 1 and t["after"] == "none" and t["interval"] >= 60   # 间隔被限制在下限


def test_failure_backoff_grows_then_resets(env):
    fq, eng = env
    t = eng.create_task({"kind": "monitor", "name": "A", "savepath": "/x/a", "links_text": "", "interval": 60})
    gaps = []
    for _ in range(7):
        eng._finish(t, {"status": "error", "msg": "x", "saved": []})
        gaps.append((t["next_run"] - time.time()) / 3600)
    assert [round(g) for g in gaps] == [1, 2, 3, 4, 6, 6, 6]           # 第 1 次失败照常，之后拉长，封顶 6 倍
    eng._finish(t, {"status": "nochange", "msg": "ok", "saved": []})
    assert t["fail_streak"] == 0 and round((t["next_run"] - time.time()) / 3600) == 1
    assert eng.public_task(t)["backoff"] == 1 and BACKOFF[0] == 1


def test_idle_does_not_reset_streak(env):
    fq, eng = env
    t = eng.create_task({"kind": "monitor", "name": "A", "savepath": "/x/a", "links_text": ""})
    eng._finish(t, {"status": "error", "msg": "x", "saved": []}); eng._finish(t, {"status": "idle", "msg": "", "saved": []})
    assert t["fail_streak"] == 1


# ---------------------------------------------------------------- 自动备份
def test_backups_rotate_list_and_name_validation():
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    store.data["auto"]["backup_keep"] = 3
    store.data["cookie"] = "SECRET"
    b = Backups(store)
    real, n = time.strftime, [0]                    # 别的测试把 sleep 改成空操作了，这里让文件名自己走时间
    def fake(fmt, *a):
        n[0] += 1
        return real(fmt, time.localtime(time.time() + n[0]))
    time.strftime = fake
    try:
        for i in range(5):
            b.snapshot("auto")
        b.snapshot("manual")
    finally:
        time.strftime = real
    names = [x["name"] for x in b.list() if x["kind"] == "auto"]
    assert len(names) == 3 and len(b.list()) == 4          # 手动的单独计数，不挤掉自动的
    return_names = names
    assert b.path(names[0]) and b.path("../config.json") is None and b.path("kuakego-auto-1.json") is None
    assert oct(os.stat(b.path(names[0])).st_mode & 0o777) == "0o600"


def test_backup_endpoints_and_restore_keeps_pre_copy():
    store, eng, c, h = make_client()
    store.path = os.path.join(tempfile.mkdtemp(), "c.json")
    eng.backups = Backups(store)
    eng.create_task({"kind": "monitor", "name": "旧任务", "savepath": "/x/a", "links_text": ""})
    name = c.post("/api/backups/now", headers=h).get_json()["name"]
    eng.create_task({"kind": "monitor", "name": "新任务", "savepath": "/x/b", "links_text": ""})
    assert len(store.data["tasks"]) == 2
    items = c.get("/api/backups", headers=h).get_json()["items"]
    assert items[0]["name"] == name and items[0]["tasks"] == 1
    assert c.get(f"/api/backups/{name}/download", headers=h).status_code == 200
    assert c.post("/api/backups/..%2Fx/restore", headers=h).status_code in (400, 404)
    r = c.post(f"/api/backups/{name}/restore", headers=h)
    assert r.status_code == 200 and [t["name"] for t in store.data["tasks"]] == ["旧任务"]
    assert any(x["kind"] == "pre" for x in eng.backups.list())          # 恢复前自动留了一份，能反悔
    assert c.post("/api/backups/kuakego-auto-19990101-000000.json/restore", headers=h).status_code == 404


def test_auto_backup_runs_once_a_day_and_can_be_turned_off():
    store, eng, c, h = make_client()
    store.path = os.path.join(tempfile.mkdtemp(), "c.json")
    eng.backups = Backups(store)
    eng.maint.auto_backup(); eng.maint.auto_backup()
    assert len(eng.backups.list()) == 1                                 # 限频：一天一份
    store.data["maint_state"].pop("backup")
    store.data["auto"]["backup_auto"] = False
    eng.maint.auto_backup()
    assert len(eng.backups.list()) == 1


# ---------------------------------------------------------------- 健康状态
def test_health_reports_states_without_triggering_anything():
    store, eng, c, h = make_client()
    store.path = os.path.join(tempfile.mkdtemp(), "c.json")
    store.data["cookie"] = ""
    store.data["emby"]["enabled"] = False
    store.data["smartstrm"].update(enabled=True, webhook="http://127.0.0.1:9/x")           # 没人监听的端口
    store.data["dingtalk"].update(enabled=True, webhook="https://oapi.dingtalk.com/x")
    from app import notify
    notify.send_dingtalk = lambda *a, **k: (_ for _ in ()).throw(AssertionError("健康检查不该真的发钉钉"))
    notify.trigger_smartstrm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("健康检查不该触发 SmartStrm"))
    r = c.post("/api/health", headers=h).get_json()
    st = {i["key"]: i for i in r["items"]}
    assert st["quark"]["state"] == "fail" and "还没有登录" in st["quark"]["msg"]
    assert st["emby"]["state"] == "off" and st["fnmedia"]["state"] == "off" and st["proxy"]["state"] == "off"
    assert st["dingtalk"]["state"] == "ok"
    assert st["smartstrm"]["state"] == "fail" and "连不上" in st["smartstrm"]["msg"]
    assert st["disk"]["state"] == "ok" and r["version"]


def test_settings_test_endpoint_still_works_after_refactor():
    store, eng, c, h = make_client()
    r = c.post("/api/settings/emby/test", json={"url": ""}, headers=h)
    assert r.status_code == 400 and "请填写地址" in r.get_json()["msg"]
