# -*- coding: utf-8 -*-
"""影视推荐的三个板块：剧集 / 电影 / 动漫。剧集和电影里不能出现动画，动画统一归「动漫」。"""
import base64, os, sys, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
time.sleep = lambda *a, **k: None
from app.tmdb import Tmdb
from tests.test_engine import env


def raw(i, genres=(18,), title=None, poster=True):
    return {"id": i, "name": title or f"剧{i}", "title": title or f"片{i}", "genre_ids": list(genres), "poster_path": "/p.jpg" if poster else None,
            "first_air_date": "2024-01-01", "release_date": "2024-01-01", "vote_average": 7}


def client(results_by_path):
    t, calls = Tmdb({"api_key": "k"}), []
    def fake(path, ttl=1800, **params):
        calls.append((path, params))
        r = results_by_path.get(path, [])
        if isinstance(r, Exception): raise r
        return {"results": r, "total_pages": 3}
    t._get = fake
    return t, calls


def test_cards_carry_media_and_genres():
    t, _ = client({"/trending/tv/week": [raw(1)], "/trending/movie/week": [raw(2)]})
    assert t.trending()["items"][0]["media"] == "tv" and t.movie_discover("trending")["items"][0]["media"] == "movie"
    assert t.trending()["items"][0]["genre_ids"] == [18]


def test_tv_board_excludes_animation_everywhere():
    t, calls = client({"/trending/tv/week": [raw(1), raw(2, (16, 10759)), raw(3, (16,)), raw(4, (35,))], "/discover/tv": [raw(5)]})
    assert [x["id"] for x in t.trending()["items"]] == [1, 4]                              # 趋势接口不能过滤，手动把动画去掉
    t.discover("cn"); p = calls[-1][1]
    assert p["without_genres"] == "16|10764|10767|99|10762" and p["with_origin_country"] == "CN|TW|HK" and "with_genres" not in p   # 动画归动漫、综艺归综艺、纪录片 / 儿童单列（同 MoviePilot）
    t.discover("kr_jp", "rating"); assert calls[-1][1]["without_genres"] == "16|10764|10767|99|10762" and calls[-1][1]["sort_by"] == "vote_average.desc"
    assert set(calls[-1][1]["with_origin_country"].split("|")) == {"JP", "KP", "KR", "TH", "IN", "SG"}   # 泰国 / 印度 / 新加坡归日韩剧
    t.discover("docu"); assert calls[-1][1]["with_genres"] == "99"
    t.discover("kids"); assert calls[-1][1]["with_genres"] == "10762"


def test_anime_board_only_animation_with_its_own_regions():
    t, calls = client({"/discover/tv": [raw(5, (16,))]})
    for cat, country in (("jp", "JP"), ("cn", "CN|TW|HK")):
        t.discover(cat, anime=True); p = calls[-1][1]
        assert p["with_genres"] == "16" and p["with_origin_country"] == country and "without_genres" not in p
    t.discover("all", anime=True); assert "with_origin_country" not in calls[-1][1]
    t.discover("us_eu", anime=True); assert "|" in calls[-1][1]["with_origin_country"]


def test_movie_board_excludes_animation_and_supports_sort_and_regions():
    t, calls = client({"/trending/movie/week": [raw(1), raw(2, (16,)), raw(3, (28, 12))], "/discover/movie": [raw(4)]})
    assert [x["id"] for x in t.movie_discover("trending")["items"]] == [1, 3]
    t.movie_discover("cn"); p = calls[-1][1]; assert p["without_genres"] == "16" and p["with_origin_country"] == "CN|HK|TW" and p["sort_by"] == "popularity.desc"
    t.movie_discover("all", 1, "rating"); p = calls[-1][1]; assert p["sort_by"] == "vote_average.desc" and p["vote_count.gte"] == 500 and "with_origin_country" not in p
    t.movie_discover("all", 1, "latest"); p = calls[-1][1]; assert p["sort_by"] == "primary_release_date.desc" and "primary_release_date.lte" in p
    t.movie_discover("top"); assert calls[-1][1]["sort_by"] == "vote_average.desc"
    t.movie_discover("jp", anime=True); p = calls[-1][1]; assert p["with_genres"] == "16" and p["with_origin_country"] == "JP" and "without_genres" not in p


def test_anime_search_merges_tv_and_movies_but_only_animation():
    t, calls = client({"/search/tv": [raw(1, (16,)), raw(2, (18,))], "/search/movie": [raw(3, (16, 10751)), raw(4, (28,))]})
    items = t.anime_search("某某")["items"]
    assert [(x["media"], x["id"]) for x in items] == [("tv", 1), ("movie", 3)]                # 番剧在前，动画电影在后；真人的都不要
    assert {c[0] for c in calls} == {"/search/tv", "/search/movie"}
    assert [x["id"] for x in t.search("某某")["items"]] == [2]                                 # 剧集板块的搜索也只留普通剧集（动画去动漫搜、综艺去综艺搜）


def test_home_rows_per_board_and_one_failure_does_not_break_the_rest():
    t, _ = client({"/trending/tv/week": [raw(1)], "/discover/tv": [raw(2)], "/trending/movie/week": [raw(3)], "/discover/movie": [raw(4)]})
    assert [r["label"] for r in t.home()] == ["🔥 本周热门", "🇨🇳 国产剧", "🇰🇷 日韩剧", "🇺🇸 欧美剧", "📚 纪录片", "🧒 儿童"]
    assert [r["label"] for r in t.movie_home()] == ["🔥 本周热门", "🇨🇳 华语", "🇰🇷 日韩", "🇺🇸 欧美", "⭐ 高分经典"]
    rows = t.anime_home()
    assert [r["label"] for r in rows] == ["🔥 热门动漫", "🇯🇵 日本动画", "🇨🇳 国产动画", "🎞 动画电影"] and [r["category"] for r in rows][-1] == "movie"
    assert rows[-1]["items"][0]["media"] == "movie" and rows[0]["items"][0]["media"] == "tv"       # 动漫板块里番剧和动画电影混排
    t2, _ = client({"/trending/tv/week": RuntimeError("boom"), "/discover/tv": [raw(2)]})
    r = t2.home(); assert r[0]["err"] == "boom" and r[0]["items"] == [] and r[1]["items"]


def test_detail_marks_animation():
    t = Tmdb({"api_key": "k"})
    t._get = lambda path, ttl=1800, **p: {"id": 1, "name": "番", "title": "片", "genres": [{"id": 16, "name": "动画"}, {"id": 35, "name": "喜剧"}], "seasons": [], "credits": {}}
    assert t.detail(1)["anime"] is True and t.movie_detail(1)["anime"] is True
    t._get = lambda path, ttl=1800, **p: {"id": 1, "name": "剧", "title": "片", "genres": [{"id": 18, "name": "剧情"}], "seasons": [], "credits": {}}
    assert t.detail(1)["anime"] is False and t.movie_detail(1)["anime"] is False


# ---------------------------------------------------------------- 目录推荐：动漫单独一类
def test_anime_directories_are_remembered_separately():
    store, eng = env()
    eng.create_task({"name": "番", "savepath": "/影视/动漫/番", "links_text": "", "kind": "subscription", "tmdb_id": 1, "anime": True})
    eng.create_task({"name": "剧", "savepath": "/影视/剧集/剧", "links_text": "", "kind": "subscription", "tmdb_id": 2})
    eng.create_task({"name": "片", "savepath": "/影视/电影/片 (2024)", "links_text": "", "media": "movie", "tmdb_id": 3, "year": 2024})
    eng.create_task({"name": "动画片", "savepath": "/影视/动漫/动画片 (2023)", "links_text": "", "media": "movie", "tmdb_id": 4, "year": 2023, "anime": True})
    kinds = {d["path"]: d["media"] for d in store.data["recent_dirs"]}
    assert kinds == {"/影视/动漫/番": "anime", "/影视/剧集/剧": "tv", "/影视/电影/片 (2024)": "movie", "/影视/动漫/动画片 (2023)": "anime"}   # 动画电影归动漫
    assert eng.public_task(eng.find(eng.create_task({"name": "x", "savepath": "/x", "links_text": "", "anime": True})["id"]))["anime"] is True


def test_seeded_recent_dirs_use_the_same_kinds():
    import tempfile
    from app.engine import Engine
    from app.store import Store
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json")); base = {"links": [], "kind": "subscription", "season": 1}
    store.data["tasks"] = [{**base, "id": "a", "name": "番", "savepath": "/动漫/番", "created": 3, "anime": True},
                           {**base, "id": "b", "name": "片", "savepath": "/电影/片", "created": 2, "media": "movie"},
                           {**base, "id": "c", "name": "剧", "savepath": "/剧集/剧", "created": 1}]
    Engine(store)
    assert [(d["path"], d["media"]) for d in store.data["recent_dirs"]] == [("/动漫/番", "anime"), ("/电影/片", "movie"), ("/剧集/剧", "tv")]


# ---------------------------------------------------------------- HTTP
def test_api_scopes_and_mixed_annotation():
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    store, eng = env(); store.data["tmdb"]["api_key"] = "k"
    c = create_app(store, eng).test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    eng.create_task({"name": "某番", "savepath": "/a", "links_text": "", "kind": "subscription", "tmdb_id": 5, "anime": True})
    eng.create_task({"name": "某片", "savepath": "/b", "links_text": "", "media": "movie", "tmdb_id": 6, "year": 2024})
    card = lambda i, m: {"id": i, "name": f"{m}{i}", "year": "", "vote_average": 0, "overview": "", "poster_url": "", "media": m, "genre_ids": [16]}
    mixed = [card(5, "tv"), card(5, "movie"), card(6, "movie")]
    with mock.patch.object(Tmdb, "anime_home", return_value=[{"label": "L", "category": "all", "items": mixed}]), \
         mock.patch.object(Tmdb, "home", return_value=[{"label": "TV", "category": "all", "items": [card(5, "tv")]}]), \
         mock.patch.object(Tmdb, "movie_home", return_value=[{"label": "MV", "category": "all", "items": [card(6, "movie")]}]):
        rows = c.get("/api/tmdb/home?scope=anime", headers=h).get_json()["rows"]
        flags = [(it["media"], it["id"], bool(it["sub_id"])) for it in rows[0]["items"]]
        assert flags == [("tv", 5, True), ("movie", 5, False), ("movie", 6, True)]               # 剧集 5 和电影 5 是两部不同的作品
        assert c.get("/api/tmdb/home?scope=movie", headers=h).get_json()["rows"][0]["label"] == "MV"
        assert c.get("/api/tmdb/home?scope=bogus", headers=h).get_json()["rows"][0]["label"] == "TV"      # 非法值回退到剧集
        assert c.get("/api/tmdb/home", headers=h).get_json()["rows"][0]["label"] == "TV"
    with mock.patch.object(Tmdb, "discover", return_value={"items": [card(5, "tv")], "total_pages": 1}) as d1, \
         mock.patch.object(Tmdb, "movie_discover", return_value={"items": [card(6, "movie")], "total_pages": 1}) as d2:
        c.get("/api/tmdb/discover?scope=tv&category=cn&sort=rating&page=2", headers=h); assert d1.call_args[0] == ("cn", "rating", 2) and not d1.call_args[1]
        c.get("/api/tmdb/discover?scope=movie&category=us_eu&sort=latest", headers=h); assert d2.call_args[0] == ("us_eu", 1, "latest")
        d1.reset_mock(); d2.reset_mock()
        c.get("/api/tmdb/discover?scope=anime&category=jp&sort=popularity", headers=h); assert d1.call_args[0] == ("jp", "popularity", 1, True)
        c.get("/api/tmdb/discover?scope=anime&category=movie&sort=rating", headers=h); assert d2.call_args[0] == ("all", 1, "rating", True)   # 动画电影
    with mock.patch.object(Tmdb, "anime_search", return_value={"items": mixed, "total_pages": 1}) as sa, \
         mock.patch.object(Tmdb, "movie_search", return_value={"items": [card(6, "movie")], "total_pages": 1}) as sm, \
         mock.patch.object(Tmdb, "search", return_value={"items": [card(5, "tv")], "total_pages": 1}) as st:
        c.get("/api/tmdb/search?scope=anime&q=番", headers=h); c.get("/api/tmdb/search?scope=movie&q=片", headers=h); c.get("/api/tmdb/search?q=剧", headers=h)
        assert sa.called and sm.called and st.called and c.get("/api/tmdb/search?scope=anime&q=", headers=h).get_json()["items"] == []
    assert c.get("/api/tmdb/home?scope=anime").status_code == 401


def test_subscription_records_anime_flag_from_tmdb():
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    store, eng = env(); store.data["tmdb"]["api_key"] = "k"
    c = create_app(store, eng).test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    tv = {"id": 1, "name": "某番", "poster_url": "", "overview": "", "first_air_date": "", "vote_average": 0, "seasons": [], "anime": True}
    mv = {"id": 2, "name": "某动画片", "poster_url": "", "overview": "", "year": "2023", "release_date": "2023-01-01", "anime": True}
    with mock.patch.object(Tmdb, "detail", return_value=tv), mock.patch.object(Tmdb, "movie_detail", return_value=mv), \
         mock.patch.object(eng, "auto_find"), mock.patch.object(eng, "run_task"):
        a = c.post("/api/subscriptions", json={"tmdb_id": 1, "season": 1, "savepath": "/动漫/某番", "links_text": ""}, headers=h).get_json()["task"]
        b = c.post("/api/subscriptions", json={"media": "movie", "tmdb_id": 2, "savepath": "/动漫/某动画片 (2023)", "links_text": ""}, headers=h).get_json()["task"]
    assert a["anime"] and b["anime"] and a["media"] == "tv" and b["media"] == "movie"
    assert [d["media"] for d in store.data["recent_dirs"]] == ["anime", "anime"]
