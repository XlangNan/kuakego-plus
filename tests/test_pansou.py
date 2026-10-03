# -*- coding: utf-8 -*-
import base64, os, sys
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tests.test_engine as T
from tests.test_engine import env, mk
from app import pansou as P
from app.pansou import PanSou, PanSouError


class R:
    def __init__(self, data=None, status=200, bad_json=False):
        self.data, self.status_code, self.bad = data, status, bad_json
    def json(self):
        if self.bad: raise ValueError("not json")
        return self.data


CFG = {"url": "http://ps:8888/", "src": "all"}
MERGED = {"total": 4, "merged_by_type": {"quark": [
    {"url": "https://pan.quark.cn/s/aaa111", "password": "ab12", "note": "<b>庆余年</b> 第二季 4K  全集", "datetime": "2026-05-01T10:00:00Z", "source": "tg:某频道"},
    {"url": "https://pan.quark.cn/s/bbb222?pwd=zz99", "password": "", "note": "庆余年2", "datetime": "2026-04-01T00:00:00Z", "source": "plugin:xx"},
    {"url": "https://pan.quark.cn/s/aaa111", "password": "ab12", "note": "重复的", "datetime": "", "source": ""},
    {"url": "https://pan.baidu.com/s/xxx", "password": "1", "note": "不是夸克", "datetime": "", "source": ""},
]}}


def search(cfg, resp, **kw):
    with mock.patch("app.pansou.requests.get", return_value=resp) as g:
        return PanSou(cfg).search("庆余年", **kw), g


def test_search_parses_dedupes_and_canonicalizes():
    items, g = search(CFG, R(MERGED))
    assert [i["pwd_id"] for i in items] == ["aaa111", "bbb222"]
    assert items[0]["url"] == "https://pan.quark.cn/s/aaa111?pwd=ab12" and items[1]["passcode"] == "zz99"
    assert items[0]["title"] == "庆余年 第二季 4K 全集" and items[0]["time"] == "2026-05-01" and items[0]["source"] == "tg:某频道"
    url, kw = g.call_args[0][0], g.call_args[1]
    assert url == "http://ps:8888/api/search"                      # 地址末尾的 / 被去掉
    assert kw["params"]["kw"] == "庆余年" and kw["params"]["cloud_types"] == "quark" and kw["params"]["res"] == "merge"


def test_search_accepts_wrapped_response_and_results_fallback():
    items, _ = search(CFG, R({"code": 0, "message": "success", "data": MERGED}))
    assert len(items) == 2
    fallback = {"results": [{"title": "某剧", "channel": "ch1", "datetime": "2026-01-02T00:00:00Z",
                             "links": [{"type": "baidu", "url": "https://pan.baidu.com/s/1"},
                                       {"type": "quark", "url": "https://pan.quark.cn/s/ccc333", "password": "pw11"}]}]}
    items, _ = search(CFG, R(fallback))
    assert len(items) == 1 and items[0]["url"].endswith("ccc333?pwd=pw11") and items[0]["source"] == "tg:ch1"


def test_channels_and_plugins_only_sent_when_relevant():
    cfg = {**CFG, "channels": "a, b，c", "plugins": "x y"}
    _, g = search({**cfg, "src": "all"}, R(MERGED)); p = g.call_args[1]["params"]
    assert p["channels"] == "a,b,c" and p["plugins"] == "x,y"
    _, g = search({**cfg, "src": "plugin"}, R(MERGED)); p = g.call_args[1]["params"]
    assert "channels" not in p and p["plugins"] == "x,y" and p["src"] == "plugin"
    _, g = search({**cfg, "src": "tg"}, R(MERGED)); p = g.call_args[1]["params"]
    assert "plugins" not in p and p["channels"] == "a,b,c"
    _, g = search({**CFG, "src": "bogus"}, R(MERGED)); assert g.call_args[1]["params"]["src"] == "all"


def test_empty_keyword_and_limit():
    assert PanSou(CFG).search("   ") == []
    many = {"merged_by_type": {"quark": [{"url": f"https://pan.quark.cn/s/id{i:04d}", "note": "x"} for i in range(100)]}}
    items, _ = search(CFG, R(many)); assert len(items) == 60


def test_auth_login_and_token_reuse_and_retry_on_401():
    P._tokens.clear()
    cfg = {**CFG, "username": "admin", "password": "pw"}
    login = R({"token": "T1", "expires_at": 9999999999, "username": "admin"})
    with mock.patch("app.pansou.requests.post", return_value=login) as post, \
         mock.patch("app.pansou.requests.get", return_value=R(MERGED)) as g:
        PanSou(cfg).search("a"); PanSou(cfg).search("b")
        assert post.call_count == 1                                   # token 复用
        assert g.call_args[1]["headers"] == {"Authorization": "Bearer T1"}
    seq = [R({}, 401), R(MERGED)]
    with mock.patch("app.pansou.requests.post", return_value=R({"data": {"token": "T2", "expires_at": 9999999999}})) as post, \
         mock.patch("app.pansou.requests.get", side_effect=seq):
        assert len(PanSou(cfg).search("c")) == 2 and post.call_count == 1   # 401 → 重新登录 → 成功


def test_errors_are_readable():
    P._tokens.clear()
    import requests
    for resp, expect in ((R({}, 401), "认证"), (R({}, 500), "500"), (R(bad_json=True), "JSON")):
        try: search(CFG, resp); assert False, expect
        except PanSouError as e: assert expect in str(e), str(e)
    with mock.patch("app.pansou.requests.get", side_effect=requests.ConnectionError("refused")):
        try: PanSou(CFG).search("a"); assert False
        except PanSouError as e: assert "连接 PanSou 失败" in str(e)
    with mock.patch("app.pansou.requests.post", return_value=R({}, 401)):
        try: PanSou({**CFG, "username": "u", "password": "bad"}).ping(); assert False
        except PanSouError as e: assert "用户名或密码" in str(e)
    try: PanSou({}).search("a"); assert False
    except PanSouError as e: assert "地址" in str(e)


# ---------------------------------------------------------------- 分享内容检测
G = 1024 ** 3


def test_inspect_share_summarizes_episodes_quality_size():
    store, eng = env()
    files = [(f"庆余年.S02E{i:02d}.2160p.HDR.mkv", 2 * G) for i in range(1, 7)] + [("花絮.mp4", G // 2), ("封面.jpg", 1)]
    fq, _ = mk(eng, {"aaa": {"files": files}}, ["aaa"])
    r = eng.inspect_share("https://pan.quark.cn/s/aaa?pwd=x")
    assert r["ok"] and r["videos"] == 7 and r["eps"] == "S02E01-E06" and r["ep_count"] == 6 and r["seasons"] == [2]
    assert r["quality"] == ["4K", "HDR"] and r["unparsed"] == 1 and r["size_gb"] == 12.5
    assert r["sample"][0].startswith("庆余年.S02E01")


def test_inspect_dead_link_cached_but_network_error_not():
    store, eng = env()
    shares = {"dead": {"status": 400, "msg": "分享已取消"}, "net": {"status": 500, "msg": "timeout"}, "ok": {"files": ["S01E01.mp4"]}}
    fq, _ = mk(eng, shares, ["ok"])
    assert eng.inspect_share("https://pan.quark.cn/s/dead") == {"ok": False, "error": "分享已取消"}
    try: eng.inspect_share("https://pan.quark.cn/s/net"); assert False
    except Exception as e: assert "网络异常" in str(e)
    assert "https://pan.quark.cn/s/net" not in eng._inspect_cache        # 网络抖动不缓存，下次重新检测
    n = len(fq.saves); eng.inspect_share("https://pan.quark.cn/s/ok"); eng.inspect_share("https://pan.quark.cn/s/ok")
    assert len(fq.saves) == n                                              # 只检测，绝不转存
    try: eng.inspect_share("随便一段文字"); assert False
    except ValueError: pass


# ---------------------------------------------------------------- HTTP 接口
def test_api_search_inspect_and_settings():
    os.environ["WEBUI_PASSWORD"] = "pw"
    from app.main import create_app
    store, eng = env()
    fq, _ = mk(eng, {"aaa": {"files": ["S01E01.mp4", "S01E02.mp4"]}}, ["aaa"])
    c = create_app(store, eng).test_client()
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    r = c.get("/api/pansou/search?kw=x", headers=h)
    assert r.status_code == 400 and "资源搜索还没有启用" in r.get_json()["error"]
    assert c.get("/api/pansou/search?kw=", headers=h).status_code == 400
    # 保存设置：密码不回显
    s = c.put("/api/settings/pansou", json={"enabled": True, "url": "http://ps:8888", "username": "u", "password": "secret", "src": "tg"}, headers=h).get_json()
    assert s["enabled"] and s["password"] == "" and s["password_set"] and s["src"] == "tg" and s["username"] == "u"
    with mock.patch("app.pansou.requests.post", return_value=R({"token": "t", "expires_at": 9999999999})), \
         mock.patch("app.pansou.requests.get", return_value=R(MERGED)):
        got = c.get("/api/pansou/search?kw=庆余年", headers=h).get_json()
        assert [i["pwd_id"] for i in got["items"]] == ["aaa111", "bbb222"]
        t = c.post("/api/settings/pansou/test", json={"url": "http://ps:8888", "username": "u", "password": ""}, headers=h)
        assert t.status_code == 200 and "连接成功" in t.get_json()["msg"] and "认证通过" in t.get_json()["msg"]   # 密码留空 = 用已保存的
    with mock.patch("app.pansou.requests.get", side_effect=__import__("requests").ConnectionError("refused")):
        t = c.post("/api/settings/pansou/test", json={"url": "http://ps:8888"}, headers=h)
        assert t.status_code == 400 and "连接 PanSou 失败" in t.get_json()["msg"]
    ins = c.post("/api/pansou/inspect", json={"url": "https://pan.quark.cn/s/aaa"}, headers=h).get_json()
    assert ins["ok"] and ins["eps"] == "S01E01-E02"
    assert c.post("/api/pansou/inspect", json={"url": "nope"}, headers=h).status_code == 400
    assert c.get("/api/pansou/search?kw=x").status_code == 401
