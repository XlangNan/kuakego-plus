# -*- coding: utf-8 -*-
import os, sys, tempfile, itertools
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp()

import pytest
from app.engine import Engine
from app.quark import Quark
from app.store import Store

_fid = itertools.count(1000)


class FakeQuark:
    """
    内存里的夸克。shares = {pwd_id: {"status":200, "msg":"", "files":[...]}}
    files 元素：str=文件名；("dir", 名, [子元素])=文件夹；(名, 字节数)=带大小的文件
    """
    extract_url = staticmethod(Quark.extract_url)

    def __init__(self, shares):
        self.shares, self.saves = shares, []
        self.path2fid = {"/剧/庆余年": "D"}
        self.dirs = {"D": {}}              # fid -> {子fid: 文件名}
        self.folders = {}                  # 分享里的文件夹 fid -> children

    @property
    def dir(self):                         # 兼容旧断言：根目录内容
        return self.dirs["D"]

    def get_stoken(self, pwd_id, passcode=""):
        s = self.shares[pwd_id]
        if s.get("status", 200) == 200:
            return {"status": 200, "data": {"stoken": "tok"}}
        return {"status": s["status"], "message": s.get("msg", "boom")}

    def _wrap(self, pwd_id, items):
        out = []
        for it in items:
            if isinstance(it, tuple) and it[0] == "dir":
                fid = f"{pwd_id}|dir|{it[1]}"
                self.folders[fid] = it[2]
                out.append({"fid": fid, "share_fid_token": "t", "file_name": it[1], "dir": True})
            else:
                name, size = it if isinstance(it, tuple) else (it, 10 * 1024 * 1024 * 100)
                out.append({"fid": f"{pwd_id}|file|{name}", "share_fid_token": "t",
                            "file_name": name, "dir": False, "size": size})
        return out

    def get_detail(self, pwd_id, stoken, pdir_fid):
        items = self.folders[pdir_fid] if pdir_fid in self.folders else self.shares[pwd_id]["files"]
        return self._wrap(pwd_id, items)

    def get_fids(self, paths):
        return [{"fid": self.path2fid[p]} for p in paths if p in self.path2fid]

    def ensure_dir(self, path):
        if path not in self.path2fid:
            fid = f"dir{next(_fid)}"
            self.path2fid[path], self.dirs[fid] = fid, {}
        return self.path2fid[path]

    def ls_dir(self, fid):
        return [{"fid": k, "file_name": v, "dir": False} for k, v in self.dirs[fid].items()]

    def save_file(self, fids, tokens, to, pwd_id, stoken):
        for f in fids:
            name = f.split("|", 2)[2]
            self.dirs[to][f"new{next(_fid)}"] = name
            self.saves.append((pwd_id, name))
        return "task"

    def wait_task(self, tid):
        return {}

    def rename(self, fid, name):
        for d in self.dirs.values():
            if fid in d:
                d[fid] = name
        return {"code": 0}


class FakeEmby:
    def __init__(self, have):
        self.have = have

    def find_series(self, name, tmdb_id=None):
        return "sid", name

    def episodes(self, sid):
        return set(self.have)

    def refresh(self, sid=None):
        self.refreshed = True


@pytest.fixture
def env():
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    store.data["emby"].update(enabled=True, url="x", api_key="y", refresh_delay=0)
    eng = Engine(store)
    eng._emby = FakeEmby({(1, i) for i in range(1, 6)})
    eng.emby = lambda: eng._emby
    return store, eng


def mk(eng, shares, links, **kw):
    fq = FakeQuark(shares)
    eng.quark = lambda: fq
    text = "\n".join(f"https://pan.quark.cn/s/{l}" for l in links)
    t = eng.create_task({"name": "庆余年", "savepath": "/剧/庆余年", "links_text": text, **kw})
    return fq, t


def test_multi_link_fills_only_missing(env):
    store, eng = env
    A = [f"庆余年.第{i:02d}集.mp4" for i in range(1, 9)]          # 1-8
    B = [f"S01E{i:02d}.2160p.mkv" for i in range(1, 11)]          # 1-10
    fq, t = mk(eng, {"aaa": {"files": A}, "bbb": {"files": B}}, ["aaa", "bbb"])
    eng.run_task(t["id"], manual=True)
    assert sorted(fq.dir.values()) == [f"S01E{i:02d}.{'mp4' if i <= 8 else 'mkv'}" for i in range(6, 11)]
    assert [p for p, _ in fq.saves] == ["aaa"] * 3 + ["bbb"] * 2   # A 补 6-8，B 补 9-10
    assert t["last_status"] == "saved" and t["saved_total"] == 5
    assert eng._emby.refreshed


def test_second_run_is_noop(env):
    store, eng = env
    fq, t = mk(eng, {"aaa": {"files": [f"S01E{i:02d}.mp4" for i in range(1, 8)]}}, ["aaa"])
    eng.run_task(t["id"], manual=True)
    n = len(fq.saves)
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == n and t["last_status"] == "nochange"


def test_dead_link_banned_and_next_link_used(env):
    store, eng = env
    fq, t = mk(eng, {"dead": {"status": 400, "msg": "分享已取消"},
                     "ok": {"files": ["S01E06.mp4"]}}, ["dead", "ok"])
    eng.run_task(t["id"], manual=True)
    assert t["links"][0]["ban"] == "分享已取消" and not t["links"][1]["ban"]
    assert fq.saves == [("ok", "S01E06.mp4")]


def test_transient_error_needs_three_strikes(env):
    store, eng = env
    fq, t = mk(eng, {"x": {"status": 400, "msg": "服务繁忙"}}, ["x"])
    for i in range(2):
        eng.run_task(t["id"], manual=True)
    assert not t["links"][0]["ban"]
    eng.run_task(t["id"], manual=True)
    assert t["links"][0]["ban"]


def test_network_error_never_bans(env):
    store, eng = env
    fq, t = mk(eng, {"x": {"status": 500, "msg": "timeout"}}, ["x"])
    for _ in range(5):
        eng.run_task(t["id"], manual=True)
    assert not t["links"][0]["ban"] and t["last_status"] == "error"


def test_baseline_only_new(env):
    store, eng = env
    shares = {"aaa": {"files": [f"S01E{i:02d}.mp4" for i in range(1, 9)]}}
    fq = FakeQuark(shares); eng.quark = lambda: fq
    t = eng.create_task({"name": "x", "savepath": "/x", "links_text": "https://pan.quark.cn/s/aaa"},
                        save_existing=False)
    eng.run_task(t["id"], manual=True)
    assert fq.saves == []                       # 已有的 6-8 不转
    shares["aaa"]["files"].append("S01E09.mp4")  # 作者更新
    eng.run_task(t["id"], manual=True)
    assert fq.saves == [("aaa", "S01E09.mp4")]


def test_emby_disabled_falls_back_to_dir(env):
    store, eng = env
    store.data["emby"]["enabled"] = False
    eng.emby = lambda: None
    fq, t = mk(eng, {"aaa": {"files": [f"S01E{i:02d}.mp4" for i in range(1, 4)]}}, ["aaa"])
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 3       # 没有 Emby 时，1-3 都是缺的


def test_unparsable_names_pass_through_once(env):
    store, eng = env
    fq, t = mk(eng, {"aaa": {"files": ["花絮.mp4", "S01E06.mp4"]}}, ["aaa"])
    eng.run_task(t["id"], manual=True)
    eng.run_task(t["id"], manual=True)
    assert sorted(n for _, n in fq.saves) == ["S01E06.mp4", "花絮.mp4"]   # 第二次不重复


def test_same_episode_two_versions_only_one(env):
    store, eng = env
    fq, t = mk(eng, {"aaa": {"files": ["S01E06.1080p.mp4", "S01E06.2160p.mkv"]}}, ["aaa"])
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 1


def test_interval_clamped_and_validation(env):
    store, eng = env
    fq, t = mk(eng, {"a": {"files": []}}, ["a"], interval=5)
    assert t["interval"] == 60
    with pytest.raises(ValueError):
        eng.normalize({"name": "x", "savepath": "/", "links_text": "", "pattern": "("})
    with pytest.raises(ValueError):
        eng.normalize({"name": "", "savepath": "", "links_text": ""})


def test_link_state_preserved_on_edit(env):
    store, eng = env
    fq, t = mk(eng, {"a": {"files": []}, "b": {"files": []}}, ["a"])
    t["links"][0]["ban"] = "dead"
    eng.update_task(t["id"], {"links_text": "https://pan.quark.cn/s/a\nhttps://pan.quark.cn/s/b"})
    assert [l["ban"] for l in t["links"]] == ["dead", ""] and len(t["links"]) == 2


def test_keep_tree_mirrors_season_folders(env):
    store, eng = env
    store.data["emby"]["enabled"] = False; eng.emby = lambda: None
    files = [("dir", "第二季", ["第01集.mp4", "第02集.mp4"]), ("dir", "第一季", ["第01集.mp4"])]
    fq, t = mk(eng, {"aaa": {"files": files}}, ["aaa"], keep_tree=True)
    eng.run_task(t["id"], manual=True)
    assert fq.dirs[fq.path2fid["/剧/庆余年/第一季"]].values().__len__() == 1
    assert sorted(fq.dirs[fq.path2fid["/剧/庆余年/第二季"]].values()) == ["S02E01.mp4", "S02E02.mp4"]
    assert list(fq.dirs[fq.path2fid["/剧/庆余年/第一季"]].values()) == ["S01E01.mp4"]


def test_flatten_when_not_keep_tree(env):
    store, eng = env
    store.data["emby"]["enabled"] = False; eng.emby = lambda: None
    files = [("dir", "第二季", ["第01集.mp4"]), ("dir", "第一季", ["第01集.mp4"])]
    fq, t = mk(eng, {"aaa": {"files": files}}, ["aaa"], keep_tree=False)
    eng.run_task(t["id"], manual=True)
    assert sorted(fq.dir.values()) == ["S01E01.mp4", "S02E01.mp4"]


def test_keyword_and_size_filters(env):
    store, eng = env
    store.data["emby"]["enabled"] = False; eng.emby = lambda: None
    big, small = 800 * 1024 * 1024, 5 * 1024 * 1024
    files = [("S01E01.2160p.mkv", big), ("S01E02.1080p.mkv", big), ("S01E03.2160p.mkv", small),
             ("S01E04.2160p.CAM.mkv", big)]
    fq, t = mk(eng, {"aaa": {"files": files}}, ["aaa"],
               include_kw="2160p", exclude_kw="cam", min_size_mb=100)
    eng.run_task(t["id"], manual=True)
    assert [n for _, n in fq.saves] == ["S01E01.2160p.mkv"]


def test_incremental_limit_defers_rest(env):
    import app.engine as E
    store, eng = env
    store.data["emby"]["enabled"] = False; eng.emby = lambda: None
    files = [f"S01E{i:02d}.mp4" for i in range(1, 51)]
    fq, t = mk(eng, {"aaa": {"files": files}}, ["aaa"])
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 50            # 首次回填不限
    t["saved_total"] = 1                   # 之后是追更，限量
    files += [f"S01E{i:02d}.mp4" for i in range(51, 111)]
    eng.run_task(t["id"], manual=True)
    assert len(fq.saves) == 50 + E.INCREMENTAL_LIMIT
    eng.run_task(t["id"], manual=True)     # 下一轮接着补
    assert len(fq.saves) == 50 + 2 * E.INCREMENTAL_LIMIT


def test_manual_total_marks_complete_and_skips(env):
    store, eng = env
    eng._emby.have = {(1, i) for i in range(1, 9)}
    fq, t = mk(eng, {"aaa": {"files": ["S01E09.mp4"]}}, ["aaa"], total_episodes=8)
    eng.run_task(t["id"], manual=False)
    assert t["last_status"] == "complete" and fq.saves == []
    t["total_episodes"] = 9
    eng.run_task(t["id"], manual=False)
    assert fq.saves == [("aaa", "S01E09.mp4")]
