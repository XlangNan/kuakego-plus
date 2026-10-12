# -*- coding: utf-8 -*-
from datetime import date, timedelta
from unittest import mock

from tests.test_layout_aliases import make_client


def iso(n):
    return (date.today() + timedelta(days=n)).isoformat()


class FT:
    ready = True
    def __init__(self, cfg): pass
    def season_episodes(self, tid, season):
        # 第 1、2 集已播，第 3 集今天，第 4 集明天，第 5 集没日期（不返回）
        return [{"ep": 1, "name": "一", "air_date": iso(-9)}, {"ep": 2, "name": "二", "air_date": iso(-2)},
                {"ep": 3, "name": "三", "air_date": iso(0)}, {"ep": 4, "name": "四", "air_date": iso(1)}]


def test_calendar_episodes_movies_status_and_range():
    import app.main as M
    store, eng, c, h = make_client()
    tv = eng.create_task({"name": "某剧", "savepath": "/剧/某剧", "links_text": "", "kind": "subscription", "tmdb_id": 7, "season": 1, "poster": "/p.jpg"})
    tv["snap"] = {"have": 1, "aired": 3, "total": 4, "missing": [2, 3], "missing_n": 2, "ts": 1}
    mv = eng.create_task({"name": "某电影", "savepath": "/电影/某电影", "links_text": "", "kind": "subscription", "media": "movie", "tmdb_id": 8, "release_date": iso(-1)})
    mon = eng.create_task({"name": "监控项", "savepath": "/x", "links_text": "", "kind": "monitor"})      # 监控不进日历
    with mock.patch.object(M, "Tmdb", FT):
        r = c.get(f"/api/calendar?start={iso(-30)}&end={iso(30)}", headers=h)
        ev = r.get_json()["events"]
        by = {(e["name"], e["ep"]): e for e in ev}
        assert r.status_code == 200 and len(ev) == 5 and "监控项" not in {e["name"] for e in ev}
        assert by[("某剧", 1)]["status"] == "have"          # 已播、不在缺集里
        assert by[("某剧", 2)]["status"] == "missing" and by[("某剧", 3)]["status"] == "missing"
        assert by[("某剧", 4)]["status"] == "unaired" and by[("某剧", 4)]["ep_name"] == "四"
        assert by[("某电影", 0)]["status"] == "missing" and by[("某电影", 0)]["media"] == "movie"     # 已上映、没转存
        assert [e["date"] for e in ev] == sorted(e["date"] for e in ev)
        ev = c.get(f"/api/calendar?start={iso(-3)}&end={iso(0)}", headers=h).get_json()["events"]
        assert {e["ep"] for e in ev if e["name"] == "某剧"} == {2, 3}                   # 只返回范围内的
        assert c.get("/api/calendar?start=x&end=y", headers=h).status_code == 400
        assert c.get(f"/api/calendar?start={iso(5)}&end={iso(1)}", headers=h).status_code == 400
    mv["done"] = True
    with mock.patch.object(M, "Tmdb", FT):
        ev = c.get(f"/api/calendar?start={iso(-30)}&end={iso(30)}", headers=h).get_json()["events"]
    assert [e["status"] for e in ev if e["media"] == "movie"] == ["have"]


def test_calendar_missing_list_truncated_means_later_eps_missing():
    import app.main as M
    store, eng, c, h = make_client()
    t = eng.create_task({"name": "长剧", "savepath": "/剧/长剧", "links_text": "", "kind": "subscription", "tmdb_id": 9, "season": 1})
    t["snap"] = {"have": 0, "aired": 3, "total": 4, "missing": [1], "missing_n": 3, "ts": 1}      # 缺 3 集，但快照里只存了第 1 集
    with mock.patch.object(M, "Tmdb", FT):
        ev = c.get(f"/api/calendar?start={iso(-30)}&end={iso(0)}", headers=h).get_json()["events"]
    assert [e["status"] for e in ev] == ["missing", "missing", "missing"]
