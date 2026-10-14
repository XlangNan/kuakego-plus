# -*- coding: utf-8 -*-
"""可选的 HTTP(S) 代理，只给指定类别的请求用（默认：TMDB）。代理连不上时可自动改直连（hosts 模式照常生效）。"""
import time
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .logbuf import log

_cfg = {}
_last_warn = [0.0]


def configure(cfg):
    global _cfg
    _cfg = dict(cfg or {})


def _proxy_url(cfg):
    url = (cfg.get("url") or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    if cfg.get("username"):
        p = urlsplit(url)
        host = p.netloc.rsplit("@", 1)[-1]
        url = urlunsplit((p.scheme, f"{quote(cfg['username'], safe='')}:{quote(cfg.get('password') or '', safe='')}@{host}", p.path, p.query, p.fragment))
    return url


def active(group, cfg=None):
    c = _cfg if cfg is None else cfg
    if not (c.get("enabled") and c.get("url")):
        return False
    return bool(c.get("use_tmdb" if group == "tmdb" else "use_platforms" if group == "platforms" else None))


def request(method, url, group="", **kw):
    """和 requests.get / requests.post 用法一样；group="tmdb" / "platforms" 才可能走代理。"""
    fn = getattr(requests, method)
    if not active(group):
        return fn(url, **kw)
    proxy = _proxy_url(_cfg)
    t = kw.get("timeout", 15)
    pkw = dict(kw, proxies={"http": proxy, "https": proxy})
    if _cfg.get("fallback", True):
        pkw["timeout"] = (6, t) if not isinstance(t, tuple) else t   # 代理连不上就尽快放弃，别拖着
    try:
        r = fn(url, **pkw)
        if r.status_code != 407:
            return r
        err = "代理认证失败(407)"
    except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        err = str(e)[:120]
    if not _cfg.get("fallback", True):
        raise requests.exceptions.ProxyError(f"代理不可用：{err}")
    if time.time() - _last_warn[0] > 60:
        _last_warn[0] = time.time()
        log.warning("代理不可用，已改直连：%s", err)
    return fn(url, **kw)


def get(url, group="", **kw):
    return request("get", url, group, **kw)


def post(url, group="", **kw):
    return request("post", url, group, **kw)


def probe(cfg, url):
    """设置页「测试代理」：分别用代理、直连请求同一个地址，返回说明文字。"""
    out = []
    proxy = _proxy_url(cfg)
    for label, px in (("代理", {"http": proxy, "https": proxy} if proxy else None), ("直连", None)):
        if label == "代理" and not px:
            out.append("代理：未填写地址")
            continue
        t0 = time.time()
        try:
            r = requests.get(url, proxies=px, timeout=12)
            out.append(f"{label}：{int((time.time() - t0) * 1000)} ms（HTTP {r.status_code}）")
        except Exception as e:
            out.append(f"{label}：失败（{str(e)[:80]}）")
    return "；".join(out)
