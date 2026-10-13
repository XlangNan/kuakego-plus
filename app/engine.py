# -*- coding: utf-8 -*-
"""任务引擎：多分享链接按顺序检查，只转存"库里缺少"的集数。"""
import fnmatch
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import date, datetime

from . import notify
from .emby import Emby
from .logbuf import log
from .library import Library
from .maintenance import Maintenance
from .pansou import PanSouError
from .parser import (RES_LABEL, emby_folder_name, fmt_eps, is_video, norm_title, parse_episodes, parse_share_text,
                     quality_reason, quality_tags, safe_name, title_seasons,
                     season_from_text)
from .quark import Quark, QuarkError
from .tmdb import Tmdb

MIN_INTERVAL = 60        # 分钟。再低容易触发夸克风控
INCREMENTAL_LIMIT = 30   # 追更时单轮最多转存个数；首次回填不限
# ---- 自动找资源的安全限制 ----
AUTO_MAX_LINKS = 5       # 每个订阅最多同时保留几个有效链接
AUTO_MAX_ADD = 3         # 每轮最多自动添加几个
AUTO_MAX_INSPECT = 8     # 每轮最多检测几个候选（检测会读夸克，别太密）
AUTO_MIN_AVG_MB = 100    # 平均每个视频小于这个体积，多半是花絮 / 预告
MOVIE_MIN_MB = 300        # 电影里最大的视频不到这个体积，多半是预告 / 片段
AUTO_MAX_ADD_MOVIE = 2   # 电影每轮最多自动添加几个（一个主用 + 一个备用）
AUTO_MAX_NAMES = 5       # 一部剧 / 电影最多用几个名称（主名 + 别名）去搜；每多一个名称就多搜一轮
AUTO_NAME_ENOUGH = 3     # 用某个名称搜到这么多个「名字对得上」的新结果，就不再换别名
AUTO_RECHECK = {"reject": 24 * 3600, "dead": 72 * 3600, "ok": 0, "cap": 0, "added": 10 ** 9}   # 看过的候选多久后再看
PERMANENT_ERR = re.compile(r"失效|取消|不存在|删除|违规|过期|封禁|提取码|passcode|expired", re.I)


class EmbyError(Exception):
    """用户开了"只转存 Emby 里缺少的集数"，但 Emby 查询失败。不能静默退回去整批转存。"""


class LinkError(Exception):
    def __init__(self, msg, permanent=False, network=False):
        super().__init__(msg)
        self.permanent, self.network = permanent, network


def _natkey(s):
    return re.sub(r"\d+", lambda m: m.group().zfill(6), s)


def _kw_match(name, kws):
    """kws: 逗号分隔的关键词，支持 * 通配；命中任意一个返回 True。"""
    low = name.lower()
    for kw in re.split(r"[,，]", kws or ""):
        kw = kw.strip().lower()
        if not kw:
            continue
        if ("*" in kw and fnmatch.fnmatch(low, f"*{kw}*")) or kw in low:
            return True
    return False


def _norm_path(p):
    p = re.sub(r"/{2,}", "/", "/" + (p or "").strip().strip("/"))
    return p


class Engine:
    def __init__(self, store):
        self.store = store
        self.run_lock = threading.Lock()
        self.pending = set()   # 排队/运行中的任务 id
        self.running = None    # 正在运行的任务 id
        self._q, self._q_cookie = None, None
        self.maint = Maintenance(self)
        self.library = Library(self)
        self._seed_recent()
        self._seed_recent_dirs()

    # ------------------------------------------------------------ 客户端
    def quark(self):
        cookie = self.store.data.get("cookie", "")
        if not cookie:
            raise QuarkError("还没有登录夸克账号，请先到「账号登录」填写 Cookie")
        if self._q is None or self._q_cookie != cookie:
            self._q, self._q_cookie = Quark(cookie), cookie
            self._q.init()
        return self._q

    def tmdb(self):
        return Tmdb(self.store.data["tmdb"])

    def emby(self):
        c = self.store.data["emby"]
        if c["enabled"] and c["url"] and c["api_key"]:
            return Emby(c["url"], c["api_key"], c.get("user_id", ""))
        return None

    # ------------------------------------------------------------ 任务 CRUD
    def find(self, tid):
        with self.store.lock:
            return next((t for t in self.store.data["tasks"] if t["id"] == tid), None)

    def normalize(self, payload, existing=None):
        """校验并生成任务字段；existing 存在时原地保留链接状态。"""
        p = payload
        old_links = {l["pwd_id"]: l for l in (existing or {}).get("links", [])}
        links = []
        if "links_text" in p:
            for it in parse_share_text(p["links_text"]):
                keep = old_links.get(it["pwd_id"])
                if keep and keep["url"] == it["url"]:
                    links.append(keep)
                else:
                    links.append({**it, "ban": "", "fails": 0, "baseline": None})
        elif existing:
            links = existing["links"]

        savepath = _norm_path(p.get("savepath", (existing or {}).get("savepath", "")))
        name = (p.get("name") or (existing or {}).get("name") or "").strip()
        if not name:
            name = savepath.rsplit("/", 1)[-1]
        if not name:
            raise ValueError("请填写名称，或选择一个非根目录的转存目录")

        def pick(key, default):
            return p[key] if key in p else (existing or {}).get(key, default)

        try:
            interval = max(MIN_INTERVAL, int(pick("interval", MIN_INTERVAL)))
            season = max(0, int(pick("season", 1)))
            tmdb_id = int(pick("tmdb_id", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError("扫描间隔、季号、TMDB ID 必须是数字")
        weeks = [int(x) for x in pick("run_weeks", [1, 2, 3, 4, 5, 6, 7]) if str(x) in "1234567"]
        pattern = pick("pattern", "").strip()
        if pattern:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"正则表达式无效：{e}")
        try:
            min_size = max(0, int(pick("min_size_mb", 0) or 0))
            total_eps = max(0, int(pick("total_episodes", 0) or 0))
        except (TypeError, ValueError):
            raise ValueError("最小体积、总集数必须是数字")
        aliases = list(dict.fromkeys(a.strip() for a in (pick("aliases", []) or []) if isinstance(a, str) and a.strip()))[:12]
        rename = pick("rename", "auto")
        if rename not in ("auto", "keep", "custom"):
            rename = "auto"
        if rename == "custom" and not (pattern and pick("replace", "")):
            raise ValueError("自定义重命名需要同时填写「匹配」和「替换」")

        kind = pick("kind", "monitor") if pick("kind", "monitor") in ("monitor", "subscription") else "monitor"
        media = pick("media", "tv") if pick("media", "tv") in ("tv", "movie") else "tv"
        if media == "movie":   # 电影只有「订阅」这一种用法，也没有季
            kind, season = "subscription", 1
        try:
            year = max(0, int(pick("year", 0) or 0))
        except (TypeError, ValueError):
            year = 0
        want_res = [r for r in dict.fromkeys(pick("want_res", []) or []) if r in RES_LABEL]
        want_hdr = pick("want_hdr", "any") if pick("want_hdr", "any") in ("any", "yes", "no") else "any"
        fields = {
            "kind": kind, "want_res": want_res, "want_hdr": want_hdr,
            "media": media, "anime": bool(pick("anime", False)), "variety": bool(pick("variety", False)), "year": year, "release_date": (pick("release_date", "") or "").strip(),
            "no_cam": bool(pick("no_cam", media == "movie")), "done": bool(pick("done", False)),
            "auto_find": bool(pick("auto_find", False)), "upgrade_watch": bool(pick("upgrade_watch", False)),
            "strict_season": bool(pick("strict_season", kind == "subscription")),
            "name": name, "links": links, "savepath": savepath, "interval": interval,
            "enabled": bool(pick("enabled", True)),
            "enddate": (pick("enddate", "") or "").strip(),
            "run_weeks": weeks or [1, 2, 3, 4, 5, 6, 7],
            "season": season, "rename": rename, "pattern": pattern,
            "replace": pick("replace", ""),
            "keep_tree": bool(pick("keep_tree", False)),   # 默认不保留分享里的子目录：压平后统一命名
            "emby_layout": bool(pick("emby_layout", False)), "aliases": aliases,
            "include_kw": (pick("include_kw", "") or "").strip(),
            "exclude_kw": (pick("exclude_kw", "") or "").strip(),
            "min_size_mb": min_size, "total_episodes": total_eps,
            "overview": pick("overview", ""),
            "unparsed": pick("unparsed", "") if pick("unparsed", "") in ("save", "skip") else "",
            "emby_name": (pick("emby_name", "") or "").strip(),
            "emby_series_id": (pick("emby_series_id", "") or "").strip(),
            "tmdb_id": tmdb_id,
            "skip_when_complete": bool(pick("skip_when_complete", True)),
            "poster": pick("poster", ""),
        }
        return fields

    @staticmethod
    def _dest(task):
        """实际转存到的目录。「Emby 规范目录」开启时，剧集再套一层季文件夹：…/觉醒年代 (2021) {tmdb-117954}/Season 1。"""
        base = _norm_path(task["savepath"])
        if task.get("emby_layout") and task.get("media", "tv") != "movie":
            s = int(task.get("season") or 0)
            return _norm_path(f"{base}/" + (f"Season {s}" if s else "Specials"))
        return base

    @staticmethod
    def _dir_kind(t):
        """「最近使用的目录」按这个分开记：tv 剧集 / movie 电影 / anime 动漫（番剧和动画电影）/ variety 综艺，推荐目录时互不干扰。"""
        return "anime" if t.get("anime") else "variety" if t.get("variety") else t.get("media", "tv")

    def _remember_dir(self, path, name, media="tv"):
        """记下最近使用的保存目录（去重、新的在前、最多 12 条；根目录不记）。调用方需持有 store.lock。"""
        path = _norm_path(path)
        if path == "/":
            return
        rd = [d for d in self.store.data["recent_dirs"] if d["path"] != path]
        rd.insert(0, {"path": path, "name": name, "media": media, "ts": int(time.time())})
        self.store.data["recent_dirs"] = rd[:12]

    def _check_dir_clash(self, fields, self_id=None):
        """两部不同的剧 / 电影放在同一个目录，已有的内容会混在一起导致漏转，所以不允许。同一部剧的不同季可以放一起。
        剧集和电影的 TMDB ID 是两套编号，所以用（类型, ID）来判断是不是同一部。"""
        tid, media = fields.get("tmdb_id"), fields.get("media", "tv")
        if not tid:
            return
        with self.store.lock:
            for t in self.store.data["tasks"]:
                if (t["id"] != self_id and t.get("tmdb_id") and (t.get("media", "tv"), t["tmdb_id"]) != (media, tid)
                        and _norm_path(t["savepath"]) == fields["savepath"]):
                    raise ValueError(f"这个目录已经被订阅《{t['name']}》使用了。两部不同的剧或电影放在同一个目录里，"
                                     f"已有的内容会混在一起、导致漏转，请换一个目录（比如在后面加上名字）")

    def create_task(self, payload, save_existing=True):
        fields = self.normalize(payload)
        self._check_dir_clash(fields)
        task = {
            "id": uuid.uuid4().hex[:8], "created": int(time.time()),
            "next_run": 0, "last_run": "", "last_status": "", "last_msg": "",
            "saved_total": 0, "history": [], **fields,
        }
        if not save_existing:  # 只追新：记下现在链接里已有的文件，之后不转
            self.snapshot_baseline(task)
        with self.store.lock:
            self.store.data["tasks"].append(task)
            self._remember_dir(task["savepath"], task["name"], self._dir_kind(task))
            self.store.save()
        dt = self.store.data["dingtalk"]
        if dt["enabled"] and dt["on_add"]:
            kind = "订阅" if task["kind"] == "subscription" else "监控"
            notify.send_dingtalk(dt, f"新增{kind}《{task['name']}》",
                                 f"### ➕ 新增{kind}《{task['name']}》\n转存到 `{task['savepath']}`，"
                                 f"共 {len(task['links'])} 个分享链接")
        return task

    def update_task(self, tid, payload):
        task = self.find(tid)
        if not task:
            raise KeyError("任务不存在")
        fields = self.normalize(payload, task)
        self._check_dir_clash(fields, self_id=tid)
        with self.store.lock:
            if fields["savepath"] != task.get("savepath"):
                self._remember_dir(fields["savepath"], fields["name"], self._dir_kind(fields))
            task.update(fields)
            self.store.save()
        return task

    def delete_task(self, tid):
        with self.store.lock:
            n = len(self.store.data["tasks"])
            self.store.data["tasks"] = [t for t in self.store.data["tasks"] if t["id"] != tid]
            self.store.save()
            return len(self.store.data["tasks"]) < n

    def delete_tasks(self, ids):
        """批量删除，返回实际删除的个数。"""
        ids = set(ids)
        with self.store.lock:
            n = len(self.store.data["tasks"])
            self.store.data["tasks"] = [t for t in self.store.data["tasks"] if t["id"] not in ids]
            self.store.save()
            return n - len(self.store.data["tasks"])

    def public_task(self, t):
        d = {k: v for k, v in t.items() if k != "links"}
        d["links"] = [
            {"url": l["url"], "passcode": l.get("passcode", ""), "ban": l.get("ban", ""),
             "fails": l.get("fails", 0), "auto": bool(l.get("auto")), "title": l.get("title", "")}
            for l in t["links"]
        ]
        d["state"] = ("running" if self.running == t["id"] else
                      "queued" if t["id"] in self.pending else "idle")
        d["unparsed"] = self._unparsed_policy(t)
        d["strict_season"] = self._strict_season(t)
        d["auto_find"] = bool(t.get("auto_find"))
        d["media"], d["year"], d["done"] = t.get("media", "tv"), t.get("year", 0), bool(t.get("done"))
        d["anime"], d["variety"] = bool(t.get("anime")), bool(t.get("variety"))
        d["upgrade_watch"], d["upgrade"] = bool(t.get("upgrade_watch")), t.get("upgrade") or None
        d["release_date"], d["no_cam"] = t.get("release_date", ""), bool(t.get("no_cam"))
        d["want_res"], d["want_hdr"] = t.get("want_res", []), t.get("want_hdr", "any")
        f = t.get("find") or {}
        d["find"] = {"last": f.get("last", 0), "next": f.get("next", 0), "msg": f.get("last_msg", ""),
                     "added_total": f.get("added_total", 0), "log": f.get("log", [])[:30]}
        d["expired"] = bool(t.get("enddate") and date.today().isoformat() > t["enddate"])
        live = [l for l in t["links"] if not l.get("ban")]
        if d["expired"]:
            d["status"] = "expired"
        elif t.get("done"):
            d["status"] = "done"
        elif not t.get("enabled", True):
            d["status"] = "paused"
        elif not t["links"]:
            d["status"] = "nolinks"
        elif not live:
            d["status"] = "invalid"
        elif t.get("last_status") == "error":
            d["status"] = "error"
        elif t.get("last_status") == "complete":
            d["status"] = "complete"
        else:
            d["status"] = "ok"
        return d

    # ------------------------------------------------------------ 分享读取
    def _get_stoken(self, q, link):
        pwd_id, passcode, _, _ = q.extract_url(link["url"])
        r = q.get_stoken(pwd_id, passcode)
        st = r.get("status")
        if st == 200:
            return r["data"]["stoken"]
        msg = r.get("message", "未知错误")
        if st == 500:
            raise LinkError(f"网络异常：{msg}", network=True)
        raise LinkError(msg, permanent=bool(PERMANENT_ERR.search(msg)))

    def _entries(self, task, q, link, stoken, max_dirs=100):
        """
        返回 [(文件dict, 季提示, 相对目录tuple)]。
        整个分享只有一个文件夹时自动剥掉这一层；递归深度 ≤4、最多读 100 个子目录。
        """
        pwd_id, _, pdir_fid, _ = q.extract_url(link["url"])
        try:
            top = q.get_detail(pwd_id, stoken, pdir_fid)
        except QuarkError as e:
            raise LinkError(str(e))
        if not top:
            raise LinkError("分享为空，文件已被分享者删除", permanent=True)
        base_hint, top_dirs = None, ()
        if len(top) == 1 and top[0]["dir"]:
            base_hint, top_dirs = season_from_text(top[0]["file_name"], None), (top[0]["file_name"],)
            top = q.get_detail(pwd_id, stoken, top[0]["fid"])
        out, budget = [], [max_dirs]

        def walk(items, rel, hint, depth):
            for f in items:
                if not f["dir"]:
                    f["_dirs"] = tuple(reversed(rel)) + top_dirs   # 所在的各层文件夹名，从近到远：判断 HDR / 分辨率时，文件名没写就看它们
                    out.append((f, hint, rel))
                elif depth < 4 and budget[0] > 0:
                    budget[0] -= 1
                    try:
                        sub = q.get_detail(pwd_id, stoken, f["fid"])
                    except QuarkError as e:
                        log.warning("读取子目录《%s》失败：%s", f["file_name"], e)
                        continue
                    walk(sub, rel + (f["file_name"],),
                         season_from_text(f["file_name"], hint), depth + 1)

        walk(top, (), base_hint, 0)
        return out

    def snapshot_baseline(self, task):
        q = self.quark()
        for link in task["links"]:
            try:
                stoken = self._get_stoken(q, link)
                link["baseline"] = [f["file_name"] for f, _, _ in self._entries(task, q, link, stoken)]
            except LinkError as e:
                raise ValueError(f"链接无法读取，未添加：{e}")

    # ------------------------------------------------------------ "已有"集合
    def _have_detail(self, task, dir_items, strict=False):
        """-> (合并后的已有集, 说明, 目录里的集, Emby 里的集)"""
        dir_have, emby_have = set(), set()
        for it in dir_items:
            if not it["dir"] and is_video(it["file_name"]):
                dir_have.update(parse_episodes(it["file_name"], task["season"]))
        emby_msg = "Emby 联动未启用（只按夸克目录里的文件判断）"
        cfg = self.store.data["emby"]
        cli = self.emby()
        if cli and not cfg["only_missing"]:
            emby_msg = "「只转存 Emby 里缺少的集数」未勾选（只按夸克目录里的文件判断）"
        elif cli:
            try:
                sid = self._emby_sid(cli, task)
                if sid:
                    emby_have = cli.episodes(sid)
                    emby_msg = f"Emby 匹配《{task.get('emby_match') or sid}》，共 {len(emby_have)} 集"
                else:
                    emby_msg = "Emby 里没找到这部剧（只按夸克目录里的文件判断）"
            except Exception as ex:
                if strict:
                    raise EmbyError(str(ex)) from ex
                emby_msg = f"Emby 查询失败（只按夸克目录里的文件判断）：{ex}"
                log.warning("《%s》%s", task["name"], emby_msg)
        return dir_have | emby_have, emby_msg, dir_have, emby_have

    def _have(self, task, dir_items):
        have, msg, _, _ = self._have_detail(task, dir_items)
        return have, msg

    @staticmethod
    def _season_warning(task, emby_have):
        """Emby 里有集、但没有本任务季号的集 —— 最常见的"已有的又被转一遍"原因。"""
        if not emby_have or any(s == task["season"] for s, _ in emby_have):
            return ""
        seasons = sorted({s for s, _ in emby_have})
        return (f"Emby 里没有第 {task['season']} 季的集，但有第 {'、'.join(map(str, seasons))} 季。"
                f"如果这就是你已有的那几集，请把任务的「季号」改成对应的季，否则会被当成缺失重新转存")

    def _emby_sid(self, cli, task):
        sid = task.get("emby_series_id")
        if sid:
            return sid
        sid, matched = cli.find_series(task.get("emby_name") or task["name"], task.get("tmdb_id") or None)
        task["emby_match"] = matched or ""
        return sid

    def wanted(self, task):
        """(应有集号集合, 本季总数, 来源)；没有任何依据时返回 (None, None, '')。"""
        tm = self.tmdb()
        if task.get("tmdb_id") and tm.ready:
            try:
                aired, total = tm.aired(task["tmdb_id"], task["season"])
                if total:
                    return aired, total, "tmdb"
            except Exception as e:
                log.warning("《%s》TMDB 查询失败：%s", task["name"], e)
        n = task.get("total_episodes") or 0
        if n:
            return set(range(1, n + 1)), n, "manual"
        return None, None, ""

    @staticmethod
    def _snap(task, have, wanted, total):
        """把进度存成快照，海报墙直接读，不用每次实时查夸克 / Emby。"""
        mine = {ep for s, ep in have if s == task["season"]}
        miss = sorted(wanted - mine) if wanted is not None else []
        task["snap"] = {"have": len(mine), "aired": len(wanted) if wanted is not None else None,
                        "total": total, "missing": miss[:30], "missing_n": len(miss), "ts": int(time.time())}

    def progress(self, task):
        if task.get("media") == "movie":
            q = self.quark()
            found = q.get_fids([_norm_path(task["savepath"])])
            have, msg = self._movie_have(task, q.ls_dir(found[0]["fid"]) if found else [])
            with self.store.lock:
                task["snap"] = {"movie": True, "have": 1 if have else 0, "ts": int(time.time())}
                if have:
                    task["done"] = True
                self.store.save()
            return {"movie": True, "have": have, "have_count": 1 if have else 0, "emby": msg,
                    "aired": None, "total": 1, "missing": [], "source": ""}
        q = self.quark()
        found = q.get_fids([self._dest(task)])  # 只查不建：看进度不该有副作用
        have, emby_msg = self._have(task, q.ls_dir(found[0]["fid"]) if found else [])
        season = task["season"]
        mine = sorted(e for s, e in have if s == season)
        aired, total, src = self.wanted(task)
        with self.store.lock:
            self._snap(task, have, aired, total)
            self.store.save()
        return {"have": mine, "have_count": len(mine), "emby": emby_msg, "source": src,
                "aired": len(aired) if aired is not None else None, "total": total,
                "missing": sorted(aired - set(mine)) if aired is not None else []}

    # ------------------------------------------------------------ 转存
    @staticmethod
    def _target_name(task, fname, eps):
        ext = os.path.splitext(fname)[1]
        mode = task.get("rename", "auto")
        if mode == "custom" and task.get("pattern") and task.get("replace"):
            try:
                rep = task["replace"].replace("{TASKNAME}", task["name"])
                return re.sub(task["pattern"], rep, fname)
            except re.error:
                return fname
        if mode == "auto" and len(eps) == 1:
            s, e = eps[0]
            if task.get("emby_layout"):   # 觉醒年代 (2021) - S01E01.mp4：文件名里带上剧名和年份，Emby 单独看文件也能认出来
                y = f" ({task['year']})" if task.get("year") else ""
                return f"{safe_name(task['name'])}{y} - S{s:02d}E{e:02d}{ext}"
            return f"S{s:02d}E{e:02d}{ext}"
        return fname

    def _dir_state(self, q, task, rel, cache):
        if rel not in cache:
            path = _norm_path(self._dest(task) + "/" + "/".join(rel))
            found = q.get_fids([path])
            if found:
                fid = found[0]["fid"]
                names = {i["file_name"] for i in q.ls_dir(fid)}
            else:
                fid, names = None, set()
            cache[rel] = {"fid": fid, "names": names, "path": path}
        return cache[rel]

    @staticmethod
    def _unparsed_policy(task):
        """解析不出集数的视频：订阅追更默认跳过（它一定是剧集，放行会导致多个链接各转一份）；
        分享监控默认转存（可能是电影 / 特辑）。"""
        return task.get("unparsed") or ("skip" if task.get("kind") == "subscription" else "save")

    @staticmethod
    def _strict_season(task):
        v = task.get("strict_season")
        return bool(v) if v is not None else task.get("kind") == "subscription"

    def _filter_reason(self, task, f, share_title=""):
        """文件不该转存的原因；该转存则返回空字符串。share_title：搜索结果 / 分享的标题，画质判断时也参考。"""
        name = f["file_name"]
        if task.get("pattern"):
            if not re.search(task["pattern"], name):
                return "不符合你设置的匹配规则"
        elif not is_video(name):
            return "不是视频文件"
        if task.get("include_kw") and not _kw_match(name, task["include_kw"]):
            return "不含你要求的关键词"
        if task.get("exclude_kw") and _kw_match(name, task["exclude_kw"]):
            return "含你要排除的关键词"
        min_b = task.get("min_size_mb", 0) * 1024 * 1024
        if min_b and f.get("size", 0) < min_b:
            return f"体积小于 {task['min_size_mb']} MB"
        return quality_reason(task.get("want_res") or [], task.get("want_hdr", "any"), name, bool(task.get("no_cam")),
                              f.get("_dirs", ()), share_title)

    @staticmethod
    def _version_rank(f, share_title=""):
        """同一集 / 同一部电影有多个版本时比较谁更好：分辨率高的优先，同级体积大的优先。"""
        t = quality_tags(f["file_name"], f.get("_dirs", ()), share_title)
        return ({"4k": 3, "1080p": 2, "720p": 1}.get(t["res"], 0), f.get("size", 0) or 0)

    def _solo_ep(self, task, have, wanted):
        """订阅只差最后一集时，返回这一集 (季, 集)，否则 None。
        用来放行「分享里只有一个视频、但文件名很怪认不出集数」的资源：搜到的标题已经是想要的了，不用再看里面叫什么，直接按缺的这一集改名。"""
        if task.get("kind") != "subscription" or task.get("media") == "movie" or not wanted:
            return None
        miss = [e for e in wanted if (task["season"], e) not in have]
        return (task["season"], miss[0]) if len(miss) == 1 else None

    def _run_link(self, task, link, q, dir_cache, have, limit=None, trace=None, dry=False, srcs=None, solo_ep=None):
        """处理单个链接，返回 [{name, eps}]。have 仅在成功后合并。"""
        stoken = self._get_stoken(q, link)
        link["fails"] = 0
        pwd_id = q.extract_url(link["url"])[0]
        entries = self._entries(task, q, link, stoken)
        baseline = set(link.get("baseline") or [])
        link_have, cands, seen, unparsed = set(have), [], set(), []
        keep_tree = task.get("keep_tree", False)
        share_title = link.get("title", "")
        chosen = {}   # 集数 -> 已选中的候选，同一集有多个版本（4K / 1080P、HDR / 普通）时挑最好的那个
        videos = [f["file_name"] for f, _, _ in entries if is_video(f["file_name"])]
        solo_name = videos[0] if len(videos) == 1 else None
        _, season_total, _src = self.wanted(task)

        def tr(name, eps, action, why):
            if trace is not None:
                trace.append({"file": name, "eps": fmt_eps(eps), "action": action, "why": why})

        def have_src(eps):
            parts = []
            for ep in eps:
                if srcs and ep in srcs["emby"]:
                    parts.append("Emby")
                elif srcs and ep in srcs["dir"]:
                    parts.append("夸克目录")
                else:
                    parts.append("前面的链接")
            return "、".join(dict.fromkeys(parts))

        for f, hint, rel in sorted(entries, key=lambda x: (x[2], _natkey(x[0]["file_name"]))):
            fname = f["file_name"]
            why = self._filter_reason(task, f, share_title)
            if why:
                tr(fname, [], "skip", why)
                continue
            eps = parse_episodes(fname, hint if hint is not None else task["season"])
            inferred = False
            if not eps and solo_ep and fname == solo_name:
                eps, inferred = [solo_ep], True   # 整个分享只有这一个视频，名字再怪也按缺的那一集处理
            if eps and self._strict_season(task):
                other = sorted({s for s, _ in eps if s != task["season"]})
                if other:
                    tr(fname, eps, "skip", f"这是第 {'、'.join(map(str, other))} 季，本订阅是第 {task['season']} 季（「编辑」里可关闭「只转存本季」）")
                    continue
            if fname in baseline:
                tr(fname, eps, "skip", "添加监控时就已存在（当时选了只追新）")
                continue
            if season_total and eps and any(sn == task["season"] and e > season_total for sn, e in eps):
                tr(fname, eps, "skip", f"集号超过本季总集数 {season_total}，多半是花絮 / 特辑 / 别的版本，不转存")
                continue
            rank = self._version_rank(f, share_title)
            prev = chosen.get(tuple(eps)) if eps else None
            replace = False
            if prev is not None:   # 这一集同一个分享里已经选了一个版本：新的更好就换掉它，否则跳过新的
                if rank > prev["rank"]:
                    cands.remove(prev)
                    seen.discard((prev["rel"], prev["target"]))
                    tr(prev["f"]["file_name"], prev["eps"], "skip", f"同一集有更好的版本（{fname}），不转存这个")
                    replace = True
                else:
                    tr(fname, eps, "skip", "同一集已选了更好的版本")
                    continue
            src_rel, rel = rel, (rel if keep_tree else ())
            d = self._dir_state(q, task, rel, dir_cache)
            if fname in d["names"]:
                tr(fname, eps, "skip", "目标目录里已有同名文件")
                continue
            if not eps and self._unparsed_policy(task) == "skip":
                unparsed.append(fname)
                tr(fname, [], "skip", "解析不出是第几集，已按「跳过」处理（可在「编辑」里改成转存）")
                continue
            if eps:
                if not replace and all(e in link_have for e in eps):
                    tr(fname, eps, "skip", f"这一集已有（{have_src(eps)}）")
                    continue
                link_have.update(eps)
            target = self._target_name(task, fname, eps)
            if target in d["names"] or (rel, target) in seen:
                tr(fname, eps, "skip", "同一集已选了另一个版本" if (rel, target) in seen else "目标目录里已有同名文件")
                continue
            seen.add((rel, target))
            cand = {"f": f, "target": target, "eps": eps, "rel": rel, "src": src_rel, "inferred": inferred, "rank": rank}
            cands.append(cand)
            if eps:
                chosen[tuple(eps)] = cand
        if unparsed:
            log.warning("《%s》有 %d 个视频解析不出是第几集，已跳过（避免重复转存）：%s%s", task["name"], len(unparsed),
                        "、".join(unparsed[:5]), " …" if len(unparsed) > 5 else "")
        if limit is not None and len(cands) > limit:
            log.info("本轮转存上限 %d 个，其余 %d 个留到下一轮", limit, len(cands) - limit)
            for c in cands[limit:]:
                tr(c["f"]["file_name"], c["eps"], "defer", f"超过单轮上限 {limit} 个，留到下一轮")
            cands = cands[:limit]
        for c in cands:
            note = "" if c["target"] == c["f"]["file_name"] else f" → 将命名为 {c['target']}"
            tr(c["f"]["file_name"], c["eps"], "save",
               ("分享里只有这一个视频，名字认不出集数，按你缺的这一集处理" if c.get("inferred") else "缺少这一集" if c["eps"] else "解析不出集数，按文件名判断") + note)
        if dry:
            for c in cands:
                have.update(c["eps"])
            return [{"name": c["target"], "eps": c["eps"]} for c in cands]
        if not cands:
            return []
        # 按"来源文件夹"分批：不同季文件夹里的同名文件（第01集.mp4）压平到同一目录会重名，
        # 所以每批转完立刻改名，再转下一批
        groups = {}
        for c in cands:
            groups.setdefault(c["src"], []).append(c)
        for _, items in sorted(groups.items()):
            d = self._dir_state(q, task, items[0]["rel"], dir_cache)
            if d["fid"] is None:
                d["fid"] = q.ensure_dir(d["path"])
            for i in range(0, len(items), 50):
                chunk = items[i:i + 50]
                tid = q.save_file([c["f"]["fid"] for c in chunk],
                                  [c["f"]["share_fid_token"] for c in chunk],
                                  d["fid"], pwd_id, stoken)
                q.wait_task(tid)
            by_name = {x["file_name"]: x for x in q.ls_dir(d["fid"])}
            for c in items:
                orig = c["f"]["file_name"]
                if c["target"] != orig and orig in by_name:
                    r = q.rename(by_name[orig]["fid"], c["target"])
                    if r.get("code") != 0:
                        log.warning("重命名失败 %s → %s：%s", orig, c["target"], r.get("message"))
                        c["target"] = orig
            d["names"].update(c["target"] for c in items)
        for c in cands:  # 只把真正转存了的集数记入"已有"
            have.update(c["eps"])
        return [{"name": c["target"], "eps": c["eps"]} for c in cands]

    def _run(self, task, manual):
        if task.get("media") == "movie":
            return self._run_movie(task, manual)
        q = self.quark()
        if not task["links"]:
            return {"status": "idle", "msg": "还没有分享链接", "saved": []}
        live = [l for l in task["links"] if not l.get("ban")]
        if not live:
            return {"status": "error", "msg": "所有分享链接均已失效", "saved": []}

        dest = self._dest(task)
        to_fid = q.ensure_dir(dest)
        dir_items = q.ls_dir(to_fid)
        dir_cache = {(): {"fid": to_fid, "names": {i["file_name"] for i in dir_items}, "path": dest}}
        try:
            have, emby_msg, dir_have, emby_have = self._have_detail(task, dir_items, strict=True)
        except EmbyError as ex:
            msg = f"Emby 查询失败，为避免把已有的集重复转存，本次不转存：{ex}"
            log.error("《%s》%s", task["name"], msg)
            return {"status": "error", "saved": [], "msg": msg}
        log.info("《%s》%s｜本任务季号 %d｜夸克目录已有：%s｜Emby 已有：%s", task["name"], emby_msg,
                 task["season"], fmt_eps(dir_have) or "无", fmt_eps(emby_have) or "无")
        warn = self._season_warning(task, emby_have)
        if warn:
            log.warning("《%s》%s", task["name"], warn)

        wanted, total_eps, src = self.wanted(task)
        if task.get("skip_when_complete") and not manual:
            if wanted and {(task["season"], e) for e in wanted} <= have:
                self._snap(task, have, wanted, total_eps)
                done = "已收齐" if src == "manual" else f"已播出 {len(wanted)} 集均已入库"
                return {"status": "complete", "saved": [], "msg": f"本季{done}，本次不检查链接"}

        limit_total = None if task.get("saved_total", 0) == 0 else INCREMENTAL_LIMIT
        saved, errors, banned = [], [], []
        solo_ep = self._solo_ep(task, have, wanted)
        for idx, link in enumerate(live, 1):
            tag = f"《{task['name']}》链接 {idx}/{len(live)}"
            left = None if limit_total is None else limit_total - len(saved)
            if left is not None and left <= 0:
                log.info("%s：本轮转存已达上限，剩余链接留到下一轮", tag)
                break
            try:
                got = self._run_link(task, link, q, dir_cache, have, left, solo_ep=solo_ep)
                if got:
                    log.info("%s：转存 %d 个 → %s", tag, len(got),
                             fmt_eps([e for g in got for e in g["eps"]]) or "无集数信息")
                    saved += [{**g, "link": link["url"]} for g in got]
                else:
                    log.info("%s：没有缺失的集数", tag)
            except LinkError as e:
                if e.network:
                    log.warning("%s：%s，跳过", tag, e)
                    errors.append(str(e))
                    continue
                link["fails"] = link.get("fails", 0) + 1
                log.warning("%s：%s（连续失败 %d 次）", tag, e, link["fails"])
                if e.permanent or link["fails"] >= 3:
                    link["ban"], link["ban_ts"] = str(e), int(time.time())
                    banned.append(f"{link['url']}：{e}")
                errors.append(str(e))
            except QuarkError as e:
                log.error("%s：%s", tag, e)
                errors.append(str(e))
                if "容量" in str(e) or "空间" in str(e):
                    self.maint.alert_space(f"《{task['name']}》转存失败：{e}") if self.maint._due("alert_space", 24 * 3600) else None
                    break  # 网盘满了，后面的链接没必要继续
            time.sleep(random.uniform(0.8, 2.0))

        self._snap(task, have, wanted, total_eps)
        if saved:
            self._after_save(task, saved)
        if banned:
            self._notify_error(task, "分享链接失效", "\n".join(banned))
        if saved:
            return {"status": "saved", "saved": saved,
                    "msg": f"转存 {len(saved)} 个：{fmt_eps([e for s in saved for e in s['eps']])}"}
        if errors:
            return {"status": "error", "saved": [], "msg": errors[0]}
        return {"status": "nochange", "saved": [], "msg": "没有缺失的集数"}

    def inspect_share(self, url, season=1, prefs=None, title=None, share_title=None):
        """
        看一眼分享里有什么：集数范围、画质、体积……给「搜索资源」和「自动找资源」用。结果缓存 10 分钟。
        season：文件名里没写季的，按这个季算。prefs：{"res": ["4k"], "hdr": "yes"}，用来统计「符合画质要求」的部分。
        """
        parsed = parse_share_text(url)
        if not parsed:
            raise ValueError("不是有效的夸克分享链接")
        link = parsed[0]
        prefs = prefs or {}
        want_res = [r for r in (prefs.get("res") or []) if r in RES_LABEL]
        want_hdr = prefs.get("hdr") if prefs.get("hdr") in ("yes", "no") else "any"
        no_cam = bool(prefs.get("no_cam"))
        share_title = (share_title or "").strip()
        ckey = (link["url"], season, tuple(sorted(want_res)), want_hdr, no_cam, norm_title(title), share_title)
        cache = self.__dict__.setdefault("_inspect_cache", {})
        hit = cache.get(ckey)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        q = self.quark()
        try:
            entries = self._entries({}, q, link, self._get_stoken(q, link), max_dirs=30)
        except LinkError as e:
            if e.network:
                raise QuarkError(str(e))   # 网络问题不缓存，也不说成"失效"
            res = {"ok": False, "error": str(e)}
            cache[ckey] = (time.time(), res)
            return res
        vids = [(f, hint) for f, hint, _ in entries if is_video(f["file_name"])]
        eps, eps_ok, expl, unparsed, size, ok_vids = set(), set(), set(), 0, 0, 0
        max_size, cam_vids, nt, title_hit = 0, 0, norm_title(title), False
        counts, first_why = {"4k": 0, "1080p": 0, "720p": 0, "other": 0, "hdr": 0}, ""
        for f, hint in vids:
            name = f["file_name"]
            got = parse_episodes(name, hint if hint is not None else -1)    # -1 = 文件名里没写季
            mapped = [(season if s == -1 else s, ep) for s, ep in got]
            expl.update(s for s, _ in got if s != -1)
            eps.update(mapped)
            unparsed += 0 if got else 1
            size += f.get("size", 0) or 0
            max_size = max(max_size, f.get("size", 0) or 0)
            title_hit = title_hit or bool(nt and nt in norm_title(name))
            t = quality_tags(name, f.get("_dirs", ()), share_title)
            cam_vids += 1 if t["cam"] else 0
            counts[t["res"] or "other"] += 1
            counts["hdr"] += 1 if t["hdr"] else 0
            why = quality_reason(want_res, want_hdr, name, no_cam, f.get("_dirs", ()), share_title)
            if why:
                first_why = first_why or why
            else:
                ok_vids += 1
                eps_ok.update(mapped)
        res = {"ok": True, "videos": len(vids), "eps": fmt_eps(eps), "ep_count": len(eps),
               "seasons": sorted({s for s, _ in eps}), "seasons_explicit": sorted(expl), "unparsed": unparsed,
               "size_gb": round(size / 1024 ** 3, 1), "avg_mb": round(size / len(vids) / 1024 ** 2) if vids else 0,
               "quality": [RES_LABEL[k] for k in ("4k", "1080p", "720p") if counts[k]] + (["HDR"] if counts["hdr"] else []),
               "quality_counts": counts, "prefs_set": bool(want_res or want_hdr != "any" or no_cam),
               "max_mb": round(max_size / 1024 ** 2), "cam_videos": cam_vids, "title_hit": title_hit if nt else None,
               "ok_videos": ok_vids, "why_skipped": first_why, "skipped_videos": len(vids) - ok_vids if (want_res or want_hdr != "any" or no_cam) else 0, "ok_eps": fmt_eps(eps_ok),
               "eps_list": [list(x) for x in sorted(eps)], "eps_ok_list": [list(x) for x in sorted(eps_ok)],
               "sample": [f["file_name"] for f, _ in vids[:3]]}
        cache[ckey] = (time.time(), res)
        return res

    # ------------------------------------------------------------ 自动找资源
    def pansou(self):
        from .pansou import PanSou
        c = self.store.data["pansou"]
        return PanSou(c) if c["enabled"] and c["url"] else None

    @staticmethod
    def _find_state(task):
        return task.setdefault("find", {"seen": {}, "log": [], "last": 0, "next": 0, "added_total": 0, "last_msg": ""})

    def auto_find_due(self, t, now=None):
        now = now or time.time()
        if t.get("kind") != "subscription" or not t.get("auto_find") or not t.get("enabled", True) or t["id"] in self.pending:
            return False
        if t.get("enddate") and date.today().isoformat() > t["enddate"]:
            return False
        if t.get("media") == "movie":
            if t.get("done") or (t.get("release_date") or "") > date.today().isoformat():
                return False   # 已入库 / 还没上映，都不用搜
        if (t.get("find") or {}).get("next", 0) > now:
            return False
        snap = t.get("snap")
        if snap and snap.get("missing_n") == 0 and snap.get("aired") is not None and any(not l.get("ban") for l in t["links"]):
            return False   # 本季已播出的都齐了，不用找
        return True

    def auto_find(self, tid, force=False):
        """搜索 PanSou → 检测候选 → 按规则筛选 → 加进订阅。force=True 用于刚订阅 / 手动点「立即搜索」。"""
        task = self.find(tid)
        ps = self.pansou()
        if not task or task.get("kind") != "subscription" or not ps or tid in self.pending:
            return None
        self.pending.add(tid)
        res = {"added": [], "msg": "", "lines": []}
        try:
            with self.run_lock:
                self.running = tid
                try:
                    res = self._auto_find(task, ps)
                except (PanSouError, QuarkError) as ex:
                    res = {"added": [], "msg": str(ex), "lines": [f"⚠ {ex}"], "error": True}
                    log.warning("《%s》自动找资源失败：%s", task["name"], ex)
                except Exception as ex:   # 兜底：不能让调度线程崩掉
                    res = {"added": [], "msg": f"内部错误：{ex}", "lines": [f"⚠ 内部错误：{ex}"], "error": True}
                    log.exception("《%s》自动找资源出错", task["name"])
                self._finish_find(task, res)
        finally:
            self.running = None
            self.pending.discard(tid)
        if (res.get("added") or force) and any(not l.get("ban") for l in task["links"]):
            self.run_task(tid, manual=True)   # 有新链接就立刻扫一遍，不用等下一轮
        return res

    def _finish_find(self, task, res):
        f, now = self._find_state(task), time.time()
        hours = 1 if res.get("error") else max(1, int(self.store.data["pansou"].get("auto_interval") or 6))
        s = task.get("snap") or {}
        if (not res.get("error") and not res.get("added") and self.store.data["auto"].get("missing_retry")
                and s.get("missing_n") and task.get("media") != "movie"):
            hours = min(hours, 2)   # 已播出的集还缺着：别等那么久，2 小时后再搜一次
        with self.store.lock:
            f["last"], f["next"] = int(now), int(now + hours * 3600 + random.randint(0, 600))
            f["last_msg"] = res["msg"]
            f["added_total"] = f.get("added_total", 0) + len(res["added"])
            for line in reversed(res.get("lines", [])):
                f["log"].insert(0, {"ts": int(now), "text": line})
            del f["log"][60:]
            if len(f["seen"]) > 300:
                for k in sorted(f["seen"], key=lambda k: f["seen"][k]["ts"])[:100]:
                    del f["seen"][k]
            self.store.save()
        dt = self.store.data["dingtalk"]
        if res["added"] and dt["enabled"] and dt["on_add"]:
            notify.send_dingtalk(dt, f"《{task['name']}》自动找到新资源",
                                 f"### 🤖《{task['name']}》自动添加了 {len(res['added'])} 个资源\n" + "\n".join(f"- {t}" for t in res["added"]))

    def _judge(self, task, ment, r, mine, need, total, title=""):
        """检测结果 → (结论, 理由, 能补的集数)。结论：ok / reject / dead。"""
        S = task["season"]
        if not r["ok"]:
            return "dead", f"链接已失效：{r['error']}", 0
        expl = set(r.get("seasons_explicit", []))
        if expl and S not in expl:
            return "reject", f"文件里写的是第 {'、'.join(map(str, sorted(expl)))} 季，不是第 {S} 季", 0
        if not expl and S > 1 and S not in ment:
            return "reject", f"没写是第几季，而本订阅是第 {S} 季，不敢要", 0
        everything = {e for s, e in map(tuple, r["eps_list"]) if s == S}
        rel = {e for s, e in map(tuple, r["eps_ok_list"] if r["prefs_set"] else r["eps_list"]) if s == S}
        if (not everything and r["videos"] == 1 and r.get("unparsed") == 1 and need is not None and len(need) == 1
                and r["avg_mb"] >= AUTO_MIN_AVG_MB):
            # 整个分享只有 1 个视频，名字认不出集数；而订阅恰好只差 1 集。搜到的标题已经对得上了，不必再看里面叫什么，转存时按缺的这一集改名
            if r["prefs_set"] and not r["ok_videos"]:
                return "reject", f"唯一的视频不符合你的画质要求（分享里是：{'、'.join(r['quality']) or '没标注画质'}）", 0
            return "ok", f"分享里只有 1 个视频（名字认不出集数），按你缺的 E{next(iter(need)):02d} 处理", 1
        if not rel:
            if r["prefs_set"] and everything:
                return "reject", f"有第 {S} 季的集，但没有符合你画质要求的（{r.get('why_skipped') or '分享里是：' + ('、'.join(r['quality']) or '没标注画质')}）", 0
            return "reject", f"没有识别到第 {S} 季的集数", 0
        if total and max(rel) > total + max(2, total // 5):
            return "reject", f"最大集号 {max(rel)} 远超本季总集数 {total}，可能不是这部剧或是合集", 0
        if r["videos"] and r["avg_mb"] < AUTO_MIN_AVG_MB:
            return "reject", f"平均每个视频只有 {r['avg_mb']} MB，多半是花絮 / 预告", 0
        useful = (rel & need) if need is not None else (rel - mine)
        if not useful:
            return "reject", f"现在能提供 {fmt_eps([(S, x) for x in rel])}，没有你缺的集", 0
        if len(rel) < 2 and not (need is not None and len(need) == 1) and self._title_upto(title) != max(rel):
            # 标题写着「更至第 N 集」且正好等于这里唯一的一集：刚开播的新剧，只有 1 集就是它本来的样子
            return "reject", "只有 1 集，不像完整的资源", 0
        extra = f"（另有 {r['skipped_videos']} 个不符合画质要求的视频会被跳过）" if r.get("skipped_videos") else ""
        return "ok", f"能补你缺的 {len(useful)} 集：{fmt_eps([(S, x) for x in useful])}{extra}", len(useful)

    _VERSION_TAG = r"(中国版|国产版|国版|中版|大陆版|日本?版|韩国?版|美国?版|泰国?版|英国?版|港版|台版|翻拍版?|重制版?|真人版)"

    @classmethod
    def _other_version(cls, task, names, title):
        """标题里「名字(中国版)」这类标注，说明是同名剧的另一个国家 / 翻拍版本。
        订阅本身（名称、别名）没带这个标注的话，就不是要的那一部，返回标注文字；没有返回 None。"""
        mine = " ".join([task.get("name", "")] + list(task.get("aliases") or []))
        for n, _ in names:
            for m in re.finditer(re.escape(n) + r"\s*[（(\[【]\s*" + cls._VERSION_TAG, title or ""):
                if m.group(1) not in mine:
                    return m.group(1)
        return None

    @staticmethod
    def _title_upto(title):
        """标题里「更至 / 更新至 / 连载至 第 N 集」的 N；没写返回 None。"""
        m = re.search(r"(?:更新?至|更新?到|连载至|连载到)\s*(?:第\s*)?(\d{1,4})\s*[集话話期]?", title or "")
        return int(m.group(1)) if m else None

    # ---- 名称 / 别名 ----
    @staticmethod
    def _strip_season(name):
        """有些剧在 TMDB 里的名字本身就带「第二季」，比对标题 / 搜索时要先去掉，否则会漏掉大量结果。"""
        return re.sub(r"[\s._-]*(第\s*[0-9一二三四五六七八九十]{1,3}\s*季|[Ss]eason\s*\d+|[Ss]\d{1,2})\s*$", "", name or "").strip() or (name or "")

    def _search_names(self, task, strip_season=True):
        """[(显示名, 规范化名)]：主名在前，TMDB 里的别名（原名、各地区译名）在后。
        老订阅没存过别名的，第一次用到时去 TMDB 补一次（失败不影响，只用主名）。"""
        if not task.get("aliases") and task.get("tmdb_id") and not task.get("aliases_tried"):
            task["aliases_tried"] = True
            try:
                tm = self.tmdb()
                if tm.ready:
                    task["aliases"] = tm.aliases(task.get("media", "tv"), task["tmdb_id"])
                    with self.store.lock:
                        self.store.save()
            except Exception as ex:
                log.warning("《%s》读取 TMDB 别名失败（只用主名搜索）：%s", task["name"], ex)
        raw = [task["name"]] + list(task.get("aliases") or [])
        out, seen = [], set()
        for i, n in enumerate(raw):
            n = self._strip_season(n) if strip_season else (n or "").strip()
            k = norm_title(n)
            # 主名一定保留；别名太短（单个汉字、三两个字母）容易和别的作品撞上，不用
            if not k or k in seen or (i and len(k) < (2 if re.search(r"[\u4e00-\u9fff]", k) else 4)):
                continue
            seen.add(k)
            out.append((n, k))
        return out[:AUTO_MAX_NAMES]

    @staticmethod
    def _match_name(names, title):
        """搜索结果标题里含有哪个名称；都不含返回 None。"""
        nt = norm_title(title)
        return next((n for n, k in names if k in nt), None)

    def _search_by_names(self, ps, names, groups, known, note):
        """groups: 每个名称对应的关键词列表。按顺序搜：搜到足够多「名字对得上的新结果」就停，否则换下一个名称（别名）再搜。"""
        results, ids = [], set()
        for gi, kws in enumerate(groups):
            for kw in kws:
                for it in ps.search(kw):
                    if it["pwd_id"] not in ids:
                        ids.add(it["pwd_id"])
                        results.append(it)
            fresh = sum(1 for it in results if it["pwd_id"] not in known and self._match_name(names, it["title"]))
            if fresh >= AUTO_NAME_ENOUGH:
                break
            if gi + 1 < len(groups):
                note("🔁", f"用「{kws[0]}」只搜到 {fresh} 个对得上的，换别名「{groups[gi + 1][0]}」再搜")
        return results

    def _auto_find(self, task, ps):
        if task.get("media") == "movie":
            return self._auto_find_movie(task, ps)
        q = self.quark()
        f, now, S = self._find_state(task), time.time(), task["season"]
        lines = []

        def note(icon, text):
            lines.append(f"{icon} {text}")
            log.info("《%s》自动找资源：%s %s", task["name"], icon, text)

        dead_auto = [l for l in task["links"] if l.get("auto") and l.get("ban")]
        if dead_auto:   # 自动加的、后来失效的链接，直接清掉
            task["links"] = [l for l in task["links"] if l not in dead_auto]
            note("🧹", f"移除了 {len(dead_auto)} 个已失效的自动链接")

        found = q.get_fids([self._dest(task)])
        have, _, _, _ = self._have_detail(task, q.ls_dir(found[0]["fid"]) if found else [])
        wanted, total, _ = self.wanted(task)
        mine = {x for s, x in have if s == S}
        need = (wanted - mine) if wanted is not None else None
        if need is not None and not need:
            return {"added": [], "msg": "本季已播出的都齐了，不用找", "lines": lines}
        slots = AUTO_MAX_LINKS - sum(1 for l in task["links"] if not l.get("ban"))
        if slots <= 0:
            note("⏸", f"已经有 {AUTO_MAX_LINKS} 个有效链接，不再添加")
            return {"added": [], "msg": "有效链接已满", "lines": lines}

        names = self._search_names(task)
        base = names[0][0]
        groups = [[n] + ([f"{n} 第{S}季"] if S > 1 and gi < 2 else []) for gi, (n, _) in enumerate(names)]
        known = {l["pwd_id"] for l in task["links"]}
        results = self._search_by_names(ps, names, groups, known, note)
        skip = {"已有": 0, "名字对不上": 0, "标题写的是别的季": 0, "标题标的是别的版本": 0, "最近看过": 0}
        cands = []
        for it in results:
            if it["pwd_id"] in known:
                skip["已有"] += 1
                continue
            hit = self._match_name(names, it["title"])
            if not hit:
                skip["名字对不上"] += 1
                continue
            if self._other_version(task, names, it["title"]):
                skip["标题标的是别的版本"] += 1   # 例如「我的大叔(中国版)」不是韩剧原版
                continue
            ment = title_seasons(it["title"], hit)
            if ment and S not in ment:
                skip["标题写的是别的季"] += 1
                continue
            sv = f["seen"].get(it["pwd_id"])
            if sv and now - sv["ts"] < AUTO_RECHECK.get(sv["verdict"], 0):
                skip["最近看过"] += 1
                continue
            cands.append((it, ment))

        def prio(c):
            t = c[0]["title"].lower()
            return (S in c[1], 2 if re.search(r"4k|2160", t) else 1 if "1080" in t else 0, c[0]["time"])
        cands.sort(key=prio, reverse=True)
        note("🔎", f"搜到 {len(results)} 个，进入检测 {len(cands)} 个（跳过：" + "，".join(f"{k} {v}" for k, v in skip.items() if v) + "）"
             if any(skip.values()) else f"搜到 {len(results)} 个，进入检测 {len(cands)} 个")
        prefs = {"res": task.get("want_res", []), "hdr": task.get("want_hdr", "any")}
        accepted, checked = [], 0
        for it, ment in cands:
            if checked >= AUTO_MAX_INSPECT:
                note("⏸", "本轮检测数量已达上限，剩下的下次再看")
                break
            try:
                r = self.inspect_share(it["url"], season=S, prefs=prefs, share_title=it["title"])
            except QuarkError as ex:
                note("⚠", f"读取分享失败，本轮先到这里：{ex}")
                break
            checked += 1
            verdict, reason, n = self._judge(task, ment, r, mine, need, total, it["title"])
            f["seen"][it["pwd_id"]] = {"ts": now, "verdict": verdict, "reason": reason}
            note("✅" if verdict == "ok" else "✗", f"{it['title'][:36]}：{reason}")
            if verdict == "ok":
                accepted.append((n, 2 if "4K" in r["quality"] else 1 if "1080P" in r["quality"] else 0, it))
            time.sleep(random.uniform(0.8, 1.6))
        accepted.sort(key=lambda x: (-x[0], -x[1]))
        add = accepted[:min(slots, AUTO_MAX_ADD)]
        for _, _, it in accepted[len(add):]:
            f["seen"][it["pwd_id"]]["verdict"] = "cap"
        for _, _, it in add:
            f["seen"][it["pwd_id"]]["verdict"] = "added"
            task["links"].append({"url": it["url"], "pwd_id": it["pwd_id"], "passcode": it["passcode"], "ban": "", "fails": 0,
                                  "baseline": None, "auto": True, "title": it["title"][:80], "added": int(now)})
        msg = (f"自动添加了 {len(add)} 个资源" if add else
               ("没有找到合适的新资源" if cands else "搜索结果里没有新的候选"))
        if add:
            note("🤖", msg)
        return {"added": [it["title"] for _, _, it in add], "msg": msg, "lines": lines}

    # ------------------------------------------------------------ 电影
    @staticmethod
    def _movie_filename(task, ext):
        """Emby 推荐的命名：片名 (年份).扩展名"""
        name = re.sub(r'[\\/:*?"<>|]+', " ", task["name"]).strip()
        return f"{name} ({task['year']}){ext}" if task.get("year") else f"{name}{ext}"

    def _emby_target(self, cli, task):
        """转存后要让 Emby 刷新的条目：电影找电影，剧集找剧。找不到返回 None（刷新整个库）。"""
        if task.get("media") == "movie":
            return cli.find_movie(task.get("emby_name") or task["name"], task.get("tmdb_id") or None, task.get("year") or None)[0]
        return self._emby_sid(cli, task)

    def _movie_have(self, task, dir_items, strict=False):
        """(是否已有, 说明)。目标目录里已经有视频，或 Emby 里已经有这部电影，都算已有。"""
        if any(not i["dir"] and is_video(i["file_name"]) for i in dir_items):
            return True, "夸克目录里已有视频"
        cli = self.emby()
        if not cli or not self.store.data["emby"]["only_missing"]:
            return False, "Emby 联动未启用（只按夸克目录判断）"
        try:
            mid, matched = cli.find_movie(task.get("emby_name") or task["name"], task.get("tmdb_id") or None, task.get("year") or None)
        except Exception as ex:
            if strict:
                raise EmbyError(str(ex)) from ex
            log.warning("《%s》Emby 查询失败（只按夸克目录判断）：%s", task["name"], ex)
            return False, f"Emby 查询失败（只按夸克目录判断）：{ex}"
        return (True, f"Emby 里已有《{matched}》") if mid else (False, "Emby 里还没有这部电影")

    def _pick_movie_file(self, task, entries, share_title=""):
        """从分享里挑出最合适的那一个视频。返回 (选中的文件或 None, [(被跳过的文件, 原因)], 通过筛选的候选列表)。
        一个分享里有多个视频就是多个版本（4K / 1080P、HDR / 普通……）：先按你的画质要求逐个筛掉不符合的，剩下的再挑最好的。"""
        nm, skipped, cands = norm_title(task["name"]), [], []
        for f, _, _ in entries:
            why = self._filter_reason(task, f, share_title)
            if not why and (f.get("size", 0) or 0) < MOVIE_MIN_MB * 1024 * 1024:
                why = f"体积小于 {MOVIE_MIN_MB} MB，多半是预告 / 花絮"
            (skipped if why else cands).append((f, why) if why else f)
        rank = {"4k": 3, "1080p": 2, "720p": 1}
        cands.sort(key=lambda f: (bool(nm) and nm in norm_title(f["file_name"]),
                                  rank.get(quality_tags(f["file_name"], f.get("_dirs", ()), share_title)["res"], 0), f.get("size", 0) or 0), reverse=True)
        return (cands[0] if cands else None), skipped, cands

    def _run_movie(self, task, manual):
        q = self.quark()
        found = q.get_fids([_norm_path(task["savepath"])])
        dir_items = q.ls_dir(found[0]["fid"]) if found else []
        try:
            have, msg = self._movie_have(task, dir_items, strict=True)
        except EmbyError as ex:
            m = f"Emby 查询失败，为避免把已有的电影重复转存，本次不转存：{ex}"
            log.error("《%s》%s", task["name"], m)
            return {"status": "error", "saved": [], "msg": m}
        task["snap"] = {"movie": True, "have": 1 if have else 0, "ts": int(time.time())}
        if have:
            task["done"] = True
            return {"status": "complete", "saved": [], "msg": f"{msg}，不用再转存"}
        task["done"] = False
        if not task["links"]:
            return {"status": "idle", "msg": "还没有分享链接", "saved": []}
        live = [l for l in task["links"] if not l.get("ban")]
        if not live:
            return {"status": "error", "msg": "所有分享链接均已失效", "saved": []}

        errors, banned = [], []
        for idx, link in enumerate(live, 1):
            tag = f"《{task['name']}》链接 {idx}/{len(live)}"
            try:
                stoken = self._get_stoken(q, link)
                link["fails"] = 0
                entries = self._entries(task, q, link, stoken, max_dirs=30)
                f, skipped, _ = self._pick_movie_file(task, entries, link.get("title", ""))
                if not f:
                    why = skipped[0][1] if skipped else "没有视频文件"
                    log.info("%s：没有可转存的视频（%s）", tag, why)
                    continue
                to_fid = q.ensure_dir(task["savepath"])
                q.wait_task(q.save_file([f["fid"]], [f["share_fid_token"]], to_fid, q.extract_url(link["url"])[0], stoken))
                target = f["file_name"]
                if task.get("rename", "auto") == "auto":
                    target = self._movie_filename(task, os.path.splitext(f["file_name"])[1])
                    got = next((x for x in q.ls_dir(to_fid) if x["file_name"] == f["file_name"]), None)
                    if got and target != f["file_name"]:
                        if q.rename(got["fid"], target).get("code") != 0:
                            target = f["file_name"]
                task["done"] = True
                task["snap"]["have"] = 1
                saved = [{"name": target, "eps": [], "link": link["url"]}]
                log.info("%s：已转存 %s（%.1f GB）", tag, target, (f.get("size", 0) or 0) / 1024 ** 3)
                self._after_save(task, saved)
                return {"status": "saved", "saved": saved, "msg": f"已转存 {target}（{(f.get('size', 0) or 0) / 1024 ** 3:.1f} GB）"}
            except LinkError as ex:
                if ex.network:
                    log.warning("%s：%s，跳过", tag, ex)
                    errors.append(str(ex))
                    continue
                link["fails"] = link.get("fails", 0) + 1
                log.warning("%s：%s（连续失败 %d 次）", tag, ex, link["fails"])
                if ex.permanent or link["fails"] >= 3:
                    link["ban"], link["ban_ts"] = str(ex), int(time.time())
                    banned.append(f"{link['url']}：{ex}")
                errors.append(str(ex))
            except QuarkError as ex:
                log.error("%s：%s", tag, ex)
                errors.append(str(ex))
                if "容量" in str(ex) or "空间" in str(ex):
                    break
            time.sleep(random.uniform(0.8, 2.0))
        if banned:
            self._notify_error(task, "分享链接失效", "\n".join(banned))
        if errors:
            return {"status": "error", "saved": [], "msg": errors[0]}
        return {"status": "nochange", "saved": [], "msg": "分享里还没有合适的视频"}

    def _preview_movie(self, task):
        q = self.quark()
        found = q.get_fids([_norm_path(task["savepath"])])
        have, msg = self._movie_have(task, q.ls_dir(found[0]["fid"]) if found else [])
        out = {"movie": True, "season": 1, "emby_msg": msg, "dir_have": "", "emby_have": "", "links": [],
               "warnings": ["这部电影已经入库，正式运行时不会再转存"] if have else []}
        for link in task["links"]:
            item = {"url": link["url"], "ban": link.get("ban", ""), "rows": [], "error": ""}
            out["links"].append(item)
            if item["ban"]:
                continue
            try:
                entries = self._entries(task, q, link, self._get_stoken(q, link), max_dirs=30)
            except (LinkError, QuarkError) as ex:
                item["error"] = str(ex)
                continue
            f, skipped, cands = self._pick_movie_file(task, entries, link.get("title", ""))
            for fx, why in skipped:
                item["rows"].append({"file": fx["file_name"], "eps": "", "action": "skip", "why": why})
            for c in cands:
                size = f"{(c.get('size', 0) or 0) / 1024 ** 3:.1f} GB"
                item["rows"].append({"file": c["file_name"], "eps": "", "action": "save" if c is f else "skip",
                                     "why": f"会转存这个：最合适（{size}）" if c is f else f"有更合适的，不转存这个（{size}）"})
            item["rows"] = item["rows"][:60]
        return out

    # ---- 自动找电影资源 ----
    def _judge_movie(self, task, r):
        """检测结果 → (结论, 理由)。电影看「是不是一部电影」，不看集数。"""
        if not r["ok"]:
            return "dead", f"链接已失效：{r['error']}"
        if r["videos"] == 0:
            return "reject", "里面没有视频"
        if r["ep_count"] >= 4:
            return "reject", f"像是剧集（识别到 {r['ep_count']} 集），不是电影"
        if r["videos"] > 8:
            return "reject", f"里面有 {r['videos']} 个视频，像是合集，不敢要"
        if r["max_mb"] < MOVIE_MIN_MB:
            return "reject", f"最大的视频只有 {r['max_mb']} MB，多半是预告 / 片段"
        if r["prefs_set"] and r["ok_videos"] == 0:
            if task.get("no_cam") and r["cam_videos"] == r["videos"]:
                return "reject", "都是枪版 / 抢先版（TC、TS、CAM）"
            return "reject", f"没有符合你要求的视频（{r.get('why_skipped') or '分享里是：' + ('、'.join(r['quality']) or '没标注画质')}）"
        # 搜索结果的标题已经对得上片名了，所以文件 / 文件夹叫什么都行（常见的是只有一个文件、名字很怪）；
        # 只有视频多、又都和片名无关时才不敢要，免得把别的电影合集转进来
        if r.get("title_hit") is False and r["videos"] > 3:
            return "reject", "里面有好几个视频，文件名里又都没有片名，不敢要"
        extra = f"；另有 {r['skipped_videos']} 个版本不符合你的画质要求，不会转存" if r.get("skipped_videos") else ""
        return "ok", f"一部 {r['max_mb'] / 1024:.1f} GB 的视频（{'、'.join(r['quality']) or '没标注画质'}）{extra}"

    def _auto_find_movie(self, task, ps):
        q = self.quark()
        f, now = self._find_state(task), time.time()
        lines = []

        def note(icon, text):
            lines.append(f"{icon} {text}")
            log.info("《%s》自动找资源：%s %s", task["name"], icon, text)

        rd = task.get("release_date") or ""
        if rd > date.today().isoformat():   # 刚订阅 / 手动点「立即搜索」也一样：没上映就不去搜（早期流出的多是枪版）
            note("⏳", f"{rd} 才上映，暂不找片源")
            return {"added": [], "msg": f"{rd} 才上映，暂不找片源", "lines": lines}
        dead_auto = [l for l in task["links"] if l.get("auto") and l.get("ban")]
        if dead_auto:
            task["links"] = [l for l in task["links"] if l not in dead_auto]
            note("🧹", f"移除了 {len(dead_auto)} 个已失效的自动链接")
        found = q.get_fids([_norm_path(task["savepath"])])
        have, msg = self._movie_have(task, q.ls_dir(found[0]["fid"]) if found else [])
        if have:
            task["done"] = True
            return {"added": [], "msg": f"{msg}，不用找", "lines": lines}
        slots = AUTO_MAX_LINKS - sum(1 for l in task["links"] if not l.get("ban"))
        if slots <= 0:
            note("⏸", f"已经有 {AUTO_MAX_LINKS} 个有效链接，不再添加")
            return {"added": [], "msg": "有效链接已满", "lines": lines}

        title, year = task["name"], task.get("year") or 0
        names = self._search_names(task, strip_season=False)
        groups = [[n] + ([f"{n} {year}"] if year else []) for n, _ in names]
        known = {l["pwd_id"] for l in task["links"]}
        results = self._search_by_names(ps, names, groups, known, note)
        skip = {"已有": 0, "名字对不上": 0, "年份对不上": 0, "标题标的是别的版本": 0, "最近看过": 0}
        cands = []
        for it in results:
            if it["pwd_id"] in known:
                skip["已有"] += 1
                continue
            if not self._match_name(names, it["title"]):
                skip["名字对不上"] += 1
                continue
            if self._other_version(task, names, it["title"]):
                skip["标题标的是别的版本"] += 1
                continue
            years = {int(y) for y in re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", it["title"])}
            if year and years and not (years & {year - 1, year, year + 1}):
                skip["年份对不上"] += 1   # 同名的另一部老片 / 翻拍版
                continue
            sv = f["seen"].get(it["pwd_id"])
            if sv and now - sv["ts"] < AUTO_RECHECK.get(sv["verdict"], 0):
                skip["最近看过"] += 1
                continue
            cands.append(it)
        cands.sort(key=lambda it: (str(year) in it["title"], bool(re.search(r"4k|2160|1080", it["title"].lower())), it["time"]), reverse=True)
        note("🔎", f"搜到 {len(results)} 个，进入检测 {len(cands)} 个" + (
            "（跳过：" + "，".join(f"{k} {v}" for k, v in skip.items() if v) + "）" if any(skip.values()) else ""))
        prefs = {"res": task.get("want_res", []), "hdr": task.get("want_hdr", "any"), "no_cam": bool(task.get("no_cam"))}
        accepted, checked = [], 0
        for it in cands:
            if checked >= AUTO_MAX_INSPECT:
                note("⏸", "本轮检测数量已达上限，剩下的下次再看")
                break
            try:
                r = self.inspect_share(it["url"], season=1, prefs=prefs, title=title, share_title=it["title"])
            except QuarkError as ex:
                note("⚠", f"读取分享失败，本轮先到这里：{ex}")
                break
            checked += 1
            verdict, reason = self._judge_movie(task, r)
            f["seen"][it["pwd_id"]] = {"ts": now, "verdict": verdict, "reason": reason}
            note("✅" if verdict == "ok" else "✗", f"{it['title'][:36]}：{reason}")
            if verdict == "ok":
                accepted.append((2 if "4K" in r["quality"] else 1 if "1080P" in r["quality"] else 0, r["max_mb"], it))
            time.sleep(random.uniform(0.8, 1.6))
        accepted.sort(key=lambda x: (-x[0], -x[1]))
        add = accepted[:min(slots, AUTO_MAX_ADD_MOVIE)]
        for _, _, it in accepted[len(add):]:
            f["seen"][it["pwd_id"]]["verdict"] = "cap"
        for _, _, it in add:
            f["seen"][it["pwd_id"]]["verdict"] = "added"
            task["links"].append({"url": it["url"], "pwd_id": it["pwd_id"], "passcode": it["passcode"], "ban": "", "fails": 0,
                                  "baseline": None, "auto": True, "title": it["title"][:80], "added": int(now)})
        msg = f"自动添加了 {len(add)} 个资源" if add else ("没有找到合适的片源" if cands else "搜索结果里没有新的候选")
        if add:
            note("🤖", msg)
        return {"added": [it["title"] for _, _, it in add], "msg": msg, "lines": lines}

    def preview(self, task):
        """试运行：不转存、不建目录，逐个文件说明"会不会转、为什么"。"""
        if task.get("media") == "movie":
            return self._preview_movie(task)
        q = self.quark()
        path = self._dest(task)
        found = q.get_fids([path])
        fid = found[0]["fid"] if found else None
        dir_items = q.ls_dir(fid) if fid else []
        dir_cache = {(): {"fid": fid, "names": {i["file_name"] for i in dir_items}, "path": path}}
        have, emby_msg, dir_have, emby_have = self._have_detail(task, dir_items)
        srcs = {"dir": dir_have, "emby": emby_have}
        warnings = [w for w in (self._season_warning(task, emby_have),) if w]
        if not found:
            warnings.append(f"转存目录 {path} 还不存在（正式运行时会自动创建）")
        out = {"season": task["season"], "emby_msg": emby_msg, "dir_have": fmt_eps(dir_have),
               "emby_have": fmt_eps(emby_have), "warnings": warnings, "links": []}
        limit_total = None if task.get("saved_total", 0) == 0 else INCREMENTAL_LIMIT
        planned = 0
        wanted = self.wanted(task)[0]
        solo_ep = self._solo_ep(task, have, wanted)
        for link in task["links"]:
            item = {"url": link["url"], "ban": link.get("ban", ""), "rows": [], "error": ""}
            out["links"].append(item)
            if item["ban"]:
                continue
            left = None if limit_total is None else max(0, limit_total - planned)
            try:
                got = self._run_link(task, link, q, dir_cache, have, left,
                                     trace=item["rows"], dry=True, srcs=srcs, solo_ep=solo_ep)
                planned += len(got)
            except (LinkError, QuarkError) as ex:
                item["error"] = str(ex)
        return out

    # ------------------------------------------------------------ 转存后联动
    def _after_save(self, task, saved):
        cfg = self.store.data
        ss = cfg["smartstrm"]
        if ss["enabled"]:
            time.sleep(min(ss.get("delay", 3), 60))
            ok, msg = notify.trigger_smartstrm(ss, self._dest(task))
            log.info("SmartStrm：%s", msg) if ok else log.warning("SmartStrm：%s", msg)
        em = cfg["emby"]
        cli = self.emby()
        if cli and em["refresh_after_save"]:
            time.sleep(min(em.get("refresh_delay", 10), 120))
            try:
                sid = self._emby_target(cli, task)
                cli.refresh(sid)
                log.info("已通知 Emby 刷新%s", "《%s》" % task["name"] if sid else "媒体库")
            except Exception as e:
                log.warning("Emby 刷新失败：%s", e)
        dt = cfg["dingtalk"]
        if dt["enabled"] and dt["on_save"]:
            lines = "\n".join(f"- {s['name']}" for s in saved[:30])
            more = f"\n- …共 {len(saved)} 个" if len(saved) > 30 else ""
            ok, msg = notify.send_dingtalk(
                dt, f"《{task['name']}》有更新",
                f"### ✅《{task['name']}》追更\n转存到 `{self._dest(task)}`\n\n{lines}{more}")
            if not ok:
                log.warning("钉钉：%s", msg)

    def _notify_error(self, task, title, body):
        dt = self.store.data["dingtalk"]
        if dt["enabled"] and dt["on_error"]:
            notify.send_dingtalk(dt, f"《{task['name']}》{title}", f"### ❌《{task['name']}》{title}\n{body}")

    # ------------------------------------------------------------ 调度
    def run_task(self, tid, manual=False):
        task = self.find(tid)
        if not task or tid in self.pending:
            return False
        self.pending.add(tid)
        try:
            with self.run_lock:
                self.running = tid
                log.info("▶ 开始检查《%s》%s", task["name"], "（手动）" if manual else "")
                try:
                    res = self._run(task, manual)
                except QuarkError as e:
                    res = {"status": "error", "msg": str(e), "saved": []}
                    log.error("《%s》%s", task["name"], e)
                except Exception as e:  # 兜底：不能让调度线程崩掉
                    res = {"status": "error", "msg": f"内部错误：{e}", "saved": []}
                    log.exception("《%s》运行出错", task["name"])
                self._finish(task, res)
        finally:
            self.running = None
            self.pending.discard(tid)
        return True

    def _finish(self, task, res):
        now = datetime.now()
        with self.store.lock:
            task["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
            task["last_status"], task["last_msg"] = res["status"], res["msg"]
            task["next_run"] = int(time.time()) + task["interval"] * 60 + random.randint(0, 90)
            if res["saved"]:
                task["saved_total"] = task.get("saved_total", 0) + len(res["saved"])
            if res["saved"]:
                recent = self.store.data["recent"]
                recent.insert(0, {
                    "ts": int(time.time()), "task_id": task["id"], "name": task["name"], "kind": task["kind"],
                    "poster": task.get("poster", ""), "season": task["season"], "count": len(res["saved"]),
                    "eps": fmt_eps([e for s in res["saved"] for e in s["eps"]]),
                    "files": [s["name"] for s in res["saved"]][:20]})
                del recent[200:]
            if res["saved"] or res["status"] == "error":
                task["history"] = ([{
                    "time": task["last_run"], "status": res["status"], "msg": res["msg"],
                    "files": [s["name"] for s in res["saved"]][:50],
                }] + task.get("history", []))[:30]
            self.store.save()
        icon = {"saved": "✅", "error": "❌", "complete": "🏁"}.get(res["status"], "⏹")
        log.info("%s《%s》%s", icon, task["name"], res["msg"])

    def _seed_recent_dirs(self):
        """升级前已有的任务：把它们的目录补进「最近使用的目录」，否则老用户第一次订阅时默认目录会退回到写死的兜底值。
        只补一次（用户点了「清除记录」之后不会再冒出来）。"""
        with self.store.lock:
            if self.store.data.get("recent_dirs_seeded"):
                return
            have = {d["path"] for d in self.store.data["recent_dirs"]}
            add = []
            for t in sorted(self.store.data["tasks"], key=lambda t: -t.get("created", 0)):   # 新建的排前面
                p = _norm_path(t["savepath"])
                if p != "/" and p not in have:
                    have.add(p)
                    add.append({"path": p, "name": t["name"], "media": self._dir_kind(t), "ts": t.get("created", 0)})
            self.store.data["recent_dirs"] = (self.store.data["recent_dirs"] + add)[:12]
            self.store.data["recent_dirs_seeded"] = True
            self.store.save()

    def _seed_recent(self):
        """升级前已有转存历史、但还没有 recent 的：从任务历史里补一份，面板不至于一片空白。"""
        with self.store.lock:
            if self.store.data["recent"]:
                return
            events = []
            for t in self.store.data["tasks"]:
                for h in t.get("history", []):
                    if h.get("status") != "saved":
                        continue
                    try:
                        ts = int(datetime.strptime(h["time"], "%Y-%m-%d %H:%M:%S").timestamp())
                    except (KeyError, ValueError):
                        continue
                    events.append({"ts": ts, "task_id": t["id"], "name": t["name"], "kind": t["kind"],
                                   "poster": t.get("poster", ""), "season": t.get("season", 1),
                                   "count": len(h.get("files", [])) or 1,
                                   "eps": (h.get("msg", "").split("：", 1) + [""])[1],
                                   "files": h.get("files", [])[:20]})
            if events:
                self.store.data["recent"] = sorted(events, key=lambda e: -e["ts"])[:200]

    def overview(self):
        """右侧「最近入库」面板的数据。"""
        now = time.time()
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.store.lock:
            tasks, rec = self.store.data["tasks"], list(self.store.data["recent"])
            nxt = [t["next_run"] for t in tasks if t.get("enabled") and t["links"] and t.get("next_run")]
            running = next((t["name"] for t in tasks if t["id"] == self.running), "")
            return {"stats": {
                "today": sum(e["count"] for e in rec if e["ts"] >= midnight),
                "week": sum(e["count"] for e in rec if e["ts"] >= now - 7 * 86400),
                "total": sum(t.get("saved_total", 0) for t in tasks),
                "subs": sum(1 for t in tasks if t["kind"] == "subscription" and t.get("media") != "movie"),
                "movies": sum(1 for t in tasks if t.get("media") == "movie"),
                "monitors": sum(1 for t in tasks if t["kind"] == "monitor"),
                "dead_links": sum(1 for t in tasks for l in t["links"] if l.get("ban")),
                "errors": sum(1 for t in tasks if t.get("last_status") == "error"),
                "next_run": min(nxt) if nxt else 0, "running": running},
                "recent": rec[:30]}

    def is_due(self, t, now=None):
        now = now or datetime.now()
        if not t.get("enabled") or not t["links"] or t["id"] in self.pending or t.get("done"):
            return False
        if t.get("enddate") and now.date().isoformat() > t["enddate"]:
            return False
        if now.isoweekday() not in t.get("run_weeks", [1, 2, 3, 4, 5, 6, 7]):
            return False
        return t.get("next_run", 0) <= time.time()

    def tick(self):
        self._daily_sign()
        self.maint.tick()
        with self.store.lock:
            due = [t["id"] for t in self.store.data["tasks"] if self.is_due(t)]
        for tid in due:
            self.run_task(tid)
        if self.pansou():
            with self.store.lock:
                finds = [t["id"] for t in self.store.data["tasks"] if self.auto_find_due(t)]
            for tid in finds:
                self.auto_find(tid)

    def _daily_sign(self):
        d = self.store.data
        today = date.today().isoformat()
        if not d.get("auto_sign") or d.get("last_sign_date") == today or not d.get("cookie"):
            return
        with self.store.lock:
            d["last_sign_date"] = today  # 无论成败一天只试一次
            self.store.save()
        try:
            ok, msg = self.quark().growth_sign()
        except QuarkError as e:
            ok, msg = False, str(e)
        if ok:
            log.info("📅 每日签到成功，获得 %.0f MB", msg / 1024 / 1024)
        else:
            log.info("📅 每日签到：%s", msg)

    # ------------------------------------------------------------ 旧版配置导入
    def import_legacy(self, path):
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
        cookie = old.get("cookie")
        if isinstance(cookie, list):
            cookie = cookie[0] if cookie else ""
        n = 0
        with self.store.lock:
            if cookie and not self.store.data["cookie"]:
                self.store.data["cookie"] = cookie.strip()
            for it in old.get("tasklist", []):
                links = parse_share_text(it.get("shareurl", ""))
                if not links:
                    continue
                pat, rep = it.get("pattern", ""), it.get("replace", "")
                rename = "auto" if pat.startswith("$") else ("custom" if rep else "keep")
                task = self.normalize({
                    "name": it.get("taskname", ""), "savepath": it.get("savepath", ""),
                    "links_text": "\n".join(l["url"] for l in links),
                    "pattern": "" if pat.startswith("$") or pat in (".*", "") else pat,
                    "replace": rep, "rename": rename, "enddate": it.get("enddate", "") or "",
                    "run_weeks": it.get("runweek") or [1, 2, 3, 4, 5, 6, 7],
                })
                self.store.data["tasks"].append({
                    "id": uuid.uuid4().hex[:8], "created": int(time.time()), "next_run": 0,
                    "last_run": "", "last_status": "", "last_msg": "", "saved_total": 0,
                    "history": [], **task,
                })
                n += 1
            self.store.save()
        return n


class Scheduler(threading.Thread):
    def __init__(self, engine, interval=20):
        super().__init__(daemon=True, name="scheduler")
        self.engine, self.interval = engine, interval

    def run(self):
        time.sleep(5)
        while True:
            try:
                self.engine.tick()
            except Exception:
                log.exception("调度线程异常")
            time.sleep(self.interval)
