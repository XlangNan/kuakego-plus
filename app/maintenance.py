# -*- coding: utf-8 -*-
"""后台维护：失效链接清理、完结自动停止、洗版提示、每日日报、Cookie / 容量提醒。
全部由调度线程每轮调用 Maintenance.tick()，每项自己限频，出错只记日志不影响转存。"""
import re
import time
from datetime import date, datetime

from . import notify
from .logbuf import log
from .parser import fmt_eps, is_video, quality_tags
from .quark import QuarkError

RES_RANK = {"4k": 3, "1080p": 2, "720p": 1}
RES_NAME = {3: "4K", 2: "1080P", 1: "720P", 0: "未知"}
_TITLE_RES = [(re.compile(r"4k|2160", re.I), 3), (re.compile(r"1080", re.I), 2), (re.compile(r"720", re.I), 1)]
GB = 1024 ** 3


def title_res_rank(title):
    for rx, rank in _TITLE_RES:
        if rx.search(title or ""):
            return rank
    return 0


class Maintenance:
    def __init__(self, engine):
        self.e = engine
        self.store = engine.store

    # ------------------------------------------------------------ 状态 / 通用
    def cfg(self):
        return self.store.data["auto"]

    def _due(self, key, every):
        """限频：距离上次执行满 every 秒才返回 True，并记下时间。"""
        st = self.store.data["maint_state"]
        now = time.time()
        if now - st.get(key, 0) < every:
            return False
        with self.store.lock:
            st[key] = int(now)
            self.store.save()
        return True

    def _dingtalk(self, title, text, kind="on_error"):
        dt = self.store.data["dingtalk"]
        if dt["enabled"] and dt.get(kind, True):
            ok, msg = notify.send_dingtalk(dt, title, text)
            if not ok:
                log.warning("钉钉：%s", msg)

    def tick(self):
        for name, fn in (("清理失效链接", self.purge_dead_links), ("完结检查", self.auto_finish),
                         ("账号检查", self.check_account), ("洗版检查", self.check_upgrades),
                         ("每日日报", self.daily_digest)):
            try:
                fn()
            except Exception:
                log.exception("维护任务「%s」出错", name)

    # ------------------------------------------------------------ 3 失效链接自动清理
    def purge_dead_links(self, force=False):
        """失效超过 N 天的链接直接移除（订阅开了自动找资源的会重新补新的）。返回清掉的个数。"""
        days = int(self.cfg().get("purge_dead_days") or 0)
        if not force and (days <= 0 or not self._due("purge", 3600)):
            return 0
        cutoff, now, removed = time.time() - days * 86400, int(time.time()), 0
        with self.store.lock:
            for t in self.store.data["tasks"]:
                gone = []
                for l in t["links"]:
                    if not l.get("ban"):
                        continue
                    if not l.get("ban_ts"):
                        l["ban_ts"] = now          # 老数据没有记录时间：从现在开始计
                        continue
                    if force or l["ban_ts"] <= cutoff:
                        gone.append(l)
                if gone:
                    t["links"] = [l for l in t["links"] if l not in gone]
                    removed += len(gone)
                    f = self.e._find_state(t)
                    f["log"].insert(0, {"ts": now, "text": f"🧹 清理了 {len(gone)} 个失效链接"})
                    del f["log"][60:]
                    log.info("《%s》清理了 %d 个失效链接", t["name"], len(gone))
            self.store.save()
        return removed

    # ------------------------------------------------------------ 4 完结自动停止
    def auto_finish(self):
        if not self.cfg().get("auto_finish") or not self._due("finish", 6 * 3600):
            return []
        tm = self.e.tmdb()
        if not tm.ready:
            return []
        done = []
        for t in list(self.store.data["tasks"]):
            s = t.get("snap") or {}
            if (t.get("kind") != "subscription" or t.get("media") == "movie" or t.get("done") or not t.get("tmdb_id")
                    or s.get("movie") or s.get("missing_n") != 0 or not s.get("total")
                    or s.get("aired") != s.get("total") or s.get("have", 0) < s["total"]):
                continue
            try:
                d = tm.detail(t["tmdb_id"])
            except Exception as ex:
                log.warning("《%s》完结检查：TMDB 查询失败：%s", t["name"], ex)
                continue
            later = any(x["season_number"] > t["season"] for x in d.get("seasons", []))
            if d.get("status") not in ("Ended", "Canceled") and not later:
                continue   # 还在播、本季又是最新一季：TMDB 可能还会补集，先不停
            with self.store.lock:
                t["done"] = True
                t["last_msg"] = f"第 {t['season']} 季已收齐，已自动停止追更"
                self.store.save()
            done.append(t["name"])
            log.info("🏁《%s》第 %d 季已收齐，自动停止追更", t["name"], t["season"])
        if done:
            self._dingtalk("追更完结", "### 🏁 已自动停止追更\n" + "\n".join(f"- {n}" for n in done), "on_save")
        return done

    # ------------------------------------------------------------ 9 Cookie / 容量提醒
    def check_account(self):
        c = self.cfg()
        if not c.get("account_alert") or not self.store.data.get("cookie") or not self._due("account", 6 * 3600):
            return
        q = self.e.quark()
        if not q.init():
            if self._due("alert_cookie", 24 * 3600):
                log.error("夸克 Cookie 已失效，请重新登录")
                self._dingtalk("夸克 Cookie 已失效", "### ❌ 夸克 Cookie 已失效\n追更已无法转存，请到「账号」页重新填写 Cookie。")
            return
        try:
            g = q.growth_info() or {}
        except Exception:
            return
        total, used = g.get("total_capacity"), g.get("use_capacity")
        if isinstance(total, (int, float)) and isinstance(used, (int, float)) and total > 0:
            left = total - used
            if left < 5 * GB and self._due("alert_space", 24 * 3600):
                self.alert_space(f"夸克网盘仅剩 {left / GB:.1f} GB")

    def alert_space(self, msg):
        log.error(msg)
        self._dingtalk("夸克网盘空间不足", f"### ⚠️ {msg}\n空间用完后追更会转存失败，请清理网盘。")

    # ------------------------------------------------------------ 2 洗版提示
    def current_res(self, task):
        q = self.e.quark()
        found = q.get_fids([self.e._dest(task)])
        best = 0
        for i in (q.ls_dir(found[0]["fid"]) if found else []):
            if not i["dir"] and is_video(i["file_name"]):
                best = max(best, RES_RANK.get(quality_tags(i["file_name"])["res"], 0))
        return best

    def check_upgrades(self, tid=None):
        """已经收齐、且开了「洗版提醒」的订阅：搜一下有没有比现有画质更高的资源，只提示，不自动替换。"""
        ps = self.e.pansou()
        if not ps:
            return []
        out = []
        for t in list(self.store.data["tasks"]):
            if tid and t["id"] != tid:
                continue
            if not t.get("upgrade_watch") or t.get("kind") != "subscription":
                continue
            s = t.get("snap") or {}
            complete = t.get("done") if t.get("media") == "movie" else (s.get("missing_n") == 0 and s.get("have", 0) > 0)
            if not complete or t["id"] in self.e.pending:
                continue
            ts = (t.get("upgrade") or {}).get("ts", 0)
            if not tid and time.time() - ts < 24 * 3600:
                continue
            hint = self._upgrade_one(t, ps)
            out.append((t["name"], hint))
        return out

    def _upgrade_one(self, t, ps):
        e, S = self.e, t.get("season", 1)
        try:
            cur = self.current_res(t)
            if cur >= 3:
                hint = {"ts": int(time.time()), "cur": cur, "items": [], "msg": "已经是 4K，无需洗版"}
            else:
                names = e._search_names(t)
                results = e._search_by_names(ps, names, [[n] + ([f"{n} 第{S}季"] if S > 1 else []) for n, _ in names],
                                             {l["pwd_id"] for l in t["links"]}, lambda *a: None)
                items = []
                for it in results:
                    if not e._match_name(names, it["title"]) or e._other_version(t, names, it["title"]):
                        continue
                    if t.get("media") != "movie":
                        from .parser import title_seasons
                        ment = title_seasons(it["title"], e._match_name(names, it["title"]))
                        if ment and S not in ment:
                            continue
                    r = title_res_rank(it["title"])
                    if r > cur:
                        items.append({"title": it["title"][:80], "url": it["url"], "res": r})
                items.sort(key=lambda x: -x["res"])
                items = items[:3]
                hint = {"ts": int(time.time()), "cur": cur, "items": items,
                        "msg": (f"现有 {RES_NAME[cur]}，发现 {len(items)} 个更高画质资源" if items else f"现有 {RES_NAME[cur]}，暂无更高画质")}
        except Exception as ex:
            log.warning("《%s》洗版检查失败：%s", t["name"], ex)
            hint = {"ts": int(time.time()), "cur": 0, "items": [], "msg": f"检查失败：{ex}"}
        old = {i["url"] for i in (t.get("upgrade") or {}).get("items", [])}
        with self.store.lock:
            t["upgrade"] = hint
            self.store.save()
        fresh = [i for i in hint["items"] if i["url"] not in old]
        if fresh:
            log.info("⬆《%s》%s", t["name"], hint["msg"])
            self._dingtalk(f"《{t['name']}》有更高画质", f"### ⬆️《{t['name']}》{hint['msg']}\n"
                           + "\n".join(f"- [{RES_NAME[i['res']]}] {i['title']}\n  {i['url']}" for i in fresh), "on_add")
        return hint

    # ------------------------------------------------------------ 5 每日日报
    def daily_digest(self, force=False):
        c = self.cfg()
        today = date.today().isoformat()
        if not force:
            if not c.get("digest") or datetime.now().hour < int(c.get("digest_hour", 20)):
                return None
            if self.store.data["maint_state"].get("digest_date") == today:
                return None
        text = self.build_digest()
        with self.store.lock:
            self.store.data["maint_state"]["digest_date"] = today
            self.store.save()
        dt = self.store.data["dingtalk"]
        ok, msg = notify.send_dingtalk(dt, "追剧日报", text, force=force)
        if not ok:
            log.warning("日报发送失败：%s", msg)
        return text if ok else None

    def build_digest(self):
        e, tm = self.e, self.e.tmdb()
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.store.lock:
            rec = [r for r in self.store.data["recent"] if r["ts"] >= midnight]
            tasks = list(self.store.data["tasks"])
        lines = [f"### 📺 追剧日报 {date.today().isoformat()}"]
        if rec:
            lines.append("**今天入库**")
            lines += [f"- 《{r['name']}》{r.get('eps') or str(r['count']) + ' 个文件'}" for r in rec[:15]]
        else:
            lines.append("**今天入库**：没有新的转存")
        airing, today = [], date.today().isoformat()
        for t in tasks:
            if (t.get("kind") != "subscription" or t.get("media") == "movie" or not t.get("enabled", True)
                    or t.get("done") or not t.get("tmdb_id") or not tm.ready):
                continue
            try:
                for ep in tm.season_episodes(t["tmdb_id"], t["season"]):
                    if ep["air_date"] == today:
                        have = ep["ep"] not in ((t.get("snap") or {}).get("missing") or [])
                        airing.append(f"- 《{t['name']}》第 {ep['ep']} 集{'（已入库）' if have else '（待转存）'}")
            except Exception:
                continue
        lines.append("\n**今天播出**" + ("" if airing else "：没有"))
        lines += airing[:15]
        miss = [t for t in tasks if (t.get("snap") or {}).get("missing_n") and t.get("enabled", True) and not t.get("done")]
        if miss:
            lines.append("\n**还缺集的订阅**")
            lines += [f"- 《{t['name']}》缺 {fmt_eps([(t.get('season', 1), e) for e in t['snap']['missing']])}" for t in miss[:10]]
        bad = [t["name"] for t in tasks if t.get("last_status") == "error" or (t["links"] and all(l.get("ban") for l in t["links"]))]
        if bad:
            lines.append("\n**需要处理**：" + "、".join(f"《{n}》" for n in bad[:10]))
        return "\n".join(lines)
