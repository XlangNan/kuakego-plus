# -*- coding: utf-8 -*-
"""网络代理：只给 TMDB 用、连不上自动改直连、密码不回显。"""
from unittest import mock

import requests

from app import netproxy
from tests.test_layout_aliases import make_client


class R:
    status_code = 200
    def __init__(self, code=200): self.status_code = code


def cfg(**kw):
    c = {"enabled": True, "url": "10.0.0.2:7890", "username": "", "password": "", "use_tmdb": True, "use_platforms": False, "fallback": True}
    c.update(kw)
    netproxy.configure(c)


def test_only_selected_groups_use_proxy():
    cfg()
    seen = []
    def fake(url, **kw): seen.append(kw.get("proxies")); return R()
    with mock.patch.object(requests, "get", fake):
        netproxy.get("https://api.themoviedb.org/3/x", "tmdb")
        netproxy.get("https://pianku.api.mgtv.com/x", "platforms")
        netproxy.get("https://x", "")
    assert seen[0] == {"http": "http://10.0.0.2:7890", "https": "http://10.0.0.2:7890"}
    assert seen[1] is None and seen[2] is None
    cfg(use_platforms=True)
    with mock.patch.object(requests, "get", fake):
        netproxy.get("https://pianku.api.mgtv.com/x", "platforms")
    assert seen[-1]["https"].startswith("http://")


def test_fallback_to_direct_and_strict_mode():
    cfg()
    calls = []
    def fake(url, **kw):
        calls.append(kw.get("proxies"))
        if kw.get("proxies"):
            raise requests.exceptions.ConnectTimeout("t")
        return R()
    with mock.patch.object(requests, "get", fake):
        assert netproxy.get("https://t", "tmdb").status_code == 200
    assert calls[0] and calls[1] is None
    cfg(fallback=False)
    with mock.patch.object(requests, "get", fake):
        try:
            netproxy.get("https://t", "tmdb"); assert False
        except requests.exceptions.ProxyError as e:
            assert "代理不可用" in str(e)


def test_auth_in_proxy_url_and_407():
    cfg(username="u@x", password="p:w")
    assert netproxy._proxy_url(netproxy._cfg) == "http://u%40x:p%3Aw@10.0.0.2:7890"
    seen = []
    def fake(url, **kw):
        seen.append(kw.get("proxies")); return R(407 if kw.get("proxies") else 200)
    with mock.patch.object(requests, "get", fake):
        assert netproxy.get("https://t", "tmdb").status_code == 200
    assert len(seen) == 2


def test_settings_hide_password_and_apply():
    store, eng, c, h = make_client()
    r = c.put("/api/settings/proxy", json={"enabled": True, "url": "http://1.2.3.4:80", "password": "secret", "use_tmdb": True}, headers=h).get_json()
    assert r["password"] == "" and r["password_set"] is True and r["fallback"] is True
    assert netproxy.active("tmdb")
    c.put("/api/settings/proxy", json={"enabled": False}, headers=h)
    assert not netproxy.active("tmdb")
    netproxy.configure({})
