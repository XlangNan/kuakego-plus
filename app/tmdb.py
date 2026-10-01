# -*- coding: utf-8 -*-
import time
from datetime import date

import requests

_cache = {}

CATEGORIES = {
    "all": {},
    "cn": {"with_origin_country": "CN"},
    "kr_jp": {"with_origin_country": "KR|JP"},
    "us_eu": {"with_origin_country": "US|GB|FR|DE|ES|IT|SE|DK|NO"},
}
SORTS = {
    "popularity": {"sort_by": "popularity.desc"},
    "rating": {"sort_by": "vote_average.desc", "vote_count.gte": 200},
    "latest": {"sort_by": "first_air_date.desc", "vote_count.gte": 3},
}


class Tmdb:
    def __init__(self, cfg):
        self.key = cfg.get("api_key", "")
        self.base = cfg.get("base_url", "https://api.themoviedb.org/3").rstrip("/")
        self.lang = cfg.get("language", "zh-CN")
        self.img = cfg.get("image_base", "https://image.tmdb.org/t/p/w342").rstrip("/")
        self.img_root, self.img_size = self.img.rsplit("/", 1)

    @property
    def ready(self):
        return bool(self.key)

    def _get(self, path, ttl=1800, **params):
        params.setdefault("language", self.lang)
        headers = {}
        if len(self.key) > 40:  # v4 读访问令牌
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        ck = (self.base, path, tuple(sorted((k, str(v)) for k, v in params.items() if k != "api_key")))
        hit = _cache.get(ck)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        r = requests.get(f"{self.base}{path}", params=params, headers=headers, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("TMDB 密钥无效")
        r.raise_for_status()
        data = r.json()
        _cache[ck] = (time.time(), data)
        return data

    def _url(self, path, size=None):
        """图片走本服务的 /api/tmdb/img 代理：浏览器不需要能访问 TMDB，容器能访问即可。"""
        if not path:
            return ""
        return f"/api/tmdb/img/{size or self.img_size}{path}"

    def card(self, it):
        return {
            "id": it["id"],
            "name": it.get("name") or it.get("original_name") or "",
            "first_air_date": it.get("first_air_date") or "",
            "vote_average": round(it.get("vote_average") or 0, 1),
            "overview": it.get("overview") or "",
            "poster_url": self._url(it.get("poster_path")),
        }

    def trending(self, page=1):
        d = self._get("/trending/tv/week", page=page)
        return {"items": [self.card(x) for x in d.get("results", [])],
                "total_pages": min(d.get("total_pages", 1), 20)}

    def discover(self, category="all", sort="popularity", page=1):
        p = {"page": page, **CATEGORIES.get(category, {}), **SORTS.get(sort, SORTS["popularity"])}
        if sort == "latest":
            p["first_air_date.lte"] = date.today().isoformat()
        d = self._get("/discover/tv", **p)
        return {"items": [self.card(x) for x in d.get("results", []) if x.get("poster_path")],
                "total_pages": min(d.get("total_pages", 1), 20)}

    def search(self, q, page=1):
        d = self._get("/search/tv", 600, query=q, page=page)
        return {"items": [self.card(x) for x in d.get("results", [])],
                "total_pages": min(d.get("total_pages", 1), 10)}

    def home(self):
        rows = [{"label": "🔥 本周热门", "category": "all", "fn": lambda: self.trending()},
                {"label": "🇨🇳 国产剧", "category": "cn", "fn": lambda: self.discover("cn")},
                {"label": "🇰🇷 日韩剧", "category": "kr_jp", "fn": lambda: self.discover("kr_jp")},
                {"label": "🇺🇸 欧美剧", "category": "us_eu", "fn": lambda: self.discover("us_eu")}]
        out = []
        for r in rows:
            try:
                out.append({"label": r["label"], "category": r["category"], "items": r["fn"]()["items"][:20]})
            except Exception as e:
                out.append({"label": r["label"], "category": r["category"], "items": [], "err": str(e)})
        return out

    def detail(self, tmdb_id):
        d = self._get(f"/tv/{tmdb_id}", append_to_response="credits")
        cast = [
            {"name": c.get("name", ""), "character": c.get("character", ""),
             "profile_url": self._url(c.get("profile_path"), "w185")}
            for c in (d.get("credits", {}).get("cast") or [])[:12]
        ]
        return {
            **self.card(d),
            "backdrop_url": self._url(d.get("backdrop_path"), "w780"),
            "genres": [g["name"] for g in d.get("genres", [])],
            "status": d.get("status", ""),
            "vote_count": d.get("vote_count", 0),
            "number_of_seasons": d.get("number_of_seasons", 0),
            "number_of_episodes": d.get("number_of_episodes", 0),
            "seasons": [
                {"season_number": s["season_number"], "name": s.get("name", ""),
                 "episode_count": s.get("episode_count", 0), "air_date": s.get("air_date") or ""}
                for s in d.get("seasons", []) if s["season_number"] > 0
            ],
            "cast": cast,
        }

    def aired(self, tmdb_id, season):
        """(已播出集号集合, 本季总集数)；air_date 缺失的按未播出处理。"""
        d = self._get(f"/tv/{tmdb_id}/season/{season}", 3600)
        today = date.today().isoformat()
        eps = d.get("episodes", [])
        aired = {e["episode_number"] for e in eps if e.get("air_date") and e["air_date"] <= today}
        return aired, len(eps)
