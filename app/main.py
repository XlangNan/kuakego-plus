# -*- coding: utf-8 -*-
import hmac
import os
import re
import secrets
import threading
import time

import requests
from flask import Flask, abort, jsonify, request, send_from_directory, Response

from . import hosts, notify
from .emby import Emby
from .engine import Engine, Scheduler
from .logbuf import log, ring, setup as setup_log
from .quark import Quark, QuarkError
from .parser import parse_share_text
from .store import CONFIG_DIR, Store
from .tmdb import Tmdb

VERSION = "1.0.0"
SECTIONS = ("dingtalk", "tmdb", "emby", "smartstrm")


def create_app(store=None, engine=None):
    setup_log(CONFIG_DIR)
    store = store or Store()
    engine = engine or Engine(store)
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False

    user = os.environ.get("WEBUI_USERNAME", "admin")
    password = os.environ.get("WEBUI_PASSWORD") or ""
    if not password:
        password = secrets.token_urlsafe(9)
        log.warning("未设置 WEBUI_PASSWORD，本次启动的随机密码：%s（用户名 %s）", password, user)

    acct = {"ts": 0, "data": {"logged_in": False, "nickname": ""}}

    # ------------------------------------------------------------ 鉴权
    @app.before_request
    def _auth():
        if request.path == "/healthz":
            return None
        a = request.authorization
        ok = bool(a) and hmac.compare_digest(a.username or "", user) and \
            hmac.compare_digest(a.password or "", password)
        if not ok:
            return Response("需要登录", 401, {"WWW-Authenticate": 'Basic realm="quark-plus"'})

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

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/")
    def index():
        return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), "index.html")

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
        return jsonify(**s, version=VERSION, running=engine.running)

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

    def annotate(items):
        """给 TMDB 条目加上：是否已订阅、订阅状态、Emby 是否已入库。"""
        subs = {x["tmdb_id"]: x for x in store.data["tasks"] if x.get("tmdb_id")}
        emby_ids = set()
        cli = engine.emby()
        if cli:
            try:
                emby_ids = cli.tmdb_ids()
            except Exception:
                pass
        for it in items:
            t = subs.get(it["id"])
            it["sub_id"] = t["id"] if t else None
            it["sub_status"] = engine.public_task(t)["status"] if t else ""
            it["in_emby"] = it["id"] in emby_ids
        return items

    img_cache = {}

    @app.get("/api/tmdb/img/<size>/<name>")
    def tmdb_img(size, name):
        # 严格白名单，避免被当成任意地址的代理（SSRF）
        if not re.fullmatch(r"w\d{2,4}|h\d{2,4}|original", size) or \
                not re.fullmatch(r"[\w\-]+\.(jpg|jpeg|png|webp)", name, re.I):
            abort(404)
        key = (size, name)
        if key not in img_cache:
            try:
                r = requests.get(f"{Tmdb(store.data['tmdb']).img_root}/{size}/{name}", timeout=15)
            except Exception:
                abort(502)
            if r.status_code != 200:
                abort(404)
            if len(img_cache) >= 400:
                img_cache.pop(next(iter(img_cache)))
            img_cache[key] = (r.content, r.headers.get("Content-Type", "image/jpeg"))
        data, ctype = img_cache[key]
        return Response(data, mimetype=ctype, headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/api/tmdb/home")
    def tmdb_home():
        rows = tmdb_or_400().home()
        for r in rows:
            annotate(r["items"])
        return jsonify(rows=rows)

    @app.get("/api/tmdb/discover")
    def tmdb_discover():
        d = tmdb_call(tmdb_or_400().discover, request.args.get("category", "all"),
                      request.args.get("sort", "popularity"), int(request.args.get("page", 1)))
        annotate(d["items"])
        return jsonify(d)

    @app.get("/api/tmdb/search")
    def tmdb_search():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify(items=[], total_pages=1)
        d = tmdb_call(tmdb_or_400().search, q, int(request.args.get("page", 1)))
        annotate(d["items"])
        return jsonify(d)

    @app.get("/api/tmdb/tv/<int:tid>")
    def tmdb_tv(tid):
        d = tmdb_call(tmdb_or_400().detail, tid)
        annotate([d])
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

    @app.post("/api/subscriptions")
    def sub_create():
        data = body()
        try:
            tid = int(data.get("tmdb_id") or 0)
            season = int(data.get("season") or 1)
        except (TypeError, ValueError):
            raise ValueError("TMDB ID、季号必须是数字")
        if not tid:
            raise ValueError("请先从推荐或搜索里选择一部剧")
        with store.lock:
            if any(t.get("tmdb_id") == tid and t.get("season") == season for t in store.data["tasks"]):
                raise ValueError("这一季已经订阅过了，请到订阅列表里添加分享链接")
        info = tmdb_call(tmdb_or_400().detail, tid)
        payload = {
            **data, "kind": "subscription", "tmdb_id": tid, "season": season,
            "name": data.get("name") or info["name"], "poster": info["poster_url"],
            "overview": info["overview"],
        }
        if not (payload.get("savepath") or "").strip("/ "):
            raise ValueError("请选择转存目录")
        has_links = bool(parse_share_text(payload.get("links_text", "")))
        task = engine.create_task(payload, save_existing=data.get("save_existing", True))
        if has_links and data.get("save_existing", True):
            threading.Thread(target=engine.run_task, args=(task["id"], True), daemon=True).start()
        return jsonify(task=engine.public_task(task))

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
        return jsonify(store.public_section(name))

    @app.post("/api/settings/<name>/test")
    def settings_test(name):
        # 用表单里刚填的值测试；密钥留空则回退到已保存的
        cur = dict(store.data[name])
        for k, v in body().items():
            if k in cur and (v or not isinstance(v, str) or k not in ("webhook", "secret", "api_key")):
                cur[k] = v if v != "__clear__" else ""
        if name == "dingtalk":
            ok, msg = notify.send_dingtalk(cur, "测试消息", "### ✅ 钉钉通知配置成功\n来自 quark-plus", force=True)
        elif name == "smartstrm":
            ok, msg = notify.trigger_smartstrm(cur, None, force=True)
        elif name == "emby":
            try:
                if not (cur["url"] and cur["api_key"]):
                    raise ValueError("请填写地址和 API Key")
                ok, msg = True, "连接成功：" + Emby(cur["url"], cur["api_key"], cur.get("user_id", "")).ping()
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
        return jsonify(ok=ok, msg=msg), (200 if ok else 400)

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
    log.info("quark-plus %s 启动，端口 %d", VERSION, port)
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
