# -*- coding: utf-8 -*-
"""镜像同步：分享里新增 / 删除 / 替换，我这边跟着变；以及各种「不能乱删」的场景。"""
import copy, os, sys, tempfile, time, types

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp()

import pytest
import app.mirror as mirror_mod
from app.engine import Engine
from app.quark import Quark, QuarkError
from app.store import Store

mirror_mod.time = types.SimpleNamespace(sleep=lambda s: None, time=time.time)   # 测试里不用真的等

DEST = "/镜像/某剧"


class FakeQuark:
    """内存里的夸克：分享是一棵嵌套 dict（名字 -> 字节数 或 子 dict），我的网盘是 路径 -> 大小 / 目录集合。"""
    extract_url = staticmethod(Quark.extract_url)

    def __init__(self):
        self.shares = {}        # pwd_id -> {"tree": {...}, "status": 200, "msg": ""}
        self.files = {}         # 我的网盘：路径 -> 大小
        self.dirs = {"/", "/镜像"}
        self.fail_dir = None    # 读这个分享子目录时报错
        self.deleted = []

    # ---- 分享
    def get_stoken(self, pwd_id, passcode=""):
        s = self.shares[pwd_id]
        if s.get("status", 200) == 200:
            return {"status": 200, "data": {"stoken": "tok"}}
        return {"status": s["status"], "message": s.get("msg", "boom")}

    def _node(self, pwd_id, rel):
        n = self.shares[pwd_id]["tree"]
        for part in [p for p in rel.split("/") if p]:
            n = n[part]
        return n

    def get_detail(self, pwd_id, stoken, pdir_fid):
        rel = "" if pdir_fid == "0" else pdir_fid.split("|", 2)[2]
        if self.fail_dir and rel == self.fail_dir:
            raise QuarkError("读取失败")
        out = []
        for name, v in self._node(pwd_id, rel).items():
            r = f"{rel}/{name}" if rel else name
            if isinstance(v, dict):
                out.append({"fid": f"D|{pwd_id}|{r}", "share_fid_token": "t", "file_name": name, "dir": True})
            else:
                out.append({"fid": f"S|{pwd_id}|{r}", "share_fid_token": "t", "file_name": name, "dir": False, "size": v})
        return out

    # ---- 我的网盘
    def get_fids(self, paths):
        return [{"fid": "P:" + p} for p in paths if p in self.dirs]

    def ensure_dir(self, path):
        cur = ""
        for part in [p for p in path.split("/") if p]:
            cur += "/" + part
            self.dirs.add(cur)
        return "P:" + path

    def ls_dir(self, fid):
        path = fid[2:]
        out = []
        for p, size in self.files.items():
            if p.rsplit("/", 1)[0] == path:
                out.append({"fid": "P:" + p, "file_name": p.rsplit("/", 1)[1], "dir": False, "size": size})
        for d in self.dirs:
            if d != path and d.rsplit("/", 1)[0] == path:
                out.append({"fid": "P:" + d, "file_name": d.rsplit("/", 1)[1], "dir": True})
        return out

    def save_file(self, fids, tokens, to, pwd_id, stoken):
        for f in fids:
            _, pid, rel = f.split("|", 2)
            self.files[to[2:] + "/" + rel.rsplit("/", 1)[-1]] = self._node(pid, rel)
        return "task"

    def wait_task(self, tid):
        return {}

    def delete(self, fids):
        for f in fids:
            p = f[2:]
            self.deleted.append(p)
            self.files.pop(p, None)
            if p in self.dirs:
                self.dirs = {d for d in self.dirs if d != p and not d.startswith(p + "/")}
                self.files = {k: v for k, v in self.files.items() if not k.startswith(p + "/")}


@pytest.fixture
def env():
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    eng = Engine(store)
    fq = FakeQuark()
    eng.quark = lambda: fq
    return fq, eng


def mk(env, tree, mirror=None, links=("aaa",), **kw):
    fq, eng = env
    for l in links:
        fq.shares.setdefault(l, {"tree": {}})
    fq.shares[links[0]]["tree"] = copy.deepcopy(tree)
    text = "\n".join(f"https://pan.quark.cn/s/{l}" for l in links)
    cfg = {"enabled": True, "dry_run": False, "grace": 2, **(mirror or {})}
    return eng.create_task({"kind": "monitor", "name": "某剧", "savepath": DEST, "links_text": text, "mirror": cfg, **kw})


def run(eng, t):
    eng.run_task(t["id"], manual=True)
    return t


TREE = {"S1": {"01.mp4": 100, "02.mp4": 200}, "S2": {"01.mp4": 300}, "海报.jpg": 5}


def test_first_sync_copies_whole_tree(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    assert fq.files == {f"{DEST}/S1/01.mp4": 100, f"{DEST}/S1/02.mp4": 200, f"{DEST}/S2/01.mp4": 300, f"{DEST}/海报.jpg": 5}
    assert t["last_status"] == "saved" and len(t["mstate"]["manifest"]) == 4


def test_new_file_in_share_is_saved(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.shares["aaa"]["tree"]["S2"]["02.mp4"] = 400
    run(eng, t)
    assert fq.files[f"{DEST}/S2/02.mp4"] == 400


def test_removed_file_deleted_only_after_grace(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    run(eng, t)
    assert f"{DEST}/S1/02.mp4" in fq.files                      # 第 1 次不在：先观察
    assert t["mstate"]["missing"]
    run(eng, t)
    assert f"{DEST}/S1/02.mp4" not in fq.files                   # 第 2 次：删
    assert f"{DEST}/S1/01.mp4" in fq.files
    assert "S1/02.mp4" not in t["mstate"]["manifest"]
    assert t["history"][0]["removed"] == ["S1/02.mp4"]


def test_reappearing_file_resets_grace(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    node = fq.shares["aaa"]["tree"]["S1"]
    size = node.pop("02.mp4")
    run(eng, t)
    node["02.mp4"] = size
    run(eng, t)
    assert not t["mstate"]["missing"]
    del node["02.mp4"]
    run(eng, t)                                                  # 重新从 1 数起
    assert f"{DEST}/S1/02.mp4" in fq.files


def test_empty_folder_cleaned_but_root_kept(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    del fq.shares["aaa"]["tree"]["S2"]
    run(eng, t); run(eng, t)
    assert f"{DEST}/S2/01.mp4" not in fq.files and f"{DEST}/S2" not in fq.dirs
    assert DEST in fq.dirs and f"{DEST}/S1" in fq.dirs


def test_dry_run_default_only_previews_then_confirm_deletes(env):
    fq, eng = env
    t = run(eng, mk(env, TREE, {"dry_run": True}))
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    run(eng, t); run(eng, t); run(eng, t)
    assert f"{DEST}/S1/02.mp4" in fq.files                       # 预览模式不真删
    pend = t["mstate"]["pending"]
    assert [p["rel"] for p in pend] == ["S1/02.mp4"] and "预览" in pend[0]["reason"]
    r = eng.mirror.resolve(t, "confirm")
    assert r["deleted"] == 1 and f"{DEST}/S1/02.mp4" not in fq.files and not t["mstate"]["pending"]


def test_ignore_pending_keeps_file_and_stops_managing(env):
    fq, eng = env
    t = run(eng, mk(env, TREE, {"dry_run": True}))
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    run(eng, t); run(eng, t)
    eng.mirror.resolve(t, "ignore")
    run(eng, t); run(eng, t)
    assert f"{DEST}/S1/02.mp4" in fq.files and not t["mstate"]["pending"]


def test_dead_share_never_deletes(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.shares["aaa"]["status"], fq.shares["aaa"]["msg"] = 404, "分享已被取消"
    for _ in range(4):
        run(eng, t)
    assert len(fq.files) == 4 and not fq.deleted
    assert t["links"][0]["ban"]


def test_network_error_never_deletes(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.shares["aaa"]["status"], fq.shares["aaa"]["msg"] = 500, "timeout"
    for _ in range(4):
        run(eng, t)
    assert len(fq.files) == 4 and not fq.deleted and not t["links"][0]["ban"]


def test_emptied_share_never_deletes(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.shares["aaa"]["tree"] = {}
    for _ in range(4):
        run(eng, t)
    assert len(fq.files) == 4 and not fq.deleted


def test_partial_listing_never_deletes(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    fq.fail_dir = "S2"                                           # 读 S2 失败：没读全，不能当成「S2 里的文件都没了」
    for _ in range(4):
        run(eng, t)
    assert len(fq.files) == 4 and not fq.deleted


def test_users_own_files_never_touched(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.files[f"{DEST}/S1/我自己的笔记.txt"] = 1
    fq.shares["aaa"]["tree"] = {"S1": {"01.mp4": 100}}           # 分享里只剩一个
    run(eng, t); run(eng, t)
    assert f"{DEST}/S1/我自己的笔记.txt" in fq.files


def test_mass_deletion_is_held_for_confirmation(env):
    fq, eng = env
    big = {"S1": {f"{i:02d}.mp4": 100 + i for i in range(1, 21)}}
    t = run(eng, mk(env, big, {"max_delete": 10}))
    for i in range(1, 13):
        del fq.shares["aaa"]["tree"]["S1"][f"{i:02d}.mp4"]
    run(eng, t); run(eng, t); run(eng, t)
    assert len(fq.files) == 20 and not fq.deleted                 # 12 个 > 10：不自动删
    assert len(t["mstate"]["pending"]) == 12 and "超过上限" in t["mstate"]["pending"][0]["reason"]
    eng.mirror.resolve(t, "confirm", ["S1/01.mp4"])               # 也可以只确认其中几个
    assert len(fq.files) == 19 and len(t["mstate"]["pending"]) == 11


def test_everything_gone_is_held_even_if_under_limit(env):
    fq, eng = env
    t = run(eng, mk(env, {"a.mp4": 1, "b.mp4": 2, "c.mp4": 3}))
    fq.shares["aaa"]["tree"] = {"x.txt": 9}                       # 原来的全没了（换了一批别的）
    run(eng, t); run(eng, t)
    assert {"a.mp4", "b.mp4", "c.mp4"} <= {p.rsplit("/", 1)[1] for p in fq.files} and not fq.deleted
    assert "大部分都没了" in t["mstate"]["pending"][0]["reason"]


def test_changed_size_replaces_file(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.shares["aaa"]["tree"]["S1"]["01.mp4"] = 999
    run(eng, t)
    assert fq.files[f"{DEST}/S1/01.mp4"] == 999 and t["mstate"]["manifest"]["S1/01.mp4"]["size"] == 999


def test_replace_can_be_turned_off(env):
    fq, eng = env
    t = run(eng, mk(env, TREE, {"replace": False}))
    fq.shares["aaa"]["tree"]["S1"]["01.mp4"] = 999
    run(eng, t)
    assert fq.files[f"{DEST}/S1/01.mp4"] == 100


def test_file_changed_by_user_is_not_deleted(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    fq.files[f"{DEST}/S1/02.mp4"] = 12345                         # 你自己换了一个文件
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    run(eng, t); run(eng, t)
    assert fq.files[f"{DEST}/S1/02.mp4"] == 12345 and "S1/02.mp4" not in t["mstate"]["manifest"]


def test_user_deleted_file_is_not_an_error(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    del fq.files[f"{DEST}/S1/02.mp4"]
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    run(eng, t); run(eng, t)
    assert "S1/02.mp4" not in t["mstate"]["manifest"] and t["last_status"] != "error"


def test_existing_same_files_are_adopted_not_duplicated(env):
    fq, eng = env
    fq.ensure_dir(f"{DEST}/S1")
    fq.files[f"{DEST}/S1/01.mp4"] = 100                           # 目录里已经有一份一样的
    t = run(eng, mk(env, {"S1": {"01.mp4": 100, "02.mp4": 200}}))
    assert fq.files[f"{DEST}/S1/02.mp4"] == 200 and "S1/01.mp4" in t["mstate"]["manifest"]
    assert t["mstate"]["manifest"]["S1/01.mp4"]["link"]


def test_first_run_holds_when_dir_has_unmatched_files(env):
    fq, eng = env
    fq.ensure_dir(DEST)
    for i in range(3):
        fq.files[f"{DEST}/某剧 - S01E0{i}.mp4"] = 50                # 以前被改过名的旧文件
    t = run(eng, mk(env, {"01.mp4": 1, "02.mp4": 2}))
    assert len(fq.files) == 3 and t["mstate"]["hold"]             # 先暂停，不重复转存
    eng.mirror.resolve(t, "go")
    run(eng, t)
    assert len(fq.files) == 5 and not t["mstate"]["hold"]


def test_file_in_other_link_is_kept(env):
    fq, eng = env
    fq.shares["bbb"] = {"tree": {"S1": {"01.mp4": 100}}}
    t = run(eng, mk(env, {"S1": {"01.mp4": 100, "02.mp4": 200}}, links=("aaa", "bbb")))
    del fq.shares["aaa"]["tree"]["S1"]["01.mp4"]                  # A 里没了，B 里还有
    run(eng, t); run(eng, t)
    assert f"{DEST}/S1/01.mp4" in fq.files


def test_one_dead_link_blocks_deletion_from_the_other(env):
    fq, eng = env
    fq.shares["bbb"] = {"tree": {"x.mp4": 1}}
    t = run(eng, mk(env, {"a.mp4": 1, "b.mp4": 2, "c.mp4": 3, "d.mp4": 4}, links=("aaa", "bbb")))
    fq.shares["bbb"]["status"], fq.shares["bbb"]["msg"] = 404, "已取消"
    del fq.shares["aaa"]["tree"]["a.mp4"]
    for _ in range(3):
        run(eng, t)
    assert f"{DEST}/a.mp4" in fq.files                             # 有链接读不出来：整轮不删


def test_removing_a_link_does_not_delete_its_files(env):
    fq, eng = env
    fq.shares["bbb"] = {"tree": {"x.mp4": 1}}
    t = run(eng, mk(env, {"a.mp4": 1}, links=("aaa", "bbb")))
    eng.update_task(t["id"], {"links_text": "https://pan.quark.cn/s/aaa"})
    run(eng, t); run(eng, t)
    assert f"{DEST}/x.mp4" in fq.files and "x.mp4" not in t["mstate"]["manifest"]


def test_preview_is_read_only(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    del fq.shares["aaa"]["tree"]["S1"]["02.mp4"]
    fq.shares["aaa"]["tree"]["S2"]["09.mp4"] = 9
    before = (dict(fq.files), dict(t["mstate"]["missing"]))
    r = eng.mirror.preview(t)
    acts = {(x["rel"], x["action"]) for x in r["rows"]}
    assert ("S2/09.mp4", "save") in acts and ("S1/02.mp4", "delete") in acts
    assert (dict(fq.files), dict(t["mstate"]["missing"])) == before


def test_changing_savepath_resets_manifest(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    eng.update_task(t["id"], {"savepath": "/镜像/换了"})
    assert "mstate" not in t


def test_public_task_hides_manifest_and_subscription_cannot_mirror(env):
    fq, eng = env
    t = run(eng, mk(env, TREE))
    pub = eng.public_task(t)
    assert "mstate" not in pub and pub["mirror"]["enabled"] and pub["mirror_info"]["managed"] == 4
    s = eng.create_task({"kind": "subscription", "name": "订阅", "savepath": "/镜像/订阅", "links_text": "", "mirror": {"enabled": True}})
    assert s["mirror"]["enabled"] is False


def test_clean_cfg_bounds():
    c = mirror_mod.clean_cfg({"grace": 99, "max_delete": 0, "max_pct": "x"})
    assert c["grace"] == 10 and c["max_delete"] == 1 and c["max_pct"] == 30 and c["dry_run"] is True
