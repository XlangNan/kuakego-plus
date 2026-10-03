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
from .pansou import PanSouError
from .parser import (RES_LABEL, fmt_eps, is_video, norm_title, parse_episodes, parse_share_text,
                     quality_reason, quality_tags, title_seasons,
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
        self._seed_recent()

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
        rename = pick("rename", "auto")
        if rename not in ("auto", "keep", "custom"):
            rename = "auto"
        if rename == "custom" and not (pattern and pick("replace", "")):
            raise ValueError("自定义重命名需要同时填写「匹配」和「替换」")

        kind = pick("kind", "monitor") if pick("kind", "monitor") in ("monitor", "subscription") else "monitor"
        want_res = [r for r in dict.fromkeys(pick("want_res", []) or []) if r in RES_LABEL]
        want_hdr = pick("want_hdr", "any") if pick("want_hdr", "any") in ("any", "yes", "no") else "any"
        fields = {
            "kind": kind, "want_res": want_res, "want_hdr": want_hdr,
            "auto_find": bool(pick("auto_find", False)),
            "strict_season": bool(pick("strict_season", kind == "subscription")),
            "name": name, "links": links, "savepath": savepath, "interval": interval,
            "enabled": bool(pick("enabled", True)),
            "enddate": (pick("enddate", "") or "").strip(),
            "run_weeks": weeks or [1, 2, 3, 4, 5, 6, 7],
            "season": season, "rename": rename, "pattern": pattern,
            "replace": pick("replace", ""),
            "keep_tree": bool(pick("keep_tree", True)),
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

    def create_task(self, payload, save_existing=True):
        fields = self.normalize(payload)
        task = {
            "id": uuid.uuid4().hex[:8], "created": int(time.time()),
            "next_run": 0, "last_run": "", "last_status": "", "last_msg": "",
            "saved_total": 0, "history": [], **fields,
        }
        if not save_existing:  # 只追新：记下现在链接里已有的文件，之后不转
            self.snapshot_baseline(task)
        with self.store.lock:
            self.store.data["tasks"].append(task)
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
        with self.store.lock:
            task.update(fields)
            self.store.save()
        return task

    def delete_task(self, tid):
        with self.store.lock:
            n = len(self.store.data["tasks"])
            self.store.data["tasks"] = [t for t in self.store.data["tasks"] if t["id"] != tid]
            self.store.save()
            return len(self.store.data["tasks"]) < n

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
        d["want_res"], d["want_hdr"] = t.get("want_res", []), t.get("want_hdr", "any")
        f = t.get("find") or {}
        d["find"] = {"last": f.get("last", 0), "next": f.get("next", 0), "msg": f.get("last_msg", ""),
                     "added_total": f.get("added_total", 0), "log": f.get("log", [])[:30]}
        d["expired"] = bool(t.get("enddate") and date.today().isoformat() > t["enddate"])
        live = [l for l in t["links"] if not l.get("ban")]
        if d["expired"]:
            d["status"] = "expired"
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
        base_hint = None
        if len(top) == 1 and top[0]["dir"]:
            base_hint = season_from_text(top[0]["file_name"], None)
            top = q.get_detail(pwd_id, stoken, top[0]["fid"])
        out, budget = [], [max_dirs]

        def walk(items, rel, hint, depth):
            for f in items:
                if not f["dir"]:
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
        q = self.quark()
        found = q.get_fids([_norm_path(task["savepath"])])  # 只查不建：看进度不该有副作用
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
            return f"S{s:02d}E{e:02d}{ext}"
        return fname

    def _dir_state(self, q, task, rel, cache):
        if rel not in cache:
            path = _norm_path(task["savepath"] + "/" + "/".join(rel))
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

    def _filter_reason(self, task, f):
        """文件不该转存的原因；该转存则返回空字符串。"""
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
        return quality_reason(task.get("want_res") or [], task.get("want_hdr", "any"), name)

    def _run_link(self, task, link, q, dir_cache, have, limit=None, trace=None, dry=False, srcs=None):
        """处理单个链接，返回 [{name, eps}]。have 仅在成功后合并。"""
        stoken = self._get_stoken(q, link)
        link["fails"] = 0
        pwd_id = q.extract_url(link["url"])[0]
        entries = self._entries(task, q, link, stoken)
        baseline = set(link.get("baseline") or [])
        link_have, cands, seen, unparsed = set(have), [], set(), []
        keep_tree = task.get("keep_tree", True)

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
            why = self._filter_reason(task, f)
            if why:
                tr(fname, [], "skip", why)
                continue
            eps = parse_episodes(fname, hint if hint is not None else task["season"])
            if eps and self._strict_season(task):
                other = sorted({s for s, _ in eps if s != task["season"]})
                if other:
                    tr(fname, eps, "skip", f"这是第 {'、'.join(map(str, other))} 季，本订阅是第 {task['season']} 季（「编辑」里可关闭「只转存本季」）")
                    continue
            if fname in baseline:
                tr(fname, eps, "skip", "添加监控时就已存在（当时选了只追新）")
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
                if all(e in link_have for e in eps):
                    tr(fname, eps, "skip", f"这一集已有（{have_src(eps)}）")
                    continue
                link_have.update(eps)
            target = self._target_name(task, fname, eps)
            if target in d["names"] or (rel, target) in seen:
                tr(fname, eps, "skip", "同一集已选了另一个版本" if (rel, target) in seen else "目标目录里已有同名文件")
                continue
            seen.add((rel, target))
            cands.append({"f": f, "target": target, "eps": eps, "rel": rel, "src": src_rel})
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
            tr(c["f"]["file_name"], c["eps"], "save", ("缺少这一集" if c["eps"] else "解析不出集数，按文件名判断") + note)
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
        q = self.quark()
        if not task["links"]:
            return {"status": "idle", "msg": "还没有分享链接", "saved": []}
        live = [l for l in task["links"] if not l.get("ban")]
        if not live:
            return {"status": "error", "msg": "所有分享链接均已失效", "saved": []}

        to_fid = q.ensure_dir(task["savepath"])
        dir_items = q.ls_dir(to_fid)
        dir_cache = {(): {"fid": to_fid, "names": {i["file_name"] for i in dir_items},
                          "path": _norm_path(task["savepath"])}}
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
        for idx, link in enumerate(live, 1):
            tag = f"《{task['name']}》链接 {idx}/{len(live)}"
            left = None if limit_total is None else limit_total - len(saved)
            if left is not None and left <= 0:
                log.info("%s：本轮转存已达上限，剩余链接留到下一轮", tag)
                break
            try:
                got = self._run_link(task, link, q, dir_cache, have, left)
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
                    link["ban"] = str(e)
                    banned.append(f"{link['url']}：{e}")
                errors.append(str(e))
            except QuarkError as e:
                log.error("%s：%s", tag, e)
                errors.append(str(e))
                if "容量" in str(e) or "空间" in str(e):
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

    def inspect_share(self, url, season=1, prefs=None):
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
        ckey = (link["url"], season, tuple(sorted(want_res)), want_hdr)
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
        counts = {"4k": 0, "1080p": 0, "720p": 0, "other": 0, "hdr": 0}
        for f, hint in vids:
            name = f["file_name"]
            got = parse_episodes(name, hint if hint is not None else -1)    # -1 = 文件名里没写季
            mapped = [(season if s == -1 else s, ep) for s, ep in got]
            expl.update(s for s, _ in got if s != -1)
            eps.update(mapped)
            unparsed += 0 if got else 1
            size += f.get("size", 0) or 0
            t = quality_tags(name)
            counts[t["res"] or "other"] += 1
            counts["hdr"] += 1 if t["hdr"] else 0
            if not quality_reason(want_res, want_hdr, name):
                ok_vids += 1
                eps_ok.update(mapped)
        res = {"ok": True, "videos": len(vids), "eps": fmt_eps(eps), "ep_count": len(eps),
               "seasons": sorted({s for s, _ in eps}), "seasons_explicit": sorted(expl), "unparsed": unparsed,
               "size_gb": round(size / 1024 ** 3, 1), "avg_mb": round(size / len(vids) / 1024 ** 2) if vids else 0,
               "quality": [RES_LABEL[k] for k in ("4k", "1080p", "720p") if counts[k]] + (["HDR"] if counts["hdr"] else []),
               "quality_counts": counts, "prefs_set": bool(want_res or want_hdr != "any"),
               "ok_videos": ok_vids, "ok_eps": fmt_eps(eps_ok),
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
        if not rel:
            if r["prefs_set"] and everything:
                return "reject", f"有第 {S} 季的集，但没有符合你画质要求的（分享里是：{'、'.join(r['quality']) or '没标注画质'}）", 0
            return "reject", f"没有识别到第 {S} 季的集数", 0
        if total and max(rel) > total * 1.5 + 5:
            return "reject", f"最大集号 {max(rel)} 远超本季总集数 {total}，可能不是这部剧或是合集", 0
        if r["videos"] and r["avg_mb"] < AUTO_MIN_AVG_MB:
            return "reject", f"平均每个视频只有 {r['avg_mb']} MB，多半是花絮 / 预告", 0
        useful = (rel & need) if need is not None else (rel - mine)
        if not useful:
            return "reject", f"现在能提供 {fmt_eps([(S, x) for x in rel])}，没有你缺的集", 0
        if len(rel) < 2 and not (need is not None and len(need) == 1):
            return "reject", "只有 1 集，不像完整的资源", 0
        return "ok", f"能补你缺的 {len(useful)} 集：{fmt_eps([(S, x) for x in useful])}", len(useful)

    def _auto_find(self, task, ps):
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

        found = q.get_fids([_norm_path(task["savepath"])])
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

        # 有些剧在 TMDB 里的名字本身就带「第二季」，比对标题 / 搜索时要先去掉，否则会漏掉大量结果
        base = re.sub(r"[\s._-]*(第\s*[0-9一二三四五六七八九十]{1,3}\s*季|[Ss]eason\s*\d+|[Ss]\d{1,2})\s*$", "", task["name"]).strip() or task["name"]
        results, ids = [], set()
        for kw in [base] + ([f"{base} 第{S}季"] if S > 1 else []):
            for it in ps.search(kw):
                if it["pwd_id"] not in ids:
                    ids.add(it["pwd_id"])
                    results.append(it)
        known, nm = {l["pwd_id"] for l in task["links"]}, norm_title(base)
        skip = {"已有": 0, "名字对不上": 0, "标题写的是别的季": 0, "最近看过": 0}
        cands = []
        for it in results:
            if it["pwd_id"] in known:
                skip["已有"] += 1
                continue
            if nm and nm not in norm_title(it["title"]):
                skip["名字对不上"] += 1
                continue
            ment = title_seasons(it["title"], base)
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
                r = self.inspect_share(it["url"], season=S, prefs=prefs)
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

    def preview(self, task):
        """试运行：不转存、不建目录，逐个文件说明"会不会转、为什么"。"""
        q = self.quark()
        path = _norm_path(task["savepath"])
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
        for link in task["links"]:
            item = {"url": link["url"], "ban": link.get("ban", ""), "rows": [], "error": ""}
            out["links"].append(item)
            if item["ban"]:
                continue
            left = None if limit_total is None else max(0, limit_total - planned)
            try:
                got = self._run_link(task, link, q, dir_cache, have, left,
                                     trace=item["rows"], dry=True, srcs=srcs)
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
            ok, msg = notify.trigger_smartstrm(ss, task["savepath"])
            log.info("SmartStrm：%s", msg) if ok else log.warning("SmartStrm：%s", msg)
        em = cfg["emby"]
        cli = self.emby()
        if cli and em["refresh_after_save"]:
            time.sleep(min(em.get("refresh_delay", 10), 120))
            try:
                sid = self._emby_sid(cli, task)
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
                f"### ✅《{task['name']}》追更\n转存到 `{task['savepath']}`\n\n{lines}{more}")
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
                "subs": sum(1 for t in tasks if t["kind"] == "subscription"),
                "monitors": sum(1 for t in tasks if t["kind"] == "monitor"),
                "dead_links": sum(1 for t in tasks for l in t["links"] if l.get("ban")),
                "errors": sum(1 for t in tasks if t.get("last_status") == "error"),
                "next_run": min(nxt) if nxt else 0, "running": running},
                "recent": rec[:30]}

    def is_due(self, t, now=None):
        now = now or datetime.now()
        if not t.get("enabled") or not t["links"] or t["id"] in self.pending:
            return False
        if t.get("enddate") and now.date().isoformat() > t["enddate"]:
            return False
        if now.isoweekday() not in t.get("run_weeks", [1, 2, 3, 4, 5, 6, 7]):
            return False
        return t.get("next_run", 0) <= time.time()

    def tick(self):
        self._daily_sign()
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
