# -*- coding: utf-8 -*-
import re
import time

import requests

_ids_cache = {}


class Emby:
    def __init__(self, url, api_key, user_id="", timeout=10):
        self.base = (url or "").rstrip("/")
        self.key = api_key
        self.user_id = user_id
        self.timeout = timeout

    def _req(self, method, path, **params):
        r = requests.request(
            method, f"{self.base}/emby{path}", params=params,
            headers={"X-Emby-Token": self.key}, timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    def ping(self):
        info = self._req("GET", "/System/Info")
        return f"{info.get('ServerName', 'Emby')} {info.get('Version', '')}".strip()

    def find_series(self, name, tmdb_id=None):
        """返回 (id, 显示名)；优先按 TMDB ID 精确匹配，其次按剧名。"""
        if tmdb_id:
            try:
                data = self._req(
                    "GET", "/Items", Recursive="true", IncludeItemTypes="Series",
                    AnyProviderIdEquals=f"tmdb.{tmdb_id}", Limit=3,
                )
                if data.get("Items"):
                    it = data["Items"][0]
                    return it["Id"], it.get("Name", name)
            except Exception:
                pass  # 老版本 Emby 不支持该参数，退回按名字
        if not name:
            return None, None
        # 先用原名搜；搜不到再去掉结尾的「第二季 / Season 2 / S02」重试（Emby 里剧名通常不带季）
        stripped = re.sub(r"[\s._-]*(第\s*[0-9一二三四五六七八九十]{1,3}\s*季|[Ss]eason\s*\d+|[Ss]\d{1,2})\s*$", "", name).strip()
        for q in dict.fromkeys([name, stripped]):
            if not q:
                continue
            items = self._req(
                "GET", "/Items", Recursive="true", IncludeItemTypes="Series",
                SearchTerm=q, Limit=10,
            ).get("Items", [])
            for it in items:  # 名字完全一致优先
                if it.get("Name") in (name, q):
                    return it["Id"], it["Name"]
            for it in items:  # 其次：互相包含。不再盲取第一个，避免匹配到别的剧
                n = it.get("Name", "")
                if n and (n in name or name in n):
                    return it["Id"], n
        return None, None

    def tmdb_ids(self, ttl=300):
        """Emby 里所有剧集的 TMDB ID 集合（缓存 5 分钟，给推荐页的"已入库"角标用）。"""
        hit = _ids_cache.get(self.base)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        data = self._req(
            "GET", "/Items", Recursive="true", IncludeItemTypes="Series",
            Fields="ProviderIds", Limit=5000,
        )
        ids = set()
        for it in data.get("Items", []):
            v = (it.get("ProviderIds") or {})
            v = v.get("Tmdb") or v.get("tmdb")
            if v and str(v).isdigit():
                ids.add(int(v))
        _ids_cache[self.base] = (time.time(), ids)
        return ids

    def episodes(self, series_id):
        """Emby 里真实存在的 {(季, 集)}；排除"显示缺失剧集"产生的虚拟条目。"""
        params = {"IsMissing": "false"}
        if self.user_id:
            params["UserId"] = self.user_id
        data = self._req("GET", f"/Shows/{series_id}/Episodes", **params)
        have = set()
        for it in data.get("Items", []):
            if it.get("IsMissing") or it.get("LocationType") == "Virtual":
                continue
            s, e = it.get("ParentIndexNumber"), it.get("IndexNumber")
            if s is None or e is None:
                continue
            for x in range(e, (it.get("IndexNumberEnd") or e) + 1):
                have.add((s, x))
        return have

    def refresh(self, series_id=None):
        if series_id:
            self._req(
                "POST", f"/Items/{series_id}/Refresh", Recursive="true",
                MetadataRefreshMode="Default", ImageRefreshMode="Default",
                ReplaceAllMetadata="false", ReplaceAllImages="false",
            )
        else:
            self._req("POST", "/Library/Refresh")
