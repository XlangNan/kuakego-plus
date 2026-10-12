# -*- coding: utf-8 -*-
"""维护功能：失效链接清理、完结自动停止、日报、洗版画质判断、备份恢复、批量粘贴、转存记录。"""
import json
import time
from unittest import mock

from app.maintenance import title_res_rank
from tests.test_layout_aliases import make_client

LINK = "https://pan.quark.cn/s/{}"


def task(eng, **kw):
    d = {"name": "某剧", "savepath": "/剧/某剧", "links_text": "", "kind": "subscription", "tmdb_id": 7, "season": 1}
    d.update(kw)
    d.setdefault("savepath", "/剧/" + d["name"])
    if kw.get("name") is None and "savepath" not in kw:
        d["savepath"] = "/剧/%s-%s" % (d["name"], d["tmdb_id"] + len(eng.store.data["tasks"]))
    else:
        d["savepath"] = "/剧/%s-%d" % (d["name"], len(eng.store.data["tasks"]))
    return eng.create_task(d)


def test_purge_dead_links_by_age_and_stamp():
    store, eng, c, h = make_client()
    t = task(eng)
    t["links"] = [{"url": LINK.format("a"), "pwd_id": "a", "ban": "失效", "ban_ts": int(time.time()) - 5 * 86400},
                  {"url": LINK.format("b"), "pwd_id": "b", "ban": "失效"},                         # 老数据：没记时间
                  {"url": LINK.format("c"), "pwd_id": "c", "ban": ""}]
    assert eng.maint.purge_dead_links() == 1
    assert [l["pwd_id"] for l in t["links"]] == ["b", "c"] and t["links"][0]["ban_ts"]            # b 只补时间戳，还没到期
    store.data["maint_state"]["purge"] = 0
    store.data["auto"]["purge_dead_days"] = 0
    assert eng.maint.purge_dead_links() == 0                                                      # 0 = 不自动清理
    assert c.post("/api/maintenance/purge-dead", headers=h).get_json()["removed"] == 1           # 手动清理：不管多久
    assert [l["pwd_id"] for l in t["links"]] == ["c"]


def test_auto_finish_only_when_complete_and_ended():
    class FT:
        ready = True
        def __init__(self, cfg=None): pass
        def detail(self, tid):
            return {"status": {7: "Ended", 8: "Returning Series", 9: "Returning Series"}[tid],
                    "seasons": [{"season_number": 1}] + ([{"season_number": 2}] if tid == 9 else [])}
    store, eng, c, h = make_client()
    eng.tmdb = lambda: FT()
    done_snap = {"have": 8, "aired": 8, "total": 8, "missing": [], "missing_n": 0, "ts": 1}
    a, b, d, e = task(eng, tmdb_id=7), task(eng, name="在播", tmdb_id=8), task(eng, name="有下一季", tmdb_id=9), task(eng, name="缺集", tmdb_id=7)
    for t in (a, b, d):
        t["snap"] = dict(done_snap)
    e["snap"] = {**done_snap, "have": 6, "missing": [7, 8], "missing_n": 2}
    assert eng.maint.auto_finish() == ["某剧", "有下一季"]
    assert a["done"] and d["done"] and not b.get("done") and not e.get("done")
    assert eng.public_task(a)["status"] == "done" and not eng.is_due(a)                           # 停止后不再被调度
    eng.update_task(a["id"], {"done": False})
    assert not a["done"]                                                                           # 可以恢复


def test_title_resolution_rank():
    assert title_res_rank("某剧 4K 60fps") == 3 and title_res_rank("某剧 2160p") == 3
    assert title_res_rank("某剧 1080P 内封") == 2 and title_res_rank("某剧 720p") == 1 and title_res_rank("某剧") == 0


def test_upgrade_hint_finds_only_higher_resolution():
    store, eng, c, h = make_client()
    t = task(eng, upgrade_watch=True)
    t["snap"] = {"have": 8, "aired": 8, "total": 8, "missing": [], "missing_n": 0, "ts": 1}
    eng.maint.current_res = lambda task: 2           # 现有 1080P
    results = [{"title": "某剧 4K 全集", "url": LINK.format("n1"), "pwd_id": "n1"},
               {"title": "某剧 1080P", "url": LINK.format("n2"), "pwd_id": "n2"},
               {"title": "别的剧 4K", "url": LINK.format("n3"), "pwd_id": "n3"}]
    eng._search_by_names = lambda *a, **k: results
    eng.pansou = lambda: object()
    out = eng.maint.check_upgrades()
    assert len(out) == 1 and [i["url"] for i in t["upgrade"]["items"]] == [LINK.format("n1")]
    assert "4K" in t["upgrade"]["msg"] or "更高" in t["upgrade"]["msg"]
    assert eng.maint.check_upgrades() == []          # 一天只查一次
    r = c.post(f"/api/tasks/{t['id']}/upgrade-check", headers=h)                                   # 手动检查不受限
    assert r.status_code == 200 and r.get_json()["checked"]


def test_missing_retry_shortens_find_interval():
    store, eng, c, h = make_client()
    t = task(eng, auto_find=True)
    store.data["pansou"]["auto_interval"] = 6
    t["snap"] = {"have": 5, "aired": 8, "total": 8, "missing": [6, 7, 8], "missing_n": 3, "ts": 1}
    eng._finish_find(t, {"added": [], "msg": "没有找到", "lines": []})
    assert t["find"]["next"] - time.time() < 2 * 3600 + 700
    t["snap"]["missing_n"] = 0
    eng._finish_find(t, {"added": [], "msg": "x", "lines": []})
    assert t["find"]["next"] - time.time() > 5 * 3600


def test_digest_text_and_settings():
    class FT:
        ready = True
        def season_episodes(self, tid, season):
            from datetime import date
            return [{"ep": 3, "name": "", "air_date": date.today().isoformat()}]
    store, eng, c, h = make_client()
    eng.tmdb = lambda: FT()
    t = task(eng)
    t["snap"] = {"have": 2, "aired": 3, "total": 8, "missing": [3], "missing_n": 1, "ts": 1}
    store.data["recent"].insert(0, {"ts": int(time.time()), "task_id": t["id"], "name": "某剧", "kind": "subscription", "season": 1, "count": 2, "eps": "E01-E02", "files": []})
    text = eng.maint.build_digest()
    assert "某剧" in text and "E01-E02" in text and "第 3 集（待转存）" in text and "缺" in text
    r = c.put("/api/settings/auto", json={"digest": True, "digest_hour": 7, "purge_dead_days": 5}, headers=h).get_json()
    assert r["digest"] is True and r["digest_hour"] == 7 and r["purge_dead_days"] == 5
    store.data["dingtalk"].update(enabled=True, webhook="http://x")
    sent = []
    with mock.patch("app.notify.send_dingtalk", lambda cfg, title, text, force=False: (sent.append(title), (True, "ok"))[1]):
        assert eng.maint.daily_digest() and sent == ["追剧日报"]
        assert eng.maint.daily_digest() is None          # 一天只发一次


def test_backup_and_restore_roundtrip():
    store, eng, c, h = make_client()
    t = task(eng)
    store.data["cookie"] = "ck=1"
    r = c.get("/api/backup", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    blob = r.get_data()
    assert json.loads(blob)["tasks"][0]["id"] == t["id"]
    eng.delete_task(t["id"]); store.data["cookie"] = ""
    r = c.post("/api/restore", data=blob, headers=h)
    assert r.get_json()["tasks"] == 1 and store.data["cookie"] == "ck=1" and store.data["tasks"][0]["id"] == t["id"]
    assert c.post("/api/restore", data=b"not json", headers=h).status_code == 400
    assert c.post("/api/restore", data=b'{"foo": 1}', headers=h).status_code == 400


def test_batch_inspect_guesses_names_and_reports_dead():
    store, eng, c, h = make_client()
    def fake(url, season=1, *a):
        if "dead" in url:
            return {"ok": False, "error": "分享已失效"}
        return {"ok": True, "videos": 8, "eps": "E01-E08", "quality": ["1080P"], "size_gb": 12.3, "sample": ["x.mkv"]}
    eng.inspect_share = fake
    text = "庆余年 第二季\nhttps://pan.quark.cn/s/abc123 提取码：x1y2\n我的大叔 https://pan.quark.cn/s/dead99\n"
    r = c.post("/api/batch/inspect", json={"text": text}, headers=h)
    items = r.get_json()["items"]
    assert [i["ok"] for i in items] == [True, False] and items[0]["name"] == "庆余年 第二季" and items[1]["name"] == "我的大叔"
    assert items[1]["error"] == "分享已失效" and items[0]["eps"] == "E01-E08"
    assert c.post("/api/batch/inspect", json={"text": "没有链接"}, headers=h).status_code == 400


def test_transfers_endpoint_filters_by_task():
    store, eng, c, h = make_client()
    store.data["recent"] = [{"ts": 2, "task_id": "a", "name": "A", "count": 1}, {"ts": 1, "task_id": "b", "name": "B", "count": 2}]
    assert len(c.get("/api/transfers", headers=h).get_json()["items"]) == 2
    assert [x["name"] for x in c.get("/api/transfers?task_id=b", headers=h).get_json()["items"]] == ["B"]
