# -*- coding: utf-8 -*-
"""订阅后自动找资源：判断规则、数量限制、定时重搜、画质筛选、只转存本季。"""
import base64, os, sys, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
time.sleep = lambda *a, **k: None          # 引擎里有"别请求太密"的随机等待，测试里不需要
import app.engine as E
from tests.test_engine import env, FakeQuark

G, MB = 1024 ** 3, 1024 ** 2


def item(pid, title, t="2026-10-01"):
    return {"url": f"https://pan.quark.cn/s/{pid}", "pwd_id": pid, "passcode": "", "title": title, "source": "plugin:x", "time": t}


class FakePS:
    def __init__(self, items): self.items, self.calls = list(items), []   # 拷贝：测试之间不能互相污染
    def search(self, kw): self.calls.append(kw); return list(self.items)


def eps(season, rng, tag="", size=2 * G):
    return [(f"庆余年.S{season:02d}E{i:02d}{tag}.mkv", size) for i in rng]


SHARES = {
    "good": {"files": eps(2, range(1, 17), ".2160p.HDR")},                       # 第 2 季全 16 集，4K HDR
    "pack": {"files": eps(1, range(1, 4), ".1080p") + eps(2, range(1, 17), ".1080p")},   # 合集：含第 1、2 季
    "partial": {"files": eps(2, range(1, 6), ".1080p", G)},                      # 只更新到 5 集
    "tiny": {"files": eps(2, range(1, 6), ".1080p", 30 * MB)},                   # 每个才 30MB：花絮 / 预告
    "noseason": {"files": [(f"第{i:02d}集.mp4", 2 * G) for i in range(1, 17)]},  # 没写是第几季
    "dead": {"status": 400, "msg": "分享已取消"},
}
ITEMS = [item("good", "庆余年 第二季 全集 4K HDR"), item("pack", "庆余年 第一季+第二季 全集 1080P"), item("partial", "庆余年 第二季 更新至05集"),
         item("tiny", "庆余年 第二季 幕后花絮"), item("noseason", "庆余年 全集 国语"), item("dead", "庆余年 第二季 备用"),
         item("s1", "庆余年 第一季 全集"), item("other", "繁花 全30集 4K")]
SHARES.update({"s1": {"files": eps(1, range(1, 4))}, "other": {"files": [("繁花.S01E01.mkv", G)]}})


def setup(items=ITEMS, shares=None, emby_have=(), **task):
    store, eng = env()
    eng._emby.have = set(emby_have)
    fq = FakeQuark(dict(shares or SHARES)); eng.quark = lambda: fq
    ps = FakePS(items); eng.pansou = lambda: ps
    calls = []
    real = eng.inspect_share
    eng.inspect_share = lambda url, season=1, prefs=None: (calls.append(url), real(url, season, prefs))[1]
    cfg = {"kind": "subscription", "name": "庆余年", "savepath": "/剧/庆余年", "season": 2, "total_episodes": 16,
           "auto_find": True, "links_text": "", **task}
    return store, eng, fq, ps, calls, eng.create_task(cfg)


def ids(t): return [l["pwd_id"] for l in t["links"]]


def test_picks_only_trustworthy_resources_and_explains_each():
    store, eng, fq, ps, calls, t = setup()
    res = eng.auto_find(t["id"], force=True)
    f = t["find"]["seen"]
    assert ids(t) == ["good", "pack", "partial"], ids(t)                       # 好的、合集、更新中的
    assert f["tiny"]["verdict"] == "reject" and "30 MB" in f["tiny"]["reason"]  # 花絮：体积
    assert f["noseason"]["verdict"] == "reject" and "没写是第几季" in f["noseason"]["reason"]
    assert f["dead"]["verdict"] == "dead" and "分享已取消" in f["dead"]["reason"]
    assert "s1" not in f and "other" not in f                                 # 标题就写了别的季 / 别的剧：根本没去检测
    assert "https://pan.quark.cn/s/s1" not in calls and "https://pan.quark.cn/s/other" not in calls
    assert ps.calls == ["庆余年", "庆余年 第2季"]                              # 第 2 季会多搜一次带季的关键词
    assert all(l["auto"] for l in t["links"]) and t["links"][0]["title"].startswith("庆余年 第二季 全集")
    assert res["added"] and any("✅" in x["text"] for x in t["find"]["log"]) and any("✗" in x["text"] for x in t["find"]["log"])


def test_force_triggers_immediate_scan_and_only_season_files():
    store, eng, fq, ps, calls, t = setup()
    eng.auto_find(t["id"], force=True)
    assert t["last_status"] == "saved" and t["saved_total"] == 16
    assert sorted(fq.dir.values()) == [f"S02E{i:02d}.mkv" for i in range(1, 17)]   # 合集里的第 1 季没被转进来
    assert [p for p, _ in fq.saves] == ["good"] * 16                               # 只来自排在前面的链接


def test_limits_cap_the_number_added():
    store, eng, fq, ps, calls, t = setup()
    old = E.AUTO_MAX_ADD; E.AUTO_MAX_ADD = 2
    try: eng.auto_find(t["id"], force=True)
    finally: E.AUTO_MAX_ADD = old
    assert ids(t) == ["good", "pack"] and t["find"]["seen"]["partial"]["verdict"] == "cap"


def test_full_links_means_no_search():
    store, eng, fq, ps, calls, t = setup(links_text="\n".join(f"https://pan.quark.cn/s/u{i}" for i in range(5)))
    r = eng.auto_find(t["id"])
    assert r["msg"] == "有效链接已满" and ps.calls == []


def test_complete_season_means_no_search():
    store, eng, fq, ps, calls, t = setup(emby_have={(2, i) for i in range(1, 17)})
    assert eng.auto_find(t["id"])["msg"].startswith("本季已播出的都齐了") and ps.calls == []


def test_recheck_rules_and_new_resources_appear_later():
    store, eng, fq, ps, calls, t = setup()
    eng.run_task = lambda *a, **k: None                        # 只看"找资源"，不顺手转存（否则整季转完就不再搜了）
    eng.auto_find(t["id"])
    first = len(calls)
    assert first == 6                                          # good pack partial tiny noseason dead
    # 用户删掉了自动加的两个链接，时间到了再搜一次：看过的都不会再去检测
    t["links"] = [l for l in t["links"] if l["pwd_id"] == "good"]
    t["find"]["next"] = 0
    eng.auto_find(t["id"])
    assert len(calls) == first                                 # 拒绝的 24h 内不重看；失效的 3 天内不重看；被删掉的"已添加"不再加回来
    # 作者发了新资源：只检测这个新的
    ps.items.append(item("new", "庆余年 第二季 4K 更新至16集"))
    eng._inspect_cache.clear()
    SHARES_NEW = {"files": eps(2, range(1, 17), ".2160p")}
    eng.quark().shares["new"] = SHARES_NEW
    t["find"]["next"] = 0
    eng.auto_find(t["id"])
    assert calls[first:] == ["https://pan.quark.cn/s/new"] and "new" in ids(t)
    # 过了 24 小时：拒绝过的会再看一次（作者可能更新了）；失效的还要等 3 天
    for v in t["find"]["seen"].values(): v["ts"] -= 25 * 3600
    t["find"]["next"] = 0; n = len(calls)
    eng.auto_find(t["id"])
    again = [u.rsplit("/", 1)[-1] for u in calls[n:]]
    assert set(again) == {"tiny", "noseason"}, again


def test_due_conditions():
    store, eng, fq, ps, calls, t = setup()
    assert eng.auto_find_due(t)
    t["find"] = {"next": time.time() + 3600}; assert not eng.auto_find_due(t)           # 没到时间
    t["find"]["next"] = 0; t["auto_find"] = False; assert not eng.auto_find_due(t)       # 没开自动
    t["auto_find"] = True; t["enabled"] = False; assert not eng.auto_find_due(t)         # 暂停了
    t["enabled"] = True; t["links"].append({"url": "u", "pwd_id": "u", "ban": ""}); t["snap"] = {"missing_n": 0, "aired": 16}
    assert not eng.auto_find_due(t)                                                       # 已经齐了
    t["snap"] = {"missing_n": 3, "aired": 16}; assert eng.auto_find_due(t)
    t["kind"] = "monitor"; assert not eng.auto_find_due(t)


def test_tick_runs_due_finds_only_when_pansou_enabled():
    store, eng, fq, ps, calls, t = setup()
    eng.tick(); assert ids(t)                                                    # 到点自动搜索并添加
    n = len(ps.calls); eng.tick(); assert len(ps.calls) == n                      # 间隔内不重复搜
    store2, eng2, fq2, ps2, calls2, t2 = setup(); eng2.pansou = lambda: None
    eng2.tick(); assert ps2.calls == [] and ids(t2) == []


def test_dead_auto_links_are_cleaned_but_user_links_kept():
    store, eng, fq, ps, calls, t = setup(items=[], links_text="https://pan.quark.cn/s/mine")
    t["links"].append({"url": "https://pan.quark.cn/s/old", "pwd_id": "old", "ban": "已失效", "auto": True})
    t["links"][0]["ban"] = "已失效"                                              # 用户自己加的失效链接不能被删
    eng.auto_find(t["id"])
    assert ids(t) == ["mine"]


# ---------------------------------------------------------------- 画质筛选
def test_quality_prefs_steer_auto_choice():
    store, eng, fq, ps, calls, t = setup(want_res=["1080p"], want_hdr="no")
    eng.auto_find(t["id"])
    f = t["find"]["seen"]
    assert "没有符合你画质要求" in f["good"]["reason"] and "4K" in f["good"]["reason"]    # 4K HDR 的被拒
    assert ids(t) == ["pack", "partial"]
    store, eng, fq, ps, calls, t = setup(want_res=["4k"], want_hdr="yes")
    eng.auto_find(t["id"]); assert ids(t) == ["good"]


def test_quality_prefs_filter_files_when_saving():
    files = [("庆余年.S02E01.2160p.HDR.mkv", 2 * G), ("庆余年.S02E02.1080p.mkv", G), ("庆余年.S02E03.720p.mkv", G),
             ("庆余年.S02E04.2160p.mkv", 2 * G), ("庆余年.S02E05.mkv", G)]
    for prefs, expect in (({"want_res": ["4k"]}, {1, 4, 5}),                    # 没标分辨率的（E05）无法判断，放行
                          ({"want_res": ["1080p", "720p"]}, {2, 3, 5}),
                          ({"want_hdr": "yes"}, {1}),                          # 要 HDR：没有标记的都当 SDR 跳过
                          ({"want_hdr": "no"}, {2, 3, 4, 5}),
                          ({"want_res": ["4k"], "want_hdr": "yes"}, {1}), ({}, {1, 2, 3, 4, 5})):
        store, eng, fq, ps, calls, t = setup(items=[], shares={"a": {"files": files}}, links_text="https://pan.quark.cn/s/a", **prefs)
        eng.run_task(t["id"], manual=True)
        got = {int(n[4:6]) for n in fq.dir.values()}
        assert got == expect, (prefs, got)
    store, eng, fq, ps, calls, t = setup(items=[], shares={"a": {"files": files}}, links_text="https://pan.quark.cn/s/a", want_res=["4k"])
    why = {r["file"]: r["why"] for r in eng.preview(t)["links"][0]["rows"] if r["action"] == "skip"}
    assert "1080P" in why["庆余年.S02E02.1080p.mkv"] and "不在你要的范围" in why["庆余年.S02E02.1080p.mkv"]


def test_inspect_reports_prefs_match():
    store, eng, fq, ps, calls, t = setup()
    r = eng.inspect_share("https://pan.quark.cn/s/pack", 2, {"res": ["4k"]})
    assert r["prefs_set"] and r["ok_videos"] == 0 and r["ok_eps"] == "" and "1080P" in r["quality"]
    r = eng.inspect_share("https://pan.quark.cn/s/good", 2, {"res": ["4k"], "hdr": "yes"})
    assert r["ok_videos"] == 16 and r["ok_eps"] == "S02E01-E16" and r["avg_mb"] == 2048 and r["seasons_explicit"] == [2]


# ---------------------------------------------------------------- 只转存本季
def test_strict_season_default_and_toggle():
    files = eps(1, range(1, 4)) + eps(2, range(1, 4))
    store, eng, fq, ps, calls, t = setup(items=[], shares={"a": {"files": files}}, links_text="https://pan.quark.cn/s/a")
    assert eng._strict_season(t)
    rows = {r["file"]: r for r in eng.preview(t)["links"][0]["rows"]}
    assert "这是第 1 季" in rows["庆余年.S01E01.mkv"]["why"] and rows["庆余年.S02E01.mkv"]["action"] == "save"
    eng.update_task(t["id"], {"strict_season": False}); assert not eng._strict_season(t)
    rows = {r["file"]: r for r in eng.preview(t)["links"][0]["rows"]}
    assert rows["庆余年.S01E01.mkv"]["action"] == "save"
    store, eng, fq, ps, calls, m = setup(items=[], shares={"a": {"files": files}}, links_text="https://pan.quark.cn/s/a", kind="monitor")
    assert not eng._strict_season(m)                                           # 分享监控保持原来的行为


# ---------------------------------------------------------------- HTTP
def test_api_status_find_and_subscribe_defaults():
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    from app.tmdb import Tmdb
    store, eng, fq, ps, calls, t = setup()
    eng.pansou = type(eng).pansou.__get__(eng)                  # 用真实的开关判断，而不是测试里固定返回的假对象
    store.data["tmdb"]["api_key"] = "k"
    c = create_app(store, eng).test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    store.data["pansou"].update(enabled=True, url="http://ps")
    assert c.get("/api/status", headers=h).get_json()["pansou"] is True
    store.data["pansou"]["enabled"] = False
    assert c.get("/api/status", headers=h).get_json()["pansou"] is False
    assert "还没有启用" in c.post(f"/api/tasks/{t['id']}/find", headers=h).get_json()["error"]
    store.data["pansou"]["enabled"] = True
    with mock.patch.object(eng, "auto_find") as af:
        assert c.post(f"/api/tasks/{t['id']}/find", headers=h).get_json()["ok"]
        import threading; [th.join(2) for th in threading.enumerate() if th is not threading.current_thread() and th.daemon]
        af.assert_called_with(t["id"], True)
    detail = {"id": 7, "name": "庆余年", "poster_url": "", "overview": "", "first_air_date": "", "vote_average": 0, "seasons": []}
    for season, (pansou_on, sent, expect) in enumerate(((True, None, True), (True, False, False), (False, True, False)), 1):
        store.data["pansou"]["enabled"] = pansou_on
        body = {"tmdb_id": 7, "season": season, "savepath": f"/x{season}", "links_text": ""}
        if sent is not None: body["auto_find"] = sent
        with mock.patch.object(Tmdb, "detail", return_value=detail), mock.patch.object(eng, "auto_find"), mock.patch.object(eng, "run_task"):
            r = c.post("/api/subscriptions", json=body, headers=h).get_json()
        assert r["task"]["auto_find"] is expect, (pansou_on, sent, r["task"]["auto_find"])
    r = c.post("/api/pansou/inspect", json={"url": "https://pan.quark.cn/s/good", "season": 2, "prefs": {"res": ["4k"], "hdr": "yes"}}, headers=h).get_json()
    assert r["ok_videos"] == 16


def test_name_with_season_suffix_still_matches_titles():
    """TMDB 里有的剧名自带「第二季」：去掉后再比对 / 搜索，否则会漏掉没写季的标题。"""
    store, eng, fq, ps, calls, t = setup(name="庆余年 第二季")
    eng.run_task = lambda *a, **k: None
    eng.auto_find(t["id"])
    assert ps.calls == ["庆余年", "庆余年 第2季"]
    assert "good" in ids(t) and t["find"]["seen"]["noseason"]["verdict"] == "reject"    # 「庆余年 全集 国语」被检测过（而不是被当成无关）
