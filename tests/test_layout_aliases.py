# -*- coding: utf-8 -*-
"""Emby 规范目录（剧名 (年份) {tmdb-ID}/Season N/…）、异形名转存、别名搜索、演员 / 相似推荐、批量删除。"""
import base64, os, sys, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
time.sleep = lambda *a, **k: None
import app.engine as E
from app.parser import emby_folder_name
from app.tmdb import Tmdb
import tempfile
from app.store import Store
from tests.test_engine import env, mk, FakeQuark, FakeEmby
from tests.test_autofind import FakePS, item, eps, setup as af_setup
from tests import test_movies as TM

G, MB = 1024 ** 3, 1024 ** 2


def new_env():
    """和 test_engine.env 夹具一样，但能直接调用（夹具函数在新版 pytest 里不能直接调）。"""
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    store.data["emby"].update(enabled=True, url="x", api_key="y", refresh_delay=0)
    eng = E.Engine(store)
    eng._emby = FakeEmby({(1, i) for i in range(1, 6)})
    eng.emby = lambda: eng._emby
    return store, eng


def no_emby(store, eng):
    store.data["emby"]["enabled"] = False
    eng.emby = lambda: None


# ------------------------------------------------------------------ 命名 / 目录
def test_emby_folder_name():
    assert emby_folder_name("觉醒年代", 2021, 117954) == "觉醒年代 (2021) {tmdb-117954}"
    assert emby_folder_name("A/B: C", 0, 5) == "A B C {tmdb-5}"          # 文件系统不允许的字符去掉
    assert emby_folder_name("某电影") == "某电影"


def test_defaults_keep_tree_off_and_layout_off():
    store, eng = new_env()
    f = eng.normalize({"name": "x", "savepath": "/a", "links_text": ""})
    assert f["keep_tree"] is False and f["emby_layout"] is False and f["aliases"] == []
    f = eng.normalize({"name": "x", "savepath": "/a", "links_text": "", "aliases": [" A ", "A", "", "B"], "emby_layout": True})
    assert f["aliases"] == ["A", "B"] and f["emby_layout"] is True


def test_season_folder_and_episode_file_names(env):
    store, eng = env; no_emby(store, eng)
    files = [("dir", "怪名字目录", ["庆余年.第01集.mp4", "庆余年.第02集.mp4"])]
    fq, t = mk(eng, {"a": {"files": files}}, ["a"], season=2, year=2024, emby_layout=True, savepath="/剧/庆余年 (2024) {tmdb-1}")
    eng.run_task(t["id"], manual=True)
    d = fq.dirs[fq.path2fid["/剧/庆余年 (2024) {tmdb-1}/Season 2"]]
    assert sorted(d.values()) == ["庆余年 (2024) - S02E01.mp4", "庆余年 (2024) - S02E02.mp4"]     # 子目录默认不保留：压平进季文件夹，统一命名
    assert "/剧/庆余年 (2024) {tmdb-1}" not in fq.path2fid          # 只建到 Season 2 这一层，上面不会有零散文件
    assert eng.progress(t)["have_count"] == 2                      # 看进度也是读季文件夹
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 2                                      # 再扫一次：已有，不重复转


def test_without_layout_nothing_changes(env):
    store, eng = env; no_emby(store, eng)
    fq, t = mk(eng, {"a": {"files": ["第01集.mp4"]}}, ["a"])
    eng.run_task(t["id"], manual=True)
    assert list(fq.dir.values()) == ["S01E01.mp4"]


def test_season_zero_goes_to_specials(env):
    store, eng = env; no_emby(store, eng)
    t = {"savepath": "/x/y", "season": 0, "emby_layout": True}
    assert eng._dest(t) == "/x/y/Specials" and eng._dest({**t, "season": 3}) == "/x/y/Season 3"
    assert eng._dest({**t, "media": "movie"}) == "/x/y" and eng._dest({**t, "emby_layout": False}) == "/x/y"


# ------------------------------------------------------------------ 异形名转存
def test_single_weird_video_is_saved_as_the_one_missing_episode(env):
    store, eng = env; no_emby(store, eng)
    shares = {"w": {"files": [("dir", "乱七八糟的目录名", [("Weird.Name.x.2160p.WEB-DL.mkv", 2 * G)])]}}
    fq, t = mk(eng, shares, ["w"], kind="subscription", total_episodes=8, savepath="/剧/庆余年")
    fq.dir.update({f"e{i}": f"S01E{i:02d}.mkv" for i in range(1, 8)})        # 已有 1~7 集，只差第 8 集
    row = eng.preview(t)["links"][0]["rows"][0]                      # 试运行里也要说清楚为什么转
    assert row["action"] == "save" and row["eps"] == "S01E08" and "按你缺的这一集处理" in row["why"]
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("w", "Weird.Name.x.2160p.WEB-DL.mkv")] and "S01E08.mkv" in fq.dir.values()


def test_weird_single_video_not_guessed_when_several_episodes_missing(env):
    store, eng = env; no_emby(store, eng)
    shares = {"w": {"files": [("Weird.Name.x.2160p.WEB-DL.mkv", 2 * G)]}}
    fq, t = mk(eng, shares, ["w"], kind="subscription", total_episodes=8, savepath="/剧/庆余年")
    fq.dir.update({f"e{i}": f"S01E{i:02d}.mkv" for i in range(1, 7)})        # 缺 7、8 两集：猜不出是哪一集，不转
    eng.run_task(t["id"], manual=True)
    assert fq.saves == []


def test_episodes_beyond_season_total_are_not_saved(env):
    store, eng = env; no_emby(store, eng)
    shares = {"w": {"files": [(f"我的大叔.S01E{i:02d}.1080p.mkv", 2 * G) for i in (1, 2, 17, 28)]}}
    fq, t = mk(eng, shares, ["w"], kind="subscription", total_episodes=16, savepath="/剧/我的大叔")
    eng.run_task(t["id"], manual=True)
    assert [n for _, n in fq.saves] == ["我的大叔.S01E01.1080p.mkv", "我的大叔.S01E02.1080p.mkv"]   # E17 / E28 超过总集数 16，不转


def test_other_version_tag_in_title_is_rejected():
    names = [("我的大叔", "我的大叔")]
    t = "秋雪漫过的冬天 / 我的大叔(中国版) / 谢谢你听见我 / 你一定要幸福呀(2026)【4K.60fps】"
    assert E.Engine._other_version({"name": "我的大叔", "aliases": []}, names, t) == "中国版"
    assert E.Engine._other_version({"name": "我的大叔(中国版)", "aliases": []}, names, t) is None
    assert E.Engine._other_version({"name": "我的大叔", "aliases": []}, names, "我的大叔 全16集 4K") is None


def test_title_upto_and_single_ep_judge():
    assert E.Engine._title_upto("Doctor X 2026 更至第1集 1080p") == 1
    assert E.Engine._title_upto("某剧 更新至12集") == 12 and E.Engine._title_upto("某剧 全16集") is None
    store, eng = new_env()
    r = {"ok": True, "seasons_explicit": [], "eps_list": [(1, 1)], "eps_ok_list": [(1, 1)], "prefs_set": False, "videos": 1,
         "avg_mb": 800, "unparsed": 0, "quality": []}
    task = {"season": 1}
    assert eng._judge(task, {1}, r, set(), {1, 2}, 12, "Doctor X 更至第1集 1080p")[0] == "ok"
    assert eng._judge(task, {1}, r, set(), {1, 2}, 12, "Doctor X 全12集 1080p")[0] == "reject"
    r2 = {**r, "eps_list": [(1, 28)], "eps_ok_list": [(1, 28)]}
    assert eng._judge(task, {1}, {**r2, "eps_list": [(1, i) for i in range(1, 29)], "eps_ok_list": [(1, i) for i in range(1, 29)]}, set(), set(range(1, 17)), 16, "t")[0] == "reject"


def test_auto_find_accepts_single_weird_episode_resource():
    shares = {"one": {"files": [("dir", "怪目录", [("xYz.2160p.mkv", 2 * G)])]}}
    store, eng, fq, ps, calls, t = af_setup(items=[item("one", "庆余年 第8集 2160P")], shares=shares, season=1, total_episodes=8,
                                           emby_have={(1, i) for i in range(1, 8)})
    r = eng.auto_find(t["id"], force=True)
    assert [l["pwd_id"] for l in t["links"]] == ["one"], r
    assert "S01E08.mkv" in fq.dir.values()


def test_auto_find_movie_with_weird_dir_and_file_names_is_saved_and_renamed():
    shares = {"w": {"files": [("dir", "随便什么目录名@@", [("xYz.2160p.mkv", 8 * G)])]}}
    store, eng, fq, ps, t = TM.setup(shares, ps_items=[item("w", "某电影 2024 4K")], links_text="")
    eng.auto_find(t["id"], force=True)
    assert fq.saves == [("w", "xYz.2160p.mkv")]
    assert list(fq.dirs[fq.path2fid["/电影/某电影 (2024)"]].values()) == ["某电影 (2024).mkv"]      # 按订阅信息改名
    assert t["done"]


# ------------------------------------------------------------------ 别名搜索
class AliasPS:
    def __init__(self, by_kw): self.by_kw, self.calls = by_kw, []
    def search(self, kw): self.calls.append(kw); return list(self.by_kw.get(kw, []))


def test_falls_back_to_aliases_when_first_name_finds_nothing():
    shares = {"jol": {"files": eps(1, range(1, 17), ".2160p")}}
    store, eng, fq, _, calls, t = af_setup(items=[], shares=shares, season=1, aliases=["Joy of Life", "X"])
    ps = AliasPS({"Joy of Life": [item("jol", "Joy of Life S01 2160p 全16集")]}); eng.pansou = lambda: ps
    eng.auto_find(t["id"], force=True)
    assert ps.calls == ["庆余年", "Joy of Life"]                         # 单字别名「X」太短，不用
    assert [l["pwd_id"] for l in t["links"]] == ["jol"]
    assert any("换别名" in x["text"] for x in t["find"]["log"])


def test_stops_at_first_name_when_it_finds_enough():
    store, eng, fq, ps, calls, t = af_setup(items=[item(f"r{i}", f"庆余年 第一季 {i}") for i in range(4)], shares={f"r{i}": {"files": []} for i in range(4)}, season=1, aliases=["Joy of Life"])
    ps2 = AliasPS({"庆余年": ps.items}); eng.pansou = lambda: ps2
    eng.auto_find(t["id"], force=True)
    assert ps2.calls == ["庆余年"]


def test_alias_in_result_title_passes_the_name_filter_for_movies():
    shares = {"w": {"files": [("Some.Movie.2024.1080p.mkv", 3 * G)]}}
    store, eng, fq, _, t = TM.setup(shares, links_text="", aliases=["Some Movie"])
    ps = AliasPS({"Some Movie": [item("w", "Some Movie 2024 1080P")], "Some Movie 2024": []}); eng.pansou = lambda: ps
    eng.auto_find(t["id"], force=True)
    assert ps.calls[:2] == ["某电影", "某电影 2024"] and "Some Movie" in ps.calls and fq.saves == [("w", "Some.Movie.2024.1080p.mkv")]


# ------------------------------------------------------------------ TMDB：别名 / 演员 / 相似
def fake_tmdb(table):
    t = Tmdb({"api_key": "k"})
    t._get = lambda path, ttl=1800, **p: table[path]
    return t


def test_aliases_prefer_chinese_regions_and_drop_duplicates():
    d = {"name": "庆余年", "original_name": "庆余年",
         "alternative_titles": {"results": [{"title": "Joy of Life", "iso_3166_1": "US"}, {"title": "慶餘年", "iso_3166_1": "TW"}, {"title": "庆余年", "iso_3166_1": "CN"}, {"title": "", "iso_3166_1": "JP"}]},
         "translations": {"translations": [{"iso_639_1": "en", "data": {"name": "Joy of Life"}}, {"iso_639_1": "de", "data": {"name": "Deutsch"}}, {"iso_639_1": "ja", "data": {"name": "慶余年"}}]}}
    assert Tmdb._aliases_from(d, "tv") == ["慶餘年", "Joy of Life", "慶余年"]            # 主名不重复；台湾译名排在美国前；德语翻译不要
    m = {"title": "某电影", "original_title": "Some Movie", "alternative_titles": {"titles": [{"title": "某片", "iso_3166_1": "HK"}]}}
    assert Tmdb._aliases_from(m, "movie") == ["Some Movie", "某片"]


def test_person_credits_sorted_deduped_and_filtered():
    cc = {"cast": [{"id": 1, "media_type": "tv", "name": "A", "popularity": 5, "poster_path": "/a", "genre_ids": [18], "character": "甲"},
                   {"id": 2, "media_type": "movie", "title": "B", "popularity": 9, "poster_path": "/b", "genre_ids": [28], "release_date": "2020-01-01"},
                   {"id": 3, "media_type": "tv", "name": "访谈", "popularity": 99, "poster_path": "/c", "genre_ids": [10767]},       # 访谈：本人出镜，不要
                   {"id": 4, "media_type": "tv", "name": "无海报", "popularity": 50, "poster_path": None, "genre_ids": []},
                   {"id": 1, "media_type": "tv", "name": "A", "popularity": 4, "poster_path": "/a", "genre_ids": [18]}]}
    d = fake_tmdb({"/person/7": {"id": 7, "name": "某演员", "profile_path": "/p", "biography": "x" * 900, "combined_credits": cc}}).person(7)
    assert [(x["media"], x["id"]) for x in d["items"]] == [("movie", 2), ("tv", 1)] and d["items"][1]["character"] == "甲"
    assert d["name"] == "某演员" and len(d["biography"]) == 500 and d["profile_url"].endswith("/w185/p")


def test_person_search_filters_and_sorts():
    res = [{"id": 1, "name": "张若昀", "profile_path": "/a", "known_for_department": "Acting", "popularity": 3, "known_for": [{"name": "庆余年"}]},
           {"id": 2, "name": "无头像", "profile_path": None, "known_for": [{"name": "X"}], "popularity": 99},
           {"id": 3, "name": "无作品", "profile_path": "/c", "known_for": [], "popularity": 99},
           {"id": 4, "name": "Tom Hanks", "profile_path": "/d", "known_for_department": "Acting", "popularity": 60, "known_for": [{"title": "Cast Away"}]},
           {"id": 5, "name": "路人", "profile_path": "/e", "known_for_department": "Crew", "popularity": 1, "known_for": [{"name": "Y"}]}]
    d = fake_tmdb({"/search/person": {"results": res}}).person_search("张若昀")
    assert [x["id"] for x in d["items"]] == [1, 4]          # 名字命中的在前；别名命中但热度高的保留；低热度杂音丢掉
    assert d["items"][0]["known_for"] == ["庆余年"] and d["items"][0]["profile_url"].endswith("/w185/a")
    assert fake_tmdb({"/search/person": {"results": []}}).person_search("庆余年")["items"] == []


def test_similar_uses_recommendations_then_similar():
    rec = {"results": [{"id": 1, "name": "A", "poster_path": "/a"}, {"id": 2, "name": "B", "poster_path": None}]}
    sim = {"results": [{"id": 1, "name": "A", "poster_path": "/a"}, {"id": 3, "name": "C", "poster_path": "/c"}]}
    d = fake_tmdb({"/tv/9/recommendations": rec, "/tv/9/similar": sim}).similar("tv", 9)
    assert [x["id"] for x in d["items"]] == [1, 3] and d["items"][0]["media"] == "tv"
    d = fake_tmdb({"/movie/9/recommendations": {"results": [{"id": 5, "title": "M", "poster_path": "/m"}]}, "/movie/9/similar": {"results": []}}).similar("movie", 9)
    assert d["items"][0]["media"] == "movie"


# ------------------------------------------------------------------ 接口
def make_client(monkey_tmdb=None):
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    store, eng = new_env()
    eng.quark = lambda: FakeQuark({}); eng.emby = lambda: None
    app = create_app(store, eng)
    c = app.test_client()
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    return store, eng, c, hdr


def test_batch_delete_endpoint():
    store, eng, c, h = make_client()
    ids = [eng.create_task({"name": f"剧{i}", "savepath": f"/x/{i}", "links_text": "", "kind": "subscription"})["id"] for i in range(3)]
    assert c.post("/api/tasks/batch-delete", json={"ids": []}, headers=h).status_code == 400
    r = c.post("/api/tasks/batch-delete", json={"ids": ids[:2] + ["nope"]}, headers=h)
    assert r.status_code == 200 and r.get_json()["deleted"] == 2
    assert [t["id"] for t in store.data["tasks"]] == [ids[2]]


def test_subscription_stores_aliases_year_and_layout():
    import app.main as M
    class FT:
        ready = True
        def __init__(self, cfg): pass
        def detail(self, tid): return {"name": "庆余年", "poster_url": "/p", "overview": "o", "aliases": ["Joy of Life"], "year": 2019, "anime": False}
        def aired(self, tid, s): return {1, 2}, 2
    store, eng, c, h = make_client()
    with mock.patch.object(M, "Tmdb", FT):
        r = c.post("/api/subscriptions", json={"tmdb_id": 5, "season": 2, "savepath": "/剧/庆余年 (2019) {tmdb-5}", "emby_layout": True, "auto_find": False,
                                               "save_existing": False}, headers=h)
    t = r.get_json()["task"]
    assert r.status_code == 200 and t["aliases"] == ["Joy of Life"] and t["year"] == 2019 and t["emby_layout"] is True and t["keep_tree"] is False
    with mock.patch.object(M, "Tmdb", FT):     # 不传 emby_layout：和以前一样
        r = c.post("/api/subscriptions", json={"tmdb_id": 6, "season": 1, "savepath": "/剧/b", "auto_find": False, "save_existing": False}, headers=h)
    assert r.get_json()["task"]["emby_layout"] is False


W = "https://pan.quark.cn/s/w"


# ------------------------------------------------------------------ 画质：文件夹名 / 分享标题也参与判断；多个版本只转符合要求的、挑最好的
def test_quality_tags_look_at_folders_then_title():
    from app.parser import quality_tags as qt, quality_reason as qr
    assert qt("E01.mkv", ("4K HDR",), "某电影 全集")["src"] == {"res": "folder", "hdr": "folder"}
    assert qr([], "no", "E01.mkv", False, (), "某电影 HDR版") == "你不要 HDR，但分享标题里标了 HDR"
    assert qr([], "no", "E01.mkv", False, ("SDR",), "某电影 HDR") == ""          # 离文件近的 SDR 优先于标题里的 HDR
    assert qr(["1080p"], "any", "E01.mkv", False, (), "某电影 4K 1080P") == ""     # 标题里列了好几种分辨率：说不准，放行
    assert qr([], "yes", "E01.mkv", False, (), "某电影 DoVi") == "" and qr([], "no", "E01.HLG.mkv") != ""    # DoVi / HLG 也认
    assert qr([], "no", "E01.mkv", False, (), "某电影 杜比视界") != ""


def test_movie_folder_with_hdr_and_plain_versions_transfers_only_the_plain_one():
    shares = {"w": {"files": [("某电影.2024.2160p.HDR.mkv", 9 * G), ("某电影.2024.1080p.mkv", 3 * G), ("某电影.2024.2160p.DV.mkv", 8 * G)]}}
    store, eng, fq, ps, t = TM.setup(shares, want_hdr="no", links_text=W)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("w", "某电影.2024.1080p.mkv")]                    # HDR / 杜比视界的版本都不要，哪怕画质更高
    store, eng, fq, ps, t = TM.setup(shares, want_hdr="yes", links_text=W)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("w", "某电影.2024.2160p.HDR.mkv")]                # 只要 HDR：HDR 里再挑体积最大的


def test_movie_hdr_only_in_folder_names():
    shares = {"w": {"files": [("dir", "4K HDR", [("a.mkv", 9 * G)]), ("dir", "1080P 普通版", [("b.mp4", 3 * G)])]}}
    store, eng, fq, ps, t = TM.setup(shares, want_hdr="no", links_text=W)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("w", "b.mp4")]
    rows = eng.preview(t)["links"][0]["rows"]
    assert any(r["file"] == "a.mkv" and "文件夹" in r["why"] and r["action"] == "skip" for r in rows)


def test_auto_find_rejects_when_only_the_share_title_says_hdr():
    shares = {"w": {"files": [("xx.mkv", 8 * G)]}, "ok": {"files": [("yy.mkv", 8 * G)]}}
    store, eng, fq, ps, t = TM.setup(shares, ps_items=[item("w", "某电影 2024 4K HDR 杜比视界"), item("ok", "某电影 2024 1080P")], links_text="", want_hdr="no")
    eng.auto_find(t["id"], force=True)
    assert [l["pwd_id"] for l in t["links"]] == ["ok"]
    assert t["find"]["seen"]["w"]["verdict"] == "reject" and "分享标题" in t["find"]["seen"]["w"]["reason"]


def test_mixed_share_is_kept_but_skips_unwanted_versions_and_says_so():
    shares = {"w": {"files": [("某电影.HDR.mkv", 9 * G), ("某电影.1080p.mkv", 3 * G)]}}
    store, eng, fq, ps, t = TM.setup(shares, ps_items=[item("w", "某电影 2024 多版本")], links_text="", want_hdr="no")
    eng.auto_find(t["id"], force=True)
    assert [l["pwd_id"] for l in t["links"]] == ["w"] and "不会转存" in t["find"]["seen"]["w"]["reason"] + str(t["find"]["log"])
    assert fq.saves == [("w", "某电影.1080p.mkv")]


def test_tv_same_episode_in_several_versions_picks_the_best_and_respects_hdr_folders(env):
    store, eng = env; no_emby(store, eng)
    files = [("庆余年.S01E01.1080p.mkv", G), ("庆余年.S01E01.2160p.mkv", 3 * G), ("庆余年.S01E02.1080p.mkv", G)]
    fq, t = mk(eng, {"a": {"files": files}}, ["a"])
    eng.run_task(t["id"], manual=True)
    assert sorted(n for _, n in fq.saves) == ["庆余年.S01E01.2160p.mkv", "庆余年.S01E02.1080p.mkv"]      # 同一集 1080P 和 4K 都有：转 4K（以前按文件名顺序会转 1080P）
    store, eng = new_env(); no_emby(store, eng)
    files = [("dir", "4K HDR", ["第01集.mkv", "第02集.mkv"]), ("dir", "1080P", ["第01集.mp4", "第02集.mp4"])]
    fq, t = mk(eng, {"a": {"files": files}}, ["a"], want_hdr="no")
    eng.run_task(t["id"], manual=True)
    assert sorted(n for _, n in fq.saves) == ["第01集.mp4", "第02集.mp4"] and sorted(fq.dir.values()) == ["S01E01.mp4", "S01E02.mp4"]


def test_tv_detail_reports_subscribed_seasons():
    import app.main as M
    class FT:
        ready = True
        def __init__(self, cfg): pass
        def detail(self, tid): return {"id": tid, "name": "庆余年", "seasons": [], "first_air_date": "2019-11-26"}
    with mock.patch.object(M, "Tmdb", FT):
        store, eng, c, h = make_client()
        store.data["tmdb"] = {"api_key": "k"}
        for sn in (1, 2):
            eng.create_task({"name": f"庆余年 S{sn}", "savepath": f"/x/{sn}", "links_text": "", "kind": "subscription",
                             "media": "tv", "tmdb_id": 77, "season": sn})
        d = c.get("/api/tmdb/tv/77", headers=h).get_json()
        assert set(d["subscribed_seasons"]) == {"1", "2"} and d["sub_id"]
        assert c.get("/api/tmdb/tv/78", headers=h).get_json()["subscribed_seasons"] == {}
