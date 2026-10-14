# -*- coding: utf-8 -*-
"""媒体库管理：只管用户手动指定的「Emby 媒体库 + 文件夹 ↔ 夸克目录」范围，删除时夸克和 Emby 一起删。"""
import os
import re
import secrets
import shutil
import time

from . import emby as emby_mod
from .fnmedia import FnMedia
from .logbuf import log
from .parser import norm_title
from .quark import QuarkError

KINDS = ("tv", "movie", "anime", "variety")


def _p(path):
    p = re.sub(r"/{2,}", "/", (path or "").replace("\\", "/").strip())
    return p.rstrip("/") if len(p) > 1 else p


def _stem(path):
    name = _p(path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


_VEXT = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".rmvb", ".iso", ".strm")
_SE = re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})")


def _base(path):
    """去掉 .strm / .mkv 等扩展名（可以叠加，如 xxx.mkv.strm），得到可以互相比较的名字。"""
    n = _p(path).rsplit("/", 1)[-1]
    while n.lower().endswith(_VEXT):
        n = n[:n.rfind(".")]
    return n


def _se(path):
    m = _SE.search(_p(path).rsplit("/", 1)[-1])
    return (int(m.group(1)), int(m.group(2))) if m else None


def _dir(path):
    return _p(path).rsplit("/", 1)[0] if "/" in _p(path) else ""


def _safe_rel(rel):
    parts = [x for x in _p(rel).split("/") if x]
    if any(x in (".", "..") for x in parts):
        raise ValueError("路径不合法")
    return "/".join(parts)


class Library:
    def __init__(self, engine):
        self.e = engine
        self.store = engine.store
        from .fnlib import FnLib
        self.fnl = FnLib(self)

    # ---------------- 范围配置 ----------------
    def scopes(self):
        with self.store.lock:
            return [dict(s) for s in self.store.data["library"]["scopes"]]

    def save_scopes(self, items):
        out = []
        for it in items or []:
            lib_id = str(it.get("lib_id") or "").strip()
            emby_path, quark_path = _p(it.get("emby_path")), _p(it.get("quark_path"))
            server = "fn" if it.get("server") == "fn" else "emby"
            if not lib_id:
                raise ValueError("请选择飞牛媒体库" if server == "fn" else "请选择 Emby 媒体库")
            if server == "emby" and (len(emby_path) < 2 or not emby_path.startswith("/")):
                raise ValueError("请填写 Emby 里的文件夹路径（不能是根目录）")
            if len(quark_path) < 2 or not quark_path.startswith("/"):
                raise ValueError("请选择对应的夸克目录（不能是网盘根目录）")
            kind = it.get("kind") if it.get("kind") in KINDS else "tv"
            out.append({
                "id": it.get("id") or secrets.token_hex(4), "kind": kind, "server": server,
                "name": (it.get("name") or "").strip() or quark_path,
                "lib_id": lib_id, "lib_name": (it.get("lib_name") or "").strip(),
                "emby_path": emby_path, "quark_path": quark_path,
                "strm_path": _p(it.get("strm_path")),   # 可选：Emby 那个文件夹挂载进本工具容器后的路径，用来直接删 strm 文件
            })
        with self.store.lock:
            self.store.data["library"]["scopes"] = out
            self.store.save()
        return out

    def scope(self, sid):
        s = next((x for x in self.scopes() if x["id"] == sid), None)
        if not s:
            raise KeyError("范围不存在，请先在「范围设置」里添加")
        return s

    def _emby(self):
        cli = self.e.emby_only()
        if not cli:
            raise ValueError("还没有启用 Emby，请先到「通知联动 → Emby」填写并开启")
        return cli

    def libraries(self, server="emby"):
        if server == "fn":
            return self.fnl.cli().libraries()
        return self._emby().libraries()

    # ---------------- 列表 ----------------
    def _locate(self, scope, path, series=False):
        """Emby 条目路径 → 相对 emby_path 的路径；不在范围内返回 None。
        剧集：整个相对路径（emby_path 填得比较高、中间隔着「国产剧」这类分类文件夹时，也只会指到这一部剧自己的文件夹）；
        电影：第一级（电影文件夹或单个文件）。"""
        base, pth = scope["emby_path"], _p(path)
        if not pth.startswith(base + "/"):
            return None
        rel = pth[len(base) + 1:]
        return rel if series else rel.split("/")[0]

    def list_items(self, sid):
        scope = self.scope(sid)
        if scope.get("server") == "fn":
            return self.fnl.list_items(scope)
        cli = self._emby()
        out = []
        for it in cli.library_items(scope["lib_id"]):
            top = self._locate(scope, it.get("Path"), it.get("Type") == "Series")
            if not top:
                continue   # 不在你指定的文件夹里（比如迅雷云盘的）→ 完全不显示
            v = it.get("ProviderIds") or {}
            out.append({
                "emby_id": it["Id"], "name": it.get("Name", top), "year": it.get("ProductionYear"),
                "type": it.get("Type"), "top": top, "added": (it.get("DateCreated") or "")[:10], "overview": it.get("Overview") or "",
                "tmdb": v.get("Tmdb") or v.get("tmdb"),
            })
        out.sort(key=lambda x: x["added"], reverse=True)
        return {"scope": scope, "items": out}

    def _item_in_scope(self, scope, emby_id):
        it = self._emby().item(emby_id)
        if not it:
            raise KeyError("Emby 里找不到这个条目，可能已被删除，请刷新列表")
        top = self._locate(scope, it.get("Path"), it.get("Type") == "Series")
        if not top:
            raise ValueError("这个条目不在你指定的范围内，已拒绝操作")
        return it, top

    # ---------------- 详情（夸克里实际有什么） ----------------
    def _tree(self, q, fid, depth=0):
        rows = []
        for f in q.ls_dir(fid):
            node = {"name": f.get("file_name", ""), "dir": bool(f.get("dir")), "size": f.get("size") or 0}
            if node["dir"] and depth < 2:
                node["children"] = self._tree(q, f["fid"], depth + 1)
                node["size"] = sum(c["size"] for c in node["children"])
            rows.append(node)
        rows.sort(key=lambda n: (not n["dir"], n["name"]))
        return rows

    def detail(self, sid, emby_id):
        scope = self.scope(sid)
        if scope.get("server") == "fn":
            return self.fnl.detail(scope, emby_id)
        it, top = self._item_in_scope(scope, emby_id)
        qpath = f"{scope['quark_path']}/{top}"
        q = self.e.quark()
        found = q.get_fids([qpath])
        created = (it.get("DateCreated") or "").replace("T", " ")[:19]
        res = {"emby_id": emby_id, "name": it.get("Name"), "year": it.get("ProductionYear"), "type": it.get("Type"),
               "added_at": created, "overview": it.get("Overview") or "", "quark_path": qpath, "in_quark": bool(found), "tree": []}
        if found:
            if found[0].get("dir", True):
                res["tree"] = self._tree(q, found[0]["fid"])
            else:
                res["tree"] = [{"name": top, "dir": False, "size": found[0].get("size") or 0}]
                res["file_item"] = True
        return res

    # ---------------- 删除 ----------------
    def delete(self, sid, emby_id, rels=None, stop_sub=True):
        scope = self.scope(sid)
        if scope.get("server") == "fn":
            return self.fnl.delete(scope, emby_id, rels, stop_sub)
        it, top = self._item_in_scope(scope, emby_id)
        qroot, eroot = scope["quark_path"], scope["emby_path"]
        qitem, eitem = f"{qroot}/{top}", f"{eroot}/{top}"
        rels = [r for r in (_safe_rel(x) for x in (rels or [])) if r]
        q = self.e.quark()
        if rels and self._covers_all(q, qitem, rels):   # 把里面的内容都选上了 = 要删整部（连文件夹、Emby 里的剧集一起删）
            rels = []
        whole = not rels
        qpaths = [qitem] if whole else [f"{qitem}/{r}" for r in rels]
        for qp in qpaths:                      # 双保险：必须在范围内、不能是范围目录本身
            if not qp.startswith(qroot + "/") or qp == qroot:
                raise ValueError("要删除的夸克路径不在指定范围内，已拒绝")
        report = {"quark": [], "emby": "", "subs": 0, "whole": whole}
        fids = self._resolve(q, qpaths)
        if fids:
            q.delete(fids)
        elif not whole:
            raise ValueError("在夸克里没找到要删除的文件，没有做任何删除。尝试的路径：" + "、".join(qpaths))
        report["quark"] = [f"已删除 {len(fids)} 项"] if fids else ["夸克里没找到这个文件夹（可能已删除）"]
        log.info("媒体库管理：删除夸克 %s", qpaths)
        report["emby"] = self._sync_emby(it, whole, [f"{eitem}/{r}" for r in rels])
        report["local"] = self._clean_local(scope, top, rels)
        report["subs"] = self._handle_subs(qitem, whole, stop_sub)
        emby_mod._ids_cache.clear()
        pv = it.get("ProviderIds") or {}
        info = {"tmdb": pv.get("Tmdb") or pv.get("tmdb"), "name": it.get("Name"), "year": it.get("ProductionYear"),
                "movie": it.get("Type") == "Movie"}
        report["peers"] = self._peers(scope, info, whole, rels)
        return report

    # ---------------- 联动另一个媒体服务器 ----------------
    @staticmethod
    def _match(info, tmdb, name, year, movie):
        """另一个服务器里的条目是不是同一部：优先 TMDB ID，其次片名 + 年份（相差不超过 1 年）。"""
        if bool(movie) != bool(info["movie"]):
            return False
        if info.get("tmdb") and tmdb and str(tmdb).isdigit():
            return int(tmdb) == int(info["tmdb"])
        if not norm_title(name) or norm_title(name) != norm_title(info.get("name")):
            return False
        try:
            return abs(int(year) - int(info["year"])) <= 1 if year and info.get("year") else True
        except (TypeError, ValueError):
            return True

    def _peers(self, scope, info, whole, rels):
        """夸克目录相同（或包含）的、另一个服务器的范围：一起删。返回要显示的提示列表。"""
        out = []
        mine, sv = _p(scope["quark_path"]), scope.get("server", "emby")
        for p in self.scopes():
            if p["id"] == scope["id"] or p.get("server", "emby") == sv:
                continue
            other = _p(p["quark_path"])
            nested = other != mine and (other.startswith(mine + "/") or mine.startswith(other + "/"))
            if other != mine and not nested:
                continue
            label = ("飞牛" if p.get("server") == "fn" else "Emby") + f"（{p['name']}）"
            if nested and not whole:
                out.append(f"{label}：夸克目录层级不同，单集 / 整季删除不联动，请到它那边处理")
                continue
            try:
                msg = self._peer_fn(p, info, whole, rels) if p.get("server") == "fn" else self._peer_emby(p, info, whole, rels)
            except Exception as e:
                log.warning("联动 %s 失败：%s", label, e)
                msg = f"同步失败：{e}"
            out.append(f"{label}：{msg}")
        return out

    def _peer_emby(self, p, info, whole, rels):
        cli = self._emby()
        found = []
        for it in cli.library_items(p["lib_id"]):
            top = self._locate(p, it.get("Path"), it.get("Type") == "Series")
            if not top:
                continue
            v = it.get("ProviderIds") or {}
            if self._match(info, v.get("Tmdb") or v.get("tmdb"), it.get("Name"), it.get("ProductionYear"), it.get("Type") == "Movie"):
                found.append((it, top))
        if len(found) != 1:
            return "没匹配到同一部（或匹配到多个，已跳过）"
        it, top = found[0]
        eitem = f"{p['emby_path']}/{top}"
        msg = self._sync_emby(it, whole, [f"{eitem}/{r}" for r in rels])
        loc = self._clean_local(p, top, rels)
        emby_mod._ids_cache.clear()
        return msg + (f"；{loc}" if loc else "")

    def _peer_fn(self, p, info, whole, rels):
        cli = self.fnl.cli()
        items = cli.library_items(p["lib_id"])
        mp = self.fnl._map(p, items)
        found = [it for it in items if it["guid"] in mp and self._match(
            info, FnMedia.tmdb_of(it), it.get("title"), str(it.get("release_date") or "")[:4], it.get("type") == "Movie")]
        if len(found) != 1:
            return "没匹配到同一部（或匹配到多个，已跳过）"
        it = found[0]
        msg = self.fnl._sync(cli, p, it, it["guid"], whole, rels)
        from . import fnmedia
        fnmedia._ids_cache.clear()
        return msg

    def _clean_local(self, scope, top, rels):
        """strm 文件直接在磁盘上删（需要把 strm 目录挂载进本容器，并在范围设置里填挂载后的路径）。"""
        root = scope.get("strm_path")
        if not root:
            return ""
        real_root = os.path.realpath(root)
        base = os.path.join(root, top)
        targets = [base] if not rels else [os.path.join(base, r) for r in rels]
        n, errs = 0, []
        for t in targets:
            rt = os.path.realpath(t)
            if not rt.startswith(real_root + os.sep) or rt == real_root:
                errs.append("路径越界已跳过")
                continue
            try:
                if os.path.isdir(rt):
                    shutil.rmtree(rt)
                    n += 1
                else:   # 夸克里是 xxx.mkv，本地是同名的 xxx.strm / xxx.nfo / xxx-thumb.jpg
                    stem, d = _base(rt), os.path.dirname(rt)
                    if os.path.isdir(d):
                        for f in os.listdir(d):
                            if f == os.path.basename(rt) or _base(f) == stem or os.path.splitext(_base(f))[0] == stem or f.startswith(stem + "-"):
                                os.remove(os.path.join(d, f))
                                n += 1
            except OSError as e:
                errs.append(str(e))
        return f"已删除本地 strm 相关文件 {n} 个" + (f"；失败：{'；'.join(errs)}" if errs else "")

    def _resolve(self, q, qpaths):
        """路径 → 夸克 fid。文件不直接按路径查（有的账号查不到），而是列出它所在的文件夹，按名字找。"""
        fids, parents = [], {}
        for qp in qpaths:
            parents.setdefault(_dir(qp), []).append(qp)
        pf = {}
        for d in parents:
            if d:
                got = q.get_fids([d])
                if got:
                    pf[d] = got[0]["fid"]
        for d, paths in parents.items():
            listing = None
            for qp in paths:
                name = qp.rsplit("/", 1)[-1]
                if d in pf:
                    if listing is None:
                        listing = {f.get("file_name"): f["fid"] for f in q.ls_dir(pf[d])}
                    if name in listing:
                        fids.append(listing[name])
                        continue
                got = q.get_fids([qp])          # 兜底：直接按路径查
                if got:
                    fids.append(got[0]["fid"])
        return list(dict.fromkeys(fids))

    def _sync_emby(self, it, whole, epaths):
        cli = self._emby()
        try:
            if whole:
                cli.delete_item(it["Id"])
                return "已从 Emby 删除"
            gone = 0
            kids = cli.children(it["Id"], "Episode,Season")
            dels = [_p(x) for x in epaths]
            kids.sort(key=lambda k: 0 if k.get("Type") == "Episode" else 1)
            for k in kids:
                kp = _p(k.get("Path"))
                if not kp:
                    continue
                if any(self._same(k, kp, d) for d in dels):
                    cli.delete_item(k["Id"])
                    gone += 1
            if gone:
                try:
                    cli.refresh(it["Id"])
                except Exception:
                    pass
            return f"已从 Emby 删除 {gone} 项" if gone else "Emby 里没有对应条目（已触发刷新）"
        except Exception as e:
            log.warning("Emby 删除失败，退回刷新: %s", e)
            try:
                cli.refresh(it["Id"])
                return f"Emby 删除没成功（{e}），已改为触发刷新，稍后会自动清掉；请检查 Emby 账号是否有删除权限"
            except Exception as e2:
                return f"Emby 同步失败：{e2}"

    def _covers_all(self, q, qitem, rels):
        """选中的这些项，是否已经包含了这部片子里所有的文件。"""
        got = q.get_fids([qitem])
        if not got or not got[0].get("dir", True):
            return False

        def leaves(nodes, pre=""):
            for n in nodes:
                rel = f"{pre}/{n['name']}" if pre else n["name"]
                if n["dir"] and "children" in n:
                    yield from leaves(n["children"], rel)
                else:
                    yield rel
        return all(any(r == x or r.startswith(x + "/") for x in rels) for r in leaves(self._tree(q, got[0]["fid"])))

    @staticmethod
    def _same(k, kp, d):
        """Emby 条目（路径 kp）是不是被删掉的 d（夸克文件 / 文件夹对应的路径）。"""
        if kp == d or kp.startswith(d + "/"):
            return True
        if _base(kp) == _base(d) and _base(d):          # 同名，只是扩展名不同（.strm / .mkv / .mkv.strm）
            return True
        se = _se(d)                                      # 兜底：季集号相同，且在同名文件夹下
        if se and k.get("Type") == "Episode" and (k.get("ParentIndexNumber"), k.get("IndexNumber")) == se:
            return _p(_dir(kp)).rsplit("/", 1)[-1] == _p(_dir(d)).rsplit("/", 1)[-1]
        return False

    def _handle_subs(self, qitem, whole, stop):
        if not stop:
            return 0
        ids = []
        with self.store.lock:
            for t in self.store.data["tasks"]:
                sp = _p(t.get("savepath"))
                if sp == qitem or sp.startswith(qitem + "/"):
                    ids.append(t["id"])
            if not ids:
                return 0
            if whole:
                pass
            else:
                for t in self.store.data["tasks"]:
                    if t["id"] in ids:
                        t["enabled"] = False
                self.store.save()
        if whole:
            self.e.delete_tasks(ids)
        return len(ids)
