# -*- coding: utf-8 -*-
"""迅雷云盘：链接识别、适配器（假的迅雷接口）、引擎端到端、登录 token 轮换、cURL 导入。"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp())

import pytest
from app import xunlei as X
from app.engine import Engine
from app.parser import parse_share_text
from app.quark import QuarkError
from app.store import Store
from tests.test_layout_aliases import make_client


class FakeXunlei(X.Xunlei):
    """把 request 换成内存里的迅雷：分享 = {share_id: [{id,name,kind,size,children}]}；自己的网盘是个目录树。"""
    def __init__(self, store, shares):
        super().__init__(store)
        self.shares, self.calls = shares, []
        self.files = {}                      # id -> {id, name, kind, parent, size}
        self._n = 0

    def _find_share(self, sid, node_id):
        def walk(items):
            for it in items:
                if it["id"] == node_id:
                    return it
                if it.get("children"):
                    r = walk(it["children"])
                    if r:
                        return r
        return walk(self.shares[sid]["files"])

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url == X.URL_USER_ME:
            return {"name": "测试用户"}
        if url == X.URL_SHARE:
            p = kw["params"]
            s = self.shares.get(p["share_id"])
            if s is None:
                raise X.XunleiError("HTTP 404：share_not_found", 404, {"error": "share_not_found"})
            if s.get("net"):
                raise X.NetworkError("网络异常：timeout")
            if not p.get("parent_id") and not p.get("page_token") and p.get("pass_code_token"):
                return {"share_status": "OK", "files": [], "next_page_token": ""}      # 真实迅雷的表现：顶层第一页带了 token 就读不到文件
            if s.get("need_pwd") and p["pass_code"] != s["need_pwd"] and not p.get("pass_code_token"):
                raise X.XunleiError("HTTP 400：pass_code_error", 400, {})
            if p.get("parent_id"):
                return {"share_status": "OK", "files": [], "next_page_token": ""}      # 真实表现（推测）：/share 带 parent_id 读不到文件夹内容
            items = s["files"]
            return {"share_status": "OK", "pass_code_token": "PCT", "next_page_token": "",
                    "files": [{k: v for k, v in it.items() if k != "children"} for it in items]}
        if url == X.URL_SHARE_DETAIL:
            p = kw["params"]
            assert "pass_code" not in p and p["pass_code_token"] == "PCT"
            node = self._find_share(p["share_id"], p["parent_id"])
            return {"share_status": "OK", "next_page_token": "",
                    "files": [{k: v for k, v in it.items() if k != "children"} for it in node["children"]]}
        if url == X.URL_FILES and method == "GET":
            parent = kw["params"]["parent_id"]
            return {"files": [dict(f) for f in self.files.values() if f["parent_id"] == parent], "next_page_token": ""}
        if url == X.URL_FILES and method == "POST":
            self._n += 1
            fid = f"my{self._n}"
            self.files[fid] = {"id": fid, "name": kw["json"]["name"], "kind": "drive#folder", "parent_id": kw["json"]["parent_id"], "size": 0}
            return {"file": {"id": fid}}
        if url.startswith(X.URL_FILES + "/") and method == "PATCH":
            self.files[url.rsplit("/", 1)[1]]["name"] = kw["json"]["name"]
            return {}
        if url == X.URL_RESTORE:
            b = kw["json"]
            assert b["pass_code_token"] == "PCT", "转存必须带 pass_code_token"
            for fid in b["file_ids"]:
                src = self._find_share(b["share_id"], fid)
                self._n += 1
                self.files[f"my{self._n}"] = {"id": f"my{self._n}", "name": src["name"], "kind": "drive#file", "parent_id": b["parent_id"], "size": src["size"]}
            return {"restore_task_id": "T1"}
        if url.startswith(X.URL_TASKS):
            return {"phase": "PHASE_TYPE_COMPLETE"}
        raise AssertionError("没模拟的接口：%s %s" % (method, url))


def _f(i, name, size=800 * 1024 * 1024):
    return {"id": i, "name": name, "kind": "drive#file", "size": size}


def _d(i, name, children):
    return {"id": i, "name": name, "kind": "drive#folder", "size": 0, "children": children}


@pytest.fixture
def env():
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    store.data["xunlei"].update(refresh_token="R", access_token="A", expires_at=9999999999, client_id="cid")
    eng = Engine(store)
    return store, eng


# ---------------------------------------------------------------- 链接
def test_parse_both_drives_and_passcode():
    t = "夸克 https://pan.quark.cn/s/abc123 提取码：x1y2\n迅雷 https://pan.xunlei.com/s/VOabc_-9?pwd=k9k9# 完"
    r = parse_share_text(t)
    assert [(x["drive"], x["pwd_id"], x["passcode"]) for x in r] == [("quark", "abc123", "x1y2"), ("xunlei", "VOabc_-9", "k9k9")]
    assert r[1]["url"] == "https://pan.xunlei.com/s/VOabc_-9?pwd=k9k9"
    assert X.Xunlei.extract_url(r[1]["url"]) == ("VOabc_-9", "k9k9", "0", [])


def test_normalize_sets_drive_rejects_mixed_and_keeps_mirror(env):
    store, eng = env
    eng._x = FakeXunlei(store, {"VO1": {"files": [_f("F1", "a.mp4")]}})
    t = eng.create_task({"kind": "monitor", "name": "迅", "savepath": "/x/a", "links_text": "https://pan.xunlei.com/s/VO1?pwd=abcd",
                         "mirror": {"enabled": True}}, save_existing=False)
    assert t["drive"] == "xunlei" and t["mirror"]["enabled"] is True
    q = eng.normalize({"kind": "monitor", "name": "夸", "savepath": "/x/b", "links_text": "https://pan.quark.cn/s/q1"})
    assert q["drive"] == "quark"
    with pytest.raises(ValueError):
        eng.normalize({"links_text": "https://pan.quark.cn/s/q1\nhttps://pan.xunlei.com/s/VO1", "savepath": "/x/c", "name": "混"})


# ---------------------------------------------------------------- 适配器
def test_adapter_shapes_ancestors_and_restore(env):
    store, _ = env
    shares = {"VO1": {"files": [_d("D1", "S01", [_d("D2", "sub", [_f("F3", "第03集.mp4")]), _f("F1", "第01集.mp4")]), _f("F9", "说明.txt", 10)]}}
    x = FakeXunlei(store, shares)
    r = x.get_stoken("VO1", "abcd")
    assert r == {"status": 200, "data": {"stoken": "PCT"}}
    top = x.get_detail("VO1", "PCT", "0")
    assert {i["file_name"]: i["dir"] for i in top} == {"S01": True, "说明.txt": False}
    inner = x.get_detail("VO1", "PCT", "D1")
    deep = x.get_detail("VO1", "PCT", "D2")
    assert [i["fid"] for i in inner] == ["D2", "F1"] and deep[0]["size"] == 800 * 1024 * 1024
    dest = x.ensure_dir("/剧/某剧")
    assert x.ensure_dir("/") == "0" and x.get_fids(["/剧/某剧"])[0]["fid"] == dest and x.get_fids(["/没有"]) == []
    tid = x.save_file(["F1", "F3", "F9"], ["", "", ""], dest, "VO1", "PCT")
    x.wait_task(tid)
    restores = [c[2]["json"] for c in x.calls if c[1] == X.URL_RESTORE]
    assert sorted((tuple(b["ancestor_ids"]), tuple(b["file_ids"])) for b in restores) == [
        ((), ("F9",)), (("D1",), ("F1",)), (("D1", "D2"), ("F3",))]      # 按祖先目录分组，各带自己的 ancestor_ids
    got = x.ls_dir(dest)
    assert sorted(i["file_name"] for i in got) == ["第01集.mp4", "第03集.mp4", "说明.txt"]
    assert x.rename(got[0]["fid"], "新名.mp4") == {"code": 0}


def test_adapter_error_mapping(env):
    store, _ = env
    x = FakeXunlei(store, {"BAD": {"files": [], "net": True}})
    assert x.get_stoken("BAD", "")["status"] == 500                         # 网络问题：和夸克一样用 500 表示，不算链接失效
    assert x.get_stoken("NOPE", "")["status"] == 400 and "share_not_found" in x.get_stoken("NOPE", "")["message"]
    x.files["a"] = {"id": "a", "name": "x", "kind": "drive#file", "parent": "", "size": 1}
    x.request = lambda m, u, **kw: x.calls.append((m, u, kw)) or {}
    assert x.delete(["a"])["code"] == 0
    m, u, kw = x.calls[-1]
    assert m == "POST" and u.endswith(":batchTrash") and kw["json"]["ids"] == ["a"]


# ---------------------------------------------------------------- 引擎端到端
def test_engine_saves_from_xunlei_share_and_renames(env):
    store, eng = env
    shares = {"VO1": {"files": [_d("D1", "某剧.S01", [_f("F1", "某剧.S01E01.mp4"), _f("F2", "某剧.S01E02.mp4")])]}}
    x = FakeXunlei(store, shares)
    eng._x = x
    eng.quark = lambda: (_ for _ in ()).throw(AssertionError("迅雷任务不该碰夸克"))
    eng.emby_only = lambda: None
    eng.fn = lambda: None
    t = eng.create_task({"kind": "monitor", "name": "某剧", "savepath": "/剧/某剧", "links_text": "https://pan.xunlei.com/s/VO1?pwd=abcd",
                         "after": "none"}, save_existing=True)
    eng.run_task(t["id"], True)
    dest = x.get_fids(["/剧/某剧"])[0]["fid"]
    names = sorted(i["file_name"] for i in x.ls_dir(dest))
    assert len(names) == 2 and all("E0" in n or "e0" in n.lower() for n in names), names
    assert t["last_status"] in ("ok", "saved", "success") or t["saved_total"] == 2


def test_dead_share_is_marked_after_failures_not_network(env):
    store, eng = env
    x = FakeXunlei(store, {"VO1": {"files": [], "net": True}})
    eng._x = x
    t = eng.create_task({"kind": "monitor", "name": "某剧", "savepath": "/剧/某剧", "links_text": "https://pan.xunlei.com/s/VO1"}, save_existing=True)
    eng.run_task(t["id"], True)
    assert not t["links"][0]["ban"] and t["links"][0]["fails"] == 0         # 网络错误不计入失效


# ---------------------------------------------------------------- 登录
def test_refresh_rotates_refresh_token_and_saves(env):
    store, _ = env
    store.data["xunlei"].update(access_token="", expires_at=0, refresh_token="OLD")
    x = X.Xunlei(store)

    class R:
        status_code = 200
        text = ""
        def json(self): return {"access_token": "NEW_A", "refresh_token": "NEW_R", "expires_in": 7200}
    posted = {}
    x.session.post = lambda url, json=None, headers=None, timeout=None: posted.update(url=url, body=json) or R()
    x.refresh()
    d = store.data["xunlei"]
    assert (d["access_token"], d["refresh_token"], d["status"]) == ("NEW_A", "NEW_R", "已登录")
    assert posted["body"]["grant_type"] == "refresh_token" and posted["body"]["refresh_token"] == "OLD"


def test_refresh_without_client_id_gives_clear_error(env):
    store, _ = env
    store.data["xunlei"].update(access_token="", expires_at=0, client_id="")
    try:
        X.Xunlei(store).refresh()
        raise AssertionError("应当报错")
    except X.AuthError as e:
        assert "client_id" in str(e)


# ---------------------------------------------------------------- 设置 / 导入
def test_settings_mask_secrets_and_curl_import():
    store, eng, c, h = make_client()
    store.data["xunlei"]["refresh_token"] = "SECRET_R"
    pub = c.get("/api/settings/xunlei", headers=h).get_json()
    assert pub["refresh_token"] == "" and pub["refresh_token_set"] is True
    curl = ("curl 'https://xluser-ssl.xunlei.com/v1/shield/captcha/init' -H 'x-client-id: ' --data-raw "
            "'{\"client_id\":\"CID1\",\"device_id\":\"DEV1\",\"action\":\"GET:/drive/v1/files\",\"meta\":{\"user_id\":\"77\",\"client_version\":\"2.0\",\"package_name\":\"pan.xunlei.com\"}}'")
    r = c.post("/api/xunlei/curl", json={"text": curl}, headers=h)
    assert r.status_code == 200 and r.get_json()["notes"]
    d = store.data["xunlei"]
    assert (d["client_id"], d["device_id"], d["user_id"], d["package_name"]) == ("CID1", "DEV1", "77", "pan.xunlei.com")
    assert d["refresh_token"] == "SECRET_R"                                  # 导入不会动已有的 token
    assert c.post("/api/xunlei/curl", json={"text": "不是 curl"}, headers=h).status_code == 400
    assert c.post("/api/settings/xunlei/test", json={}, headers=h).status_code in (200, 400)


# ---------------------------------------------------------------- 订阅 / PanSou
def test_pansou_searches_the_requested_drive():
    from unittest import mock
    from app.pansou import PanSou
    data = {"merged_by_type": {"xunlei": [{"url": "https://pan.xunlei.com/s/VOx1", "password": "ab12", "note": "某剧 全集", "datetime": "2026-01-02", "source": "tg:c"},
                                          {"url": "https://pan.quark.cn/s/qqq", "password": "", "note": "混进来的夸克", "datetime": "", "source": ""}],
                               "quark": [{"url": "https://pan.quark.cn/s/qq2", "password": "", "note": "夸克的", "datetime": "", "source": ""}]}}

    class R:
        status_code = 200
        def json(self): return data
    with mock.patch("app.pansou.requests.get", return_value=R()) as g:
        xs = PanSou({"url": "http://ps:8888", "src": "all"}).search("某剧", drive="xunlei")
        qs = PanSou({"url": "http://ps:8888", "src": "all"}).search("某剧")
    assert g.call_args_list[0][1]["params"]["cloud_types"] == "xunlei" and g.call_args_list[1][1]["params"]["cloud_types"] == "quark"
    assert [(i["pwd_id"], i["url"]) for i in xs] == [("VOx1", "https://pan.xunlei.com/s/VOx1?pwd=ab12")]      # 只留迅雷链接
    assert [i["pwd_id"] for i in qs] == ["qq2"]


def test_subscription_without_links_keeps_chosen_drive(env):
    store, eng = env
    t = eng.create_task({"kind": "subscription", "name": "某剧", "savepath": "/剧/某剧", "links_text": "", "drive": "xunlei", "season": 1})
    assert t["drive"] == "xunlei" and t["mirror"]["enabled"] is False      # 订阅不用镜像同步
    eng.update_task(t["id"], {"interval": 120})
    assert t["drive"] == "xunlei"                                              # 改别的字段，网盘不变
    assert eng.create_task({"kind": "subscription", "name": "夸克剧", "savepath": "/剧/b", "links_text": ""})["drive"] == "quark"


def test_auto_find_searches_only_the_tasks_drive_and_saves_from_xunlei(env):
    store, eng = env
    shares = {"VOa": {"files": [_d("D1", "某剧.S01", [_f("F%d" % i, "某剧.S01E%02d.mp4" % i) for i in (1, 2, 3)])]}}
    x = FakeXunlei(store, shares)
    eng._x = x
    eng.quark = lambda: (_ for _ in ()).throw(AssertionError("迅雷订阅不该碰夸克"))
    eng.emby_only = lambda: None
    eng.fn = lambda: None
    seen = []

    class PS:
        def search(self, kw, limit=60, drive="quark"):
            seen.append(drive)
            return [{"url": "https://pan.xunlei.com/s/VOa", "pwd_id": "VOa", "passcode": "", "drive": "xunlei",
                     "title": "某剧 第一季 全集", "source": "tg:x", "time": "2026-10-01"}] if drive == "xunlei" else []
    eng.pansou = lambda: PS()
    t = eng.create_task({"kind": "subscription", "name": "某剧", "savepath": "/剧/某剧", "links_text": "", "drive": "xunlei",
                         "season": 1, "total_episodes": 3, "auto_find": True})
    eng.auto_find(t["id"], force=True)
    assert set(seen) == {"xunlei"} and [l["pwd_id"] for l in t["links"]] == ["VOa"] and t["links"][0]["drive"] == "xunlei"
    dest = x.get_fids(["/剧/某剧"])[0]["fid"]
    assert len(x.ls_dir(dest)) == 3                                            # 搜到 → 检测 → 加链接 → 转存，全程走迅雷


def test_invalid_share_id_message_is_readable(env):
    store, _ = env

    class X2(FakeXunlei):
        def request(self, method, url, **kw):
            if url == X.URL_SHARE:
                raise X.XunleiError('HTTP 400：{"error_description": "请求参数错误", "detail": "share_id can not be empty or share_id invalid"}', 400, {})
            return super().request(method, url, **kw)
    r = X2(store, {}).get_stoken("VOdead", "")
    assert r["status"] == 400 and "分享不存在或已被取消" in r["message"]


def test_top_level_read_uses_passcode_without_token_and_lists_files(env):
    store, _ = env
    x = FakeXunlei(store, {"VOp": {"files": [_f("F1", "某剧.S01E01.mp4")], "need_pwd": "k9k9"}})
    assert x.get_stoken("VOp", "wrong")["status"] == 400
    assert x.get_stoken("VOp", "k9k9")["status"] == 200
    assert [i["file_name"] for i in x.get_detail("VOp", "PCT", "0")] == ["某剧.S01E01.mp4"]     # 不能是「没有文件」


def test_diagnose_lists_each_step_without_tokens(env):
    store, _ = env
    x = FakeXunlei(store, {"VOd": {"files": [_d("D1", "假面美颜", [_f("F1", "01.mp4")])]}})
    r = x.diagnose("https://pan.xunlei.com/s/VOd?pwd=k9k9")
    assert r["share_id"] == "VOd" and r["has_passcode"] and [s["ok"] for s in r["steps"]] == [True, True, True]
    assert r["steps"][0]["files"][0]["kind"] == "drive#folder" and r["steps"][1]["folder"] == "假面美颜" and r["steps"][1]["file_count"] == 1 and r["steps"][2]["file_count"] == 0
    assert "PCT" not in json.dumps(r) and "A" != r["steps"][0].get("pass_code_token")
    assert x.diagnose("没有链接")["steps"][0]["ok"] is False


def test_restore_retries_without_ancestors_when_rejected(env):
    store, _ = env

    class X3(FakeXunlei):
        def request(self, method, url, **kw):
            if url == X.URL_RESTORE and kw["json"]["ancestor_ids"]:
                self.calls.append((method, url, kw))
                raise X.XunleiError("HTTP 400：invalid ancestor_ids", 400, {})
            return super().request(method, url, **kw)
    x = X3(store, {"VOr": {"files": [_d("D1", "S01", [_f("F1", "a.mp4")])]}})
    x.get_stoken("VOr", "")
    x.get_detail("VOr", "PCT", "0"); x.get_detail("VOr", "PCT", "D1")
    dest = x.ensure_dir("/剧/x")
    x.save_file(["F1"], [""], dest, "VOr", "PCT")
    sent = [c[2]["json"]["ancestor_ids"] for c in x.calls if c[1] == X.URL_RESTORE]
    assert sent == [["D1"], []] and [i["file_name"] for i in x.ls_dir(dest)] == ["a.mp4"]


def test_mirror_sync_on_xunlei_saves_then_deletes_removed_file(env):
    store, eng = env
    shares = {"VO1": {"files": [_f("F1", "某剧.S01E01.mp4"), _f("F2", "某剧.S01E02.mp4")]}}
    x = FakeXunlei(store, shares)
    eng._x = x
    eng.quark = lambda: (_ for _ in ()).throw(AssertionError("迅雷任务不该碰夸克"))
    eng.emby_only = lambda: None
    eng.fn = lambda: None
    trashed = []
    orig = x.request
    def req(method, url, **kw):
        if url.endswith(":batchTrash"):
            trashed.extend(kw["json"]["ids"])
            for i in kw["json"]["ids"]:
                x.files.pop(i, None)
            return {}
        return orig(method, url, **kw)
    x.request = req
    t = eng.create_task({"kind": "monitor", "name": "某剧", "savepath": "/剧/某剧", "links_text": "https://pan.xunlei.com/s/VO1?pwd=abcd",
                         "after": "none", "mirror": {"enabled": True, "dry_run": False, "grace": 1}}, save_existing=True)
    assert t["mirror"]["enabled"] is True
    eng.run_task(t["id"], True)
    dest = x.get_fids(["/剧/某剧"])[0]["fid"]
    assert len(x.ls_dir(dest)) == 2
    shares["VO1"]["files"] = [_f("F1", "某剧.S01E01.mp4"), _f("F3", "某剧.S01E03.mp4")]     # 分享里删了 E02、新增 E03
    for _ in range(3):
        eng.run_task(t["id"], True)
    names = sorted(i["file_name"] for i in x.ls_dir(dest))
    assert any("E03" in n or "e03" in n.lower() for n in names), names
    assert not any("E02" in n or "e02" in n.lower() for n in names), names
    assert trashed
