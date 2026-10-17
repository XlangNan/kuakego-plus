# -*- coding: utf-8 -*-
"""迅雷云盘客户端（适配器）。

登录方式搬自 xlpan：配置里存 refresh_token / access_token（以及签发它们的 client_id、device_id），
access_token 快过期时用 refresh_token 换新的，并把轮换后的新 refresh_token 立刻写回配置；
接口要 captcha_token 时自动向迅雷申请。

对外的方法名和返回的字典形状都做成和 quark.Quark 一样（get_stoken / get_detail / get_fids / ls_dir /
ensure_dir / rename / save_file / wait_task），引擎不用区分是哪家网盘。错误统一抛 QuarkError。
目录 id：夸克根目录是 "0"，迅雷根目录是 ""，适配器内部自动互转。

!! 接口路径和参数是按社区实现（AList 迅雷驱动等）写的，没有官方文档，也没在真实账号上长期验证。
!! 所有接口集中在本文件；迅雷改了只需要改这里。第一次使用请先点「测试连接」，失败时会显示迅雷返回的原文。
"""
import hashlib
import json
import re
import threading
import time

import requests

from .logbuf import log
from .quark import QuarkError

API_PAN = "https://api-pan.xunlei.com"
API_USER = "https://xluser-ssl.xunlei.com"
URL_TOKEN = API_USER + "/v1/auth/token"
URL_ALGORITHMS = "https://risk-account-ssl.office.k8s.xunlei.cn/amaze/v1.0/algorithms"
URL_CAPTCHA_INIT = API_USER + "/v1/shield/captcha/init"
URL_USER_ME = API_USER + "/v1/user/me"
URL_FILES = API_PAN + "/drive/v1/files"
URL_SHARE = API_PAN + "/drive/v1/share"
URL_SHARE_DETAIL = API_PAN + "/drive/v1/share/detail"      # 进入分享里的文件夹用这个（cloud-auto-save-x 的做法）
URL_RESTORE = API_PAN + "/drive/v1/share/restore"
URL_TASKS = API_PAN + "/drive/v1/tasks/"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_ACCOUNT = {
    "refresh_token": "", "access_token": "", "expires_at": 0,
    "client_id": "", "client_secret": "", "device_id": "",
    "captcha_token": "", "captcha_meta": {}, "client_version": "", "package_name": "",
    "captcha_algorithms": "", "sign_key": "", "alg_version": "1",
    "captcha_action_style": "upper", "captcha_extra": {},
    "user_id": "", "user_agent": "", "extra_headers": {},
    "status": "未登录", "nickname": "",
}
SECRET_KEYS = ("refresh_token", "access_token", "client_secret", "captcha_token", "sign_key")

URL_RE = re.compile(r"(?:https?://)?pan\.xunlei\.com/s/([A-Za-z0-9_\-]+)(?:\?[^\s#]*)?(?:#[^\s]*)?", re.I)


def is_xunlei_url(url):
    return "pan.xunlei.com/s/" in (url or "").lower()


class XunleiError(QuarkError):
    def __init__(self, message, status=0, body=None):
        super().__init__(message)
        self.status, self.body = status, body


class NetworkError(XunleiError):
    """网络异常 / 服务暂时不可用：不计入链接失败次数。"""


class AuthError(XunleiError):
    """token 失效且无法刷新：需要重新提供 token。"""


class CaptchaError(XunleiError):
    """接口要求 captcha_token，自动申请也没成功。"""


def captcha_sign(client_id, client_version, package_name, device_id, algorithms, timestamp=None, prefix="1."):
    """AList/OpenList 同款签名：str = client_id+client_version+package_name+device_id+timestamp(毫秒)，
    再对每个算法串做 str = md5(str + alg)，结果前面加 "1."。"""
    ts = str(timestamp or int(time.time() * 1000))
    st = client_id + client_version + package_name + device_id + ts
    for a in algorithms:
        st = hashlib.md5((st + a).encode()).hexdigest()
    return ts, prefix + st


class Xunlei:
    _refresh_lock = threading.Lock()
    _alg_cache = (0, [])

    def __init__(self, store, timeout=20):
        self.store = store
        self.timeout = timeout
        self.session = requests.Session()
        self.nickname = ""
        self._pass = {}       # share_id -> (提取码, pass_code_token)
        self._first = {}      # share_id -> 顶层第一页（get_stoken 时读到的）
        self._anc = {}        # (share_id, 文件 id) -> 祖先文件夹 id 列表（转存带目录结构的文件要用）
        self._captcha_note = ""

    # ---------------------------------------------------------------- 配置读写
    def _acct(self):
        with self.store.lock:
            return dict(self.store.data["xunlei"])

    def _update(self, **kv):
        with self.store.lock:
            self.store.data["xunlei"].update(kv)
            self.store.save()

    # ---------------------------------------------------------------- 登录 / token
    def _headers(self, a):
        h = {"User-Agent": a.get("user_agent") or DEFAULT_UA, "Accept": "application/json",
             "Content-Type": "application/json", "Origin": "https://pan.xunlei.com", "Referer": "https://pan.xunlei.com/"}
        for k, hk in (("device_id", "x-device-id"), ("client_id", "x-client-id"), ("client_version", "x-client-version")):
            if a.get(k):
                h[hk] = a[k]
        if a.get("access_token"):
            h["Authorization"] = "Bearer " + a["access_token"]
        if a.get("captcha_token"):
            h["x-captcha-token"] = a["captcha_token"]
        h.update(a.get("extra_headers") or {})
        return h

    def refresh(self, force=False):
        with self._refresh_lock:
            a = self._acct()
            if not force and a.get("access_token") and a.get("expires_at", 0) - 120 > time.time():
                return a
            if not a.get("refresh_token"):
                if a.get("access_token"):
                    return a                      # 只有 access_token：用到它失效为止
                raise AuthError("还没有登录迅雷云盘，请先到「账号登录」填写 refresh_token")
            if not a.get("client_id"):
                raise AuthError("还没有填 client_id。到「账号登录」页填写（浏览器 F12 里请求头的 x-client-id），"
                                "或用下面的「导入 cURL」自动填")
            payload = {"grant_type": "refresh_token", "refresh_token": a["refresh_token"], "client_id": a["client_id"]}
            if a.get("client_secret"):
                payload["client_secret"] = a["client_secret"]
            headers = {"User-Agent": a.get("user_agent") or DEFAULT_UA, "Content-Type": "application/json"}
            for k, hk in (("device_id", "x-device-id"), ("client_id", "x-client-id")):
                if a.get(k):
                    headers[hk] = a[k]
            try:
                r = self.session.post(URL_TOKEN, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                raise NetworkError("刷新 token 时网络异常：%s" % e)
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text[:300]}
            if r.status_code != 200 or not data.get("access_token"):
                self._update(status="登录失效")
                raise AuthError("刷新 token 失败（HTTP %s）：%s" % (r.status_code, json.dumps(data, ensure_ascii=False)[:300]),
                                r.status_code, data)
            kv = {"access_token": data["access_token"], "expires_at": int(time.time()) + int(data.get("expires_in") or 3600),
                  "status": "已登录"}
            if data.get("refresh_token"):         # 轮换：必须立刻保存新的
                kv["refresh_token"] = data["refresh_token"]
            self._update(**kv)
            log.info("迅雷 access_token 已刷新，%s 秒后过期", data.get("expires_in") or 3600)
            return self._acct()

    def _fetch_algorithms(self, a):
        ts0, cached = Xunlei._alg_cache
        if cached and time.time() - ts0 < 1800:
            return cached
        if not cached and time.time() - ts0 < 600:
            return []
        cid, ver, pkg = a.get("client_id", ""), a.get("client_version", ""), a.get("package_name", "")
        av, key, ts = str(a.get("alg_version") or "1"), a.get("sign_key", ""), str(int(time.time()))
        sg = hashlib.md5((cid + ver + pkg + ts + av + key).encode()).hexdigest()
        params = {"client_id": cid, "client_version": ver, "package_name": pkg, "timestamp": ts, "alg_version": av, "sign": sg}
        try:
            r = self.session.get(URL_ALGORITHMS, params=params, timeout=8, headers={"User-Agent": a.get("user_agent") or DEFAULT_UA})
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            Xunlei._alg_cache = (time.time(), [])
            log.warning("迅雷：获取签名算法失败（10 分钟内不再重试）：%s", str(e)[:160])
            return []
        salts = [str(it.get("salt") if isinstance(it, dict) else it) for it in (data.get("algorithms") or [])
                 if (it.get("salt") if isinstance(it, dict) else it)] if isinstance(data, dict) else []
        Xunlei._alg_cache = (time.time(), salts)
        if not salts:
            log.warning("迅雷：获取签名算法没有拿到盐：%s", str(data)[:200])
        return salts

    def refresh_captcha(self, method, url):
        a = self._acct()
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
        meta = {}
        if a.get("user_id"):
            meta["user_id"] = a["user_id"]
        meta.update(a.get("captcha_meta") or {})
        for k in ("client_version", "package_name"):
            if a.get(k):
                meta.setdefault(k, a[k])
        algs = a.get("captcha_algorithms") or []
        if isinstance(algs, str):
            algs = [x.strip() for x in algs.split(",") if x.strip()]
        if not algs and a.get("sign_key"):
            algs = self._fetch_algorithms(a)
        if algs:
            ts, sign = captcha_sign(a.get("client_id", ""), a.get("client_version", ""), a.get("package_name", ""),
                                    a.get("device_id", ""), algs, prefix="%s." % (a.get("alg_version") or "1"))
            meta["timestamp"], meta["captcha_sign"] = ts, sign
        m = method.lower() if a.get("captcha_action_style") == "lower" else method.upper()
        body = {"client_id": a.get("client_id", ""), "action": "%s:%s" % (m, path), "device_id": a.get("device_id", ""), "meta": meta}
        body.update(a.get("captcha_extra") or {})
        headers = {"User-Agent": a.get("user_agent") or DEFAULT_UA, "Content-Type": "application/json",
                   "Origin": "https://pan.xunlei.com", "Referer": "https://pan.xunlei.com/"}
        for k, hk in (("device_id", "x-device-id"), ("client_id", "x-client-id"), ("client_version", "x-client-version")):
            if a.get(k):
                headers[hk] = a[k]
        try:
            r = self.session.post(URL_CAPTCHA_INIT, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise NetworkError("申请 captcha_token 时网络异常：%s" % e)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        token = data.get("captcha_token") if isinstance(data, dict) else ""
        if r.status_code != 200 or not token:
            raise CaptchaError("自动申请 captcha_token 失败（HTTP %s）：%s。可以导入 cURL 补全参数，或手动填 captcha_token"
                               % (r.status_code, json.dumps(data, ensure_ascii=False)[:300]), r.status_code, data)
        self._captcha_note = ("迅雷要求人工验证，请在浏览器里打开并完成：%s" % data["url"]) if data.get("url") else ""
        self._update(captcha_token=token)
        log.info("迅雷：已自动申请新的 captcha_token（%s）%s", body["action"], "；" + self._captcha_note if self._captcha_note else "")
        return token

    # ---------------------------------------------------------------- 通用请求
    def request(self, method, url, retry_auth=True, retry_captcha=True, **kw):
        a = self.refresh()
        try:
            r = self.session.request(method, url, headers=self._headers(a), timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise NetworkError("网络异常：%s" % e)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code == 401 and retry_auth:
            self.refresh(force=True)
            return self.request(method, url, retry_auth=False, retry_captcha=retry_captcha, **kw)
        err = str(data.get("error") or "") if isinstance(data, dict) else ""
        if r.status_code >= 400 or err:
            text = json.dumps(data, ensure_ascii=False)[:300] if data is not None else r.text[:300]
            low = text.lower()
            if "captcha" in low:
                if retry_captcha:
                    self.refresh_captcha(method, url)
                    return self.request(method, url, retry_auth=retry_auth, retry_captcha=False, **kw)
                raise CaptchaError("接口要求 captcha_token：%s%s" % (text, "　" + self._captcha_note if self._captcha_note else ""),
                                   r.status_code, data)
            if r.status_code in (401, 403) and ("token" in low or "unauth" in low):
                self._update(status="登录失效")
                raise AuthError("登录失效：%s" % text, r.status_code, data)
            if r.status_code >= 500 or r.status_code == 429:
                raise NetworkError("迅雷服务暂时不可用（HTTP %s）：%s" % (r.status_code, text), r.status_code, data)
            raise XunleiError("HTTP %s：%s" % (r.status_code, text), r.status_code, data)
        return data if data is not None else {}

    # ---------------------------------------------------------------- 账号
    def init(self):
        data = self.request("GET", URL_USER_ME)
        self.nickname = str(data.get("name") or data.get("nickname") or data.get("username") or data.get("sub") or "")
        self._update(nickname=self.nickname, status="已登录")
        return data

    # ---------------------------------------------------------------- 目录 id 互转
    @staticmethod
    def _pid(fid):
        return "" if fid in ("0", 0, None) else fid

    @staticmethod
    def _item(f):
        d = f.get("kind") == "drive#folder"
        return {"fid": f.get("id", ""), "file_name": f.get("name", ""), "dir": d, "file": not d,
                "size": int(f.get("size") or 0), "share_fid_token": "", "updated_at": f.get("modified_time", "")}

    # ---------------------------------------------------------------- 分享（接口和 Quark 同名）
    @staticmethod
    def extract_url(url):
        m = URL_RE.search(url or "")
        sid = m.group(1) if m else None
        q = re.search(r"[?&]pwd=([A-Za-z0-9]+)", url or "") or re.search(r"提取码[:： ]*([A-Za-z0-9]{4})", url or "")
        return sid, (q.group(1) if q else ""), "0", []

    def _share_page(self, sid, parent_id="", page_token="", use_pct=True):
        pwd, pct = self._pass.get(sid, ("", ""))
        if not use_pct:
            pct = ""
        params = {"share_id": sid, "pass_code": pwd, "limit": 100, "pass_code_token": pct,
                  "page_token": page_token, "thumbnail_size": "SIZE_SMALL"}
        if parent_id:
            # 进入文件夹：走 /share/detail，只带 token、不带提取码；它不行再退回老办法（/share + parent_id）
            dp = {"share_id": sid, "pass_code_token": pct, "parent_id": parent_id, "limit": 100,
                  "page_token": page_token, "thumbnail_size": "SIZE_SMALL"}
            try:
                data = self.request("GET", URL_SHARE_DETAIL, params=dp)
            except NetworkError:
                raise
            except XunleiError as e:
                log.info("迅雷：/share/detail 读取文件夹失败（%s），改用 /share + parent_id", str(e)[:120])
                data = self.request("GET", URL_SHARE, params=dict(params, parent_id=parent_id))
        else:
            data = self.request("GET", URL_SHARE, params=params)
        status = data.get("share_status")
        if status and status != "OK":
            raise XunleiError("分享状态异常：%s" % status, 0, data)
        return data

    def get_stoken(self, pwd_id, passcode=""):
        """和 Quark.get_stoken 一样返回 {status, data:{stoken}}；这里的 stoken 就是迅雷的 pass_code_token。"""
        self._pass[pwd_id] = (passcode or "", "")
        try:
            first = self._share_page(pwd_id, use_pct=False)     # 顶层第一页只带提取码、不带 token（和 xlpan 跑通的做法一致）
        except NetworkError as e:
            return {"status": 500, "message": str(e).replace("网络异常：", "", 1)}
        except XunleiError as e:
            msg = str(e)
            if "share_id" in msg and "invalid" in msg:      # 迅雷对不存在 / 已取消 / 已过期的分享都是这个回答
                msg = "分享不存在或已被取消（迅雷返回：share_id invalid）"
            return {"status": 400, "message": msg}
        pct = first.get("pass_code_token", "")
        self._pass[pwd_id] = (passcode or "", pct)
        self._first[pwd_id] = first
        return {"status": 200, "data": {"stoken": pct or "-"}}

    def get_detail(self, pwd_id, stoken, pdir_fid):
        out, token, parent = [], "", self._pid(pdir_fid)
        anc = self._anc.get((pwd_id, parent), []) if parent else []
        while True:
            try:
                if not parent and not token and pwd_id in self._first:
                    page = self._first[pwd_id]              # 顶层第一页：直接用 get_stoken 时读到的
                else:
                    page = self._share_page(pwd_id, parent, token)
            except XunleiError as e:
                raise QuarkError(str(e))
            for f in page.get("files") or []:
                it = self._item(f)
                self._anc[(pwd_id, it["fid"])] = anc + ([parent] if parent else [])
                out.append(it)
            token = page.get("next_page_token") or ""
            if not token:
                return out

    # ---------------------------------------------------------------- 自己的网盘
    def ls_dir(self, pdir_fid):
        items, token = [], ""
        flt = json.dumps({"trashed": {"eq": False}, "phase": {"eq": "PHASE_TYPE_COMPLETE"}})
        while True:
            data = self.request("GET", URL_FILES, params={"parent_id": self._pid(pdir_fid), "limit": 100, "with_audit": "true",
                                                         "filters": flt, "page_token": token, "space": ""})
            items.extend(self._item(f) for f in data.get("files") or [])
            token = data.get("next_page_token") or ""
            if not token:
                return items

    def _mkdir(self, parent_id, name):
        data = self.request("POST", URL_FILES, json={"kind": "drive#folder", "parent_id": parent_id, "name": name, "space": ""})
        fid = (data.get("file") or {}).get("id")
        if not fid:
            raise XunleiError("创建目录没有返回 id：%s" % json.dumps(data, ensure_ascii=False)[:200])
        return fid

    def _resolve(self, path, create):
        parent = ""
        for part in [p for p in (path or "").split("/") if p]:
            hit = next((i["fid"] for i in self.ls_dir(parent) if i["dir"] and i["file_name"] == part), None)
            if hit is None:
                if not create:
                    return None
                hit = self._mkdir(parent, part)
            parent = hit
        return parent or "0"

    def get_fids(self, paths):
        """只查不建；和 Quark 一样，找不到的路径不出现在结果里（这里每个路径单独查）。"""
        out = []
        for p in paths:
            fid = self._resolve(p, False)
            if fid is not None:
                out.append({"fid": fid, "file_path": p})
        return out

    def ensure_dir(self, path):
        return self._resolve(path, True)

    def rename(self, fid, name):
        try:
            self.request("PATCH", "%s/%s" % (URL_FILES, fid), json={"name": name, "space": ""})
        except XunleiError as e:
            return {"code": 1, "message": str(e)}
        return {"code": 0}

    def delete(self, fids):
        """删除 = 移到迅雷的回收站（可以找回）。接口是 files:batchTrash，和 PikPak 系同源；没在真实账号上验证过。"""
        ids = [self._pid(f) for f in fids if f]
        if not ids:
            return {"code": 0}
        try:
            self.request("POST", URL_FILES + ":batchTrash", json={"ids": ids, "space": ""})
        except XunleiError as e:
            raise QuarkError("迅雷删除失败：%s" % e)
        return {"code": 0}

    def save_file(self, fid_list, fid_token_list, to_pdir_fid, pwd_id, stoken):
        """转存（restore）。迅雷按祖先文件夹分组转存；转存任务在这里等完，返回的 id 交给 wait_task（直接放行）。"""
        pct = self._pass.get(pwd_id, ("", ""))[1]
        groups = {}
        for fid in fid_list:
            groups.setdefault(tuple(self._anc.get((pwd_id, fid), [])), []).append(fid)
        for anc, ids in groups.items():
            def go(ancestors):
                return self.request("POST", URL_RESTORE, json={
                    "parent_id": self._pid(to_pdir_fid), "share_id": pwd_id, "pass_code_token": pct,
                    "ancestor_ids": list(ancestors), "file_ids": ids, "specify_parent_id": True})
            try:
                data = go(anc)
            except NetworkError:
                raise
            except XunleiError as e:
                if not anc:
                    raise
                log.info("迅雷：带祖先目录转存失败（%s），改为不带 ancestor_ids 重试（cloud-auto-save-x 的做法）", str(e)[:120])
                data = go(())
            tid = data.get("restore_task_id") or data.get("task_id")
            if tid:
                self._wait(tid)
        return "done"

    def _wait(self, task_id, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.request("GET", URL_TASKS + str(task_id))
            phase = t.get("phase", "")
            if phase == "PHASE_TYPE_COMPLETE":
                return t
            if phase == "PHASE_TYPE_ERROR":
                raise XunleiError("转存任务失败：%s" % (t.get("message") or json.dumps(t, ensure_ascii=False)[:200]), 0, t)
            time.sleep(1.5)
        raise NetworkError("转存任务 %s 超时（%s 秒）仍未完成" % (task_id, timeout))

    def wait_task(self, task_id, timeout=120):
        return {"status": 200, "data": {"status": 2}}

    def diagnose(self, url):
        """诊断一个分享链接：把每一步迅雷返回了什么（不含任何 token）列出来，方便定位「读不到文件」。"""
        sid, pwd, _, _ = self.extract_url(url)
        out = {"share_id": sid, "has_passcode": bool(pwd), "steps": []}

        def brief(files):
            return [{"name": f.get("name"), "kind": f.get("kind"), "id": (f.get("id") or "")[:12] + "…", "size": f.get("size")} for f in files[:15]]

        def step(title, fn):
            try:
                d = fn()
                out["steps"].append({"step": title, "ok": True, **d})
                return d
            except Exception as e:      # noqa: BLE001  诊断要把任何错误原样给人看
                out["steps"].append({"step": title, "ok": False, "error": str(e)[:600]})
                return None

        if not sid:
            out["steps"].append({"step": "解析链接", "ok": False, "error": "认不出分享 ID"})
            return out
        self._pass[sid] = (pwd, "")

        def first():
            page = self._share_page(sid, use_pct=False)
            pct = page.get("pass_code_token", "")
            self._pass[sid] = (pwd, pct)
            self._first[sid] = page
            files = page.get("files") or []
            return {"response_keys": sorted(page.keys()), "share_status": page.get("share_status"), "token_returned": bool(pct),
                    "next_page": bool(page.get("next_page_token")), "file_count": len(files), "files": brief(files),
                    "title": (page.get("title") or page.get("share_title") or "")[:60], "_files": files}
        d1 = step("读取顶层（只带提取码）", first)
        if d1 and d1.get("_files"):
            folders = [f for f in d1["_files"] if f.get("kind") == "drive#folder"]
            if folders:
                def sub():
                    page = self._share_page(sid, folders[0]["id"])
                    files = page.get("files") or []
                    return {"folder": folders[0].get("name"), "response_keys": sorted(page.keys()), "file_count": len(files), "files": brief(files)}

                def legacy():
                    pwd2, pct2 = self._pass[sid]
                    page = self.request("GET", URL_SHARE, params={"share_id": sid, "pass_code": pwd2, "pass_code_token": pct2, "parent_id": folders[0]["id"],
                                                                  "limit": 100, "thumbnail_size": "SIZE_SMALL"})
                    files = page.get("files") or []
                    return {"folder": folders[0].get("name"), "response_keys": sorted(page.keys()), "file_count": len(files), "files": brief(files)}
                step("读取第一个文件夹（/share/detail，当前使用）", sub)
                step("读取第一个文件夹（/share + parent_id，对照）", legacy)
        if d1:
            d1.pop("_files", None)
        return out

    def growth_sign(self):
        return False, "迅雷云盘没有签到功能"
