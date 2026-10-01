# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import parser


def with_fake(result, fn):
    old = parser._guessit
    parser._guessit = result if callable(result) else (lambda name, opts=None: result)
    try:
        return fn()
    finally:
        parser._guessit = old


def test_builtin_wins_guessit_not_consulted():
    def boom(*a, **k): raise AssertionError("不应该被调用")
    assert with_fake(boom, lambda: parser.parse_episodes("S01E05.mp4")) == [(1, 5)]
    assert with_fake(boom, lambda: parser.parse_episodes("第12集.mp4")) == [(1, 12)]


def test_fallback_used_only_when_builtin_fails():
    got = with_fake({"season": 2, "episode": 7}, lambda: parser.parse_episodes("奇怪的命名.mp4"))
    assert got == [(2, 7)]


def test_list_episode_and_missing_season():
    got = with_fake({"episode": [3, 4]}, lambda: parser.parse_episodes("奇怪的命名.mp4", 3))
    assert got == [(3, 3), (3, 4)]


def test_suspicious_results_rejected():
    for bad in ({"episode": 5000}, {"episode": list(range(1, 60))}, {"episode": 0}, {"episode": "x"}, {}):
        assert with_fake(bad, lambda: parser.parse_episodes("奇怪的命名.mp4")) == [], bad


def test_guessit_exception_is_swallowed():
    def boom(*a, **k): raise RuntimeError("x")
    assert with_fake(boom, lambda: parser.parse_episodes("奇怪的命名.mp4")) == []


def test_not_installed_behaves_like_before():
    old = parser._guessit; parser._guessit = None
    try:
        assert parser.parse_episodes("奇怪的命名.mp4") == []
        assert parser.parse_episodes("S01E05.mp4") == [(1, 5)]
    finally:
        parser._guessit = old
