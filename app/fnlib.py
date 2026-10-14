# -*- coding: utf-8 -*-
"""媒体库管理 · 飞牛影视版。

飞牛的条目接口里没有文件路径，没法像 Emby 那样「按路径筛出夸克的内容」。所以这里换个办法：
范围 = 飞牛的一个媒体库 + 一个夸克目录；只显示「能在这个夸克目录里找到对应文件夹/文件」的条目
（先按订阅任务里记的 TMDB ID / 保存路径对应，对不上再按名字对应）——迅雷网盘的内容在夸克里找不到，自然不显示。"""
import time

from .fnmedia import FnMedia
from .library import _VEXT, _base, _p, _se, _safe_rel
from .logbuf import log
from .parser import norm_title, parse_episodes, title_seasons
import re


def _ts(v):
    """飞牛的时间可能是毫秒/秒时间戳，也可能是字符串。"""
    if v in (None, ""):
        return ""
    try:
        n = float(v)
        if n > 1e11:
            n /= 1000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n))
    except (TypeError, ValueError):
        return str(v).replace("T", " ")[:19]


def _clean(name):
    n = _base(name)
    n = re.sub(r"[\(\[（【]\s*(?:19|20)\d{2}\s*[\)\]）】]|[\[\{]\s*tmdb(?:id)?[=-]\s*\d+\s*[\]\}]", " ", n, flags=re.I)
    return norm_title(n)


def _year_of(name):
    m = re.search(r"(?:19|20)\d{2}", name or "")
    return int(m.group(0)) if m else None


class FnLib:
    def __init__(self, lib):
        self.lib, self.e, self.store = lib, lib.e, lib.store

    def cli(self):
        c = self.e.fn()
        if not c:
            raise ValueError("还没有启用飞牛影视，请先到「通知联动 → 飞牛影视」填写并开启")
        return c

    # ---------------- 条目 ↔ 夸克文件夹 ----------------
    def _quark_tops(self, scope):
        q = self.e.quark()
        got = q.get_fids([scope["quark_path"]])
        if not got:
            raise ValueError(f"夸克里找不到目录 {scope['quark_path']}")
        return {f.get("file_name", ""): f for f in q.ls_dir(got[0]["fid"])}

    def _sub_tops(self, scope):
        root, out = scope["quark_path"], {}
        for t in self.store.data["tasks"]:
            sp = _p(t.get("savepath"))
            if t.get("tmdb_id") and sp.startswith(root + "/"):
                out[(t.get("media", "tv") == "movie" and "movie" or "tv", int(t["tmdb_id"]))] = sp[len(root) + 1:].split("/")[0]
        return out

    def _map(self, scope, items):
        """{飞牛 guid: 夸克里的第一级名称}；对不上的不在结果里。"""
        tops = self._quark_tops(scope)
        subs = self._sub_tops(scope)
        names = [(n, _clean(n), _year_of(n)) for n in tops]
        res = {}
        for it in items:
            media = "movie" if it.get("type") == "Movie" else "tv"
            tm = FnMedia.tmdb_of(it)
            top = subs.get((media, tm)) if tm else None
            if top not in tops:
                top = None
            if not top:
                t = norm_title(it.get("title") or it.get("tv_title") or "")
                y = _year_of(str(it.get("release_date") or ""))
                if not t:
                    continue
                exact = [n for n, c, ny in names if c == t and not (y and ny and abs(y - ny) > 1)]
                if len(exact) == 1:
                    top = exact[0]
                elif not exact:
                    part = [n for n, c, ny in names if c and min(len(c), len(t)) >= 3 and (t in c or c in t)
                            and not (y and ny and abs(y - ny) > 1)]
                    if len(part) == 1:
                        top = part[0]
            if top:
                res[it["guid"]] = top
        return res

    def _item(self, cli, scope, guid):
        it = cli.item(guid)
        if not it:
            raise KeyError("飞牛里找不到这个条目，可能已被删除，请刷新列表")
        if it.get("type") not in ("Movie", "TV"):
            raise ValueError("只支持整部剧集 / 电影")
        top = self._map(scope, [dict(it, guid=guid)]).get(guid)
        if not top:
            raise ValueError("这个条目在指定的夸克目录里找不到对应文件夹，已拒绝操作")
        return it, top

    # ---------------- 列表 / 详情 ----------------
    def list_items(self, scope):
        cli = self.cli()
        items = cli.library_items(scope["lib_id"])
        mp = self._map(scope, items)
        out = []
        for it in items:
            top = mp.get(it["guid"])
            if not top:
                continue
            out.append({
                "emby_id": it["guid"], "name": it.get("title") or top, "year": str(it.get("release_date") or "")[:4] or None,
                "type": "Movie" if it.get("type") == "Movie" else "Series", "top": top,
                "added": _ts(it.get("create_time") or it.get("update_time"))[:10], "overview": it.get("overview") or "",
                "tmdb": FnMedia.tmdb_of(it), "server": "fn",
            })
        out.sort(key=lambda x: x["added"], reverse=True)
        return {"scope": scope, "items": out}

    def detail(self, scope, guid):
        cli = self.cli()
        it, top = self._item(cli, scope, guid)
        qpath = f"{scope['quark_path']}/{top}"
        q = self.e.quark()
        found = q.get_fids([qpath])
        res = {"emby_id": guid, "name": it.get("title"), "year": str(it.get("release_date") or "")[:4] or None,
               "type": "Movie" if it.get("type") == "Movie" else "Series",
               "added_at": _ts(it.get("create_time") or it.get("update_time")), "overview": it.get("overview") or "",
               "quark_path": qpath, "in_quark": bool(found), "tree": [], "server": "fn"}
        if found:
            if found[0].get("dir", True):
                res["tree"] = self.lib._tree(q, found[0]["fid"])
            else:
                res["tree"] = [{"name": top, "dir": False, "size": found[0].get("size") or 0}]
                res["file_item"] = True
        return res

    # ---------------- 删除 ----------------
    def delete(self, scope, guid, rels=None, stop_sub=True):
        lib = self.lib
        cli = self.cli()
        it, top = self._item(cli, scope, guid)
        qroot = scope["quark_path"]
        qitem = f"{qroot}/{top}"
        rels = [r for r in (_safe_rel(x) for x in (rels or [])) if r]
        q = self.e.quark()
        if rels and lib._covers_all(q, qitem, rels):
            rels = []
        whole = not rels
        qpaths = [qitem] if whole else [f"{qitem}/{r}" for r in rels]
        for qp in qpaths:
            if not qp.startswith(qroot + "/") or qp == qroot:
                raise ValueError("要删除的夸克路径不在指定范围内，已拒绝")
        report = {"quark": [], "emby": "", "subs": 0, "whole": whole, "server": "fn"}
        fids = lib._resolve(q, qpaths)
        if fids:
            q.delete(fids)
        elif not whole:
            raise ValueError("在夸克里没找到要删除的文件，没有做任何删除。尝试的路径：" + "、".join(qpaths))
        report["quark"] = [f"已删除 {len(fids)} 项"] if fids else ["夸克里没找到这个文件夹（可能已删除）"]
        log.info("媒体库管理(飞牛)：删除夸克 %s", qpaths)
        report["emby"] = self._sync(cli, scope, it, guid, whole, rels)
        report["local"] = ""
        report["subs"] = lib._handle_subs(qitem, whole, stop_sub)
        from . import fnmedia
        fnmedia._ids_cache.clear()
        return report

    @staticmethod
    def _targets(rels, default_season=1):
        """夸克里被删的路径 → ({(季,集)}, {整季})。"""
        eps, seasons = set(), set()
        for r in rels:
            parts = r.split("/")
            season = default_season
            for seg in parts[:-1]:
                sn = title_seasons(seg)
                if sn:
                    season = sorted(sn)[0]
            name = parts[-1]
            if name.lower().endswith(_VEXT):
                se = _se(name)
                found = [se] if se else parse_episodes(name, season)
                eps.update(found)
            else:
                seasons.update(title_seasons(name))
        return eps, seasons

    def _sync(self, cli, scope, it, guid, whole, rels):
        try:
            if whole:
                cli.delete_item(guid, True)
                return "已从飞牛删除（含 strm 文件）"
            if it.get("type") == "Movie":
                cli.delete_item(guid, True)
                return "已从飞牛删除（含 strm 文件）"
            eps, seasons = self._targets(rels)
            gone = 0
            for sn in cli.seasons(guid):
                n = sn.get("season_number")
                if n in seasons:
                    cli.delete_item(sn["guid"], True)
                    gone += 1
                    continue
                for ep in cli.season_episodes(sn["guid"]):
                    s = ep.get("season_number", n)
                    if (s, ep.get("episode_number")) in eps:
                        cli.delete_item(ep["guid"], True)
                        gone += 1
            try:
                cli.scan_library(scope["lib_id"])
            except Exception:
                pass
            return f"已从飞牛删除 {gone} 项" if gone else "飞牛里没有对应集（已触发扫描）"
        except Exception as e:
            log.warning("飞牛删除失败，退回扫描: %s", e)
            try:
                cli.scan_library(scope["lib_id"])
                return f"飞牛删除没成功（{e}），已改为触发扫描，稍后会自动清掉；请检查飞牛账号是否有删除权限"
            except Exception as e2:
                return f"飞牛同步失败：{e2}"
