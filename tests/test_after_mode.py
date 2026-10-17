# -*- coding: utf-8 -*-
"""任务级「转存后联动」：all 全触发 / no_media 只通知 SmartStrm / none 都不触发。"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["CONFIG_DIR"] = tempfile.mkdtemp()

import pytest
from app import notify
from app.engine import Engine
from app.store import Store


@pytest.fixture
def env():
    store = Store(os.path.join(tempfile.mkdtemp(), "c.json"))
    store.data["emby"].update(enabled=True, url="http://x", api_key="k", refresh_after_save=True, refresh_delay=0)
    store.data["smartstrm"].update(enabled=True, webhook="http://ss", delay=0)
    eng = Engine(store)
    calls = []
    notify.trigger_smartstrm = lambda i, p, force=False, **kw: (calls.append("ss") or (True, "ok"))
    eng._refresh_one = lambda cli, label, task: calls.append("refresh")
    return eng, calls


def task(eng, **kw):
    return eng.create_task({"kind": "monitor", "name": "工具", "savepath": "/资料/工具", "links_text": "", **kw}, save_existing=True)


def test_default_triggers_everything(env):
    eng, calls = env
    t = task(eng)
    assert t["after"] == "all"
    eng._after_save(t, [{"name": "a.zip"}])
    assert calls == ["ss", "refresh"]


def test_no_media_only_notifies_smartstrm(env):
    eng, calls = env
    eng._after_save(task(eng, after="no_media"), [{"name": "a.zip"}])
    assert calls == ["ss"]


def test_none_triggers_nothing(env):
    eng, calls = env
    eng._after_save(task(eng, after="none"), [{"name": "a.zip"}])
    assert calls == []


def test_bound_smartstrm_also_respects_mode(env):
    eng, calls = env
    eng.store.data["smartstrm"].update(target="emby")
    eng._after_save(task(eng, after="no_media"), [{"name": "a.zip"}])
    assert calls == ["ss"]                       # 指定了给 Emby 生成，也不再刷新 Emby
    calls.clear()
    eng._after_save(task(eng, after="none", savepath="/资料/工具2", name="工具2"), [{"name": "a.zip"}])
    assert calls == []


def test_invalid_value_falls_back_and_subscription_is_forced(env):
    eng, calls = env
    assert task(eng, after="nonsense", savepath="/资料/x", name="x")["after"] == "all"
    s = eng.create_task({"kind": "subscription", "name": "剧", "savepath": "/剧/剧", "links_text": "", "after": "none"})
    assert s["after"] == "all"                   # 订阅的是影视，转存后一定通知媒体库


def test_edit_keeps_and_changes_mode(env):
    eng, calls = env
    t = task(eng, after="none")
    eng.update_task(t["id"], {"name": "改名"})
    assert t["after"] == "none"
    eng.update_task(t["id"], {"after": "all"})
    assert t["after"] == "all" and eng.public_task(t)["after"] == "all"
