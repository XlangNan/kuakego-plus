# -*- coding: utf-8 -*-
"""四个板块：剧集 / 电影 / 动漫 / 综艺 各自独立，互不掺合。"""
from unittest import mock

from app.tmdb import Tmdb, section_of
from tests.test_layout_aliases import make_client

TV, MV, ANI, VAR, TALK = 18, 28, 16, 10764, 10767


class FakeTmdb(Tmdb):
    def __init__(self):
        super().__init__({"api_key": "k"})
        self.calls = []

    def _get(self, path, ttl=1800, **params):
        self.calls.append((path, params))
        if path.startswith("/person/"):
            return {"id": 1, "name": "某人", "combined_credits": {"cast": [
                {"id": 1, "media_type": "tv", "name": "剧", "genre_ids": [TV], "poster_path": "/a", "popularity": 9},
                {"id": 2, "media_type": "tv", "name": "真人秀", "genre_ids": [VAR], "poster_path": "/b", "popularity": 8},
                {"id": 3, "media_type": "tv", "name": "访谈", "genre_ids": [TALK], "poster_path": "/c", "popularity": 7},
                {"id": 4, "media_type": "tv", "name": "番", "genre_ids": [ANI], "poster_path": "/d", "popularity": 6},
                {"id": 5, "media_type": "movie", "title": "电影", "genre_ids": [MV], "poster_path": "/e", "popularity": 5},
                {"id": 6, "media_type": "movie", "title": "动画电影", "genre_ids": [ANI], "poster_path": "/f", "popularity": 4},
                {"id": 7, "media_type": "tv", "name": "新闻", "genre_ids": [10763], "poster_path": "/g", "popularity": 3}]}}
        if path.startswith("/search/tv"):
            return {"results": [{"id": 1, "name": "剧", "genre_ids": [TV]}, {"id": 2, "name": "秀", "genre_ids": [VAR]},
                                {"id": 3, "name": "番", "genre_ids": [ANI]}, {"id": 4, "name": "番综", "genre_ids": [ANI, VAR]}]}
        if path.startswith("/search/movie"):
            return {"results": [{"id": 5, "title": "片", "genre_ids": [MV]}, {"id": 6, "title": "动画片", "genre_ids": [ANI]}]}
        if path == "/trending/tv/week":
            return {"results": [{"id": 1, "name": "剧", "genre_ids": [TV], "poster_path": "/a"}, {"id": 2, "name": "秀", "genre_ids": [VAR], "poster_path": "/b"},
                                {"id": 3, "name": "番", "genre_ids": [ANI], "poster_path": "/c"}]}
        if path == "/tv/7":
            return {"id": 7, "name": "某综艺", "genres": [{"id": VAR}], "seasons": []}
        if path.startswith("/tv/") and path.endswith(("recommendations", "similar")):
            return {"results": [{"id": 10, "name": "同类综艺", "genre_ids": [VAR], "poster_path": "/x"}, {"id": 11, "name": "剧", "genre_ids": [TV], "poster_path": "/y"}]}
        if path.startswith("/tv/"):
            return {"id": 7, "name": "某综艺", "genres": [{"id": VAR}], "seasons": []}
        return {"results": []}


def test_section_of():
    assert section_of("tv", [TV]) == "tv" and section_of("tv", [VAR]) == "variety" and section_of("tv", [TALK]) == "variety"
    assert section_of("tv", [ANI, VAR]) == "anime"          # 动画优先
    assert section_of("movie", [MV]) == "movie" and section_of("movie", [ANI]) == "anime"
    assert section_of("movie", [VAR]) == "movie"             # 电影没有综艺板块


def test_person_items_carry_sections_and_news_dropped():
    items = FakeTmdb().person(1)["items"]
    assert {i["name"]: i["section"] for i in items} == {"剧": "tv", "真人秀": "variety", "访谈": "variety", "番": "anime", "电影": "movie", "动画电影": "anime"}


def test_each_board_searches_only_its_own_section():
    t = FakeTmdb()
    assert [i["name"] for i in t.search("x")["items"]] == ["剧"]
    assert [i["name"] for i in t.variety_search("x")["items"]] == ["秀"]
    assert [i["name"] for i in t.movie_search("x")["items"]] == ["片"]
    assert [i["name"] for i in t.anime_search("x")["items"]] == ["番", "番综", "动画片"]
    assert [i["name"] for i in t.trending()["items"]] == ["剧"]


def test_discover_filters_genres():
    t = FakeTmdb()
    t.discover("cn")
    assert t.calls[-1][1]["without_genres"] == "16|10764|10767|99|10762"      # 剧集板块排除动画、综艺，纪录片 / 儿童单列
    t.variety_discover("cn")
    p = t.calls[-1][1]
    assert p["with_genres"] == "10764|10767" and p["without_genres"] == "16" and p["with_origin_country"] == "CN|TW|HK"


def test_similar_stays_in_same_section():
    t = FakeTmdb()
    names = [i["name"] for i in t.similar("tv", 7)["items"]]
    assert names == ["同类综艺"]


def test_variety_scope_endpoints_and_subscription_flag():
    import app.main as M
    class FT(FakeTmdb):
        def __init__(self, cfg=None): super().__init__()
        def detail(self, tid):
            return {"id": tid, "name": "某综艺", "poster_url": "/p", "overview": "", "aliases": [], "year": 2024, "anime": False, "variety": True}
    with mock.patch.object(M, "Tmdb", FT):
        store, eng, c, h = make_client()
        store.data["tmdb"] = {**store.data["tmdb"], "api_key": "k"}
        rows = c.get("/api/tmdb/home?scope=variety", headers=h).get_json()["rows"]
        assert [r["label"] for r in rows][0] == "🔥 热门综艺"
        assert c.get("/api/tmdb/search?scope=variety&q=x", headers=h).status_code == 200
        r = c.post("/api/subscriptions", json={"tmdb_id": 7, "season": 1, "savepath": "/影视/综艺/某综艺", "name": "某综艺", "auto_find": False}, headers=h)
        assert r.status_code == 200, r.get_data(as_text=True)
        t = eng.find(r.get_json()["task"]["id"])
        assert t["variety"] is True and eng.public_task(t)["variety"] is True and eng._dir_kind(t) == "variety"
        assert store.data["recent_dirs"][0]["media"] == "variety"       # 综艺单独记目录


def test_backfill_marks_old_variety_subscriptions_once():
    store, eng, c, h = make_client()
    class FT:
        ready = True
        n = 0
        def detail(self, tid):
            FT.n += 1
            return {"variety": tid == 7, "anime": tid == 8}
    eng.tmdb = lambda: FT()
    a = eng.create_task({"name": "综艺", "savepath": "/a", "links_text": "", "kind": "subscription", "tmdb_id": 7})
    b = eng.create_task({"name": "番", "savepath": "/b", "links_text": "", "kind": "subscription", "tmdb_id": 8})
    for t in (a, b):
        t.pop("variety", None)
    assert eng.maint.backfill_sections() == 2
    assert a["variety"] is True and b["variety"] is False and b["anime"] is True
    assert eng.maint.backfill_sections() == 0 and FT.n == 2       # 查过的不再查
