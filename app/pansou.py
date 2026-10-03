# -*- coding: utf-8 -*-
"""PanSou（https://github.com/fish2018/pansou）客户端：只调用它的 HTTP 接口，不包含其代码。"""
import re
import time

import requests

from .parser import parse_share_text

_tokens = {}  # (地址, 用户名) -> (token, 过期时间戳)


class PanSouError(Exception):
    pass


def _clean(s, n=160):
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("…" if len(s) > n else "")


class PanSou:
    def __init__(self, cfg):
        self.base = (cfg.get("url") or "").strip().rstrip("/")
        self.user, self.pwd = (cfg.get("username") or "").strip(), cfg.get("password") or ""
        self.src = cfg.get("src") if cfg.get("src") in ("all", "tg", "plugin") else "all"
        self.channels = re.sub(r"[\s,，]+", ",", (cfg.get("channels") or "").strip()).strip(",")
        self.plugins = re.sub(r"[\s,，]+", ",", (cfg.get("plugins") or "").strip()).strip(",")
        try:
            self.timeout = max(5, min(int(cfg.get("timeout") or 30), 120))
        except (TypeError, ValueError):
            self.timeout = 30

    # ---------------------------------------------------------------- 认证（可选）
    def _token(self, force=False):
        if not (self.user and self.pwd):
            return ""
        key = (self.base, self.user)
        hit = _tokens.get(key)
        if hit and not force and hit[1] > time.time() + 60:
            return hit[0]
        try:
            r = requests.post(f"{self.base}/api/auth/login", timeout=self.timeout,
                              json={"username": self.user, "password": self.pwd})
        except requests.RequestException as e:
            raise PanSouError(f"连接 PanSou 失败：{e}")
        if r.status_code in (401, 403):
            raise PanSouError("PanSou 用户名或密码错误")
        if r.status_code >= 400:
            raise PanSouError(f"PanSou 登录失败：HTTP {r.status_code}")
        d = r.json()
        d = d["data"] if isinstance(d.get("data"), dict) else d
        if not d.get("token"):
            raise PanSouError("PanSou 登录失败：没有返回 token")
        exp = float(d.get("expires_at") or time.time() + 3600)
        exp = exp / 1000 if exp > 1e11 else exp   # 兼容毫秒时间戳
        _tokens[key] = (d["token"], exp)
        return d["token"]

    def _get(self, path, params=None):
        if not self.base:
            raise PanSouError("还没有填写 PanSou 地址")
        r = None
        for attempt in (0, 1):
            tok = self._token(force=bool(attempt))
            try:
                r = requests.get(self.base + path, params=params, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {tok}"} if tok else {})
            except requests.RequestException as e:
                raise PanSouError(f"连接 PanSou 失败：{e}")
            if r.status_code == 401 and attempt == 0 and self.user:
                continue   # token 可能过期了，重新登录再试一次
            break
        if r.status_code == 401:
            raise PanSouError("PanSou 开启了认证：请填写用户名和密码")
        if r.status_code >= 400:
            raise PanSouError(f"PanSou 返回 HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise PanSouError("PanSou 返回的不是 JSON（地址是不是填成了网页版的地址？请填接口地址，如 http://IP:8888）")

    # ---------------------------------------------------------------- 接口
    def ping(self):
        self._get("/api/health")
        if self.user:
            self._token(force=True)   # 顺便验证用户名密码
        return "连接成功" + ("，认证通过" if self.user else "")

    def search(self, kw, limit=60):
        """返回夸克链接列表 [{url, pwd_id, passcode, title, source, time}]，保持 PanSou 的排序。"""
        kw = (kw or "").strip()
        if not kw:
            return []
        params = {"kw": kw, "cloud_types": "quark", "res": "merge", "src": self.src}
        if self.channels and self.src != "plugin":
            params["channels"] = self.channels
        if self.plugins and self.src != "tg":
            params["plugins"] = self.plugins
        d = self._get("/api/search", params)
        if isinstance(d.get("data"), dict):   # 新版本外面多包了一层 {code, message, data}
            d = d["data"]
        raw = list((d.get("merged_by_type") or {}).get("quark") or [])
        if not raw:   # 兜底：results 格式（每条结果带 links）
            for r in d.get("results") or []:
                for l in r.get("links") or []:
                    if l.get("type") == "quark":
                        raw.append({"url": l.get("url"), "password": l.get("password"), "note": r.get("title"),
                                    "datetime": r.get("datetime"), "source": f"tg:{r.get('channel', '')}"})
        out, seen = [], set()
        for it in raw:
            url, pw = it.get("url") or "", it.get("password") or ""
            parsed = parse_share_text(url + (f" 提取码：{pw}" if pw and "pwd=" not in url else ""))
            if not parsed or parsed[0]["pwd_id"] in seen:
                continue
            p = parsed[0]
            seen.add(p["pwd_id"])
            out.append({"url": p["url"], "pwd_id": p["pwd_id"], "passcode": p["passcode"],
                        "title": _clean(it.get("note")), "source": _clean(it.get("source"), 40),
                        "time": str(it.get("datetime") or "")[:10]})
            if len(out) >= limit:
                break
        return out
