# -*- coding: utf-8 -*-
"""
镜像同步：把别人的分享链接当作「标准答案」，让我的网盘目录和它保持一致。

  分享里新增的文件  → 转存进来
  分享里删掉的文件  → 把我这边对应的文件删掉（夸克会放进回收站）
  分享里被替换的文件（同路径、大小变了）→ 删旧的、转新的

删除是危险操作，所以：
  · 只管「清单」里的文件（本任务转存进来的），目录里别的文件一律不碰；
  · 只要有一个链接读不出来（失效 / 网络错误 / 目录没读全 / 分享被清空），本轮就一个都不删；
  · 一个文件要连续 N 次都不在分享里才删（宽限）；
  · 一次要删太多，或者几乎全没了，不自动删，放进「待确认」让人点一下；
  · 默认是预览模式：只列出会删什么，不真删。
"""
import random
import time

from .logbuf import log
from .quark import QuarkError

DEFAULTS = {"enabled": False, "dry_run": True, "grace": 2, "max_delete": 10, "max_pct": 30, "replace": True}
MAX_SAVE_PER_RUN = 500       # 首次同步文件很多时，分几轮转完
MAX_DEPTH, MAX_DIRS = 8, 400  # 读分享目录树的深度 / 子目录个数上限，超了就当作「没读全」，不删东西


def clean_cfg(raw, old=None):
    base = {**DEFAULTS, **(old or {})}
    raw = raw if isinstance(raw, dict) else {}

    def num(k, lo, hi):
        try:
            return max(lo, min(hi, int(raw.get(k, base[k]))))
        except (TypeError, ValueError):
            return base[k]
    return {"enabled": bool(raw.get("enabled", base["enabled"])), "dry_run": bool(raw.get("dry_run", base["dry_run"])),
            "grace": num("grace", 1, 10), "max_delete": num("max_delete", 1, 1000), "max_pct": num("max_pct", 1, 100),
            "replace": bool(raw.get("replace", base["replace"]))}


def _norm(p):
    import re
    return re.sub(r"/{2,}", "/", "/" + (p or "").strip().strip("/"))


def _parent(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _base(rel):
    return rel.rsplit("/", 1)[-1]


class Mirror:
    def __init__(self, engine):
        self.e = engine

    # ------------------------------------------------------------ 状态
    @staticmethod
    def state(task):
        st = task.setdefault("mstate", {})
        st.setdefault("manifest", {})   # 相对路径 -> {size, link, ts}
        st.setdefault("missing", {})    # 相对路径 -> 连续几次不在分享里
        st.setdefault("pending", [])    # 待确认 / 预览出来的删除
        st.setdefault("go", False)      # 首次同步前的「目录对不上」检查是否已放行
        st.setdefault("hold", "")
        st.setdefault("last", {})
        return st

    def info(self, task):
        """给界面看的概要（不含整张清单）。"""
        cfg = clean_cfg(task.get("mirror"))
        st = task.get("mstate") or {}
        return {"enabled": cfg["enabled"], "managed": len(st.get("manifest") or {}), "pending": list(st.get("pending") or [])[:200],
                "pending_n": len(st.get("pending") or []), "hold": st.get("hold", ""), "last": st.get("last") or {}}

    # ------------------------------------------------------------ 读分享的完整目录树
    def scan(self, q, link):
        """-> (stoken, {相对路径: 文件信息})。任何一处没读成功都抛异常，绝不返回「不完整」的结果。
        文件夹结构原样保留，不像普通监控那样剥掉最外层的单个文件夹：对账靠相对路径，分享里多出一个顶层项就会让所有路径前缀变掉。"""
        from .engine import LinkError
        stoken = self.e._get_stoken(q, link)
        pwd_id, _, pdir_fid, _ = q.extract_url(link["url"])
        try:
            top = q.get_detail(pwd_id, stoken, pdir_fid)
            if not top:
                raise LinkError("分享为空，文件已被分享者删除", permanent=True)
        except QuarkError as ex:
            raise LinkError(str(ex))
        files, budget = {}, [MAX_DIRS]

        def walk(items, rel, depth):
            for f in items:
                name = f["file_name"]
                if not f["dir"]:
                    files["/".join(rel + (name,))] = {"fid": f["fid"], "token": f.get("share_fid_token", ""), "size": int(f.get("size") or 0),
                                                      "pwd_id": pwd_id, "stoken": stoken, "name": name}
                    continue
                if depth >= MAX_DEPTH or budget[0] <= 0:
                    raise QuarkError("分享的目录层级太深或文件夹太多，没法完整读取")
                budget[0] -= 1
                walk(q.get_detail(pwd_id, stoken, f["fid"]), rel + (name,), depth + 1)
        walk(top, (), 0)
        return stoken, files

    # ------------------------------------------------------------ 我这边目录的列表（带缓存）
    @staticmethod
    def _dirmap(q, dest, rel_dir, cache):
        if rel_dir not in cache:
            found = q.get_fids([dest + ("/" + rel_dir if rel_dir else "")])
            cache[rel_dir] = ({i["file_name"]: i for i in q.ls_dir(found[0]["fid"])}, found[0]["fid"]) if found else (None, None)
        return cache[rel_dir]

    # ------------------------------------------------------------ 分析（只读，不改任何东西）
    def analyze(self, task, q):
        cfg, st = clean_cfg(task.get("mirror")), self.state(task)
        dest = _norm(task["savepath"])
        present, scanned_ok, link_errors, other_errors = {}, {}, [], []
        for link in task["links"]:
            pid = link.get("pwd_id") or q.extract_url(link["url"])[0]
            if link.get("ban"):
                scanned_ok[pid] = False
                other_errors.append(f"链接已标记失效：{link['ban']}")
                continue
            try:
                _, files = self.scan(q, link)
                scanned_ok[pid] = True
                for rel, f in files.items():
                    present.setdefault(rel, f)
            except Exception as ex:   # LinkError / QuarkError
                from .engine import LinkError
                scanned_ok[pid] = False
                if isinstance(ex, LinkError):
                    link_errors.append((link, ex))
                elif isinstance(ex, QuarkError):
                    other_errors.append(str(ex))
                else:
                    raise
        man = st["manifest"]
        all_ok = bool(task["links"]) and all(scanned_ok.values()) and not link_errors and not other_errors
        pids = {l.get("pwd_id") or q.extract_url(l["url"])[0] for l in task["links"]}
        new, adopt, conflict, changed = [], [], [], []
        cache = {}
        for rel, f in present.items():
            m = man.get(rel)
            if m is None:
                names, _ = self._dirmap(q, dest, _parent(rel), cache)
                ex = (names or {}).get(f["name"])
                if ex and not ex.get("dir"):
                    (adopt if ex.get("size") == f["size"] else conflict).append(rel)
                else:
                    new.append(rel)
            elif cfg["replace"] and m.get("size") and f["size"] and m["size"] != f["size"]:
                changed.append(rel)
            elif m.get("link") != f["pwd_id"]:
                m["link"] = f["pwd_id"]
        removed = [r for r, m in man.items() if r not in present and m.get("link") in pids]
        orphan = [r for r, m in man.items() if m.get("link") not in pids]
        hold = ""
        if not man and not st.get("go") and present:   # 首次同步：目录里已有一堆对不上的文件（多半是以前被改过名），先别转，免得重复
            names_by_dir = {}
            for rel in present:
                names_by_dir.setdefault(_parent(rel), set()).add(_base(rel))
            others = sum(1 for d, (names, _) in cache.items() if names for n, i in names.items() if not i.get("dir") and n not in names_by_dir.get(d, ()))
            if others and len(adopt) < len(present) / 2:
                hold = (f"目录里已有 {others} 个文件和分享对不上（可能以前被重命名过），为避免重复转存，先暂停。"
                        f"确认要照样转存，请点「继续」；否则请换一个空目录。")
        return {"cfg": cfg, "dest": dest, "present": present, "all_ok": all_ok, "link_errors": link_errors, "errors": other_errors,
                "new": new, "adopt": adopt, "conflict": conflict, "changed": changed, "removed": removed, "orphan": orphan, "hold": hold}

    # ------------------------------------------------------------ 试运行
    def preview(self, task):
        q = self.e.quark()
        st = self.state(task)
        an = self.analyze(task, q)
        man, cfg = st["manifest"], an["cfg"]
        rows = ([{"rel": r, "size": an["present"][r]["size"], "action": "save", "why": "分享里新增"} for r in an["new"]] +
                [{"rel": r, "size": an["present"][r]["size"], "action": "replace", "why": "分享里这个文件变了（大小不同），会删旧的转新的"} for r in an["changed"]] +
                [{"rel": r, "size": an["present"][r]["size"], "action": "adopt", "why": "目录里已有同名同大小的文件，纳入管理，不重复转存"} for r in an["adopt"]] +
                [{"rel": r, "size": an["present"][r]["size"], "action": "skip", "why": "目录里有同名但大小不同的文件，当作你自己的，不动"} for r in an["conflict"]])
        total = len(man)
        for r in an["removed"]:
            n = st["missing"].get(r, 0) + 1
            rows.append({"rel": r, "size": man[r].get("size", 0), "action": "delete",
                         "why": (f"分享里已经没有了（连续第 {n} 次，满 {cfg['grace']} 次才会删）" if an["all_ok"] else "分享里没有，但有链接读取失败，本轮不会删")})
        return {"rows": rows, "ok": an["all_ok"], "managed": total, "hold": an["hold"], "cfg": cfg,
                "errors": [str(e) for _, e in an["link_errors"]] + an["errors"]}

    # ------------------------------------------------------------ 删除我这边的文件
    def _delete(self, q, task, rels, man):
        """删掉 rels 对应的文件。只删「名字和大小都对得上」的：你自己改过 / 换过的文件不动。
        -> (已删除, 本来就不在了, 被改动过所以没删)"""
        dest = _norm(task["savepath"])
        by_dir, deleted, gone, kept = {}, [], [], []
        for r in rels:
            by_dir.setdefault(_parent(r), []).append(r)
        for d, rs in by_dir.items():
            found = q.get_fids([dest + ("/" + d if d else "")])
            if not found:
                gone += rs
                continue
            items = {i["file_name"]: i for i in q.ls_dir(found[0]["fid"])}
            todo = []
            for r in rs:
                it = items.get(_base(r))
                if not it or it.get("dir"):
                    gone.append(r)
                    continue
                ms = (man.get(r) or {}).get("size")
                if ms and it.get("size") and int(it["size"]) != int(ms):
                    kept.append(r)
                    continue
                todo.append((r, it["fid"]))
            for i in range(0, len(todo), 50):
                chunk = todo[i:i + 50]
                q.delete([fid for _, fid in chunk])
                deleted += [r for r, _ in chunk]
        # 删完之后留下的空文件夹一并清掉（只清我们刚动过的那几层，根目录不动）
        for d in sorted(set(by_dir), key=lambda x: -x.count("/")):
            cur = d
            while cur:
                try:
                    found = q.get_fids([dest + "/" + cur])
                    if not found or q.ls_dir(found[0]["fid"]):
                        break
                    q.delete([found[0]["fid"]])
                except QuarkError as ex:
                    log.warning("清理空文件夹 %s 失败：%s", cur, ex)
                    break
                cur = _parent(cur)
        return deleted, gone, kept

    # ------------------------------------------------------------ 转存新文件
    def _save(self, q, task, rels, an, man, errors):
        dest, present, saved = an["dest"], an["present"], []
        by_dir = {}
        for r in rels[:MAX_SAVE_PER_RUN]:
            by_dir.setdefault(_parent(r), []).append(r)
        if len(rels) > MAX_SAVE_PER_RUN:
            log.info("《%s》本轮最多转存 %d 个，其余 %d 个留到下一轮", task["name"], MAX_SAVE_PER_RUN, len(rels) - MAX_SAVE_PER_RUN)
        for d, rs in sorted(by_dir.items()):
            try:
                fid = q.ensure_dir(dest + ("/" + d if d else ""))
                groups = {}
                for r in rs:
                    f = present[r]
                    groups.setdefault((f["pwd_id"], f["stoken"]), []).append(r)
                for (pwd, stoken), grp in groups.items():
                    for i in range(0, len(grp), 50):
                        chunk = grp[i:i + 50]
                        tid = q.save_file([present[r]["fid"] for r in chunk], [present[r]["token"] for r in chunk], fid, pwd, stoken)
                        q.wait_task(tid)
                names = {x["file_name"]: x for x in q.ls_dir(fid)}
                for r in rs:
                    if _base(r) in names:
                        man[r] = {"size": present[r]["size"], "link": present[r]["pwd_id"], "ts": int(time.time())}
                        saved.append({"name": r, "eps": [], "link": ""})
                    else:
                        log.warning("《%s》转存后没在目录里找到 %s，下一轮重试", task["name"], r)
            except QuarkError as ex:
                log.error("《%s》转存 %s 失败：%s", task["name"], d or "根目录", ex)
                errors.append(str(ex))
                if "容量" in str(ex) or "空间" in str(ex):
                    break
            time.sleep(random.uniform(0.3, 0.9))
        return saved

    # ------------------------------------------------------------ 正式运行
    def run(self, task, manual):
        e = self.e
        q = e.quark()
        if not task["links"]:
            return {"status": "idle", "msg": "还没有分享链接", "saved": []}
        st = self.state(task)
        dest = _norm(task["savepath"])
        q.ensure_dir(dest)
        an = self.analyze(task, q)
        cfg, man = an["cfg"], st["manifest"]
        errors, banned = list(an["errors"]), []
        for link in task["links"]:   # 读成功的链接清零失败计数
            if not link.get("ban") and not any(l is link for l, _ in an["link_errors"]):
                link["fails"] = 0
        for link, ex in an["link_errors"]:   # 和普通监控一样：网络问题只跳过，链接本身的问题累计后标记失效
            if ex.network:
                log.warning("《%s》%s，跳过", task["name"], ex)
                errors.append(str(ex))
                continue
            link["fails"] = link.get("fails", 0) + 1
            log.warning("《%s》%s（连续失败 %d 次）", task["name"], ex, link["fails"])
            if ex.permanent or link["fails"] >= 3:
                link["ban"], link["ban_ts"] = str(ex), int(time.time())
                banned.append(f"{link['url']}：{ex}")
            errors.append(str(ex))
        for r in an["orphan"]:   # 链接已从任务里去掉：不再管这些文件，也不删它们
            man.pop(r, None)
            st["missing"].pop(r, None)
        st["hold"] = an["hold"]
        notes, saved, removed = [], [], []
        managed_before = len(man)
        if an["hold"]:
            notes.append("首次同步已暂停（目录里有对不上的文件）")
            if st.get("hold_notified") != an["hold"]:
                st["hold_notified"] = an["hold"]
                e._notify_error(task, "镜像同步已暂停", an["hold"])
        else:
            # 纳入管理（目录里已有同名同大小的文件）
            for r in an["adopt"]:
                man[r] = {"size": an["present"][r]["size"], "link": an["present"][r]["pwd_id"], "ts": int(time.time())}
            # 被替换的：先删旧的
            if an["changed"]:
                if an["all_ok"]:
                    d_ok, gone, kept = self._delete(q, task, an["changed"], man)
                    for r in kept:
                        man.pop(r, None)
                    redo = [r for r in an["changed"] if r not in kept]
                    for r in d_ok + gone:
                        man.pop(r, None)
                    if kept:
                        notes.append(f"{len(kept)} 个文件被你改动过，不替换")
                    removed += [{"name": r, "why": "被替换"} for r in d_ok]
                    an["new"] += [r for r in redo if r not in an["new"]]
                else:
                    notes.append("有链接读取失败，本轮不替换文件")
            if an["conflict"]:
                notes.append(f"{len(an['conflict'])} 个同名但大小不同的文件当作你自己的，没动")
            saved = self._save(q, task, an["new"], an, man, errors)
            if len(an["new"]) > MAX_SAVE_PER_RUN:
                notes.append(f"还有 {len(an['new']) - MAX_SAVE_PER_RUN} 个留到下一轮")
            # 分享里删掉的 → 宽限计数 → 删除 / 待确认
            ready = []
            if an["all_ok"]:
                gone_set = set(an["removed"])
                for r in gone_set:
                    st["missing"][r] = st["missing"].get(r, 0) + 1
                for r in list(st["missing"]):
                    if r not in gone_set:
                        del st["missing"][r]
                ready = [r for r in an["removed"] if st["missing"][r] >= cfg["grace"]]
                waiting = len(an["removed"]) - len(ready)
                if waiting:
                    notes.append(f"{waiting} 个文件不在分享里，再观察 {cfg['grace'] - 1} 次以上才会删")
                total = managed_before
                reason = ""
                if len(ready) >= 3 and len(ready) >= total * 0.7:
                    reason = "分享里的文件大部分都没了，不自动删"
                elif len(ready) > cfg["max_delete"]:
                    reason = f"一次要删 {len(ready)} 个，超过上限 {cfg['max_delete']} 个"
                elif total >= 10 and len(ready) * 100 / total > cfg["max_pct"]:
                    reason = f"要删的占已管理文件的 {round(len(ready) * 100 / total)}%，超过 {cfg['max_pct']}%"
                elif cfg["dry_run"] and ready:
                    reason = "预览模式（还没开启真删）"
                if ready and reason:
                    st["pending"] = [{"rel": r, "size": man[r].get("size", 0), "reason": reason} for r in ready]
                    notes.append(f"{len(ready)} 个待删除：{reason}")
                    sig = "|".join(sorted(ready))
                    if not cfg["dry_run"] and st.get("pending_notified") != sig:
                        st["pending_notified"] = sig
                        e._notify_error(task, "镜像同步有待确认的删除", f"{reason}\n\n" + "\n".join(f"- {r}" for r in ready[:30]) + "\n\n请到监控页确认或忽略。")
                elif ready:
                    d_ok, gone, kept = self._delete(q, task, ready, man)
                    for r in d_ok + gone + kept:
                        man.pop(r, None)
                        st["missing"].pop(r, None)
                    removed += [{"name": r, "why": "分享里已删除"} for r in d_ok]
                    st["pending"] = []
                    if kept:
                        notes.append(f"{len(kept)} 个文件被你改动过，没删，已不再管理")
                else:
                    st["pending"] = []
            else:
                notes.append("有链接读取失败，本轮不删除任何文件")
        st["last"] = {"ts": int(time.time()), "new": len(saved), "removed": len(removed), "pending": len(st["pending"]), "note": "；".join(notes)}
        with e.store.lock:
            e.store.save()
        if banned:
            e._notify_error(task, "分享链接失效", "\n".join(banned))
        if saved or removed:
            e._after_save(task, saved, [x["name"] for x in removed])
        bits = ([f"转存 {len(saved)} 个"] if saved else []) + ([f"删除 {len(removed)} 个"] if removed else []) + notes
        if saved or removed:
            return {"status": "saved", "saved": saved, "removed": removed, "msg": "，".join(bits)}
        if errors:
            return {"status": "error", "saved": [], "removed": [], "msg": errors[0] + ("；" + "；".join(notes) if notes else "")}
        return {"status": "nochange", "saved": [], "removed": [], "msg": "，".join(bits) or "已和分享保持一致"}

    # ------------------------------------------------------------ 界面上的「确认 / 忽略 / 继续」
    def resolve(self, task, action, rels=None):
        """confirm：把待删除的真删掉；ignore：不删，也不再管这些文件；go：放行首次同步。"""
        e = self.e
        st = self.state(task)
        if action == "go":
            st["go"], st["hold"] = True, ""
            with e.store.lock:
                e.store.save()
            return {"ok": True}
        pend = {p["rel"]: p for p in st["pending"]}
        pick = [r for r in (rels or list(pend)) if r in pend]
        if not pick:
            return {"ok": True, "n": 0}
        man = st["manifest"]
        deleted = []
        if action == "confirm":
            q = e.quark()
            d_ok, gone, kept = self._delete(q, task, pick, man)
            deleted = d_ok
            for r in d_ok + gone + kept:
                man.pop(r, None)
        else:
            for r in pick:
                man.pop(r, None)
        for r in pick:
            st["missing"].pop(r, None)
        st["pending"] = [p for p in st["pending"] if p["rel"] not in pick]
        st["pending_notified"] = ""
        st["last"] = {**st.get("last", {}), "pending": len(st["pending"]), "note": "" if not st["pending"] else st.get("last", {}).get("note", "")}
        with e.store.lock:
            e.store.save()
        if deleted:
            e._after_save(task, [], deleted)
            log.info("《%s》已按你的确认删除 %d 个文件", task["name"], len(deleted))
        return {"ok": True, "n": len(pick), "deleted": len(deleted)}
