# -*- coding: utf-8 -*-
"""飞牛影视：签名、与 Emby 并存的合并客户端、媒体库管理（飞牛版）。"""
import hashlib
import re

from app.fnmedia import FnMedia, Multi, norm_host
from tests.test_layout_aliases import make_client
from tests.test_library import QFake


def test_host_and_authx():
    assert norm_host("192.168.1.2:5666/") == "http://192.168.1.2:5666/v"
    assert norm_host("http://a:5666/v") == "http://a:5666/v"
    c = FnMedia("http://a:5666", "u", "p")
    ax = c._authx("/api/v1/sys/version", None)
    m = re.fullmatch(r"nonce=(\d{6})&timestamp=(\d+)&sign=([0-9a-f]{32})", ax)
    assert m
    n, ts, sign = m.groups()
    h = hashlib.md5(b"").hexdigest()
    exp = hashlib.md5("_".join(["NDzZTVxnRKP8Z0jXg1VAMonaG8akvh", "/v/api/v1/sys/version", n, ts, h,
                                "16CCEB3D-AB42-077D-36A1-F355324E4237"]).encode()).hexdigest()
    assert sign == exp


class FnFake(FnMedia):
    def __init__(self):
        super().__init__("http://a:5666", "u", "p")
        self.token = "t"
        self.deleted, self.scanned = [], []
        self.tv = [{"guid": "g1", "type": "TV", "title": "庆余年", "release_date": "2019-11-26", "trim_id": "tt1", "create_time": 1700000000000},
                   {"guid": "g2", "type": "TV", "title": "迅雷剧", "release_date": "2020-01-01", "trim_id": "tt2"}]
        self.sea = [{"guid": "s1", "season_number": 1}]
        self.eps = [{"guid": "e1", "season_number": 1, "episode_number": 1}, {"guid": "e2", "season_number": 1, "episode_number": 2}]

    def call(self, api, **kw):
        if api == "/item/list":
            return {"list": self.tv, "total": len(self.tv)}
        if api == "/search/list":
            return self.tv
        if api.startswith("/item/") and kw.get("method") == "delete":
            self.deleted.append(api.split("/")[-1]); return True
        if api.startswith("/item/"):
            return next((x for x in self.tv if x["guid"] == api.split("/")[-1]), None)
        if api.startswith("/season/list"):
            return self.sea
        if api.startswith("/episode/list"):
            return self.eps
        if api.startswith("/mdb/scan/"):
            self.scanned.append(api); return True
        if api == "/mdb/scanall":
            self.scanned.append("all"); return True
        if api == "/mdb/list":
            return [{"guid": "L9", "name": "飞牛剧集", "category": "TV", "dir_list": ["/vol1/tv"]}]
        raise AssertionError(api)


def test_fn_client_basics():
    f = FnFake()
    assert f.tmdb_ids("Series") == {1, 2}
    assert f.find_series("庆余年", 1) == ("g1", "庆余年")
    assert f.find_series("庆余年第二季")[0] == "g1"
    assert f.episodes("g1") == {(1, 1), (1, 2)}
    assert f.libraries()[0]["locations"] == ["/vol1/tv"]


class EmbyStub:
    base = "e"
    def tmdb_ids(self, kind="Series", ttl=300): return {99}
    def find_series(self, n, t=None): return ("7", "庆余年") if n == "庆余年" else (None, None)
    def episodes(self, sid): return {(1, 3)}
    def refresh(self, sid=None): self.r = sid


def test_multi_union():
    e, f = EmbyStub(), FnFake()
    m = Multi(e, f)
    assert m.tmdb_ids("Series") == {1, 2, 99}
    tok, name = m.find_series("庆余年", 1)
    assert tok == "emby:7|fn:g1" and name == "庆余年"
    assert m.episodes(tok) == {(1, 1), (1, 2), (1, 3)}
    assert m.episodes("7") == {(1, 3)}        # 纯 ID = Emby
    m.refresh(tok)
    assert e.r == "7" and f.scanned == ["all"]
    tok2, _ = m.find_series("没有这部")
    assert tok2 is None


def test_engine_coexist():
    store, eng, c, h = make_client()
    del eng.emby   # make_client 把它替换成了 lambda，这里测真实的
    store.data["emby"].update(enabled=False, url="", api_key="")
    assert eng.emby() is None
    store.data["fnmedia"].update(enabled=True, url="http://a:5666", username="u", password="p")
    assert eng.emby().__class__.__name__ == "FnMedia"
    store.data["emby"].update(enabled=True, url="http://e", api_key="k")
    assert eng.emby().__class__.__name__ == "Multi" and eng.emby_only() is not None
    assert eng.media_cfg() is store.data["emby"]


def setup():
    store, eng, c, h = make_client()
    q, f = QFake(), FnFake()
    q.tree["/影视/剧集"] = ("dir", ["庆余年", "别的"])
    eng.quark = lambda: q
    eng.fn = lambda: f
    r = c.put("/api/library/scopes", json={"scopes": [{"name": "飞牛夸克", "kind": "tv", "server": "fn", "lib_id": "L9",
              "quark_path": "/影视/剧集"}]}, headers=h)
    assert r.status_code == 200, r.get_data(as_text=True)
    sc = r.get_json()["scopes"][0]
    assert sc["server"] == "fn"
    return store, eng, c, h, q, f, sc["id"]


def test_fn_scope_validation():
    store, eng, c, h = make_client()
    assert c.put("/api/library/scopes", json={"scopes": [{"server": "fn", "lib_id": "", "quark_path": "/x/y"}]}, headers=h).status_code == 400
    assert c.put("/api/library/scopes", json={"scopes": [{"server": "fn", "lib_id": "L", "quark_path": "/x/y"}]}, headers=h).status_code == 200


def test_fn_list_maps_by_name():
    store, eng, c, h, q, f, sid = setup()
    items = c.get(f"/api/library/items?scope={sid}", headers=h).get_json()["items"]
    names = {i["name"]: i for i in items}
    assert "庆余年" in names and names["庆余年"]["top"] == "庆余年" and names["庆余年"]["server"] == "fn"
    assert names["庆余年"]["added"].startswith("2023-")
    d = c.get(f"/api/library/detail?scope={sid}&id=g1", headers=h).get_json()
    assert d["in_quark"] and d["tree"]


def test_fn_delete_whole_and_partial():
    store, eng, c, h, q, f, sid = setup()
    r = c.post("/api/library/delete", json={"scope": sid, "id": "g1", "paths": ["第1季/E01.mkv"]}, headers=h).get_json()
    assert not r["whole"] and f.deleted == ["e1"] and q.deleted == ["/影视/剧集/庆余年/第1季/E01.mkv"]
    assert f.scanned
    r = c.post("/api/library/delete", json={"scope": sid, "id": "g1", "paths": []}, headers=h).get_json()
    assert r["whole"] and f.deleted[-1] == "g1" and "飞牛" in r["emby"]


def test_fn_refuses_unmapped():
    store, eng, c, h, q, f, sid = setup()
    f.tv.append({"guid": "g3", "type": "TV", "title": "完全不同的名字", "release_date": "2020"})
    r = c.post("/api/library/delete", json={"scope": sid, "id": "g3", "paths": []}, headers=h)
    assert r.status_code == 400 and "g3" not in f.deleted


def test_peer_sync_emby_to_fn_and_back():
    from tests.test_library import EFake
    store, eng, c, h = make_client()
    q, f, e = QFake(), FnFake(), EFake()
    q.tree["/影视/剧集"] = ("dir", ["庆余年", "别的"])
    eng.quark = lambda: q; eng.fn = lambda: f; eng.emby = eng.emby_only = lambda: e
    r = c.put("/api/library/scopes", json={"scopes": [
        {"name": "E", "kind": "tv", "server": "emby", "lib_id": "L1", "emby_path": "/mnt/quark/剧集", "quark_path": "/影视/剧集"},
        {"name": "F", "kind": "tv", "server": "fn", "lib_id": "L9", "quark_path": "/影视/剧集"}]}, headers=h)
    sc = {s["server"]: s["id"] for s in r.get_json()["scopes"]}
    # Emby 里整部删 → 飞牛同步
    r = c.post("/api/library/delete", json={"scope": sc["emby"], "id": "s1", "paths": []}, headers=h).get_json()
    assert e.deleted == ["s1"] and f.deleted == ["g1"] and "飞牛" in r["peers"][0]
    # 飞牛里整部删 → Emby 同步
    e.deleted.clear(); f.deleted.clear(); q.deleted.clear()
    r = c.post("/api/library/delete", json={"scope": sc["fn"], "id": "g1", "paths": []}, headers=h).get_json()
    assert f.deleted == ["g1"] and e.deleted == ["s1"] and "Emby" in r["peers"][0]


def test_peer_skipped_when_quark_dirs_differ():
    from tests.test_library import EFake
    store, eng, c, h = make_client()
    q, f, e = QFake(), FnFake(), EFake()
    q.tree["/影视/剧集"] = ("dir", ["庆余年"])
    eng.quark = lambda: q; eng.fn = lambda: f; eng.emby = eng.emby_only = lambda: e
    r = c.put("/api/library/scopes", json={"scopes": [
        {"name": "E", "kind": "tv", "server": "emby", "lib_id": "L1", "emby_path": "/mnt/quark/剧集", "quark_path": "/影视/剧集"},
        {"name": "F", "kind": "tv", "server": "fn", "lib_id": "L9", "quark_path": "/别处/剧集"}]}, headers=h)
    sc = {s["server"]: s["id"] for s in r.get_json()["scopes"]}
    r = c.post("/api/library/delete", json={"scope": sc["emby"], "id": "s1", "paths": []}, headers=h).get_json()
    assert r["peers"] == [] and f.deleted == []


def test_fn_image_route_uses_fn_server():
    store, eng, c, h = make_client()
    f = FnFake()
    f.image = lambda guid, width=300: (b"IMG", "image/jpeg")
    eng.fn = lambda: f
    r = c.get("/api/library/img/g1?s=fn", headers=h)
    assert r.status_code == 200 and r.data == b"IMG"


def test_fn_category_layer_maps_to_show_folder_not_category():
    """夸克里是 /影视/剧集/国产剧/庆余年 (2019)：删除只能动这部剧的文件夹，绝不能动「国产剧」整个目录。"""
    store, eng, c, h = make_client()
    q, f = QFake(), FnFake()
    q.tree = {"/影视/剧集": ("dir", ["国产剧"]), "/影视/剧集/国产剧": ("dir", ["庆余年 (2019)", "别的剧 (2020)"]),
              "/影视/剧集/国产剧/庆余年 (2019)": ("dir", ["Season 1"]), "/影视/剧集/国产剧/别的剧 (2020)": ("dir", []),
              "/影视/剧集/国产剧/庆余年 (2019)/Season 1": ("dir", ["E01.mkv"]),
              "/影视/剧集/国产剧/庆余年 (2019)/Season 1/E01.mkv": ("file", 5)}
    eng.quark = lambda: q; eng.fn = lambda: f
    sc = c.put("/api/library/scopes", json={"scopes": [{"name": "F", "kind": "tv", "server": "fn", "lib_id": "L9",
               "quark_path": "/影视/剧集"}]}, headers=h).get_json()["scopes"][0]["id"]
    items = c.get(f"/api/library/items?scope={sc}", headers=h).get_json()["items"]
    top = {i["name"]: i["top"] for i in items}
    assert top["庆余年"] == "国产剧/庆余年 (2019)"
    d = c.get(f"/api/library/detail?scope={sc}&id=g1", headers=h).get_json()
    assert [n["name"] for n in d["tree"]] == ["Season 1"]          # 只有这部剧自己的内容
    r = c.post("/api/library/delete", json={"scope": sc, "id": "g1", "paths": []}, headers=h).get_json()
    assert q.deleted == ["/影视/剧集/国产剧/庆余年 (2019)"] and r["whole"]


def test_fn_subscription_savepath_with_season_suffix():
    store, eng, c, h = make_client()
    q, f = QFake(), FnFake()
    q.tree = {"/影视/剧集": ("dir", ["国产剧"]), "/影视/剧集/国产剧": ("dir", ["不同的名字 (2019)"]),
              "/影视/剧集/国产剧/不同的名字 (2019)": ("dir", [])}
    eng.quark = lambda: q; eng.fn = lambda: f
    store.data["tasks"].append({"id": "t1", "name": "庆余年", "tmdb_id": 1, "media": "tv", "kind": "subscription",
                                "savepath": "/影视/剧集/国产剧/不同的名字 (2019)/Season 1"})
    sc = c.put("/api/library/scopes", json={"scopes": [{"name": "F", "kind": "tv", "server": "fn", "lib_id": "L9",
               "quark_path": "/影视/剧集"}]}, headers=h).get_json()["scopes"][0]["id"]
    items = c.get(f"/api/library/items?scope={sc}", headers=h).get_json()["items"]
    assert items[0]["top"] == "国产剧/不同的名字 (2019)"


def test_fn_flat_show_folder_without_season_dir():
    """剧名文件夹下直接是视频文件（没有 Season 层），不带年份也能认作一部剧；单集删除按文件名里的季集号对应。"""
    store, eng, c, h = make_client()
    q, f = QFake(), FnFake()
    q.tree = {"/影视/剧集": ("dir", ["国产剧"]), "/影视/剧集/国产剧": ("dir", ["庆余年"]),
              "/影视/剧集/国产剧/庆余年": ("dir", ["庆余年.S01E01.mkv", "庆余年.S01E02.mkv"]),
              "/影视/剧集/国产剧/庆余年/庆余年.S01E01.mkv": ("file", 5), "/影视/剧集/国产剧/庆余年/庆余年.S01E02.mkv": ("file", 5)}
    eng.quark = lambda: q; eng.fn = lambda: f
    sc = c.put("/api/library/scopes", json={"scopes": [{"name": "F", "kind": "tv", "server": "fn", "lib_id": "L9",
               "quark_path": "/影视/剧集"}]}, headers=h).get_json()["scopes"][0]["id"]
    items = c.get(f"/api/library/items?scope={sc}", headers=h).get_json()["items"]
    assert items[0]["top"] == "国产剧/庆余年"
    r = c.post("/api/library/delete", json={"scope": sc, "id": "g1", "paths": ["庆余年.S01E02.mkv"]}, headers=h).get_json()
    assert not r["whole"] and f.deleted == ["e2"]
    assert q.deleted == ["/影视/剧集/国产剧/庆余年/庆余年.S01E02.mkv"]
