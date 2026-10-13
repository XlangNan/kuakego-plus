# -*- coding: utf-8 -*-
"""电影订阅：只挑一个最合适的视频、枪版/画质筛选、入库后完成、自动找片源、目录冲突、TMDB / Emby 电影接口。"""
import base64, os, sys, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
time.sleep = lambda *a, **k: None
import app.engine as E
from app.emby import Emby
from app.tmdb import Tmdb
from tests.test_engine import env, FakeQuark
from tests.test_autofind import FakePS, item

G, MB = 1024 ** 3, 1024 ** 2


class FakeEmbyMovies:
    def __init__(self, have=(), fail=False): self.have, self.fail, self.refreshed = set(have), fail, []
    def find_movie(self, name, tmdb_id=None, year=None):
        if self.fail: raise RuntimeError("connection refused")
        return ("m1", name) if tmdb_id in self.have or name in self.have else (None, None)
    def refresh(self, sid=None): self.refreshed.append(sid)


def setup(shares, emby=None, ps_items=None, **task):
    store, eng = env()
    eng.emby = (lambda: emby) if emby else (lambda: None)
    fq = FakeQuark(dict(shares)); eng.quark = lambda: fq
    ps = FakePS(ps_items or []); eng.pansou = lambda: ps
    cfg = {"media": "movie", "name": "某电影", "year": 2024, "tmdb_id": 99, "savepath": "/电影/某电影 (2024)", "release_date": "2024-01-01",
           "links_text": "https://pan.quark.cn/s/a", **task}
    return store, eng, fq, ps, eng.create_task(cfg)


FILES = {"a": {"files": [("Movie.2024.2160p.HDR.mkv", 8 * G), ("Movie.2024.1080p.mkv", 3 * G), ("sample.mp4", 20 * MB), ("poster.jpg", 100)]}}


def test_movie_task_model_defaults():
    store, eng, fq, ps, t = setup(FILES)
    assert t["media"] == "movie" and t["kind"] == "subscription" and t["season"] == 1 and t["no_cam"] is True and t["done"] is False
    s, e2 = env()
    tv = e2.create_task({"name": "剧", "savepath": "/x", "links_text": "", "kind": "subscription", "season": 3})
    assert tv["media"] == "tv" and tv["no_cam"] is False and tv["season"] == 3          # 剧集不受影响


def test_picks_the_single_best_file_renames_and_finishes():
    emby = FakeEmbyMovies()
    store, eng, fq, ps, t = setup(FILES, emby=emby)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("a", "Movie.2024.2160p.HDR.mkv")]                  # 只转 1 个：画质最高的；预告 / 封面不转
    assert sorted(fq.dirs[fq.path2fid["/电影/某电影 (2024)"]].values()) == ["某电影 (2024).mkv"]   # Emby 推荐命名
    assert t["done"] and t["last_status"] == "saved" and "8.0 GB" in t["last_msg"] and t["snap"]["have"] == 1
    assert emby.refreshed == [None] or emby.refreshed == ["m1"]               # 转存后通知 Emby 刷新
    assert not eng.is_due({**t, "next_run": 0, "enabled": True}) and eng.public_task(t)["status"] == "done"
    eng.run_task(t["id"], manual=True)                                       # 手动再扫一次：已入库，不会重复转存
    assert len(fq.saves) == 1 and t["last_status"] == "complete"
    assert eng.overview()["recent"][0]["name"] == "某电影" and eng.overview()["recent"][0]["files"] == ["某电影 (2024).mkv"]


def test_keep_original_name_when_rename_is_off():
    store, eng, fq, ps, t = setup(FILES, rename="keep")
    eng.run_task(t["id"], manual=True)
    assert list(fq.dirs[fq.path2fid["/电影/某电影 (2024)"]].values()) == ["Movie.2024.2160p.HDR.mkv"]


def test_cam_versions_are_skipped_by_default_and_quality_prefs_apply():
    shares = {"a": {"files": [("Movie.2024.TS.1080p.mkv", 3 * G), ("Movie.2024.720p.WEB-DL.mkv", 1500 * MB)]}}
    store, eng, fq, ps, t = setup(shares)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("a", "Movie.2024.720p.WEB-DL.mkv")]                   # 枪版（TS）不要，哪怕它画质更高
    store, eng, fq, ps, t = setup(shares, no_cam=False)
    eng.run_task(t["id"], manual=True); assert fq.saves == [("a", "Movie.2024.TS.1080p.mkv")]   # 关掉「排除枪版」就会选它
    store, eng, fq, ps, t = setup(shares, want_res=["4k"])
    eng.run_task(t["id"], manual=True); assert fq.saves == [] and t["last_status"] == "nochange" and not t["done"]
    store, eng, fq, ps, t = setup({"a": {"files": [("Movie.2024.mkv.ts", 3 * G)]}})                  # .ts 是视频扩展名，不是枪版
    eng.run_task(t["id"], manual=True); assert len(fq.saves) == 1


def test_prefers_file_whose_name_contains_the_title():
    shares = {"a": {"files": [("别的电影.2024.1080p.mkv", 9 * G), ("某电影.2024.1080p.mkv", 3 * G)]}}
    store, eng, fq, ps, t = setup(shares)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("a", "某电影.2024.1080p.mkv")]


def test_already_in_directory_or_emby_means_done_without_saving():
    store, eng, fq, ps, t = setup(FILES)
    fq.path2fid["/电影/某电影 (2024)"] = "D2"; fq.dirs["D2"] = {"x": "已经有的.mkv"}
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [] and t["done"] and "夸克目录里已有视频" in t["last_msg"]
    store, eng, fq, ps, t = setup(FILES, emby=FakeEmbyMovies(have={99}))
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [] and t["done"] and "Emby 里已有" in t["last_msg"]


def test_emby_failure_never_causes_a_duplicate_save():
    store, eng, fq, ps, t = setup(FILES, emby=FakeEmbyMovies(fail=True))
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [] and t["last_status"] == "error" and "不转存" in t["last_msg"] and not t["done"]


def test_dead_link_banned_and_next_link_used():
    shares = {"dead": {"status": 400, "msg": "分享已取消"}, "a": FILES["a"]}
    store, eng, fq, ps, t = setup(shares, links_text="https://pan.quark.cn/s/dead\nhttps://pan.quark.cn/s/a")
    eng.run_task(t["id"], manual=True)
    assert t["links"][0]["ban"] == "分享已取消" and t["done"] and len(fq.saves) == 1


def test_no_links_waits_and_unreleased_is_not_searched():
    store, eng, fq, ps, t = setup(FILES, links_text="")
    eng.run_task(t["id"], manual=True); assert t["last_status"] == "idle"
    t["auto_find"] = True
    assert eng.auto_find_due(t)
    t["release_date"] = "2999-01-01"; assert not eng.auto_find_due(t)                  # 还没上映：不搜
    t["release_date"] = ""; t["done"] = True; assert not eng.auto_find_due(t)           # 已入库：不搜


# ---------------------------------------------------------------- 自动找电影片源
SHARES = {
    "good": {"files": [("某电影.2024.2160p.mkv", 8 * G)]},
    "good2": {"files": [("某电影.2024.1080p.mkv", 3 * G)]},
    "series": {"files": [(f"某电影.S01E{i:02d}.mkv", G) for i in range(1, 11)]},       # 其实是剧集
    "trailer": {"files": [("某电影 预告.mp4", 50 * MB)]},
    "cam": {"files": [("某电影.2024.TS.1080p.mkv", 3 * G)]},
    "pack": {"files": [(f"系列{c}.mkv", 3 * G) for c in "甲乙丙丁戊己庚辛壬癸"]},          # 合集（文件名里没有数字，不会被当成剧集）
    "dead": {"status": 400, "msg": "分享已取消"},
    "old": {"files": [("某电影.1999.mkv", 3 * G)]}, "other": {"files": [("别的电影.2024.mkv", 3 * G)]},
}
ITEMS = [item("good", "某电影 2024 4K 国英双语"), item("good2", "某电影 (2024) 1080P"), item("series", "某电影 全集"), item("trailer", "某电影 2024 预告片"),
         item("cam", "某电影 2024 抢先版"), item("pack", "某电影 系列合集 2024"), item("dead", "某电影 2024 备用"),
         item("old", "某电影 1999 国语"), item("other", "别的电影 2024")]


def test_auto_find_movie_judges_and_explains_each_candidate():
    store, eng, fq, ps, t = setup(SHARES, ps_items=ITEMS, links_text="", auto_find=True)
    eng.run_task = lambda *a, **k: None
    eng.auto_find(t["id"])
    seen = t["find"]["seen"]
    assert ps.calls == ["某电影", "某电影 2024"]
    assert [l["pwd_id"] for l in t["links"]] == ["good", "good2"]                      # 一个主用 + 一个备用，4K 在前
    assert seen["series"]["verdict"] == "reject" and "像是剧集" in seen["series"]["reason"]
    assert "50 MB" in seen["trailer"]["reason"] and "预告" in seen["trailer"]["reason"]
    assert "枪版" in seen["cam"]["reason"] and "合集" in seen["pack"]["reason"] and seen["dead"]["verdict"] == "dead"
    assert "识别到 10 集" in seen["series"]["reason"]
    assert "old" not in seen and "other" not in seen                                   # 年份对不上 / 名字对不上：根本没去检测
    log = " ".join(x["text"] for x in t["find"]["log"]); assert "年份对不上 1" in log and "名字对不上 1" in log


def test_auto_find_movie_end_to_end_then_stops():
    store, eng, fq, ps, t = setup(SHARES, ps_items=ITEMS, links_text="", auto_find=True)
    eng.auto_find(t["id"], force=True)                                                 # 找到后立刻转存
    assert t["done"] and fq.saves == [("good", "某电影.2024.2160p.mkv")] and t["last_status"] == "saved"
    n = len(ps.calls); t["find"]["next"] = 0
    assert not eng.auto_find_due(t)                                                   # 入库后不再搜
    eng.auto_find(t["id"]); assert eng.find(t["id"])["done"]


def test_auto_find_respects_quality_and_cam_settings():
    store, eng, fq, ps, t = setup(SHARES, ps_items=ITEMS, links_text="", auto_find=True, want_res=["1080p"])
    eng.run_task = lambda *a, **k: None
    eng.auto_find(t["id"])
    assert [l["pwd_id"] for l in t["links"]] == ["good2"] and "没有符合你要求" in t["find"]["seen"]["good"]["reason"]
    store, eng, fq, ps, t = setup(SHARES, ps_items=ITEMS, links_text="", auto_find=True, no_cam=False)
    eng.run_task = lambda *a, **k: None
    eng.auto_find(t["id"]); assert t["find"]["seen"]["cam"]["verdict"] != "reject"          # 关掉「排除枪版」后，它不再被拒绝（名额有限时可能排在后面）


def test_unreleased_movie_is_never_searched_even_when_forced():
    store, eng, fq, ps, t = setup(SHARES, ps_items=ITEMS, links_text="", auto_find=True, release_date="2999-01-01")
    r = eng.auto_find(t["id"], force=True)                                              # 刚订阅 / 手动点「立即搜索」都不搜
    assert ps.calls == [] and "才上映" in r["msg"] and t["links"] == [] and not t["done"]
    assert "才上映" in eng.public_task(t)["find"]["msg"]
    t["release_date"] = "2000-01-01"; eng.run_task = lambda *a, **k: None               # 上映后正常搜
    eng.auto_find(t["id"], force=True); assert ps.calls and [l["pwd_id"] for l in t["links"]] == ["good", "good2"]


def test_movie_already_owned_means_no_search():
    store, eng, fq, ps, t = setup(SHARES, emby=FakeEmbyMovies(have={99}), ps_items=ITEMS, links_text="", auto_find=True)
    r = eng.auto_find(t["id"])
    assert ps.calls == [] and t["done"] and "不用找" in r["msg"]


# ---------------------------------------------------------------- 预览 / 进度 / 目录
def test_preview_and_progress_for_movies_have_no_side_effects():
    store, eng, fq, ps, t = setup(FILES)
    before = (dict(fq.path2fid), list(fq.saves))
    pv = eng.preview(t)
    rows = {r["file"]: r for r in pv["links"][0]["rows"]}
    assert pv["movie"] and rows["Movie.2024.2160p.HDR.mkv"]["action"] == "save" and "最合适" in rows["Movie.2024.2160p.HDR.mkv"]["why"]
    assert "有更合适的" in rows["Movie.2024.1080p.mkv"]["why"] and "预告" in rows["sample.mp4"]["why"] and rows["poster.jpg"]["action"] == "skip"
    p = eng.progress(t)
    assert p["movie"] and not p["have"] and (dict(fq.path2fid), list(fq.saves)) == before and "/电影/某电影 (2024)" not in fq.path2fid


def test_directory_clash_uses_media_and_id_together():
    store, eng, fq, ps, t = setup(FILES)
    tv = {"name": "某剧", "savepath": "/电影/某电影 (2024)", "links_text": "", "kind": "subscription", "tmdb_id": 99, "season": 1}
    try: eng.create_task(tv); assert False                                          # 剧集 99 和电影 99 是两部不同的作品
    except ValueError as e: assert "《某电影》" in str(e) and "混在一起" in str(e)
    eng.create_task({**tv, "savepath": "/剧/某剧"})                                     # 不同目录没问题
    try: eng.create_task({**tv, "media": "movie", "tmdb_id": 100, "name": "另一部"}); assert False
    except ValueError: pass
    assert store.data["recent_dirs"][0]["media"] == "tv" and any(d["media"] == "movie" for d in store.data["recent_dirs"])


# ---------------------------------------------------------------- Emby / TMDB / HTTP
def test_emby_find_movie_by_id_then_name_and_year():
    def fake(items_by_key):
        e = Emby("http://x", "k")
        e._req = lambda m, path, **kw: {"Items": items_by_key.get(kw.get("AnyProviderIdEquals") or kw.get("SearchTerm"), [])}
        return e
    e = fake({"tmdb.99": [{"Id": "7", "Name": "某电影"}]}); assert e.find_movie("x", 99, 2024) == ("7", "某电影")
    e = fake({"某电影": [{"Id": "8", "Name": "某电影", "ProductionYear": 1999}]})
    assert e.find_movie("某电影", None, 2024) == (None, None)                          # 同名的老片：年份差太多，不算
    e = fake({"某电影": [{"Id": "8", "Name": "某电影", "ProductionYear": 2025}]}); assert e.find_movie("某电影", None, 2024) == ("8", "某电影")
    assert fake({}).find_movie("", None, None) == (None, None)


def test_emby_tmdb_ids_cached_per_kind():
    e = Emby("http://cache-test", "k"); calls = []
    def req(m, path, **kw): calls.append(kw["IncludeItemTypes"]); return {"Items": [{"ProviderIds": {"Tmdb": "5"}}]}
    e._req = req
    assert e.tmdb_ids("Series") == {5} and e.tmdb_ids("Movie") == {5} and e.tmdb_ids("Movie") == {5}
    assert calls == ["Series", "Movie"]


def test_tmdb_movie_client():
    t = Tmdb({"api_key": "k"}); seen = []
    def fake_get(path, ttl=1800, **params): seen.append((path, params)); return {"results": [{"id": 1, "title": "某电影", "release_date": "2024-05-01", "poster_path": "/p.jpg", "vote_average": 7.84}], "total_pages": 99}
    t._get = fake_get
    d = t.movie_discover("trending"); assert seen[-1][0] == "/trending/movie/week" and d["items"][0] == {
        "id": 1, "name": "某电影", "year": "2024", "release_date": "2024-05-01", "vote_average": 7.8, "overview": "", "poster_url": "/api/tmdb/img/w342/p.jpg",
        "media": "movie", "genre_ids": [], "section": "movie"} and d["total_pages"] == 20
    t.movie_discover("cn"); assert seen[-1][0] == "/discover/movie" and seen[-1][1]["with_origin_country"] == "CN|HK|TW"
    t.movie_discover("top"); assert seen[-1][1]["sort_by"] == "vote_average.desc" and seen[-1][1]["vote_count.gte"] == 500
    t.movie_search("某电影"); assert seen[-1][0] == "/search/movie" and seen[-1][1]["query"] == "某电影"
    t._get = lambda path, ttl=1800, **p: {"id": 1, "title": "某电影", "release_date": "2024-05-01", "runtime": 118, "genres": [{"name": "剧情"}],
                                           "credits": {"cast": [{"name": "甲", "character": "乙"}]}, "backdrop_path": "/b.jpg", "tagline": "标语"}
    m = t.movie_detail(1); assert m["runtime"] == 118 and m["genres"] == ["剧情"] and m["cast"][0]["name"] == "甲" and m["backdrop_url"].endswith("/w780/b.jpg")


def test_api_movie_endpoints_and_subscription():
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    store, eng, fq, ps, t = setup(FILES)
    eng.pansou = type(eng).pansou.__get__(eng); store.data["tmdb"]["api_key"] = "k"
    c = create_app(store, eng).test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    card = lambda i: {"id": i, "name": f"片{i}", "year": "2024", "release_date": "2024-01-01", "vote_average": 7, "overview": "", "poster_url": ""}
    with mock.patch.object(Tmdb, "movie_discover", return_value={"items": [card(99), card(98)], "total_pages": 3}):
        items = c.get("/api/tmdb/movie/discover?category=cn", headers=h).get_json()["items"]
    assert items[0]["sub_id"] == t["id"] and items[0]["sub_status"] and items[1]["sub_id"] is None        # 电影 99 已订阅，98 没有
    tv = eng.create_task({"name": "某剧", "savepath": "/剧/某剧", "links_text": "", "kind": "subscription", "tmdb_id": 98})
    with mock.patch.object(Tmdb, "movie_discover", return_value={"items": [card(98)], "total_pages": 1}):
        assert c.get("/api/tmdb/movie/discover", headers=h).get_json()["items"][0]["sub_id"] is None       # 剧集 98 不会让电影 98 显示「已订阅」
    with mock.patch.object(Tmdb, "movie_search", return_value={"items": [card(5)], "total_pages": 1}):
        assert c.get("/api/tmdb/movie/search?q=片", headers=h).get_json()["items"][0]["id"] == 5
        assert c.get("/api/tmdb/movie/search?q=", headers=h).get_json()["items"] == []
    detail = {**card(77), "name": "新电影", "year": "2025", "release_date": "2025-03-01", "overview": "简介", "poster_url": "p", "runtime": 100, "genres": [], "cast": [], "backdrop_url": ""}
    with mock.patch.object(Tmdb, "movie_detail", return_value=detail), mock.patch.object(eng, "auto_find"), mock.patch.object(eng, "run_task"):
        assert c.get("/api/tmdb/movie/77", headers=h).get_json()["runtime"] == 100
        r = c.post("/api/subscriptions", json={"media": "movie", "tmdb_id": 77, "savepath": "/电影/新电影 (2025)", "links_text": ""}, headers=h).get_json()["task"]
        assert r["media"] == "movie" and r["name"] == "新电影" and r["year"] == 2025 and r["release_date"] == "2025-03-01" and r["season"] == 1 and r["no_cam"] and r["kind"] == "subscription"
        dup = c.post("/api/subscriptions", json={"media": "movie", "tmdb_id": 77, "savepath": "/电影/另一个目录", "links_text": ""}, headers=h)
        assert dup.status_code == 400 and "已经订阅过" in dup.get_json()["error"]
        ok = c.post("/api/subscriptions", json={"media": "tv", "tmdb_id": 77, "season": 1, "savepath": "/剧/同号的剧", "links_text": ""}, headers=h)
        assert ok.status_code != 400 or "已经订阅" not in ok.get_json().get("error", "")                # 剧集 77 和电影 77 互不影响
    ov = c.get("/api/overview", headers=h).get_json()["stats"]; assert ov["movies"] == 2 and ov["subs"] >= 1
    assert c.get("/api/tmdb/movie/discover").status_code == 401
