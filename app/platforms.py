# -*- coding: utf-8 -*-
"""各流媒体平台的片库浏览（「影视推荐 → 平台」板块）。

国内平台（腾讯视频 / 芒果TV / 爱奇艺 / 哔哩哔哩）直接用各自网站的公开片库接口，接口写法参考
MoviePilot 的探索类插件（DDSRem-Dev/MoviePilot-Plugins、jxxghp/MoviePilot-Plugins）；
海外平台（Netflix 等）用 TMDB 的「可观看平台」筛选。
"""
import json
import re
import time
from datetime import date

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
SECS = ("tv", "movie", "anime", "variety")
LABEL = {"tv": "剧集", "movie": "电影", "anime": "动漫", "variety": "综艺"}
_cache = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    if len(_cache) > 600:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.time(), val)
    return val


def _https(url):
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return "https://" + url[7:] if url.startswith("http://") else url


def _chs(*pairs):
    return [{"key": k, "label": l, "section": s} for k, l, s in pairs]


STD = _chs(("tv", "剧集", "tv"), ("movie", "电影", "movie"), ("anime", "动漫", "anime"), ("variety", "综艺", "variety"))
PLATFORMS = [
    {"id": "tencent", "name": "腾讯视频", "kind": "native", "channels": STD},
    {"id": "mango", "name": "芒果TV", "kind": "native", "channels": STD},
    {"id": "iqiyi", "name": "爱奇艺", "kind": "native", "channels": STD},
    {"id": "bilibili", "name": "哔哩哔哩", "kind": "native",
     "channels": _chs(("tv", "剧集", "tv"), ("movie", "电影", "movie"), ("bangumi", "番剧", "anime"),
                      ("guo", "国创", "anime"), ("variety", "综艺", "variety"))},
    {"id": "netflix", "name": "Netflix", "kind": "tmdb", "provider": 8, "region": "US", "channels": STD},
    {"id": "disney", "name": "Disney+", "kind": "tmdb", "provider": 337, "region": "US", "channels": STD},
    {"id": "appletv", "name": "Apple TV+", "kind": "tmdb", "provider": 350, "region": "US", "channels": STD},
    {"id": "max", "name": "Max", "kind": "tmdb", "provider": 1899, "region": "US", "channels": STD},
]
BY_ID = {p["id"]: p for p in PLATFORMS}


def catalog():
    return [{"id": p["id"], "name": p["name"], "kind": p["kind"],
             "channels": [{"key": c["key"], "label": c["label"], "section": c["section"]} for c in p["channels"]]}
            for p in PLATFORMS]


def _item(title, year, poster, key):
    return {"title": (title or "").strip(), "year": str(year or "").strip()[:4], "poster": _https(poster), "key": str(key or "")}


def _opt(value, name):
    return {"value": str(value), "name": str(name)}


# ============================================================ 腾讯视频
T_URL = "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc.PageServer/GetPageData"
T_PARAMS = {"video_appid": "1000005", "vplatform": "2", "vversion_name": "8.9.10", "new_mark_label_enabled": "1"}
T_HEAD = {"User-Agent": UA, "Referer": "https://v.qq.com/"}
T_CH = {"tv": "100113", "movie": "100173", "variety": "100109", "anime": "100119"}


def _t_post(body):
    r = requests.post(T_URL, params=T_PARAMS, json=body, headers=T_HEAD, timeout=15)
    r.raise_for_status()
    return r.json().get("data") or {}


def _t_body(ch, filters=None):
    b = {"page_params": {"channel_id": T_CH[ch], "page_type": "channel_operation", "page_id": "channel_list_second_page"}}
    if filters:
        b["page_params"]["filter_params"] = "&".join(f"{k}={v}" for k, v in filters.items())
    return b


def tencent_filters(ch):
    data = _t_post(_t_body(ch))
    mods = data.get("module_list_datas", [])
    if len(mods) < 2:
        return []
    datas = (mods[1].get("module_datas") or [{}])[0].get("item_data_lists", {}).get("item_datas", [])
    groups = {}
    for it in datas:
        if str(it.get("item_type")) != "11":
            continue
        p = it.get("item_params", {})
        name = p.get("index_name") or p.get("option_name") or ""
        g = groups.setdefault(name, {"key": p.get("index_item_key"), "name": name, "options": []})
        if str(p.get("option_value", "")) not in ("-1", ""):
            g["options"].append(_opt(p["option_value"], p.get("option_name", "")))
    return [g for g in groups.values() if g["key"] and g["options"]]


def tencent_items(ch, filters, page):
    body = _t_body(ch, filters)
    data = _t_post(body)
    if page > 1:
        ctx = data.get("next_page_context", {})
        key = next((k for k in ctx if k.startswith("_ds_cli_") and k.endswith("_page")), None)
        if not key:
            return [], False
        body["page_context"] = {key: str(page - 1)}
        data = _t_post(body)
    for ml in data.get("module_list_datas", []):
        for md in ml.get("module_datas", []):
            ds = md.get("item_data_lists", {}).get("item_datas", [])
            if any(str(i.get("item_type")) == "2" for i in ds):
                out = []
                for i in ds:
                    if str(i.get("item_type")) != "2":
                        continue
                    p = i.get("item_params", {})
                    pic = p.get("new_pic_vt") or p.get("pic_url") or p.get("image_url") or ""
                    out.append(_item(p.get("title"), p.get("year"), re.sub(r"/350$", "", pic), p.get("cid")))
                return out, bool(out)
    return [], False


# ============================================================ 芒果TV
M_HEAD = {"User-Agent": UA, "Referer": "https://www.mgtv.com"}
M_CH = {"tv": "2", "movie": "3", "anime": "50", "variety": "1"}


def mango_filters(ch):
    r = requests.get("https://pianku.api.mgtv.com/rider/config/channel/v1", headers=M_HEAD, timeout=15,
                     params={"platform": "pcweb", "allowedRC": "1", "channelId": M_CH[ch], "_support": "10000000"})
    r.raise_for_status()
    out = []
    for g in (r.json().get("data") or {}).get("listItems", []):
        opts = [_opt(i["tagId"], i["tagName"]) for i in g.get("items", []) if i.get("tagName") != "全部" and i.get("tagId") not in (None, "")]
        if opts and g.get("eName"):
            out.append({"key": g["eName"], "name": g.get("typeName", ""), "options": opts})
    return out


def mango_items(ch, filters, page, size=30):
    params = {"allowedRC": "1", "platform": "pcweb", "channelId": M_CH[ch], "pn": str(page), "pc": str(size),
              "hudong": "1", "_support": "10000000", **filters}
    r = requests.get("https://pianku.api.mgtv.com/rider/list/pcweb/v3", headers=M_HEAD, params=params, timeout=15)
    r.raise_for_status()
    docs = (r.json().get("data") or {}).get("hitDocs") or []
    return [_item(d.get("title"), d.get("year"), d.get("img"), d.get("clipId")) for d in docs], len(docs) >= size


# ============================================================ 爱奇艺
Q_HEAD = {"User-Agent": UA, "Referer": "https://www.iqiyi.com/"}
Q_CH = {"tv": "2", "movie": "1", "anime": "4", "variety": "6"}
Q_BASE = {"uid": "", "passport_id": "", "ret_num": "60", "pcv": "17.084.26143", "version": "17.084.26143",
          "device_id": "4b0c3e7cb568d5e6ccc4bf823293e709", "session": "", "token": "", "os": "10.0",
          "conduit_id": "", "vip": "0", "auth": "", "recent_selected_tag": ""}
# 筛选分组名 → 请求里 filter 的字段名（和 MoviePilot 插件一致）
Q_GROUP = {"排序": "mode", "类型": "type", "地区": "area", "时间": "year", "资费": "pay", "付费": "pay",
           "殿堂": "hall", "推荐": "recommend", "奖项": "award", "剧场": "theater"}
Q_FIELD = {"mode": "mode", "type": "three_category_id_v2", "area": "three_category_id_v2",
           "year": "market_release_date_level", "pay": "is_purchase", "hall": "smart_tag_v2",
           "recommend": "smart_tag_v2", "theater": "smart_tag_v2", "award": "structure_id"}


def iqiyi_filters(ch):
    r = requests.get("https://mesh.if.iqiyi.com/portal/lw/videolib/tag", headers=Q_HEAD, timeout=15,
                     params={"channel_id": Q_CH[ch], "tagAdd": "", "selected_tag_name": "免费",
                             "version": Q_BASE["version"], "device": Q_BASE["device_id"], "uid": ""})
    r.raise_for_status()
    groups = r.json()
    out = []
    for g in groups if isinstance(groups, list) else []:
        key = Q_GROUP.get(g.get("group"))
        opts = [_opt(t.get("query_value", ""), t.get("text", "")) for t in g.get("tags", []) if t.get("query_value") not in (None, "")]
        if g.get("display") and key and opts and not any(x["key"] == key for x in out):
            out.append({"key": key, "name": g.get("group"), "options": opts})
    return out


def iqiyi_items(ch, filters, page, cookie=""):
    flt = {}
    for k, v in filters.items():
        if k in Q_FIELD:
            flt[Q_FIELD[k]] = v
    params = dict(Q_BASE, channel_id=Q_CH[ch], page_id=str(page), filter=json.dumps(flt or {"mode": "11"}, ensure_ascii=False))
    head = dict(Q_HEAD)
    if cookie:
        head["Cookie"] = cookie
        m = re.search(r"(?:^|;\s*)QC005=([^;]+)", cookie)
        if m:
            params["device_id"] = m.group(1).strip()
    r = requests.get("https://mesh.if.iqiyi.com/portal/lw/videolib/data", headers=head, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    rows = data.get("data") or []
    if data.get("code") == 0 and not rows:
        raise RuntimeError("爱奇艺返回了空数据，大概率是被风控拦截了。请在「平台设置」里填入爱奇艺的 Cookie（浏览器登录 iqiyi.com 后复制）")
    out = []
    for it in rows:
        if str(it.get("channel_id")) != Q_CH[ch]:
            continue
        date_ = it.get("date") if isinstance(it.get("date"), dict) else {}
        year = date_.get("year") or str(it.get("showDate") or "").split("-")[0]
        poster = it.get("image_url_2x") or it.get("image_url_normal") or it.get("image_cover") or ""
        out.append(_item(it.get("display_name") or it.get("title"), year, poster, it.get("album_id") or it.get("entity_id")))
    return out, bool(out)


# ============================================================ 哔哩哔哩
B_HEAD = {"User-Agent": UA, "Referer": "https://www.bilibili.com"}
B_CH = {"tv": "5", "movie": "2", "variety": "7", "bangumi": "1", "guo": "4"}


def bilibili_filters(ch):
    y = date.today().year
    anime = ch in ("bangumi", "guo")
    if anime:
        years = [(f"[{n},{n + 1})", str(n)) for n in range(y, y - 9, -1)] + \
                [("[2010,2016)", "2015-2010"), ("[2005,2010)", "2009-2005"), ("[2000,2005)", "2004-2000"),
                 ("[1990,2000)", "90年代"), ("[,1990)", "更早")]
    else:
        f = lambda n: f"{n}-01-01 00:00:00"
        years = [(f"[{f(n)},{f(n + 1)})", str(n)) for n in range(y, y - 9, -1)] + \
                [(f"[{f(2010)},{f(2016)})", "2015-2010"), (f"[{f(2005)},{f(2010)})", "2009-2005"),
                 (f"[{f(2000)},{f(2005)})", "2004-2000"), (f"[{f(1990)},{f(2000)})", "90年代"), (f"[,{f(1990)})", "更早")]
    return [{"key": "year" if anime else "release_date", "name": "年份", "options": [_opt(v, n) for v, n in years]},
            {"key": "season_status", "name": "付费", "options": [_opt("1", "免费"), _opt("4,6", "大会员"), _opt("2,6", "付费")]}]


def bilibili_items(ch, filters, page, size=20):
    params = {"type": "1", "st": B_CH[ch], "season_type": B_CH[ch], "page": page, "pagesize": size, **filters}
    r = requests.get("https://api.bilibili.com/pgc/season/index/result", headers=B_HEAD, params=params, timeout=15)
    r.raise_for_status()
    rows = (r.json().get("data") or {}).get("list") or []
    return [_item(x.get("title"), "", x.get("cover"), x.get("media_id")) for x in rows], len(rows) >= size


NATIVE = {"tencent": (tencent_filters, tencent_items), "mango": (mango_filters, mango_items),
          "bilibili": (bilibili_filters, bilibili_items), "iqiyi": (iqiyi_filters, iqiyi_items)}
TMDB_SORTS = [_opt("popularity", "热度"), _opt("rating", "评分"), _opt("latest", "最新")]


# ============================================================ 对外接口
def _channel(pid, key):
    p = BY_ID.get(pid)
    if not p:
        raise KeyError("未知平台")
    c = next((c for c in p["channels"] if c["key"] == key), None)
    if not c:
        raise KeyError("这个平台没有该分类")
    return p, c


def filters(pid, ch):
    p, _ = _channel(pid, ch)
    if p["kind"] == "tmdb":
        return [{"key": "sort", "name": "排序", "options": TMDB_SORTS}]
    return _cached(("f", pid, ch), 6 * 3600, lambda: NATIVE[pid][0](ch))


def items(pid, ch, flt, page, tmdb=None, cfg=None):
    """返回 {items, has_more, native}。native=True 的条目只有标题 / 年份 / 海报，点开时要去 TMDB 匹配。"""
    p, c = _channel(pid, ch)
    page = max(int(page or 1), 1)
    flt = {k: v for k, v in (flt or {}).items() if v not in (None, "")}
    if p["kind"] == "tmdb":
        if tmdb is None or not tmdb.ready:
            raise RuntimeError("还没有配置 TMDB 密钥")
        d = tmdb.provider_discover(p["provider"], p["region"], c["section"], flt.get("sort", "popularity"), page)
        return {"items": d["items"], "has_more": page < d["total_pages"], "native": False}
    key = ("i", pid, ch, tuple(sorted(flt.items())), page, (cfg or {}).get("iqiyi_cookie", "")[:12])
    if pid == "iqiyi":
        fn = lambda: iqiyi_items(ch, flt, page, (cfg or {}).get("iqiyi_cookie", ""))
    else:
        fn = lambda: NATIVE[pid][1](ch, flt, page)
    rows, more = _cached(key, 1800, fn)
    return {"items": [dict(x, section=c["section"]) for x in rows], "has_more": more, "native": True}


_CLEAN = re.compile(r"[（(【\[].*?[）)】\]]|第\s*[0-9一二三四五六七八九十]+\s*[季部]|[Ss]eason\s*\d+|\s*(普通话|粤语|国语|日语|中文|高清|超前点映|会员|独播|纯享)版?\s*")


def clean_title(t):
    c = _CLEAN.sub(" ", t or "")
    return re.sub(r"\s+", " ", c).strip() or (t or "").strip()


def match(tmdb, title, year, section):
    """平台上的条目 → TMDB 条目：按片名（+年份）搜，优先同一分类、年份相差不超过 1 的。返回 {id, media, name} 或 None。"""
    q = clean_title(title)
    if not q:
        return None
    order = {"movie": ("movie_search", "search", "anime_search", "variety_search"),
             "anime": ("anime_search", "search", "movie_search", "variety_search"),
             "variety": ("variety_search", "search", "anime_search", "movie_search")}.get(
        section, ("search", "variety_search", "anime_search", "movie_search"))
    try:
        y = int(str(year)[:4]) if year else None
    except ValueError:
        y = None
    first = None
    for fn in order:
        try:
            res = getattr(tmdb, fn)(q).get("items", [])[:6]
        except Exception:
            continue
        for it in res:
            iy = str(it.get("year") or it.get("first_air_date") or it.get("release_date") or "")[:4]
            if first is None:
                first = it
            if y and iy.isdigit() and abs(int(iy) - y) <= 1:
                return {"id": it["id"], "media": it.get("media", "tv"), "name": it.get("name", "")}
        if res and not y:
            return {"id": res[0]["id"], "media": res[0].get("media", "tv"), "name": res[0].get("name", "")}
    if first and not y:
        return {"id": first["id"], "media": first.get("media", "tv"), "name": first.get("name", "")}
    return None
