# -*- coding: utf-8 -*-
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp())
from app import hosts

BASE = "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost\n172.17.0.2\tabc123\n"


def setup(content):
    d = tempfile.mkdtemp(); etc = os.path.join(d, "etc_hosts")
    open(etc, "w").write(BASE)
    if content is not None:
        open(os.path.join(d, "hosts"), "w").write(content)
    return d, etc


def test_applies_and_keeps_original():
    d, etc = setup("1.2.3.4 api.themoviedb.org\n5.6.7.8  image.tmdb.org  alias.example # 注释\n")
    assert hosts.apply(d, etc) == 2
    t = open(etc).read()
    assert t.startswith(BASE) and "1.2.3.4\tapi.themoviedb.org" in t and "5.6.7.8\timage.tmdb.org alias.example" in t


def test_idempotent_and_replaces_old_block():
    d, etc = setup("1.2.3.4 api.themoviedb.org\n")
    hosts.apply(d, etc); first = open(etc).read(); hosts.apply(d, etc)
    assert open(etc).read() == first and first.count(hosts.BEGIN) == 1
    open(os.path.join(d, "hosts"), "w").write("9.9.9.9 api.themoviedb.org\n")
    hosts.apply(d, etc); t = open(etc).read()
    assert "9.9.9.9" in t and "1.2.3.4" not in t and t.count(hosts.BEGIN) == 1


def test_empty_file_clears_block():
    d, etc = setup("1.2.3.4 api.themoviedb.org\n"); hosts.apply(d, etc)
    open(os.path.join(d, "hosts"), "w").write("# 全注释\n")
    assert hosts.apply(d, etc) == 0 and open(etc).read() == BASE


def test_invalid_lines_ignored():
    d, etc = setup("not-an-ip foo.com\n1.2.3.4\n999.1.1.1 x.com\n::1 v6.example\n1.2.3.4 ok.example\n")
    assert hosts.apply(d, etc) == 2
    t = open(etc).read()
    assert "ok.example" in t and "v6.example" in t and "foo.com" not in t and "x.com" not in t


def test_creates_template_when_missing():
    d, etc = setup(None)
    assert hosts.apply(d, etc) == 0
    assert os.path.exists(os.path.join(d, "hosts")) and open(etc).read() == BASE
    assert "api.themoviedb.org" in open(os.path.join(d, "hosts")).read()   # 示例是注释，不生效


def test_no_permission_does_not_crash():
    d, etc = setup("1.2.3.4 a.com\n")
    assert hosts.apply(d, "/proc/definitely/not/writable") is None
