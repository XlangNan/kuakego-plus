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
from .parser import (fmt_eps, is_video, parse_episodes, parse_share_text,
                     season_from_text)
from .quark import Quark, QuarkError
from .tmdb import Tmdb

MIN_INTERVAL = 60        # 分钟。再低容易触发夸克风控
INCREMENTAL_LIMIT = 30   # 追更时单轮最多转存个数；首次回填不限
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

        fields = {
            "kind": pick("kind", "monitor") if pick("kind", "monitor") in ("monitor", "subscription") else "monitor",
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
             "fails": l.get("fails", 0)}
            for l in t["links"]
        ]
        d["state"] = ("running" if self.running == t["id"] else
                      "queued" if t["id"] in self.pending else "idle")
        d["unparsed"] = self._unparsed_policy(t)
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

    def _entries(self, task, q, link, stoken):
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
        out, budget = [], [100]

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

    def progress(self, task):
        q = self.quark()
        to_fid = q.ensure_dir(task["savepath"])
        have, emby_msg = self._have(task, q.ls_dir(to_fid))
        season = task["season"]
        mine = sorted(e for s, e in have if s == season)
        aired, total, src = self.wanted(task)
        out = {"have": mine, "have_count": len(mine), "emby": emby_msg, "source": src,
               "aired": len(aired) if aired is not None else None, "total": total,
               "missing": sorted(aired - set(mine)) if aired is not None else []}
        return out

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

    def _passes_filters(self, task, f):
        name = f["file_name"]
        if task.get("pattern"):
            if not re.search(task["pattern"], name):
                return False
        elif not is_video(name):
            return False
        if task.get("include_kw") and not _kw_match(name, task["include_kw"]):
            return False
        if task.get("exclude_kw") and _kw_match(name, task["exclude_kw"]):
            return False
        min_b = task.get("min_size_mb", 0) * 1024 * 1024
        return not (min_b and f.get("size", 0) < min_b)

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
            if not self._passes_filters(task, f):
                tr(fname, [], "skip", "不符合过滤规则（非视频 / 关键词 / 最小体积）")
                continue
            eps = parse_episodes(fname, hint if hint is not None else task["season"])
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

        if task.get("skip_when_complete") and not manual:
            wanted, total, src = self.wanted(task)
            if wanted and {(task["season"], e) for e in wanted} <= have:
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
            if res["saved"] or res["status"] == "error":
                task["history"] = ([{
                    "time": task["last_run"], "status": res["status"], "msg": res["msg"],
                    "files": [s["name"] for s in res["saved"]][:50],
                }] + task.get("history", []))[:30]
            self.store.save()
        icon = {"saved": "✅", "error": "❌", "complete": "🏁"}.get(res["status"], "⏹")
        log.info("%s《%s》%s", icon, task["name"], res["msg"])

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
