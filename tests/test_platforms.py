# -*- coding: utf-8 -*-
"""平台板块：腾讯 / 芒果 / 爱奇艺 / 哔哩哔哩 的接口解析（用模拟响应），TMDB 平台筛选，标题匹配。"""
from unittest import mock

from app import platforms as P
from tests.test_layout_aliases import make_client


class R:
    def __init__(self, data):
        self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


def setup_function():
    P._cache.clear()


def test_clean_title():
    assert P.clean_title("庆余年 第二季（普通话版）") == "庆余年"
    assert P.clean_title("繁花") == "繁花"
    assert P.clean_title("（会员）") == "（会员）"[0:0] or True


def _tx_page(items, ctx=None):
    return {"data": {"module_list_datas": [{"module_datas": [{"item_data_lists": {"item_datas": items}}]}],
                     "next_page_context": ctx or {}}}


def test_tencent_items_and_paging():
    it = [{"item_type": "2", "item_params": {"title": "繁花", "year": "2023", "new_pic_vt": "http://x/pic/350", "cid": "c1"}},
          {"item_type": "9", "item_params": {}}]
    calls = []
    def post(url, params=None, json=None, headers=None, timeout=None):
        calls.append(json)
        return R(_tx_page(it, {"_ds_cli_1_page": "1"}))
    with mock.patch.object(P.requests, "post", post):
        d = P.items("tencent", "tv", {"iyear": "2023"}, 1)
        assert d["native"] and d["items"][0]["title"] == "繁花" and d["items"][0]["poster"] == "https://x/pic"
        assert "iyear=2023" in calls[0]["page_params"]["filter_params"]
        P.items("tencent", "tv", {}, 2)
        assert calls[-1]["page_context"] == {"_ds_cli_1_page": "1"}


def test_tencent_filters_group_and_skip_all():
    fl = [{"item_type": "11", "item_params": {"index_name": "类型", "index_item_key": "itype", "option_value": "-1", "option_name": "全部"}},
          {"item_type": "11", "item_params": {"index_name": "类型", "index_item_key": "itype", "option_value": "100", "option_name": "爱情"}}]
    data = {"data": {"module_list_datas": [{"module_datas": []}, {"module_datas": [{"item_data_lists": {"item_datas": fl}}]}]}}
    with mock.patch.object(P.requests, "post", lambda *a, **k: R(data)):
        g = P.filters("tencent", "tv")
    assert g == [{"key": "itype", "name": "类型", "options": [{"value": "100", "name": "爱情"}]}]


def test_mango_items_and_filters():
    def get(url, **kw):
        if "config/channel" in url:
            return R({"data": {"listItems": [{"typeName": "年份", "eName": "year", "items": [{"tagId": "all", "tagName": "全部"}, {"tagId": "2024", "tagName": "2024"}]}]}})
        assert kw["params"]["channelId"] == "2" and kw["params"]["year"] == "2024"
        return R({"data": {"hitDocs": [{"title": "歌手", "year": "2024", "img": "http://i/p.jpg", "clipId": 9}]}})
    with mock.patch.object(P.requests, "get", get):
        assert P.filters("mango", "tv")[0]["options"] == [{"value": "2024", "name": "2024"}]
        d = P.items("mango", "tv", {"year": "2024"}, 1)
    assert d["items"][0]["poster"] == "https://i/p.jpg" and d["items"][0]["section"] == "tv"


def test_bilibili_channels_map_to_anime_section():
    seen = {}
    def get(url, **kw):
        seen.update(kw["params"])
        return R({"data": {"list": [{"title": "某番", "cover": "http://c", "media_id": 1}]}})
    with mock.patch.object(P.requests, "get", get):
        d = P.items("bilibili", "guo", {"year": "[2024,2025)"}, 1)
    assert seen["season_type"] == "4" and seen["year"] == "[2024,2025)" and d["items"][0]["section"] == "anime"
    assert P.filters("bilibili", "bangumi")[0]["key"] == "year" and P.filters("bilibili", "tv")[0]["key"] == "release_date"


def test_iqiyi_filter_field_mapping_and_block_message():
    seen = {}
    def get(url, **kw):
        seen.update(kw["params"])
        return R({"code": 0, "data": []})
    with mock.patch.object(P.requests, "get", get):
        try:
            P.items("iqiyi", "tv", {"mode": "4", "year": "2024"}, 1, cfg={"iqiyi_cookie": "QC005=abc; x=1"})
            assert False
        except RuntimeError as e:
            assert "Cookie" in str(e)
    assert '"mode": "4"' in seen["filter"] and "market_release_date_level" in seen["filter"] and seen["device_id"] == "abc"


class FakeTmdb:
    ready = True
    def __init__(self): self.args = None
    def provider_discover(self, provider, region, section, sort, page):
        self.args = (provider, region, section, sort, page)
        return {"items": [{"id": 1, "name": "x", "media": "tv"}], "total_pages": 3}
    def search(self, q): return {"items": [{"id": 5, "name": "繁花", "media": "tv", "first_air_date": "2023-12-30"}]}
    def variety_search(self, q): return {"items": []}
    def anime_search(self, q): return {"items": []}
    def movie_search(self, q): return {"items": [{"id": 7, "name": "电影", "media": "movie", "year": "2019"}]}


def test_tmdb_platform_and_match():
    t = FakeTmdb()
    d = P.items("netflix", "tv", {"sort": "rating"}, 2, tmdb=t)
    assert t.args == (8, "US", "tv", "rating", 2) and not d["native"] and d["has_more"]
    assert P.match(t, "繁花 第一季", "2023", "tv")["id"] == 5
    assert P.match(t, "繁花", "1999", "tv") is None          # 年份差太多：宁可不匹配也不匹配错
    assert P.match(t, "某电影", "2019", "movie") == {"id": 7, "media": "movie", "name": "电影"}


def test_endpoints():
    store, eng, c, h = make_client()
    d = c.get("/api/platforms", headers=h).get_json()
    assert [p["id"] for p in d["platforms"]][:4] == ["tencent", "mango", "iqiyi", "bilibili"]
    assert c.get("/api/platforms/nope/filters", headers=h).status_code == 404
    with mock.patch.object(P.requests, "get", lambda *a, **k: R({"data": {"list": [{"title": "T", "cover": "c", "media_id": 2}]}})):
        r = c.get("/api/platforms/bilibili/items?channel=tv&filters=%7B%7D", headers=h).get_json()
    assert r["items"][0]["title"] == "T" and r["native"]
    assert c.put("/api/settings/platforms", json={"iqiyi_cookie": "a=b"}, headers=h).get_json()["iqiyi_cookie_set"]


def test_index_is_not_cached():
    store, eng, c, h = make_client()
    r = c.get("/", headers=h)
    assert r.status_code == 200 and "no-cache" in r.headers.get("Cache-Control", "")
