# -*- coding: utf-8 -*-
import time
from datetime import date

import requests

from .parser import emby_folder_name, norm_title

_cache = {}

CATEGORIES = {
    "all": {},
    "cn": {"with_origin_country": "CN"},
    "kr_jp": {"with_origin_country": "KR|JP"},
    "us_eu": {"with_origin_country": "US|GB|FR|DE|ES|IT|SE|DK|NO"},
}
MOVIE_COUNTRIES = {"cn": "CN|HK|TW", "kr_jp": "KR|JP", "jp": "JP", "us_eu": "US|GB|FR|DE|ES|IT|SE|DK|NO"}
ANIMATION = 16   # TMDB 的「动画」类型 ID（剧集和电影通用）。剧集 / 电影板块排除它，动漫板块只要它
ANIME_COUNTRIES = {"jp": {"with_origin_country": "JP"}, "cn": {"with_origin_country": "CN"},
                   "us_eu": {"with_origin_country": "US|GB|FR|DE|ES|IT|SE|DK|NO"}}
# 别名：优先取这些地区的译名（华语地区、日韩、英美），其次是各语言的翻译
ALT_REGIONS = ("CN", "TW", "HK", "SG", "MO", "JP", "KR", "US", "GB")
ALT_LANGS = ("zh", "en", "ja", "ko")
TALK_NEWS = {10767, 10763}   # 访谈 / 新闻：演员「参演作品」里这类多是本人出镜，没有订阅价值
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
            "media": "tv", "genre_ids": it.get("genre_ids") or [],
        }

    def trending(self, page=1):
        """本周热门剧集。趋势接口没法按类型过滤，所以在这里把动画去掉（动画归「动漫」板块）。"""
        d = self._get("/trending/tv/week", page=page)
        return {"items": [self.card(x) for x in d.get("results", []) if ANIMATION not in (x.get("genre_ids") or [])],
                "total_pages": min(d.get("total_pages", 1), 20)}

    def discover(self, category="all", sort="popularity", page=1, anime=False):
        """anime=False：剧集板块（排除动画）；anime=True：动漫板块（只要动画，category 是 jp / cn / us_eu / all）。"""
        p = {"page": page, **SORTS.get(sort, SORTS["popularity"])}
        if anime:
            p["with_genres"] = str(ANIMATION)
            p.update(ANIME_COUNTRIES.get(category, {}))
        else:
            p["without_genres"] = str(ANIMATION)
            p.update(CATEGORIES.get(category, {}))
        if sort == "latest":
            p["first_air_date.lte"] = date.today().isoformat()
        d = self._get("/discover/tv", **p)
        return {"items": [self.card(x) for x in d.get("results", []) if x.get("poster_path")],
                "total_pages": min(d.get("total_pages", 1), 20)}

    def search(self, q, page=1):
        d = self._get("/search/tv", 600, query=q, page=page)
        return {"items": [self.card(x) for x in d.get("results", [])],
                "total_pages": min(d.get("total_pages", 1), 10)}

    def anime_search(self, q, page=1):
        """动漫板块的搜索：剧集和电影里的动画一起搜（番剧 + 动画电影）。"""
        keep = lambda x: ANIMATION in (x.get("genre_ids") or [])
        tv = [self.card(x) for x in self._get("/search/tv", 600, query=q, page=page).get("results", []) if keep(x)]
        mv = [self.movie_card(x) for x in self._get("/search/movie", 600, query=q, page=page, include_adult="false").get("results", []) if keep(x)]
        return {"items": tv + mv, "total_pages": 1}

    @staticmethod
    def _rows(rows):
        out = []
        for r in rows:
            try:
                out.append({"label": r["label"], "category": r["category"], "sort": r.get("sort", "popularity"),
                            "items": r["fn"]()["items"][:20]})
            except Exception as e:
                out.append({"label": r["label"], "category": r["category"], "sort": r.get("sort", "popularity"), "items": [], "err": str(e)})
        return out

    def home(self):
        return self._rows([
            {"label": "🔥 本周热门", "category": "all", "fn": lambda: self.trending()},
            {"label": "🇨🇳 国产剧", "category": "cn", "fn": lambda: self.discover("cn")},
            {"label": "🇰🇷 日韩剧", "category": "kr_jp", "fn": lambda: self.discover("kr_jp")},
            {"label": "🇺🇸 欧美剧", "category": "us_eu", "fn": lambda: self.discover("us_eu")}])

    def movie_home(self):
        return self._rows([
            {"label": "🔥 本周热门", "category": "all", "fn": lambda: self.movie_discover("trending")},
            {"label": "🇨🇳 华语", "category": "cn", "fn": lambda: self.movie_discover("cn")},
            {"label": "🇰🇷 日韩", "category": "kr_jp", "fn": lambda: self.movie_discover("kr_jp")},
            {"label": "🇺🇸 欧美", "category": "us_eu", "fn": lambda: self.movie_discover("us_eu")},
            {"label": "⭐ 高分经典", "category": "all", "sort": "rating", "fn": lambda: self.movie_discover("top")}])

    def anime_home(self):
        return self._rows([
            {"label": "🔥 热门动漫", "category": "all", "fn": lambda: self.discover("all", anime=True)},
            {"label": "🇯🇵 日本动画", "category": "jp", "fn": lambda: self.discover("jp", anime=True)},
            {"label": "🇨🇳 国产动画", "category": "cn", "fn": lambda: self.discover("cn", anime=True)},
            {"label": "🎞 动画电影", "category": "movie", "fn": lambda: self.movie_discover("all", anime=True)}])

    # ------------------------------------------------------------ 别名 / 演员 / 相似
    @staticmethod
    def _aliases_from(d, media):
        """一部作品常有好几个名字（原名、港台译名、英文名……）。主名搜不到资源时拿它们去搜。返回的列表不含主名。"""
        primary = norm_title(d.get("name") or d.get("title") or "")
        out = [d.get("original_name") or d.get("original_title") or ""]
        alt = d.get("alternative_titles") or {}
        rows = alt.get("results") if media == "tv" else alt.get("titles")
        rows = sorted(rows or [], key=lambda x: ALT_REGIONS.index(x["iso_3166_1"]) if x.get("iso_3166_1") in ALT_REGIONS else 99)
        out += [x.get("title") or "" for x in rows]
        trs = [t for t in (d.get("translations") or {}).get("translations") or [] if t.get("iso_639_1") in ALT_LANGS]
        trs.sort(key=lambda t: ALT_LANGS.index(t["iso_639_1"]))
        out += [(t.get("data") or {}).get("name") or (t.get("data") or {}).get("title") or "" for t in trs]
        res, seen = [], {primary}
        for n in out:
            n = (n or "").strip()
            k = norm_title(n)
            if k and k not in seen and len(n) <= 60:
                seen.add(k)
                res.append(n)
        return res[:10]

    def aliases(self, media, tmdb_id):
        d = self._get(f"/{'movie' if media == 'movie' else 'tv'}/{tmdb_id}", 3600, append_to_response="alternative_titles,translations")
        return self._aliases_from(d, "movie" if media == "movie" else "tv")

    def person(self, pid):
        """演员（或导演等）的资料和参演的全部作品，按热度排序。"""
        d = self._get(f"/person/{pid}", append_to_response="combined_credits")
        items, seen = [], set()
        for c in sorted((d.get("combined_credits") or {}).get("cast") or [], key=lambda c: c.get("popularity") or 0, reverse=True):
            mt = c.get("media_type")
            key = (mt, c.get("id"))
            if mt not in ("tv", "movie") or not c.get("poster_path") or key in seen or TALK_NEWS & set(c.get("genre_ids") or []):
                continue
            seen.add(key)
            card = self.card(c) if mt == "tv" else self.movie_card(c)
            card["character"] = c.get("character") or ""
            items.append(card)
        return {"id": d["id"], "name": d.get("name", ""), "profile_url": self._url(d.get("profile_path"), "w185"),
                "biography": (d.get("biography") or "")[:500], "birthday": d.get("birthday") or "",
                "place_of_birth": d.get("place_of_birth") or "", "department": d.get("known_for_department") or "",
                "items": items[:90]}

    def person_search(self, q, limit=8):
        """按名字搜演员。没有头像、没有代表作的人丢掉；演员排前面，同类再按热度。
        名字和搜索词对不上（比如用中文名搜外国演员，靠别名命中）的，要热度够高才留，免得把搜剧名时的杂音带出来。"""
        d = self._get("/search/person", 600, query=q, page=1)
        nq, rows = norm_title(q), []
        for x in d.get("results") or []:
            known = [k.get("name") or k.get("title") or "" for k in (x.get("known_for") or [])]
            known = [k for k in known if k]
            if not x.get("profile_path") or not known:
                continue
            nn = norm_title(x.get("name") or "")
            hit = bool(nn and nq and (nq in nn or nn in nq))
            pop = x.get("popularity") or 0
            if not hit and pop < 5:
                continue
            rows.append({"id": x["id"], "name": x.get("name") or "", "profile_url": self._url(x["profile_path"], "w185"),
                         "department": x.get("known_for_department") or "", "known_for": known[:3],
                         "_k": (0 if hit else 1, 0 if x.get("known_for_department") == "Acting" else 1, -pop)})
        rows.sort(key=lambda r: r["_k"])
        for r in rows:
            r.pop("_k")
        return {"items": rows[:limit]}

    def similar(self, media, tmdb_id):
        """推荐相似的作品：先取 TMDB 的「推荐」，不够再补「相似」。"""
        media = "movie" if media == "movie" else "tv"
        rows, seen = [], set()
        for kind in ("recommendations", "similar"):
            try:
                got = self._get(f"/{media}/{tmdb_id}/{kind}").get("results") or []
            except Exception:
                got = []
            for x in got:
                if x.get("id") not in seen and x.get("poster_path"):
                    seen.add(x["id"])
                    rows.append(x)
            if len(rows) >= 12:
                break
        return {"items": [self.card(x) if media == "tv" else self.movie_card(x) for x in rows[:18]]}

    def detail(self, tmdb_id):
        d = self._get(f"/tv/{tmdb_id}", append_to_response="credits,alternative_titles,translations")
        cast = [
            {"id": c.get("id"), "name": c.get("name", ""), "character": c.get("character", ""),
             "profile_url": self._url(c.get("profile_path"), "w185")}
            for c in (d.get("credits", {}).get("cast") or [])[:12]
        ]
        year = int((d.get("first_air_date") or "0000")[:4] or 0)
        return {
            **self.card(d),
            "year": year, "aliases": self._aliases_from(d, "tv"),
            "folder_name": emby_folder_name(d.get("name") or d.get("original_name") or "", year, d["id"]),
            "backdrop_url": self._url(d.get("backdrop_path"), "w780"),
            "genres": [g["name"] for g in d.get("genres", [])],
            "anime": any(g.get("id") == ANIMATION for g in d.get("genres", [])),
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

    # ------------------------------------------------------------ 电影
    def movie_card(self, it):
        rd = it.get("release_date") or ""
        return {"id": it["id"], "name": it.get("title") or it.get("original_title") or "", "year": rd[:4],
                "release_date": rd, "vote_average": round(it.get("vote_average") or 0, 1),
                "overview": it.get("overview") or "", "poster_url": self._url(it.get("poster_path")),
                "media": "movie", "genre_ids": it.get("genre_ids") or []}

    def movie_discover(self, category="trending", page=1, sort="popularity", anime=False):
        """
        category: trending 本周热门 / all 全部 / cn 华语 / kr_jp 日韩 / jp 日本 / us_eu 欧美 / top 高分
        anime=False：电影板块（排除动画）；anime=True：只要动画电影（归「动漫」板块）。
        """
        if category == "trending" and not anime:
            d = self._get("/trending/movie/week", page=page)   # 趋势接口不能过滤类型，下面手动去掉动画
            d["results"] = [x for x in d.get("results", []) if ANIMATION not in (x.get("genre_ids") or [])]
        else:
            p = {"page": page, "include_adult": "false", "with_genres" if anime else "without_genres": str(ANIMATION)}
            if category == "top" or sort == "rating":
                p.update(sort_by="vote_average.desc", **{"vote_count.gte": 500})
            elif sort == "latest":
                p.update(sort_by="primary_release_date.desc", **{"primary_release_date.lte": date.today().isoformat(), "vote_count.gte": 3})
            else:
                p["sort_by"] = "popularity.desc"
            if category in MOVIE_COUNTRIES:
                p["with_origin_country"] = MOVIE_COUNTRIES[category]
            d = self._get("/discover/movie", **p)
        return {"items": [self.movie_card(x) for x in d.get("results", []) if x.get("poster_path")],
                "total_pages": min(d.get("total_pages", 1), 20)}

    def movie_search(self, q, page=1):
        d = self._get("/search/movie", 600, query=q, page=page, include_adult="false")
        return {"items": [self.movie_card(x) for x in d.get("results", [])],
                "total_pages": min(d.get("total_pages", 1), 10)}

    def movie_detail(self, tmdb_id):
        d = self._get(f"/movie/{tmdb_id}", append_to_response="credits,alternative_titles,translations")
        cast = [{"id": c.get("id"), "name": c.get("name", ""), "character": c.get("character", ""),
                 "profile_url": self._url(c.get("profile_path"), "w185")}
                for c in (d.get("credits", {}).get("cast") or [])[:12]]
        card = self.movie_card(d)
        return {**card, "aliases": self._aliases_from(d, "movie"),
                "folder_name": emby_folder_name(card["name"], int(card["year"] or 0), d["id"]), "backdrop_url": self._url(d.get("backdrop_path"), "w780"),
                "genres": [g["name"] for g in d.get("genres", [])], "status": d.get("status", ""),
                "anime": any(g.get("id") == ANIMATION for g in d.get("genres", [])),
                "runtime": d.get("runtime") or 0, "tagline": d.get("tagline") or "",
                "vote_count": d.get("vote_count", 0), "cast": cast}

    def aired(self, tmdb_id, season):
        """(已播出集号集合, 本季总集数)；air_date 缺失的按未播出处理。"""
        d = self._get(f"/tv/{tmdb_id}/season/{season}", 3600)
        today = date.today().isoformat()
        eps = d.get("episodes", [])
        aired = {e["episode_number"] for e in eps if e.get("air_date") and e["air_date"] <= today}
        return aired, len(eps)
