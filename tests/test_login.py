# -*- coding: utf-8 -*-
"""登录页：Cookie 会话、Basic 兼容、限速、不弹浏览器登录框。"""
import base64

from tests.test_layout_aliases import make_client

BROWSER = {"User-Agent": "Mozilla/5.0"}


def test_browser_redirects_and_no_basic_prompt():
    store, eng, c, h = make_client()
    r = c.get("/", headers=BROWSER)
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")
    r = c.get("/api/overview", headers=BROWSER)
    assert r.status_code == 401 and "WWW-Authenticate" not in r.headers
    r = c.get("/api/overview")   # 脚本（无浏览器 UA）仍然能收到 Basic 质询
    assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"]
    assert c.get("/login", headers=BROWSER).status_code == 200


def test_login_cookie_flow():
    store, eng, c, h = make_client()
    assert c.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401
    r = c.post("/api/login", json={"username": "admin", "password": "pw", "remember": True})
    ck = " ".join(r.headers.get_all("Set-Cookie"))
    assert r.status_code == 200 and "kq_session=" in ck and "HttpOnly" in ck
    assert c.get("/api/overview", headers=BROWSER).status_code == 200      # cookie 自动带上
    assert c.get("/login", headers=BROWSER).status_code == 302            # 已登录直接回首页
    c.post("/api/logout")
    assert c.get("/api/overview", headers=BROWSER).status_code == 401


def test_basic_still_works_and_forged_cookie_rejected():
    store, eng, c, h = make_client()
    assert c.get("/api/overview", headers=h).status_code == 200
    c.set_cookie("kq_session", "9999999999.deadbeef")
    assert c.get("/api/overview", headers=BROWSER).status_code == 401


def test_rate_limit():
    store, eng, c, h = make_client()
    for _ in range(5):
        c.post("/api/login", json={"username": "admin", "password": "bad"})
    assert c.post("/api/login", json={"username": "admin", "password": "pw"}).status_code == 429


def test_logout_ignores_cached_basic_in_browser():
    store, eng, c, h = make_client()
    hb = dict(h, **BROWSER)
    assert c.get("/api/overview", headers=hb).status_code == 200      # 浏览器缓存了 Basic 凭证
    c.post("/api/logout")
    assert c.get("/api/overview", headers=hb).status_code == 401      # 退出后不再认
    c.post("/api/login", json={"username": "admin", "password": "pw"})
    assert c.get("/api/overview", headers=hb).status_code == 200
