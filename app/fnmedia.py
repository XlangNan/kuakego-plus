# -*- coding: utf-8 -*-
"""飞牛影视（fnOS 影视 / TrimeMedia）客户端 + 与 Emby 并存时的合并客户端。

飞牛没有公开 API，这里按网页端的私有接口实现（签名算法参考 MoviePilot 的 trimemedia 模块）。
对外暴露和 emby.Emby 同名的方法（ping / find_series / find_movie / tmdb_ids / episodes / refresh），
引擎里的「入库判断 / 缺集判断 / 转存后刷新」不用关心是 Emby 还是飞牛。
"""
import hashlib
import json
import random
import re
import time
from urllib.parse import quote

import requests

from .logbuf import log

_APIKEY = "16CCEB3D-AB42-077D-36A1-F355324E4237"
_SALT = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
_ids_cache = {}


class FnError(Exception):
    pass


def norm_host(url):
    u = (url or "").strip().rstrip("/")
    if u and not re.match(r"https?://", u):
        u = "http://" + u
    if u and not u.endswith("/v"):
        u += "/v"
    return u


class FnMedia:
    kind = "fn"

    def __init__(self, url, username, password, access_code="", timeout=15):
        self.host = norm_host(url)
        self.base = self.host  # 缓存键
        self.username, self.password, self.code = username, password, access_code or ""
        self.timeout = timeout
        self.token = None
        self.s = requests.Session()
        self.api_path = "/api/v1"
        self.posters = {}   # guid -> 海报路径（列表接口里就带着，省得每张图再查一次详情）

    # ---------------- 底层 ----------------
    def _authx(self, api_path, body):
        if not api_path.startswith("/v"):
            api_path = "/v" + api_path
        nonce, ts = str(random.randint(100000, 999999)), str(int(time.time() * 1000))
        h = hashlib.md5((body or "").encode()).hexdigest()
        sign = hashlib.md5("_".join([_SALT, api_path, nonce, ts, h, _APIKEY]).encode()).hexdigest()
        return f"nonce={nonce}&timestamp={ts}&sign={sign}"

    def _raw(self, api, method=None, params=None, data=None, base_path=None, stream=False):
        prefix = self.api_path if base_path is None else base_path
        api_path = prefix + (api if api.startswith("/") else "/" + api)
        method = (method or ("get" if data is None else "post")).lower()
        body = json.dumps(data, allow_nan=False) if (method != "get" and data) else ("" if method != "get" else None)
        q = "&".join(f"{k}={v}" for k, v in params.items()) if params else None
        headers = {"Accept": "application/json", "Referer": self.host, "Authorization": self.token or "",
                   "authx": self._authx(api_path, body or q)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.s.request(method, self.host + api_path, headers=headers, params=params,
                              data=body, timeout=self.timeout, stream=stream)

    def request(self, api, method=None, params=None, data=None, base_path=None, _retry=True):
        r = self._raw(api, method, params, data, base_path)
        try:
            j = r.json()
        except ValueError:
            raise FnError(f"{api} 返回的不是 JSON（HTTP {r.status_code}）：地址是否填成了飞牛影视的 /v 地址？")
        code = int(j.get("code", -1))
        if code != 0:
            # 令牌过期 / 未登录：重新登录一次
            if _retry and self.token is not None and api != "/user/loginByPassword" and j.get("msg") and \
                    any(k in str(j.get("msg")) for k in ("token", "登录", "login", "auth")):
                self.login()
                return self.request(api, method, params, data, base_path, False)
            raise FnError(f"{api} 失败：{code} {j.get('msg')}")
        return j.get("data")

    def verify_code(self):
        if not self.code:
            return
        root = self.host[:-2]
        r = self.s.get(f"{root}/c/{quote(self.code, safe='')}", allow_redirects=True, timeout=self.timeout)
        if r.status_code == 404:
            raise FnError("访问码不正确")
        if not r.ok:
            raise FnError(f"访问码校验失败（HTTP {r.status_code}）")

    def login(self):
        if not self.host or not self.username:
            raise FnError("请填写地址和用户名")
        self.verify_code()
        self.token = None
        h = hashlib.sha256(self.password.encode()).hexdigest()
        r = self._raw("/user/loginByPassword", data={"username": self.username, "password": h,
                                                     "app_name": "trimemedia-web"}, base_path="/api/v2")
        try:
            j = r.json()
        except ValueError:
            j = None
        if not self.password:
            raise FnError("密码是空的：请在密码框里输入后再点测试，或先保存")
        v2err = None
        if j is not None and int(j.get("code", -1)) == 0:
            self.token = (j.get("data") or {}).get("token")
        else:
            if j is not None and r.status_code != 404:
                v2err = f"{j.get('code')} {j.get('msg')}"
            # v2 没成功（旧版服务端 / 摘要方式不被接受）：再试一次 v1 明文登录
            try:
                d = self.request("/login", data={"username": self.username, "password": self.password,
                                                  "app_name": "trimemedia-web"}, _retry=False)
                self.token = (d or {}).get("token")
            except FnError as e:
                raise FnError(f"登录失败：{v2err or e}（用户名 {self.username}，密码 {len(self.password)} 位；请确认账号密码，注意前后有没有空格）")
        if not self.token:
            raise FnError("登录失败：没有拿到令牌（账号或密码不对？）")
        return self.token

    def _ensure(self):
        if not self.token:
            self.login()

    def call(self, api, **kw):
        self._ensure()
        return self.request(api, **kw)

    # ---------------- 通用接口（和 Emby 同名） ----------------
    def ping(self):
        v = self.call("/sys/version") or {}
        return f"飞牛影视 {v.get('version', '')} (后端 {v.get('mediasrvVersion', '?')})".strip()

    def libraries(self):
        """[{id, name, type, locations}]。管理员能拿到目录列表，普通账号只有名字。"""
        data = None
        try:
            data = self.call("/mdb/list")
        except FnError:
            data = None
        if not data:
            data = self.call("/mediadb/list")
        out = []
        for m in data or []:
            out.append({"id": str(m.get("guid")), "name": m.get("name") or m.get("title") or "",
                        "type": m.get("category") or "", "locations": m.get("dir_list") or []})
        return out

    def _list(self, guid=None, types=("Movie", "TV"), page_size=200, max_pages=60):
        items, page = [], 1
        while page <= max_pages:
            post = {"tags": {"type": list(types)}, "sort_type": "DESC", "sort_column": "create_time",
                    "page": page, "page_size": page_size, "exclude_grouped_video": 1}
            if guid:
                post["ancestor_guid"] = guid
            d = self.call("/item/list", data=post) or {}
            got = d.get("list") or []
            for it in got:
                if it.get("guid") and (it.get("poster") or it.get("posters")):
                    self.posters[it["guid"]] = it.get("poster") or it.get("posters")
            items += got
            total = d.get("total") if d.get("total") is not None else d.get("total_count")
            if not got or len(got) < page_size or (total is not None and len(items) >= int(total)):
                break
            page += 1
        return items

    def library_items(self, lib_id, kinds=("Movie", "TV")):
        return self._list(lib_id, kinds)

    @staticmethod
    def tmdb_of(it):
        t = str(it.get("trim_id") or "")
        if t[:2] in ("tt", "tm") and t[2:].isdigit():
            return int(t[2:])
        return None

    def tmdb_ids(self, kind="Series", ttl=300):
        hit = _ids_cache.get((self.base, kind))
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        ty = "Movie" if kind == "Movie" else "TV"
        ids = {self.tmdb_of(i) for i in self._list(None, (ty,))}
        ids.discard(None)
        _ids_cache[(self.base, kind)] = (time.time(), ids)
        return ids

    def _search(self, q, ty):
        res = self.call("/search/list", params={"q": q}) or []
        return [i for i in res if i.get("type") == ty]

    def find_series(self, name, tmdb_id=None):
        if tmdb_id:
            try:
                for it in self._list(None, ("TV",)):
                    if self.tmdb_of(it) == int(tmdb_id):
                        return it["guid"], it.get("title") or name
            except Exception:
                pass
        if not name:
            return None, None
        stripped = re.sub(r"[\s._-]*(第\s*[0-9一二三四五六七八九十]{1,3}\s*季|[Ss]eason\s*\d+|[Ss]\d{1,2})\s*$", "", name).strip()
        for q in dict.fromkeys([name, stripped]):
            if not q:
                continue
            items = self._search(q, "TV")
            for it in items:
                if it.get("title") in (name, q):
                    return it["guid"], it["title"]
            for it in items:
                n = it.get("title") or ""
                if n and (n in name or name in n):
                    return it["guid"], n
        return None, None

    def find_movie(self, name, tmdb_id=None, year=None):
        if tmdb_id:
            try:
                for it in self._list(None, ("Movie",)):
                    if self.tmdb_of(it) == int(tmdb_id):
                        return it["guid"], it.get("title") or name
            except Exception:
                pass
        if not name:
            return None, None
        for it in self._search(name, "Movie"):
            n = it.get("title") or ""
            y = str(it.get("release_date") or "")[:4]
            if year and y.isdigit() and abs(int(y) - int(year)) > 1:
                continue
            if n == name or (n and (n in name or name in n)):
                return it["guid"], n
        return None, None

    def seasons(self, tv_guid):
        return self.call(f"/season/list/{tv_guid}") or []

    def season_episodes(self, season_guid):
        return self.call(f"/episode/list/{season_guid}") or []

    def episodes(self, series_id):
        have = set()
        for sn in self.seasons(series_id):
            for ep in self.season_episodes(sn["guid"]):
                s = ep.get("season_number", sn.get("season_number"))
                e = ep.get("episode_number")
                if s is None or e is None:
                    continue
                have.add((int(s), int(e)))
        return have

    def refresh(self, series_id=None):
        """飞牛没有「只刷新某部剧」的接口，统一扫描媒体库。"""
        self.call("/mdb/scanall", method="post")

    def scan_library(self, lib_id):
        self.call(f"/mdb/scan/{lib_id}", data={})
        return True

    # ---------------- 媒体库管理 ----------------
    def item(self, guid):
        return self.call(f"/item/{guid}")

    def delete_item(self, guid, delete_file=False):
        self.call(f"/item/{guid}", method="delete", data={"delete_file": 1 if delete_file else 0, "media_guids": []})

    def image(self, path_or_guid, width=300):
        """海报：传条目 guid（优先用列表里带的海报路径，没有再查详情）。返回 (内容, 类型)；没有图返回 None。"""
        self._ensure()
        p = str(path_or_guid)
        if not p.startswith("/"):
            guid = p
            p = self.posters.get(guid) or ""
            if not p:
                it = self.item(guid) or {}
                p = it.get("poster") or it.get("posters") or ""
                if p:
                    self.posters[guid] = p
        if not p:
            log.warning("飞牛海报：条目 %s 没有海报路径", path_or_guid)
            return None
        if not p.startswith("/"):
            p = "/" + p
        last = ""
        for cand in dict.fromkeys([p, quote(p, safe="/%")]):
            for extra in ({"w": width}, None):   # 有的版本支持 w 缩放参数，不支持就取原图
                r = self._raw("/sys/img" + cand, params=extra)
                ct = r.headers.get("Content-Type", "")
                if r.status_code == 200 and r.content and not ct.startswith(("application/json", "text/")):
                    return r.content, ct if ct.startswith("image/") else "image/jpeg"
                last = f"HTTP {r.status_code} {ct} {r.content[:60]!r}"
        log.warning("飞牛海报取不到：%s -> %s", p, last)
        return None

    def diagnose(self):
        """给设置页「测试连接」用：每一步都列出来，方便对不上时把结果发给开发者。"""
        out = {}
        out["version"] = self.ping()
        libs = self.libraries()
        out["libraries"] = libs
        sample = None
        for lb in libs:
            try:
                its = self._list(lb["id"], ("Movie", "TV"), page_size=1, max_pages=1)
            except Exception as e:
                out["list_error"] = str(e)
                break
            if its:
                sample = its[0]
                break
        out["sample_item"] = sample
        return out


class Multi:
    """Emby 和飞牛同时启用时的合并视角：已入库 = 任意一边有；缺集 = 各边已有集的并集。
    条目 ID 形如 `emby:123|fn:abcd`；不带前缀的纯 ID（任务里手填的 Emby 剧集 ID）当作 Emby 的。"""

    def __init__(self, emby, fn):
        self.parts = [("emby", emby), ("fn", fn)]

    def _cli(self, tag):
        return dict(self.parts)[tag]

    @staticmethod
    def _split(token):
        out = {}
        for p in str(token or "").split("|"):
            if not p:
                continue
            tag, _, v = p.partition(":") if p.split(":", 1)[0] in ("emby", "fn") else ("emby", "", p)
            out[tag] = v
        return out

    def ping(self):
        return " + ".join(c.ping() for _, c in self.parts)

    def tmdb_ids(self, kind="Series", ttl=300):
        ids, ok = set(), False
        for _, c in self.parts:
            try:
                ids |= c.tmdb_ids(kind, ttl)
                ok = True
            except Exception:
                pass
        if not ok:
            raise FnError("Emby 和飞牛都查询失败")
        return ids

    def _find(self, fn_name, *a):
        toks, name, err = [], None, None
        for tag, c in self.parts:
            try:
                i, n = getattr(c, fn_name)(*a)
            except Exception as e:
                err = e
                continue
            if i:
                toks.append(f"{tag}:{i}")
                name = name or n
        if not toks and err:
            # 两边都没找到，且至少一边是报错：把错误抛出去，让调用方走"查询失败"分支
            raise err
        return ("|".join(toks) or None), name

    def find_series(self, name, tmdb_id=None):
        return self._find("find_series", name, tmdb_id)

    def find_movie(self, name, tmdb_id=None, year=None):
        return self._find("find_movie", name, tmdb_id, year)

    def episodes(self, token):
        have = set()
        for tag, v in self._split(token).items():
            have |= self._cli(tag).episodes(v)
        return have

    def refresh(self, token=None):
        got, errs = self._split(token), []
        for tag, c in self.parts:
            try:
                c.refresh(got.get(tag))
            except Exception as e:
                errs.append(e)
        if len(errs) == len(self.parts):
            raise errs[0]
