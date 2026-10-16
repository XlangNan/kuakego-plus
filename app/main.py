# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote

import requests
from flask import Flask, abort, jsonify, redirect, request, send_from_directory, Response

from . import hosts, netproxy, notify, platforms
from .emby import Emby
from .fnmedia import FnError, FnMedia
from .engine import Engine, Scheduler
from .logbuf import log, ring, setup as setup_log
from .quark import Quark, QuarkError
from .pansou import PanSou, PanSouError
from .parser import parse_share_text
from .store import CONFIG_DIR, DEFAULTS, Store, _merge
from .tmdb import Tmdb

VERSION = "1.18.0"
# 图标不含敏感信息，浏览器取 favicon 时不一定带登录凭据，所以不要求认证
PUBLIC_ASSETS = {"/favicon.svg", "/favicon.ico", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"}
SECTIONS = ("dingtalk", "tmdb", "emby", "fnmedia", "proxy", "smartstrm", "pansou", "auto", "platforms")


def create_app(store=None, engine=None):
    setup_log(CONFIG_DIR)
    store = store or Store()
    engine = engine or Engine(store)
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    netproxy.configure(store.data.get("proxy"))

    user = os.environ.get("WEBUI_USERNAME", "admin")
    password = os.environ.get("WEBUI_PASSWORD") or ""
    if not password:
        password = secrets.token_urlsafe(9)
        log.warning("未设置 WEBUI_PASSWORD，本次启动的随机密码：%s（用户名 %s）", password, user)

    acct = {"ts": 0, "data": {"logged_in": False, "nickname": ""}}

    # ------------------------------------------------------------ 鉴权
    # 浏览器：登录页 + 签名 Cookie；脚本 / 反向代理：仍然支持 Basic 认证
    sess_key = hashlib.sha256(("kq|" + user + "|" + password).encode()).digest()   # 改了密码，旧的登录态自动失效
    COOKIE = "kq_session"
    fails = {}   # ip -> [次数, 解锁时间]

    def _sign(exp):
        return hmac.new(sess_key, f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()[:40]

    def _cookie_ok():
        v = request.cookies.get(COOKIE, "")
        exp, _, sig = v.partition(".")
        return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))

    def _basic_ok():
        # 浏览器会把用过的 Basic 凭证缓存起来、自动带上；点了「退出登录」后要忽略它，否则刷新又登录回去了
        if request.cookies.get("kq_out") and "Mozilla" in request.headers.get("User-Agent", ""):
            return False
        a = request.authorization
        return bool(a) and hmac.compare_digest(a.username or "", user) and hmac.compare_digest(a.password or "", password)

    def _authed():
        return _cookie_ok() or _basic_ok()

    @app.before_request
    def _auth():
        if request.path in ("/healthz", "/login", "/api/login", "/api/logout") or request.path in PUBLIC_ASSETS:
            return None
        if _authed():
            return None
        browser = "Mozilla" in request.headers.get("User-Agent", "")
        if request.path.startswith("/api/"):
            h = {} if browser else {"WWW-Authenticate": 'Basic realm="kuakego-plus"'}   # 浏览器里不再弹系统登录框
            return Response(json.dumps({"error": "需要登录"}, ensure_ascii=False), 401, h, mimetype="application/json")
        if browser:
            return redirect("/login" + ("" if request.path == "/" else "?next=" + quote(request.full_path.rstrip("?"))))
        return Response("需要登录", 401, {"WWW-Authenticate": 'Basic realm="kuakego-plus"'})

    @app.get("/login")
    def login_page():
        if _authed():
            return redirect("/")
        r = send_from_directory(os.path.join(os.path.dirname(__file__), "static"), "login.html", max_age=0)
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/api/login")
    def login_info():
        return jsonify(version=VERSION)

    @app.post("/api/login")
    def login_do():
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
        n, until = fails.get(ip, [0, 0])
        if until > time.time():
            return jsonify(error=f"尝试次数过多，请 {int(until - time.time()) + 1} 秒后再试"), 429
        d = request.get_json(silent=True) or {}
        if hmac.compare_digest(str(d.get("username", "")), user) and hmac.compare_digest(str(d.get("password", "")), password):
            fails.pop(ip, None)
            days = 30 if d.get("remember") else 0
            exp = int(time.time() + (days or 1) * 86400)
            resp = jsonify(ok=True)
            resp.delete_cookie("kq_out")
            resp.set_cookie(COOKIE, f"{exp}.{_sign(exp)}", max_age=days * 86400 or None, httponly=True, samesite="Lax",
                            secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
            return resp
        n += 1
        fails[ip] = [n, time.time() + min(300, 5 * 2 ** (n - 5)) if n >= 5 else 0]
        log.warning("登录失败（%s），第 %s 次", ip, n)
        return jsonify(error="用户名或密码不对"), 401

    @app.post("/api/logout")
    def logout_do():
        resp = jsonify(ok=True)
        resp.delete_cookie(COOKIE)
        resp.set_cookie("kq_out", "1", max_age=30 * 86400, httponly=True, samesite="Lax")
        return resp

    def body():
        data = request.get_json(silent=True)  # 严格要求 application/json，天然防跨站表单伪造
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON")
        return data

    @app.errorhandler(ValueError)
    def _bad(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(QuarkError)
    def _quark(e):
        return jsonify(error=str(e)), 502

    @app.errorhandler(KeyError)
    def _nf(e):
        return jsonify(error=str(e).strip("'\"")), 404

    @app.errorhandler(FnError)
    def _fn(e):
        return jsonify(error=f"飞牛影视：{e}"), 502

    @app.errorhandler(Exception)
    def _any(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        log.exception("请求出错 %s", request.path)
        return jsonify(error=f"{type(e).__name__}: {e}"), 500

    @app.get("/healthz")
    def healthz():
        return "ok"

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    for _path in PUBLIC_ASSETS:
        app.add_url_rule(
            _path, endpoint="asset_" + _path.strip("/").replace(".", "_").replace("-", "_"),
            view_func=lambda _n=_path.strip("/"): send_from_directory(static_dir, _n, max_age=86400))

    @app.get("/api/overview")
    def overview():
        return jsonify(engine.overview())

    @app.get("/")
    def index():
        r = send_from_directory(os.path.join(os.path.dirname(__file__), "static"), "index.html", max_age=0)
        r.headers["Cache-Control"] = "no-store"      # 不缓存、不进「后退/前进缓存」：退出登录后刷新或后退都会重新校验。升级后浏览器必须重新校验首页，不然普通刷新还是旧界面
        return r

    # ------------------------------------------------------------ 账号
    def account_state(force=False):
        if not force and time.time() - acct["ts"] < 60:
            return acct["data"]
        data = {"logged_in": False, "nickname": ""}
        if store.data["cookie"]:
            try:
                q = Quark(store.data["cookie"])
                info = q.init()
                if info:
                    data = {"logged_in": True, "nickname": q.nickname, "can_sign": bool(q.mparam)}
                else:
                    data["error"] = "Cookie 已失效，请重新登录"
            except Exception as e:
                data["error"] = str(e)
        acct.update(ts=time.time(), data=data)
        return data

    @app.get("/api/status")
    def status():
        s = account_state()
        pc = store.data["pansou"]
        return jsonify(**s, version=VERSION, running=engine.running, pansou=bool(pc["enabled"] and pc["url"]))

    @app.get("/api/account")
    def account_get():
        s = dict(account_state(True))
        s["auto_sign"] = store.data["auto_sign"]
        if s.get("logged_in") and s.get("can_sign"):
            try:
                g = Quark(store.data["cookie"]).growth_info()
                if g:
                    s["sign"] = {
                        "signed": bool(g.get("cap_sign", {}).get("sign_daily")),
                        "progress": g.get("cap_sign", {}).get("sign_progress"),
                        "target": g.get("cap_sign", {}).get("sign_target"),
                    }
            except Exception:
                pass
        return jsonify(s)

    @app.post("/api/account")
    def account_set():
        cookie = (body().get("cookie") or "").strip()
        if not cookie:
            raise ValueError("Cookie 不能为空")
        q = Quark(cookie)
        if not q.init():
            raise ValueError("Cookie 无效或已过期，请重新从浏览器复制")
        with store.lock:
            store.data["cookie"] = cookie
            store.save()
        acct["ts"] = 0
        return jsonify(ok=True, nickname=q.nickname, can_sign=bool(q.mparam))

    @app.put("/api/account")
    def account_prefs():
        with store.lock:
            store.data["auto_sign"] = bool(body().get("auto_sign"))
            store.save()
        return jsonify(ok=True)

    @app.delete("/api/account")
    def account_logout():
        with store.lock:
            store.data["cookie"] = ""
            store.save()
        acct["ts"] = 0
        return jsonify(ok=True)

    @app.post("/api/account/sign")
    def account_sign():
        ok, msg = engine.quark().growth_sign()
        if ok:
            return jsonify(ok=True, msg=f"签到成功，获得 {msg / 1024 / 1024:.0f} MB")
        return jsonify(ok=False, msg=str(msg)), 400

    # ------------------------------------------------------------ 目录浏览
    @app.get("/api/browse")
    def browse():
        fid = request.args.get("fid", "0")
        items = engine.quark().ls_dir(fid)
        dirs = [{"fid": i["fid"], "name": i["file_name"]} for i in items if i["dir"]]
        dirs.sort(key=lambda d: d["name"])
        return jsonify(dirs=dirs)

    @app.post("/api/browse/mkdir")
    def browse_mkdir():
        path = body().get("path", "").strip()
        if not path.strip("/"):
            raise ValueError("目录名不能为空")
        fid = engine.quark().ensure_dir(path)
        return jsonify(ok=True, fid=fid)

    # ------------------------------------------------------------ 任务
    @app.post("/api/parse")
    def parse():
        return jsonify(links=parse_share_text(body().get("text", "")))

    @app.get("/api/tasks")
    def tasks_list():
        kind = request.args.get("kind")
        with store.lock:
            items = [engine.public_task(t) for t in store.data["tasks"]
                     if not kind or t.get("kind") == kind]
        return jsonify(tasks=items)

    @app.post("/api/tasks")
    def tasks_create():
        data = body()
        if not parse_share_text(data.get("links_text", "")) and data.get("kind") != "subscription":
            raise ValueError("没有识别到夸克分享链接（形如 https://pan.quark.cn/s/xxxx）")
        task = engine.create_task(data, save_existing=data.get("save_existing", True))
        if task["links"] and data.get("save_existing", True):
            threading.Thread(target=engine.run_task, args=(task["id"], True), daemon=True).start()
        return jsonify(task=engine.public_task(task))

    @app.post("/api/tasks/batch-delete")
    def tasks_batch_delete():
        ids = body().get("ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("请先选择要删除的订阅")
        return jsonify(ok=True, deleted=engine.delete_tasks([str(i) for i in ids]))

    @app.post("/api/tasks/batch-update")
    def tasks_batch_update():
        d = body()
        ids = d.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("请先勾选要修改的任务")
        ok, bad = engine.batch_update([str(i) for i in ids], d.get("changes") or {})
        return jsonify(ok=True, updated=ok, failed=[{"name": n, "error": e} for n, e in bad])

    @app.put("/api/tasks/<tid>")
    def tasks_update(tid):
        return jsonify(task=engine.public_task(engine.update_task(tid, body())))

    @app.delete("/api/tasks/<tid>")
    def tasks_delete(tid):
        if not engine.delete_task(tid):
            raise KeyError("任务不存在")
        return jsonify(ok=True)

    @app.post("/api/tasks/<tid>/run")
    def tasks_run(tid):
        if not engine.find(tid):
            raise KeyError("任务不存在")
        if tid in engine.pending:
            return jsonify(ok=False, msg="该任务正在运行或排队中"), 409
        threading.Thread(target=engine.run_task, args=(tid, True), daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/tasks/<tid>/find")
    def tasks_find(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        if t.get("kind") != "subscription":
            raise ValueError("只有订阅追更支持自动找资源")
        if not engine.pansou():
            raise ValueError("资源搜索还没有启用：请先到「资源搜索」页配置 PanSou")
        if tid in engine.pending:
            return jsonify(ok=False, msg="该任务正在运行或排队中"), 409
        threading.Thread(target=engine.auto_find, args=(tid, True), daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/tasks/<tid>/toggle")
    def tasks_toggle(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        with store.lock:
            t["enabled"] = not t.get("enabled", True)
            store.save()
        return jsonify(enabled=t["enabled"])

    @app.post("/api/tasks/<tid>/reset-links")
    def tasks_reset(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        with store.lock:
            for l in t["links"]:
                l["ban"], l["fails"] = "", 0
            store.save()
        return jsonify(ok=True)

    @app.post("/api/tasks/<tid>/preview")
    def tasks_preview(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        if not t["links"]:
            raise ValueError("这个任务还没有分享链接")
        with engine.run_lock:  # 与正式扫描互斥，避免同时读写目录
            return jsonify(engine.preview(t))

    # 镜像同步：试运行（只看不动）/ 确认 / 忽略 / 放行首次同步
    @app.post("/api/tasks/<tid>/mirror/preview")
    def mirror_preview(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        if t.get("kind") != "monitor":
            raise ValueError("镜像同步只用于分享监控")
        with engine.run_lock:
            return jsonify(engine.mirror.preview(t))

    @app.get("/api/tasks/<tid>/mirror/log")
    def mirror_log(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        return jsonify(items=engine.mirror.log_view(t), managed=len((t.get("mstate") or {}).get("manifest") or {}))

    @app.post("/api/trash/clean")
    def trash_clean():
        days = int(store.data["auto"].get("trash_clean_days") or 0)
        if days <= 0:
            raise ValueError("先在上面设置「回收站放几天后清理」（大于 0）再点")
        with engine.run_lock:
            n = engine.mirror.clean_trash(days)
        return jsonify(ok=True, cleaned=n)

    @app.get("/api/trash/info")
    def trash_info():
        return jsonify(hint=engine.mirror.trash_hint())

    @app.post("/api/tasks/<tid>/mirror/resolve")
    def mirror_resolve(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        d = body()
        if d.get("action") not in ("confirm", "ignore", "go"):
            raise ValueError("未知操作")
        rels = d.get("rels")
        with engine.run_lock:
            r = engine.mirror.resolve(t, d["action"], [str(x) for x in rels] if isinstance(rels, list) else None)
        return jsonify(**r, task=engine.public_task(t))

    @app.get("/api/tasks/<tid>/progress")
    def tasks_progress(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        try:
            return jsonify(engine.progress(t))
        except QuarkError as e:
            return jsonify(error=str(e)), 502
        except Exception as e:
            return jsonify(error=f"读取进度失败：{e}"), 502

    @app.post("/api/import-legacy")
    def import_legacy():
        path = os.path.join(CONFIG_DIR, "quark_config.json")
        if not os.path.exists(path):
            raise ValueError("config 目录下没有 quark_config.json")
        return jsonify(imported=engine.import_legacy(path))

    # ------------------------------------------------------------ TMDB / 推荐 / 订阅
    def tmdb_or_400():
        t = Tmdb(store.data["tmdb"])
        if not t.ready:
            raise ValueError("请先到「TMDB 设置」填写 API Key")
        return t

    def tmdb_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            raise QuarkError(f"TMDB 请求失败：{e}")

    def annotate(items, media="tv"):
        """给 TMDB 条目加上：是否已订阅、订阅状态、Emby 是否已入库。剧集和电影的 TMDB ID 是两套编号，要分开比对。"""
        subs = {(x.get("media", "tv"), x["tmdb_id"]): x for x in store.data["tasks"] if x.get("tmdb_id")}
        emby_ids = set()
        cli = engine.emby()
        if cli:
            try:
                emby_ids = cli.tmdb_ids("Movie" if media == "movie" else "Series")
            except Exception:
                pass
        for it in items:
            t = subs.get((media, it["id"]))
            it["sub_id"] = t["id"] if t else None
            it["sub_status"] = engine.public_task(t)["status"] if t else ""
            it["in_emby"] = it["id"] in emby_ids
        return items

    def annotate_mixed(items):
        """动漫板块里剧集和电影混排：按各自的类型分别标记（两套 TMDB ID 不能混用）。"""
        for media in ("tv", "movie"):
            group = [x for x in items if x.get("media", "tv") == media]
            if group:
                annotate(group, media)
        return items

    def scope_arg():
        s = request.args.get("scope", "tv")
        return s if s in ("tv", "movie", "anime", "variety") else "tv"

    img_cache = {}

    @app.get("/api/calendar")
    def calendar():
        """追剧日历：[start, end] 日期范围内，订阅里每一集的播出日 / 电影的上映日，以及它们的入库状态。"""
        from concurrent.futures import ThreadPoolExecutor
        from datetime import date as _date
        start, end = request.args.get("start", ""), request.args.get("end", "")
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)) or end < start:
            raise ValueError("请给出 start / end（YYYY-MM-DD）")
        today = _date.today().isoformat()
        tm = Tmdb(store.data["tmdb"])
        tasks = [t for t in store.data["tasks"] if t.get("kind") == "subscription"]
        events = []

        def tv_status(t, ep, air):
            if air > today:
                return "unaired"
            snap = t.get("snap")
            if not snap:
                return "unknown"
            miss = snap.get("missing") or []
            if ep in miss or (snap.get("missing_n", 0) > len(miss) and miss and ep > max(miss)):
                return "missing"
            return "have"

        def one(t):
            rows = []
            base = {"task_id": t["id"], "name": t["name"], "poster": t.get("poster", ""), "media": t.get("media", "tv")}
            if t.get("media") == "movie":
                rd = t.get("release_date") or ""
                if start <= rd <= end:
                    st = "have" if t.get("done") else ("unaired" if rd > today else "missing")
                    rows.append({**base, "date": rd, "season": 0, "ep": 0, "ep_name": "", "status": st})
            elif t.get("tmdb_id") and tm.ready:
                try:
                    for e in tm.season_episodes(t["tmdb_id"], t["season"]):
                        if start <= e["air_date"] <= end:
                            rows.append({**base, "date": e["air_date"], "season": t["season"], "ep": e["ep"], "ep_name": e["name"],
                                         "status": tv_status(t, e["ep"], e["air_date"])})
                except Exception as ex:
                    log.warning("追剧日历：《%s》第 %s 季读取失败：%s", t["name"], t.get("season"), ex)
            return rows

        with ThreadPoolExecutor(8) as pool:
            for rows in pool.map(one, tasks):
                events += rows
        events.sort(key=lambda e: (e["date"], e["name"], e["ep"]))
        return jsonify(events=events, today=today, tmdb=tm.ready)

    @app.get("/api/tmdb/img/<size>/<name>")
    def tmdb_img(size, name):
        # 严格白名单，避免被当成任意地址的代理（SSRF）
        if not re.fullmatch(r"w\d{2,4}|h\d{2,4}|original", size) or \
                not re.fullmatch(r"[\w\-]+\.(jpg|jpeg|png|webp)", name, re.I):
            abort(404)
        key = (size, name)
        if key not in img_cache:
            try:
                r = netproxy.get(f"{Tmdb(store.data['tmdb']).img_root}/{size}/{name}", "tmdb", timeout=15)
            except Exception:
                abort(502)
            if r.status_code != 200:
                abort(404)
            if len(img_cache) >= 400:
                img_cache.pop(next(iter(img_cache)))
            img_cache[key] = (r.content, r.headers.get("Content-Type", "image/jpeg"))
        data, ctype = img_cache[key]
        return Response(data, mimetype=ctype, headers={"Cache-Control": "public, max-age=604800"})

    # ------------------------------------------------------------ 平台板块：腾讯 / 芒果 / 爱奇艺 / 哔哩哔哩 / Netflix …
    @app.get("/api/platforms")
    def platforms_list():
        return jsonify(platforms=platforms.catalog(), iqiyi_cookie_set=bool(store.data["platforms"].get("iqiyi_cookie")))

    def plat_call(fn, name, *a, **kw):
        try:
            return fn(*a, **kw)
        except KeyError:
            raise
        except Exception as e:
            raise QuarkError(f"{name} 请求失败：{e}")

    @app.get("/api/platforms/<pid>/filters")
    def platforms_filters(pid):
        ch = request.args.get("channel", "tv")
        return jsonify(groups=plat_call(platforms.filters, pid, pid, ch))

    @app.get("/api/platforms/<pid>/items")
    def platforms_items(pid):
        ch, page = request.args.get("channel", "tv"), int(request.args.get("page", 1))
        try:
            flt = json.loads(request.args.get("filters") or "{}")
        except ValueError:
            flt = {}
        d = plat_call(platforms.items, pid, pid, ch, flt if isinstance(flt, dict) else {}, page,
                      tmdb=Tmdb(store.data["tmdb"]), cfg=store.data["platforms"])
        if not d["native"]:
            annotate_mixed(d["items"])
        return jsonify(d)

    @app.get("/api/platforms/match")
    def platforms_match():
        tm = tmdb_or_400()
        r = platforms.match(tm, request.args.get("title", ""), request.args.get("year", ""), request.args.get("section", "tv"))
        return jsonify(match=r)

    # 影视推荐页的四个板块：scope = tv 剧集 / movie 电影 / anime 动漫（番剧 + 动画电影）/ variety 综艺（真人秀 + 脱口秀）
    @app.get("/api/tmdb/home")
    def tmdb_home():
        tm = tmdb_or_400()
        rows = {"tv": tm.home, "movie": tm.movie_home, "anime": tm.anime_home, "variety": tm.variety_home}[scope_arg()]()
        for r in rows:
            annotate_mixed(r["items"])
        return jsonify(rows=rows)

    @app.get("/api/tmdb/discover")
    def tmdb_discover():
        tm, scope = tmdb_or_400(), scope_arg()
        cat, sort, page = request.args.get("category", "all"), request.args.get("sort", "popularity"), int(request.args.get("page", 1))
        if scope == "movie":
            d = tmdb_call(tm.movie_discover, cat, page, sort)
        elif scope == "anime":
            d = (tmdb_call(tm.movie_discover, "all", page, sort, True) if cat == "movie"
                 else tmdb_call(tm.discover, cat, sort, page, True))
        elif scope == "variety":
            d = tmdb_call(tm.variety_discover, cat, sort, page)
        else:
            d = tmdb_call(tm.discover, cat, sort, page)
        annotate_mixed(d["items"])
        return jsonify(d)

    @app.get("/api/tmdb/search")
    def tmdb_search():
        q, tm, scope = request.args.get("q", "").strip(), tmdb_or_400(), scope_arg()
        if not q:
            return jsonify(items=[], total_pages=1)
        page = int(request.args.get("page", 1))
        d = tmdb_call({"tv": tm.search, "movie": tm.movie_search, "anime": tm.anime_search, "variety": tm.variety_search}[scope], q, page)
        annotate_mixed(d["items"])
        return jsonify(d)

    @app.get("/api/tmdb/tv/<int:tid>")
    def tmdb_tv(tid):
        d = tmdb_call(tmdb_or_400().detail, tid)
        annotate([d])
        # 每一季各自的订阅状态（同一部剧可以分季订阅，不能只看"有没有订阅"）
        d["subscribed_seasons"] = {
            str(x.get("season") or 1): x["id"] for x in store.data["tasks"]
            if x.get("media", "tv") == "tv" and x.get("tmdb_id") == tid and x.get("kind") == "subscription"}
        d["sub_id"] = d["sub_id"] if d["subscribed_seasons"] or d["sub_id"] else None
        cli = engine.emby()
        if cli and d["in_emby"]:
            try:
                sid, _ = cli.find_series(d["name"], tid)
                eps = cli.episodes(sid) if sid else set()
                for s in d["seasons"]:
                    s["emby_have"] = sum(1 for (sn, _) in eps if sn == s["season_number"])
            except Exception:
                pass
        return jsonify(d)

    @app.get("/api/tmdb/person-search")
    def tmdb_person_search():
        q, tm = request.args.get("q", "").strip(), tmdb_or_400()
        if not q:
            return jsonify(items=[])
        return jsonify(tmdb_call(tm.person_search, q))

    @app.get("/api/tmdb/person/<int:pid>")
    def tmdb_person(pid):
        d = tmdb_call(tmdb_or_400().person, pid)
        annotate_mixed(d["items"])
        return jsonify(d)

    @app.get("/api/tmdb/<media>/<int:tid>/similar")
    def tmdb_similar(media, tid):
        if media not in ("tv", "movie"):
            raise KeyError("未知类型")
        d = tmdb_call(tmdb_or_400().similar, media, tid)
        annotate(d["items"], media)
        return jsonify(d)

    @app.get("/api/tmdb/movie/discover")
    def movie_discover():
        d = tmdb_call(tmdb_or_400().movie_discover, request.args.get("category", "trending"), int(request.args.get("page", 1)))
        annotate(d["items"], "movie")
        return jsonify(d)

    @app.get("/api/tmdb/movie/search")
    def movie_search():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify(items=[], total_pages=1)
        d = tmdb_call(tmdb_or_400().movie_search, q, int(request.args.get("page", 1)))
        annotate(d["items"], "movie")
        return jsonify(d)

    @app.get("/api/tmdb/movie/<int:tid>")
    def movie_detail(tid):
        d = tmdb_call(tmdb_or_400().movie_detail, tid)
        annotate([d], "movie")
        return jsonify(d)

    @app.post("/api/subscriptions")
    def sub_create():
        data = body()
        media = "movie" if data.get("media") == "movie" else "tv"
        try:
            tid = int(data.get("tmdb_id") or 0)
            season = 1 if media == "movie" else int(data.get("season") or 1)
        except (TypeError, ValueError):
            raise ValueError("TMDB ID、季号必须是数字")
        if not tid:
            raise ValueError("请先从推荐或搜索里选择一部" + ("电影" if media == "movie" else "剧"))
        with store.lock:
            if any(t.get("media", "tv") == media and t.get("tmdb_id") == tid and (media == "movie" or t.get("season") == season)
                   for t in store.data["tasks"]):
                raise ValueError("这部电影已经订阅过了" if media == "movie" else "这一季已经订阅过了，请到订阅列表里添加分享链接")
        tm = tmdb_or_400()
        info = tmdb_call(tm.movie_detail if media == "movie" else tm.detail, tid)
        payload = {
            **data, "media": media, "kind": "subscription", "tmdb_id": tid, "season": season,
            "name": data.get("name") or info["name"], "poster": info["poster_url"],
            "overview": info["overview"],
        }
        payload["variety"] = bool(info.get("variety"))   # 综艺（真人秀 / 脱口秀）：目录和订阅分类里单独成类
        payload["anime"] = bool(info.get("anime"))   # 动画（番剧 / 动画电影）：目录推荐时和普通剧集、电影分开
        payload["aliases"] = info.get("aliases") or []   # 其他名称：主名搜不到资源时，自动找资源会换别名再搜
        payload["emby_layout"] = bool(data.get("emby_layout", False)) and media == "tv"
        payload["year"] = int(info.get("year") or 0)       # 剧集的年份用于「剧名 (年份) - S01E01」命名
        if media == "movie":
            payload["release_date"] = info.get("release_date") or ""
        if not (payload.get("savepath") or "").strip("/ "):
            raise ValueError("请选择转存目录")
        has_links = bool(parse_share_text(payload.get("links_text", "")))
        pc = store.data["pansou"]
        pansou_on = bool(pc["enabled"] and pc["url"])
        payload["auto_find"] = bool(data.get("auto_find", pansou_on)) and pansou_on   # 没配 PanSou 就没法自动找
        task = engine.create_task(payload, save_existing=data.get("save_existing", True))

        def kick():
            if task["auto_find"]:
                engine.auto_find(task["id"], force=True)   # 先搜资源，找到后内部会立刻扫描转存
            elif has_links and data.get("save_existing", True):
                engine.run_task(task["id"], True)
        threading.Thread(target=kick, daemon=True).start()
        return jsonify(task=engine.public_task(task))

    # ------------------------------------------------------------ 最近使用的目录
    @app.get("/api/recent-dirs")
    def recent_dirs_get():
        with store.lock:
            return jsonify(dirs=list(store.data["recent_dirs"]))

    @app.delete("/api/recent-dirs")
    def recent_dirs_clear():
        path = request.args.get("path")   # 不带 path = 全部清除
        with store.lock:
            store.data["recent_dirs"] = [d for d in store.data["recent_dirs"] if path and d["path"] != path]
            store.save()
        return jsonify(ok=True)

    # ------------------------------------------------------------ 维护 / 备份 / 批量
    # ------------------------------------------------------------ 媒体库管理
    @app.get("/api/library/scopes")
    def lib_scopes():
        return jsonify(scopes=engine.library.scopes())

    @app.put("/api/library/scopes")
    def lib_scopes_save():
        return jsonify(scopes=engine.library.save_scopes(body().get("scopes") or []))

    @app.get("/api/library/emby-libraries")
    def lib_emby_libs():
        return jsonify(libraries=engine.library.libraries(request.args.get("server", "emby")))

    @app.get("/api/library/items")
    def lib_items():
        return jsonify(engine.library.list_items(request.args.get("scope", "")))

    @app.get("/api/library/img/<iid>")
    def lib_img(iid):
        if not re.fullmatch(r"[\w\-]+", iid):
            abort(404)
        fn = request.args.get("s") == "fn"
        cli = engine.fn() if fn else engine.emby_only()
        if not cli:
            abort(404)
        key = ("lib", "fn" if fn else "emby", iid)
        if key not in img_cache:
            try:
                got = cli.image(iid)
            except Exception:
                abort(502)
            if not got:
                abort(404)
            if len(img_cache) >= 400:
                img_cache.pop(next(iter(img_cache)))
            img_cache[key] = got
        data, ctype = img_cache[key]
        return Response(data, mimetype=ctype, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/library/detail")
    def lib_detail():
        return jsonify(engine.library.detail(request.args.get("scope", ""), request.args.get("id", "")))

    @app.post("/api/library/delete")
    def lib_delete():
        d = body()
        return jsonify(ok=True, **engine.library.delete(
            d.get("scope", ""), d.get("id", ""), d.get("paths") or [], bool(d.get("stop_sub", True))))

    @app.get("/api/transfers")
    def transfers():
        """转存记录：每次有新文件入库的事件（最近 200 条），可按任务过滤。"""
        tid = request.args.get("task_id")
        with store.lock:
            items = [r for r in store.data["recent"] if not tid or r.get("task_id") == tid]
        return jsonify(items=items)

    @app.post("/api/tasks/<tid>/purge-links")
    def tasks_purge_links(tid):
        t = engine.find(tid)
        if not t:
            raise KeyError("任务不存在")
        with store.lock:
            n = len(t["links"])
            t["links"] = [l for l in t["links"] if not l.get("ban")]
            store.save()
        return jsonify(ok=True, removed=n - len(t["links"]))

    @app.post("/api/maintenance/purge-dead")
    def maint_purge_dead():
        return jsonify(ok=True, removed=engine.maint.purge_dead_links(force=True))

    @app.post("/api/tasks/<tid>/upgrade-check")
    def tasks_upgrade_check(tid):
        t = engine.find(tid)
        if not t or t.get("kind") != "subscription":
            raise KeyError("任务不存在")
        if not engine.pansou():
            raise ValueError("资源搜索还没有启用：请先到「通知联动 → 资源搜索」配置 PanSou")
        if not t.get("upgrade_watch"):
            raise ValueError("这个订阅没有开启「洗版提醒」")
        res = engine.maint.check_upgrades(tid)
        return jsonify(ok=True, upgrade=t.get("upgrade"), checked=bool(res))

    @app.post("/api/maintenance/digest")
    def maint_digest():
        if not store.data["dingtalk"].get("webhook"):
            raise ValueError("请先配置钉钉 Webhook")
        text = engine.maint.daily_digest(force=True)
        if not text:
            raise ValueError("发送失败，详见日志")
        return jsonify(ok=True, text=text)

    @app.get("/api/backup")
    def backup_get():
        import json as _json
        data = _json.dumps(store.data, ensure_ascii=False, indent=2)
        name = time.strftime("kuakego-backup-%Y%m%d-%H%M%S.json")
        return Response(data, mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    def _restore_data(data):
        """校验并整体替换当前配置（手动导入、从自动备份恢复共用）。恢复前自动留两份：config.json.bak 和 backups/ 里的 pre 备份。"""
        import shutil
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("这不是 kuakego-plus 的备份文件（缺少 tasks）")
        for t in data["tasks"]:
            if not isinstance(t, dict) or "id" not in t or not isinstance(t.get("links", []), list):
                raise ValueError("备份里的订阅数据格式不对")
        try:
            engine.backups.snapshot("pre")
        except OSError as e:
            log.warning("恢复前留备份失败（继续恢复）：%s", e)
        with store.lock:
            if os.path.exists(store.path):
                shutil.copyfile(store.path, store.path + ".bak")
            store.data = _merge(DEFAULTS, data)
            store.save()
        acct["ts"] = 0
        return len(store.data["tasks"])

    @app.post("/api/restore")
    def backup_restore():
        import json as _json
        try:
            data = _json.loads(request.get_data().decode("utf-8"))
        except Exception:
            raise ValueError("文件不是有效的 JSON 备份")
        return jsonify(ok=True, tasks=_restore_data(data))

    # 自动备份：每天一份，页面上可以看 / 下载 / 一键恢复
    @app.get("/api/backups")
    def backups_list():
        a = store.data["auto"]
        return jsonify(items=engine.backups.list(), auto=bool(a.get("backup_auto", True)), keep=a.get("backup_keep", 7))

    @app.post("/api/backups/now")
    def backups_now():
        return jsonify(ok=True, name=engine.backups.snapshot("manual"))

    @app.get("/api/backups/<name>/download")
    def backups_download(name):
        p = engine.backups.path(name)
        if not p:
            raise KeyError("备份不存在")
        with open(p, "rb") as f:
            return Response(f.read(), mimetype="application/json", headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.post("/api/backups/<name>/restore")
    def backups_restore(name):
        import json as _json
        p = engine.backups.path(name)
        if not p:
            raise KeyError("备份不存在")
        with open(p, encoding="utf-8") as f:
            data = _json.load(f)
        return jsonify(ok=True, tasks=_restore_data(data))

    @app.post("/api/batch/inspect")
    def batch_inspect():
        """批量粘贴：一次识别多个分享链接，逐个检测内容并猜名称，前端确认后再逐个创建。"""
        text = body().get("text", "")
        links = parse_share_text(text)
        if not links:
            raise ValueError("没有识别到夸克分享链接")
        if len(links) > 20:
            raise ValueError("一次最多 20 个链接")
        lines = text.splitlines()
        out = []
        for l in links:
            guess = ""
            for i, ln in enumerate(lines):
                if l["pwd_id"] in ln:
                    guess = re.sub(r"https?://\S+|提取码.*|pwd=\S+|[：:]\s*$", "", ln).strip(" -—|·\t")
                    if not guess and i:
                        guess = re.sub(r"https?://\S+", "", lines[i - 1]).strip(" -—|·\t")
                    break
            item = {"url": l["url"], "pwd_id": l["pwd_id"], "name": guess[:60], "ok": False, "error": ""}
            try:
                r = engine.inspect_share(l["url"], 1)
                item.update(ok=bool(r.get("ok")), error=r.get("error", ""), videos=r.get("videos", 0), eps=r.get("eps", ""),
                            quality=r.get("quality", []), size_gb=r.get("size_gb", 0), sample=r.get("sample", []))
            except QuarkError as e:
                item["error"] = str(e)
            out.append(item)
        return jsonify(items=out)

    # ------------------------------------------------------------ 资源搜索（PanSou）
    @app.get("/api/pansou/search")
    def pansou_search():
        kw = request.args.get("kw", "").strip()
        if not kw:
            raise ValueError("请输入关键词")
        c = store.data["pansou"]
        if not (c["enabled"] and c["url"]):
            raise ValueError("资源搜索还没有启用：请先到「资源搜索」页填写 PanSou 地址并勾选启用")
        try:
            return jsonify(items=PanSou(c).search(kw))
        except PanSouError as e:
            return jsonify(error=str(e)), 502

    @app.post("/api/pansou/inspect")
    def pansou_inspect():
        d = body()
        try:
            season = int(d.get("season") or 1)
        except (TypeError, ValueError):
            season = 1
        return jsonify(engine.inspect_share(d.get("url", ""), season, d.get("prefs"), d.get("title"), d.get("share_title")))

    # ------------------------------------------------------------ 设置
    @app.get("/api/settings/<name>")
    def settings_get(name):
        if name not in SECTIONS:
            raise KeyError("未知设置")
        return jsonify(store.public_section(name))

    @app.put("/api/settings/<name>")
    def settings_put(name):
        if name not in SECTIONS:
            raise KeyError("未知设置")
        store.update_section(name, body())
        if name == "proxy":
            netproxy.configure(store.data["proxy"])
        return jsonify(store.public_section(name))

    # 第 2、3… 套 SmartStrm（装在别的机器上，给另一个媒体服务器用）
    @app.get("/api/smartstrm/extra")
    def ss_extra_get():
        with store.lock:
            items = [dict(i, webhook="", webhook_set=bool(i.get("webhook"))) for i in store.data["smartstrm_more"]["items"]]
        return jsonify(items=items)

    @app.put("/api/smartstrm/extra")
    def ss_extra_put():
        with store.lock:
            old = {i["id"]: i for i in store.data["smartstrm_more"]["items"]}
            out = [notify.clean_ss(i, old.get(i.get("id"))) for i in (body().get("items") or [])[:8]]
            store.data["smartstrm_more"]["items"] = out
            store.save()
        return ss_extra_get()

    @app.post("/api/smartstrm/extra/test")
    def ss_extra_test():
        d = body()
        with store.lock:
            old = next((i for i in store.data["smartstrm_more"]["items"] if i["id"] == d.get("id")), None)
        ok, msg = notify.trigger_smartstrm(notify.clean_ss(d, old), None, force=True)
        return jsonify(ok=ok, msg=msg), (200 if ok else 400)

    def run_test(name, cur):
        """按给定配置测一下某个服务能不能连通 -> (是否成功, 说明)。「测试连接」按钮和「健康状态」页共用。"""
        if name == "dingtalk":
            ok, msg = notify.send_dingtalk(cur, "测试消息", "### ✅ 钉钉通知配置成功\n来自 kuakego-plus", force=True)
        elif name == "smartstrm":
            ok, msg = notify.trigger_smartstrm(cur, None, force=True)
        elif name == "pansou":
            try:
                ok, msg = True, PanSou(cur).ping()
            except PanSouError as e:
                ok, msg = False, str(e)
        elif name == "emby":
            try:
                if not (cur["url"] and cur["api_key"]):
                    raise ValueError("请填写地址和 API Key")
                ok, msg = True, "连接成功：" + Emby(cur["url"], cur["api_key"], cur.get("user_id", "")).ping()
            except Exception as e:
                ok, msg = False, f"连接失败：{e}"
        elif name == "proxy":
            if not cur.get("url"):
                ok, msg = False, "请先填写代理地址"
            else:
                tm = Tmdb(store.data["tmdb"])
                msg = netproxy.probe(cur, f"{tm.base}/configuration" + (f"?api_key={tm.key}" if tm.key and len(tm.key) <= 40 else ""))
                ok = "失败" not in msg.split("；")[0]
        elif name == "fnmedia":
            try:
                if not (cur["url"] and cur["username"]):
                    raise ValueError("请填写地址和用户名")
                d = FnMedia(cur["url"], cur["username"], cur["password"], cur.get("access_code", "")).diagnose()
                libs = "、".join(x["name"] for x in d["libraries"]) or "（无）"
                ok, msg = True, f"连接成功：{d['version']}｜媒体库：{libs}"
                if d.get("sample_item"):
                    sm = d["sample_item"]
                    msg += f"｜示例：{sm.get('title')} type={sm.get('type')} trim_id={sm.get('trim_id')} poster={sm.get('poster')} posters={sm.get('posters')}"
                if d.get("list_error"):
                    msg += f"｜列表失败：{d['list_error']}"
            except Exception as e:
                ok, msg = False, f"连接失败：{e}"
        else:
            try:
                t = Tmdb(cur)
                if not t.ready:
                    raise ValueError("请填写 API Key")
                n = len(t.trending()["items"])
                ok, msg = True, f"连接成功，取到 {n} 条热门剧集"
            except Exception as e:
                ok, msg = False, f"连接失败：{e}"
        return ok, msg

    @app.post("/api/settings/<name>/test")
    def settings_test(name):
        # 用表单里刚填的值测试；密钥留空则回退到已保存的
        cur = dict(store.data[name])
        for k, v in body().items():
            if k in cur and (v or not isinstance(v, str) or k not in ("webhook", "secret", "api_key", "password")):
                cur[k] = v if v != "__clear__" else ""
        ok, msg = run_test(name, cur)
        return jsonify(ok=ok, msg=msg), (200 if ok else 400)

    # ------------------------------------------------------------ 健康状态
    @app.post("/api/health")
    def health():
        """把各个服务的连接状态一次查完。钉钉、SmartStrm 只看配置 / 端口通不通，不会真的发消息或触发任务。"""
        import shutil, socket
        from concurrent.futures import ThreadPoolExecutor, wait
        from urllib.parse import urlparse
        d = store.data

        def tcp(url):
            u = urlparse(url if "//" in url else "http://" + url)
            port = u.port or (443 if u.scheme == "https" else 80)
            socket.create_connection((u.hostname, port), timeout=3).close()

        def quark_check():
            if not d.get("cookie"):
                return "fail", "还没有登录夸克账号"
            q = engine.quark()
            return "ok", "已登录：" + (q.nickname or "（取不到昵称）")

        def svc(name, ready, off_msg):
            def f():
                if not ready:
                    return "off", off_msg
                ok, msg = run_test(name, d[name])
                return ("ok" if ok else "fail"), msg
            return f

        def dingtalk_check():
            c = d["dingtalk"]
            return ("ok", "已配置机器人（不会真的发消息）") if c["enabled"] and c["webhook"] else ("off", "未启用")

        def ss_check(inst, label):
            def f():
                if not (inst.get("enabled") and inst.get("webhook")):
                    return "off", "未启用"
                try:
                    tcp(inst["webhook"])
                except Exception as e:
                    return "fail", f"{label}的地址连不上：{e}"
                return "ok", "端口可以连通（不会真的触发任务）"
            return f

        def disk_check():
            base = os.path.dirname(store.path)
            free = shutil.disk_usage(base).free
            ok = os.access(base, os.W_OK)
            return ("ok" if ok and free > 200 * 1024 ** 2 else "fail"), f"配置目录{'可写' if ok else '不可写'}，剩余 {free / 1024 ** 3:.1f} GB"

        checks = [("quark", "夸克账号", quark_check),
                  ("tmdb", "TMDB", svc("tmdb", bool(d["tmdb"].get("api_key")), "未填写密钥")),
                  ("emby", "Emby", svc("emby", d["emby"]["enabled"] and d["emby"]["url"] and d["emby"]["api_key"], "未启用")),
                  ("fnmedia", "飞牛影视", svc("fnmedia", d["fnmedia"]["enabled"] and d["fnmedia"]["url"] and d["fnmedia"]["username"], "未启用")),
                  ("proxy", "网络代理", svc("proxy", d["proxy"]["enabled"] and d["proxy"]["url"], "未启用")),
                  ("pansou", "资源搜索（PanSou）", svc("pansou", d["pansou"]["enabled"] and d["pansou"]["url"], "未启用")),
                  ("dingtalk", "钉钉通知", dingtalk_check),
                  ("smartstrm", "SmartStrm（主）", ss_check(d["smartstrm"], "主 SmartStrm"))]
        for i, it in enumerate(d.get("smartstrm_more", {}).get("items", []), 2):
            nm = it.get("name") or f"第 {i} 套"
            checks.append((f"smartstrm{i}", f"SmartStrm（{nm}）", ss_check(it, nm)))
        checks.append(("disk", "配置目录", disk_check))

        def timed(fn):
            t0 = time.time()
            try:
                st, msg = fn()
            except Exception as e:
                st, msg = "fail", str(e)
            return st, msg, int((time.time() - t0) * 1000)

        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [(k, n, ex.submit(timed, fn)) for k, n, fn in checks]
            wait([f for _, _, f in futs], timeout=45)
            items = []
            for k, n, f in futs:
                if f.done():
                    st, msg, ms = f.result()
                else:
                    st, msg, ms = "fail", "检测超时", 45000
                items.append({"key": k, "name": n, "state": st, "msg": msg, "ms": ms})
        with store.lock:
            tasks = list(d["tasks"])
        return jsonify(items=items, version=VERSION, running=engine.running, tasks=len(tasks),
                       errors=sum(1 for t in tasks if t.get("last_status") == "error"),
                       backoff=sum(1 for t in tasks if t.get("fail_streak", 0) >= 2))

    # ------------------------------------------------------------ 日志
    @app.get("/api/logs")
    def logs_get():
        items = ring.since(int(request.args.get("since", 0)), request.args.get("level"))
        return jsonify(items=items)

    @app.delete("/api/logs")
    def logs_clear():
        ring.clear()
        return jsonify(ok=True)

    app.engine, app.store = engine, store
    return app


def main():
    from waitress import serve

    store = Store()
    engine = Engine(store)
    legacy = os.path.join(CONFIG_DIR, "quark_config.json")
    if os.path.exists(legacy) and not store.data["tasks"] and not store.data["cookie"]:
        try:
            log.info("检测到旧版 quark_config.json，已导入 %d 个任务", engine.import_legacy(legacy))
        except Exception as e:
            log.warning("导入旧配置失败：%s", e)
    app = create_app(store, engine)
    hosts.apply(CONFIG_DIR)
    hosts.watch(CONFIG_DIR)
    Scheduler(engine).start()
    port = int(os.environ.get("PORT", 5005))
    log.info("kuakego-plus %s 启动，端口 %d", VERSION, port)
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
