# -*- coding: utf-8 -*-
"""复现"库里已有 8 集，却又把前 8 集转存了一遍"，并确认诊断能指出原因。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tests.test_engine as T
from tests.test_engine import env, mk, FakeQuark, FakeEmby as FakeEmby_
from app.emby import Emby

SHARE = {"aaa": {"files": [f"庆余年.第{i:02d}集.mp4" for i in range(1, 11)]}}  # 分享里有 1-10，文件名没写季


def test_symptom_season_mismatch_resaves_first_8():
    store, eng = env()
    eng._emby.have = {(2, i) for i in range(1, 9)}         # Emby：第 2 季 1-8 集
    fq, t = mk(eng, SHARE, ["aaa"])                         # 任务季号默认 1
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 10                              # ← 用户遇到的症状：全部重转
    pv = eng.preview(t)
    assert any("第 1 季" in w and "第 2 季" in w for w in pv["warnings"]), pv["warnings"]


def test_fix_by_setting_season_2():
    store, eng = env()
    eng._emby.have = {(2, i) for i in range(1, 9)}
    fq, t = mk(eng, SHARE, ["aaa"], season=2)
    pv = eng.preview(t)
    assert pv["warnings"] == [] and pv["emby_have"] == "S02E01-E08"
    eng.run_task(t["id"], manual=True)
    assert sorted(n for _, n in fq.saves) == ["第09集.mp4".replace("第09集", "庆余年.第09集"),
                                              "庆余年.第10集.mp4"]


def test_symptom_emby_not_enabled():
    store, eng = env()
    store.data["emby"]["enabled"] = False; eng.emby = lambda: None
    fq, t = mk(eng, SHARE, ["aaa"], season=2)
    assert len(eng.preview(t)["emby_msg"]) and "未启用" in eng.preview(t)["emby_msg"]
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 10


def test_preview_explains_each_file_and_has_no_side_effects():
    store, eng = env()
    eng._emby.have = {(1, i) for i in range(1, 9)}
    fq, t = mk(eng, SHARE, ["aaa"])
    before = (dict(fq.path2fid), {k: dict(v) for k, v in fq.dirs.items()}, list(fq.saves))
    pv = eng.preview(t)
    rows = pv["links"][0]["rows"]
    assert [r["action"] for r in rows] == ["skip"] * 8 + ["save"] * 2
    assert all("Emby" in r["why"] for r in rows[:8]) and rows[0]["eps"] == "S01E01"
    assert (dict(fq.path2fid), {k: dict(v) for k, v in fq.dirs.items()}, list(fq.saves)) == before


def test_preview_does_not_create_missing_dir():
    store, eng = env()
    fq = FakeQuark(SHARE); eng.quark = lambda: fq
    t = eng.create_task({"name": "x", "savepath": "/不存在的目录", "links_text": "https://pan.quark.cn/s/aaa"})
    pv = eng.preview(t)
    assert "/不存在的目录" not in fq.path2fid and any("还不存在" in w for w in pv["warnings"])


def test_preview_reports_dir_source():
    store, eng = env()
    eng._emby.have = set()
    fq, t = mk(eng, SHARE, ["aaa"])
    fq.dirs["D"]["x1"] = "S01E01.mp4"
    rows = eng.preview(t)["links"][0]["rows"]
    assert rows[0]["action"] == "skip" and "夸克目录" in rows[0]["why"]


def test_emby_name_matching_no_blind_first_hit():
    def fake(items_by_term):
        e = Emby("http://x", "k")
        e._req = lambda m, p, **kw: {"Items": items_by_term.get(kw.get("SearchTerm"), [])}
        return e
    # 任务名带「第二季」：先搜原名没有，去掉季后命中
    e = fake({"庆余年": [{"Id": "9", "Name": "庆余年"}]})
    assert e.find_series("庆余年 第二季") == ("9", "庆余年")
    # 搜到的是毫不相关的剧：不再盲取第一个
    e = fake({"第二季": [{"Id": "1", "Name": "完全无关的剧"}]})
    assert e.find_series("第二季") == (None, None)


def test_emby_failure_does_not_mass_resave():
    store, eng = env()
    class Broken(FakeEmby_):
        def episodes(self, sid): raise RuntimeError("connection refused")
    eng.emby = lambda: Broken(set())
    fq, t = mk(eng, SHARE, ["aaa"])
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [] and t["last_status"] == "error" and "不转存" in t["last_msg"]
    eng.emby = lambda: FakeEmby_({(1, i) for i in range(1, 9)})     # Emby 恢复后正常工作
    eng.run_task(t["id"], manual=True)
    assert sorted(n for _, n in fq.saves) == ["庆余年.第09集.mp4", "庆余年.第10集.mp4"]


def test_emby_series_not_found_still_allows_new_show():
    store, eng = env()
    class NotFound(FakeEmby_):
        def find_series(self, name, tmdb_id=None): return None, None
    eng.emby = lambda: NotFound(set())
    fq, t = mk(eng, SHARE, ["aaa"])
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 10                    # 新剧还没入库：不是错误，正常转存


# ---- 用户的真实事故：两个链接各 16 个文件，全被转了（共 32 个）----
def _share16(fmt):
    return {"files": [fmt % i for i in range(1, 17)]}


def test_incident_unparsable_names_in_subscription_do_not_mass_save():
    """文件名内置规则认不出来（这里用一个故意无法识别的格式模拟）时，订阅追更不能整批放行。"""
    store, eng = env()
    eng._emby.have = {(1, i) for i in range(1, 9)}
    shares = {"aaa": _share16("法医秦明之龙番往事·第%02d部分.mp4".replace("第%02d部分", "壹%02d贰")),
              "bbb": _share16("龙番往事（%02d）完整版.mp4".replace("（%02d）", "〔%02d〕"))}
    shares = {"aaa": {"files": [f"法医秦明之龙番往事壹{i:02d}贰.mp4" for i in range(1, 17)]},
              "bbb": {"files": [f"龙番往事〔{i:02d}〕完整版.mp4" for i in range(1, 17)]}}
    fq, t = mk(eng, shares, ["aaa", "bbb"], kind="subscription")
    assert eng._unparsed_policy(t) == "skip"
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [], len(fq.saves)                    # 以前：32 个全转
    rows = eng.preview(t)["links"][0]["rows"]
    assert rows and all("解析不出" in r["why"] for r in rows)


def test_incident_monitor_default_still_saves_unparsed():
    store, eng = env()
    shares = {"aaa": {"files": ["电影正片.mp4", "特辑.mp4"]}}
    fq, t = mk(eng, shares, ["aaa"])                        # 分享监控，默认照样转存
    assert eng._unparsed_policy(t) == "save"
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 2
    eng.update_task(t["id"], {"unparsed": "skip"})
    assert eng._unparsed_policy(t) == "skip"


def test_glued_digit_names_now_parse_and_dedupe_across_links():
    store, eng = env()
    eng._emby.have = {(1, i) for i in range(1, 9)}
    shares = {"aaa": {"files": [f"法医秦明之龙番往事{i:02d}.mp4" for i in range(1, 17)]},
              "bbb": {"files": [f"法医秦明之龙番往事.{i:02d}集.4K.mp4" for i in range(1, 17)]}}
    fq, t = mk(eng, shares, ["aaa", "bbb"], kind="subscription")
    eng.run_task(t["id"], manual=True)
    assert [p for p, _ in fq.saves] == ["aaa"] * 8                       # 只补 9-16，且只来自第一个链接
    assert sorted(fq.dir.values()) == [f"S01E{i:02d}.mp4" for i in range(9, 17)]


def test_snapshot_after_run_and_progress_has_no_side_effects():
    store, eng = env()
    eng._emby.have = {(1, i) for i in range(1, 9)}
    shares = {"aaa": {"files": [f"S01E{i:02d}.mp4" for i in range(1, 11)]}}
    fq, t = mk(eng, shares, ["aaa"], total_episodes=12)
    eng.run_task(t["id"], manual=True)
    s = t["snap"]
    assert s["have"] == 10 and s["total"] == 12 and s["missing"] == [11, 12] and s["missing_n"] == 2
    assert eng.public_task(t)["snap"]["have"] == 10                       # 海报墙从公开字段里直接读到
    # 进度查询：目录不存在时不能顺手建出来
    eng._emby.have = set()
    t2 = eng.create_task({"name": "y", "savepath": "/还没有的目录", "links_text": "https://pan.quark.cn/s/aaa"})
    p = eng.progress(t2)
    assert "/还没有的目录" not in fq.path2fid and p["have_count"] == 0 and t2["snap"]["have"] == 0


def test_recent_events_and_overview():
    store, eng = env()
    eng._emby.have = {(1, i) for i in range(1, 6)}
    shares = {"aaa": {"files": [f"S01E{i:02d}.mp4" for i in range(1, 9)]}}
    fq, t = mk(eng, shares, ["aaa"])
    assert eng.overview()["recent"] == [] and eng.overview()["stats"]["today"] == 0
    eng.run_task(t["id"], manual=True)                       # 补 6-8，共 3 个
    o = eng.overview()
    ev = o["recent"][0]
    assert ev["count"] == 3 and ev["eps"] == "S01E06-E08" and ev["name"] == "庆余年" and ev["task_id"] == t["id"]
    assert o["stats"]["today"] == 3 and o["stats"]["week"] == 3 and o["stats"]["total"] == 3 and o["stats"]["monitors"] == 1
    eng.run_task(t["id"], manual=True)                       # 没有新内容：不产生事件
    assert len(eng.overview()["recent"]) == 1


def test_recent_seeded_from_old_history_and_capped():
    store, eng = env()
    fq, t = mk(eng, {"aaa": {"files": []}}, ["aaa"])
    t["history"] = [{"time": "2026-09-30 20:12:03", "status": "saved", "msg": "转存 2 个：S01E05-E06", "files": ["a", "b"]},
                    {"time": "2026-09-29 08:00:00", "status": "error", "msg": "x", "files": []}]
    store.data["recent"] = []
    eng._seed_recent()
    r = store.data["recent"]
    assert len(r) == 1 and r[0]["count"] == 2 and r[0]["eps"] == "S01E05-E06"
    store.data["recent"] = [{"ts": i, "count": 1} for i in range(300)]
    t["saved_total"] = 0
    eng._finish(t, {"status": "saved", "msg": "m", "saved": [{"name": "x", "eps": [(1, 1)]}]})
    assert len(store.data["recent"]) == 200 and store.data["recent"][0]["name"] == "庆余年"


def test_icons_are_public_but_api_is_not():
    import base64
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    store, eng = env()
    c = create_app(store, eng).test_client()
    for path, ctype in (("/favicon.svg", "svg"), ("/favicon.ico", "icon"), ("/apple-touch-icon.png", "png")):
        r = c.get(path)
        assert r.status_code == 200 and ctype in r.content_type, (path, r.status_code, r.content_type)
    assert c.get("/api/overview").status_code == 401 and c.get("/").status_code == 401
    assert c.get("/favicon.png").status_code in (401, 404)
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    assert c.get("/api/overview", headers=h).get_json()["stats"]["subs"] == 0
