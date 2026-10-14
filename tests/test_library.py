# -*- coding: utf-8 -*-
"""媒体库管理：范围限制、夸克 + Emby 同步删除、订阅处理。"""
from tests.test_layout_aliases import make_client


class QFake:
    def __init__(self):
        self.tree = {"/影视/剧集/庆余年": ("dir", ["第1季", "第2季"]), "/影视/剧集/庆余年/第1季": ("dir", ["E01.mkv", "E02.mkv"]),
                     "/影视/剧集/庆余年/第2季": ("dir", []), "/影视/剧集/庆余年/第1季/E01.mkv": ("file", 5),
                     "/影视/剧集/庆余年/第1季/E02.mkv": ("file", 5), "/影视/剧集/别的": ("dir", [])}
        self.deleted = []

    def get_fids(self, paths):
        return [{"fid": p, "dir": self.tree[p][0] == "dir", "size": 0} for p in paths if p in self.tree]

    def ls_dir(self, fid):
        kind, kids = self.tree[fid]
        return [{"fid": fid + "/" + k, "file_name": k, "dir": self.tree[fid + "/" + k][0] == "dir",
                 "size": self.tree[fid + "/" + k][1] if self.tree[fid + "/" + k][0] == "file" else 0} for k in kids] \
            if kind == "dir" and kids and isinstance(kids[0], str) and (fid + "/" + kids[0]) in self.tree else []

    def delete(self, fids):
        self.deleted += fids


class EFake:
    def __init__(self, fail=False):
        self.deleted, self.refreshed, self.fail = [], [], fail
        self.items = {
            "s1": {"Id": "s1", "Name": "庆余年", "Type": "Series", "Path": "/mnt/quark/剧集/庆余年", "ProductionYear": 2019},
            "s2": {"Id": "s2", "Name": "迅雷剧", "Type": "Series", "Path": "/mnt/xunlei/剧集/迅雷剧"},
        }
        self.kids = [{"Id": "e1", "Type": "Episode", "Path": "/mnt/quark/剧集/庆余年/第1季/E01.strm"},
                     {"Id": "e2", "Type": "Episode", "Path": "/mnt/quark/剧集/庆余年/第1季/E02.strm"}]

    def libraries(self): return [{"id": "L1", "name": "剧集", "type": "tvshows", "locations": ["/mnt/quark/剧集", "/mnt/xunlei/剧集"]}]
    def library_items(self, lib_id): return list(self.items.values())
    def item(self, i): return self.items.get(i)
    def children(self, i, kinds): return list(self.kids)
    def delete_item(self, i):
        if self.fail: raise RuntimeError("403")
        self.deleted.append(i)
    def refresh(self, i=None): self.refreshed.append(i)


def setup(fail=False):
    store, eng, c, h = make_client()
    q, e = QFake(), EFake(fail)
    eng.quark = lambda: q; eng.emby = eng.emby_only = lambda: e
    r = c.put("/api/library/scopes", json={"scopes": [{"name": "夸克剧集", "kind": "tv", "lib_id": "L1",
              "emby_path": "/mnt/quark/剧集", "quark_path": "/影视/剧集"}]}, headers=h)
    return store, eng, c, h, q, e, r.get_json()["scopes"][0]["id"]


def test_scope_validation():
    store, eng, c, h = make_client()
    for bad in ({"lib_id": "", "emby_path": "/a/b", "quark_path": "/x"}, {"lib_id": "L", "emby_path": "/", "quark_path": "/x"},
                {"lib_id": "L", "emby_path": "/a/b", "quark_path": "/"}):
        assert c.put("/api/library/scopes", json={"scopes": [bad]}, headers=h).status_code == 400


def test_list_hides_items_outside_scope():
    store, eng, c, h, q, e, sid = setup()
    items = c.get("/api/library/items?scope=" + sid, headers=h).get_json()["items"]
    assert [i["name"] for i in items] == ["庆余年"]          # 迅雷的不显示


def test_detail_tree():
    store, eng, c, h, q, e, sid = setup()
    d = c.get(f"/api/library/detail?scope={sid}&id=s1", headers=h).get_json()
    assert d["in_quark"] and d["tree"][0]["name"] == "第1季"
    assert c.get(f"/api/library/detail?scope={sid}&id=s2", headers=h).status_code == 400   # 范围外拒绝


def test_delete_whole_removes_both_and_subscription():
    store, eng, c, h, q, e, sid = setup()
    t = eng.create_task({"name": "庆余年", "savepath": "/影视/剧集/庆余年", "links_text": "", "kind": "subscription"})
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1"}, headers=h).get_json()
    assert q.deleted == ["/影视/剧集/庆余年"] and e.deleted == ["s1"] and r["subs"] == 1
    assert not store.data["tasks"]


def test_delete_outside_scope_refused():
    store, eng, c, h, q, e, sid = setup()
    assert c.post("/api/library/delete", json={"scope": sid, "id": "s2"}, headers=h).status_code == 400
    assert not q.deleted and not e.deleted


def test_delete_files_partial_and_pause_sub():
    store, eng, c, h, q, e, sid = setup()
    t = eng.create_task({"name": "庆余年", "savepath": "/影视/剧集/庆余年", "links_text": "", "kind": "subscription"})
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季/E01.mkv"]}, headers=h).get_json()
    assert q.deleted == ["/影视/剧集/庆余年/第1季/E01.mkv"]
    assert e.deleted == ["e1"]                       # strm 与 mkv 同名，按文件名对上
    assert store.data["tasks"][0]["enabled"] is False


def test_path_traversal_rejected():
    store, eng, c, h, q, e, sid = setup()
    assert c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["../别的"]}, headers=h).status_code == 400


def test_emby_failure_falls_back_to_refresh():
    store, eng, c, h, q, e, sid = setup(fail=True)
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1"}, headers=h).get_json()
    assert q.deleted and e.refreshed == ["s1"] and "刷新" in r["emby"]


def test_image_proxy_and_detail_fields():
    store, eng, c, h, q, e, sid = setup()
    e.image = lambda i: (b"\x89PNG", "image/png")
    r = c.get("/api/library/img/s1", headers=h)
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert c.get("/api/library/img/bad%2Fid", headers=h).status_code == 404
    e.items["s1"]["DateCreated"] = "2026-10-01T08:30:00.0000000Z"
    d = c.get(f"/api/library/detail?scope={sid}&id=s1", headers=h).get_json()
    assert d["added_at"] == "2026-10-01 08:30:00" and d["year"] == 2019


def test_partial_delete_when_quark_cannot_resolve_file_paths():
    store, eng, c, h, q, e, sid = setup()
    orig = q.get_fids
    q.get_fids = lambda paths: [f for f in orig(paths) if f["dir"]]       # 夸克按「文件路径」查不到，只能查文件夹
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季/E02.mkv"]}, headers=h)
    assert r.status_code == 200 and q.deleted == ["/影视/剧集/庆余年/第1季/E02.mkv"] and e.deleted == ["e2"]


def test_partial_delete_not_found_is_an_error_not_silent():
    store, eng, c, h, q, e, sid = setup()
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季/不存在.mkv"]}, headers=h)
    assert r.status_code == 400 and "没找到" in r.get_json()["error"] and not q.deleted and not e.deleted


def test_local_strm_cleanup():
    import pathlib, tempfile
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    store, eng, c, h, q, e, sid = setup()
    root = tmp_path / "strm"; d = root / "庆余年" / "第1季"; d.mkdir(parents=True)
    for n in ("E01.strm", "E01.nfo", "E02.strm"):
        (d / n).write_text("x")
    sc = eng.library.scopes()[0]; sc["strm_path"] = str(root)
    eng.library.save_scopes([sc])
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季/E01.mkv"]}, headers=h).get_json()
    assert sorted(x.name for x in d.iterdir()) == ["E02.strm"] and "2" in r["local"]
    c.post("/api/library/delete", json={"scope": sid, "id": "s1"}, headers=h)
    assert not (root / "庆余年").exists() and root.exists()


def test_partial_delete_matches_mkv_strm_and_season_episode_numbers():
    store, eng, c, h, q, e, sid = setup()
    q.tree["/影视/剧集/庆余年/Season 1"] = ("dir", ["庆余年 - S01E01.mkv"])
    q.tree["/影视/剧集/庆余年/Season 1/庆余年 - S01E01.mkv"] = ("file", 5)
    e.kids = [{"Id": "k1", "Type": "Episode", "Path": "/mnt/quark/剧集/庆余年/Season 1/庆余年 - S01E01.mkv.strm"},
              {"Id": "k2", "Type": "Episode", "Path": "/mnt/quark/剧集/庆余年/Season 1/别的名字.strm", "ParentIndexNumber": 1, "IndexNumber": 1},
              {"Id": "k3", "Type": "Episode", "Path": "/mnt/quark/剧集/庆余年/Season 1/庆余年 - S01E02.mkv.strm", "ParentIndexNumber": 1, "IndexNumber": 2}]
    c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["Season 1/庆余年 - S01E01.mkv"]}, headers=h)
    assert sorted(e.deleted) == ["k1", "k2"]          # 不会误删第 2 集


def test_selecting_everything_deletes_whole_show():
    store, eng, c, h, q, e, sid = setup()
    eng.create_task({"name": "庆余年", "savepath": "/影视/剧集/庆余年", "links_text": "", "kind": "subscription"})
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季/E01.mkv", "第1季/E02.mkv"]}, headers=h).get_json()
    assert r["whole"] and q.deleted == ["/影视/剧集/庆余年"] and e.deleted == ["s1"] and not store.data["tasks"]


def test_selecting_whole_season_folder_when_only_season_deletes_show():
    store, eng, c, h, q, e, sid = setup()
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季"]}, headers=h).get_json()
    assert r["whole"] and e.deleted == ["s1"]          # 第2季是空文件夹，不算内容


def test_partial_selection_stays_partial():
    store, eng, c, h, q, e, sid = setup()
    r = c.post("/api/library/delete", json={"scope": sid, "id": "s1", "paths": ["第1季/E01.mkv"]}, headers=h).get_json()
    assert not r["whole"] and "s1" not in e.deleted
