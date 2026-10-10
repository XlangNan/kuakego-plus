"""离线预览 UI：用内存里的假夸克 / 假 TMDB 起一个完整的后端。
    python tests/dev_server.py   →  http://127.0.0.1:5099  （admin / dev）
"""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp())
os.environ["WEBUI_PASSWORD"] = "dev"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import types
sys.modules.setdefault("pytest", types.SimpleNamespace(fixture=lambda f: f, raises=None))
from tests.test_engine import FakeQuark, FakeEmby          # noqa: E402
from app.engine import Engine, Scheduler                    # noqa: E402
from app.main import create_app                             # noqa: E402
from app.store import Store                                 # noqa: E402

ANIME_TV = [{"id": 200 + i, "name": n, "first_air_date": d, "vote_average": v, "overview": "这是一部番剧的简介。" * 3, "poster_path": "/a%d.jpg" % i,
             "genre_ids": [16, 10759]} for i, (n, d, v) in enumerate([("某某番剧", "2024-04-06", 8.6), ("国漫之光", "2024-06-01", 8.2), ("热血少年", "2023-10-02", 8.9)], 1)]
ANIME_MOVIES = [{"id": 300 + i, "title": n, "release_date": d, "vote_average": v, "poster_path": "/am%d.jpg" % i, "overview": "动画电影简介。",
                 "genre_ids": [16, 10751]} for i, (n, d, v) in enumerate([("动画电影一号", "2024-07-19", 8.0), ("剧场版某某", "2023-12-01", 8.5)], 1)]
SHOWS = [{"id": i, "name": n, "first_air_date": d, "vote_average": v, "overview": "这是一段剧情简介，用来检查两行截断和排版效果是否正常。" * 2,
          "poster_path": "/s%d.jpg" % i, "backdrop_path": None, "origin_country": ["CN"], "genre_ids": [18]}
         for i, (n, d, v) in enumerate([("庆余年 第二季", "2024-05-16", 8.3), ("长相思", "2024-07-22", 7.9), ("繁花", "2023-12-27", 8.5),
                                         ("三体", "2023-01-15", 8.1), ("漫长的季节", "2023-04-22", 9.4), ("狂飙", "2023-01-14", 8.6),
                                         ("去有风的地方", "2023-01-02", 7.7), ("莲花楼", "2023-07-19", 8.2)], 1)]


MOVIES = [{"id": 101 + i, "title": t, "release_date": d, "vote_average": v, "poster_path": "/p%d.jpg" % i,
           "overview": "这是一部电影的简介，用来检查两行截断和排版效果是否正常。" * 2, "genre_ids": [18]}
          for i, (t, d, v) in enumerate([("某电影", "2024-06-01", 7.8), ("另一部电影", "2023-12-15", 8.4), ("即将上映的电影", "2999-01-01", 0),
                                          ("经典老片", "1999-03-31", 8.7), ("华语新片", "2025-02-10", 7.1), ("动作大片", "2024-07-19", 8.0)])]


ALL_TV = {x["id"]: x for x in SHOWS + ANIME_TV}
ALL_MOVIES = {x["id"]: x for x in MOVIES + ANIME_MOVIES}


class Tmdb(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path); p, q = u.path, parse_qs(u.query)
        with_g, without_g = q.get("with_genres", [""])[0], q.get("without_genres", [""])[0]
        def filt(items):   # 模拟 TMDB 的 with_genres / without_genres
            if with_g == "16": return [x for x in items if 16 in x["genre_ids"]]
            if without_g == "16": return [x for x in items if 16 not in x["genre_ids"]]
            return items
        if p.startswith("/person/"):
            d = {"id": 501, "name": "张若昀", "profile_path": None, "known_for_department": "Acting", "birthday": "1988-08-13", "place_of_birth": "北京",
                 "biography": "中国内地男演员。" * 10,
                 "combined_credits": {"cast": [{"id": x["id"], "media_type": "tv", "name": x["name"], "first_air_date": x["first_air_date"], "vote_average": x["vote_average"],
                                                "poster_path": x["poster_path"], "genre_ids": x["genre_ids"], "popularity": 50 - i, "character": "范闲"} for i, x in enumerate(SHOWS[:5])] +
                                     [{"id": MOVIES[0]["id"], "media_type": "movie", "title": MOVIES[0]["title"], "release_date": "2024-06-01", "vote_average": 7.8,
                                       "poster_path": "/p0.jpg", "genre_ids": [18], "popularity": 60, "character": "男主"}]}}
        elif p.endswith(("/recommendations", "/similar")):
            d = {"results": (MOVIES[:4] if p.startswith("/movie/") else SHOWS[2:7])}
        elif p.startswith("/movie/"):
            mv = ALL_MOVIES[int(p.split("/")[2])]
            d = {**mv, "runtime": 118, "genres": [{"id": g, "name": "动画" if g == 16 else "剧情"} for g in mv["genre_ids"]], "vote_count": 1234,
                 "tagline": "一句电影标语", "backdrop_path": None, "status": "Released",
                 "original_title": "Some Movie", "alternative_titles": {"titles": [{"title": "某片", "iso_3166_1": "HK"}]},
                 "credits": {"cast": [{"id": 501, "name": "演员甲", "character": "角色乙"}, {"id": 502, "name": "演员丙", "character": "角色丁"}]}}
        elif p == "/trending/movie/week": d = {"results": MOVIES + ANIME_MOVIES, "total_pages": 3}          # 趋势接口混着动画，由服务端去掉
        elif p == "/discover/movie": d = {"results": filt(MOVIES + ANIME_MOVIES), "total_pages": 3}
        elif p == "/search/movie": d = {"results": MOVIES + ANIME_MOVIES, "total_pages": 1}
        elif p == "/trending/tv/week": d = {"results": SHOWS + ANIME_TV, "total_pages": 3}
        elif p == "/discover/tv": d = {"results": filt(SHOWS + ANIME_TV), "total_pages": 3}
        elif p == "/search/tv": d = {"results": SHOWS + ANIME_TV, "total_pages": 1}
        elif p.startswith("/tv/") and "/season/" in p:
            d = {"episodes": [{"episode_number": i, "air_date": "2024-05-%02d" % i if i <= 8 else "2099-01-01"} for i in range(1, 11)]}
        elif p.startswith("/tv/"):
            s_ = ALL_TV[int(p.split("/")[2])]
            d = {**s_, "genres": [{"id": g, "name": "动画" if g == 16 else "剧情"} for g in s_["genre_ids"]], "status": "Returning Series", "vote_count": 420,
                 "number_of_seasons": 2, "number_of_episodes": 46,
                 "seasons": [{"season_number": 1, "name": "第 1 季", "episode_count": 46, "air_date": "2019-11-26"},
                             {"season_number": 2, "name": "第 2 季", "episode_count": 36, "air_date": "2024-05-16"}],
                 "original_name": "庆余年", "first_air_date": s_["first_air_date"],
                 "alternative_titles": {"results": [{"title": "Joy of Life", "iso_3166_1": "US"}, {"title": "慶餘年", "iso_3166_1": "TW"}]},
                 "credits": {"cast": [{"id": 501, "name": "张若昀", "character": "范闲"}, {"id": 502, "name": "李沁", "character": "林婉儿"}]}}
        else:
            d = {"results": SHOWS, "total_pages": 3}
        b = json.dumps(d).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)


PANSOU_RESULTS = [
    {"url": "https://pan.quark.cn/s/ccc", "password": "ab12", "note": "法医秦明之龙番往事 全16集 1080P 国语中字", "datetime": "2026-09-28T10:00:00Z", "source": "tg:Quark_Movies"},
    {"url": "https://pan.quark.cn/s/bbb", "password": "", "note": "【4K】法医秦明之龙番往事 2160P HDR 杜比视界 持续更新中", "datetime": "2026-09-30T08:30:00Z", "source": "plugin:quark4k"},
    {"url": "https://pan.quark.cn/s/aaa", "password": "wxyz", "note": "法医秦明之龙番往事 (2026) 1-8集", "datetime": "2026-09-20T00:00:00Z", "source": "plugin:hunhepan"},
    {"url": "https://pan.quark.cn/s/dead", "password": "", "note": "法医秦明之龙番往事 合集（备用）", "datetime": "2026-08-01T00:00:00Z", "source": "tg:ucquark"},
    {"url": "https://pan.quark.cn/s/ddd", "password": "k9k9", "note": "法医秦明 龙番往事 高清 带花絮和幕后 这是一个标题非常非常长的资源用来检查两行截断是否正常显示", "datetime": "2026-07-12T00:00:00Z", "source": "plugin:sousou"},
]


PANSOU_QYN = [   # 搜「庆余年」时返回的：好的、画质不符的、花絮、没写季的、别的季、失效的、别的剧
    {"url": "https://pan.quark.cn/s/q1", "password": "", "note": "庆余年 第二季 全集 4K HDR 杜比视界", "datetime": "2026-09-30T08:00:00Z", "source": "plugin:quark4k"},
    {"url": "https://pan.quark.cn/s/q2", "password": "ab12", "note": "庆余年 第二季 1080P 更新至10集", "datetime": "2026-09-29T08:00:00Z", "source": "tg:Quark_Movies"},
    {"url": "https://pan.quark.cn/s/q3", "password": "", "note": "庆余年 第一季 全集 国语", "datetime": "2026-09-01T08:00:00Z", "source": "plugin:hunhepan"},
    {"url": "https://pan.quark.cn/s/q4", "password": "", "note": "庆余年 第二季 幕后花絮 4K HDR", "datetime": "2026-09-28T08:00:00Z", "source": "plugin:sousou"},
    {"url": "https://pan.quark.cn/s/q5", "password": "", "note": "庆余年 全集 国语中字", "datetime": "2026-09-27T08:00:00Z", "source": "plugin:pansearch"},
    {"url": "https://pan.quark.cn/s/q6", "password": "", "note": "庆余年 第二季 备用链接", "datetime": "2026-09-26T08:00:00Z", "source": "tg:ucquark"},
    {"url": "https://pan.quark.cn/s/q7", "password": "", "note": "繁花 全30集 4K", "datetime": "2026-09-25T08:00:00Z", "source": "plugin:labi"},
]


PANSOU_MOVIE = [   # 搜「某电影」时返回的
    {"url": "https://pan.quark.cn/s/m1", "password": "", "note": "某电影 2024 4K HDR 国英双语 内封字幕", "datetime": "2026-09-30T08:00:00Z", "source": "plugin:quark4k"},
    {"url": "https://pan.quark.cn/s/m2", "password": "ab12", "note": "某电影 (2024) 1080P 国语中字", "datetime": "2026-09-29T08:00:00Z", "source": "tg:Quark_Movies"},
    {"url": "https://pan.quark.cn/s/m3", "password": "", "note": "某电影 2024 抢先版 TS", "datetime": "2026-06-10T08:00:00Z", "source": "plugin:sousou"},
    {"url": "https://pan.quark.cn/s/m4", "password": "", "note": "某电影 全集 12集 国语", "datetime": "2026-09-27T08:00:00Z", "source": "plugin:pansearch"},
    {"url": "https://pan.quark.cn/s/m5", "password": "", "note": "某电影 2024 备用链接", "datetime": "2026-09-26T08:00:00Z", "source": "tg:ucquark"},
    {"url": "https://pan.quark.cn/s/m6", "password": "", "note": "某电影 1999 国语", "datetime": "2026-09-25T08:00:00Z", "source": "plugin:hunhepan"},
]


class PanSouFake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        kw = (parse_qs(urlparse(self.path).query).get("kw") or [""])[0]
        res = PANSOU_MOVIE if "某电影" in kw else PANSOU_QYN if "庆余年" in kw else PANSOU_RESULTS
        d = {"status": "ok"} if self.path.startswith("/api/health") else {"code": 0, "message": "success", "data": {"total": len(res), "merged_by_type": {"quark": res}}}
        b = json.dumps(d).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)


def build():
    threading.Thread(target=HTTPServer(("127.0.0.1", 5998), Tmdb).serve_forever, daemon=True).start()
    threading.Thread(target=HTTPServer(("127.0.0.1", 5997), PanSouFake).serve_forever, daemon=True).start()
    store = Store(os.path.join(os.environ["CONFIG_DIR"], "config.json"))
    store.data["cookie"] = "fake"
    store.data["tmdb"].update(api_key="k", base_url="http://127.0.0.1:5998")
    store.data["pansou"].update(enabled=True, url="http://127.0.0.1:5997")
    eng = Engine(store)
    shares = {"aaa": {"files": [f"庆余年.第{i:02d}集.mp4" for i in range(1, 9)]},
              "bbb": {"files": [f"S02E{i:02d}.2160p.mkv" for i in range(1, 11)]},
              "dead": {"status": 400, "msg": "分享已取消"},
              "m1": {"files": [("某电影.2024.2160p.HDR.mkv", 8 * 1024 ** 3), ("花絮.mp4", 80 * 1024 ** 2), ("海报.jpg", 2048)]},
              "m2": {"files": [("某电影.2024.1080p.mkv", 3 * 1024 ** 3)]},
              "m3": {"files": [("某电影.2024.TS.1080p.mkv", 3 * 1024 ** 3)]},
              "m4": {"files": [(f"某电影.S01E{i:02d}.mkv", 1024 ** 3) for i in range(1, 13)]},
              "m5": {"status": 400, "msg": "分享已取消"},
              "q1": {"files": [(f"庆余年.S02E{i:02d}.2160p.HDR.mkv", 2 * 1024 ** 3) for i in range(1, 17)]},
              "q2": {"files": [(f"庆余年.S02E{i:02d}.1080p.mkv", 1200 * 1024 ** 2) for i in range(1, 11)]},
              "q3": {"files": [(f"庆余年.S01E{i:02d}.mkv", 1024 ** 3) for i in range(1, 4)]},
              "q4": {"files": [(f"庆余年.S02E{i:02d}.2160p.HDR.mkv", 30 * 1024 ** 2) for i in range(1, 6)]},
              "q5": {"files": [(f"庆余年.第{i:02d}集.mp4", 1024 ** 3) for i in range(1, 17)]},
              "q6": {"status": 400, "msg": "分享已取消"},
              "q7": {"files": [("繁花.S01E01.mkv", 1024 ** 3)]},
              "ccc": {"files": [(f"法医秦明之龙番往事.第{i:02d}集.1080P.mp4", 900 * 1024 * 1024) for i in range(1, 17)]},
              "ddd": {"files": [(f"龙番往事{i:02d}.mp4", 600 * 1024 * 1024) for i in range(1, 13)] + [("幕后花絮.mp4", 200 * 1024 * 1024), ("彩蛋.mp4", 100 * 1024 * 1024)]}}
    fq = FakeQuark(shares)
    fq.path2fid.update({"/影视": "R1", "/影视/剧集": "R2"}); fq.dirs.update({"R1": {}, "R2": {}})
    real_ls = fq.ls_dir
    fq.ls_dir = lambda fid: ([{"fid": "R1", "file_name": "影视", "dir": True}, {"fid": "R9", "file_name": "电影", "dir": True}] if fid == "0"
                             else [{"fid": "R2", "file_name": "剧集", "dir": True}] if fid == "R1" else real_ls(fid) if fid in fq.dirs else [])
    fq.nickname = "测试用户"
    eng.quark = lambda: fq
    eng._emby = FakeEmby({(2, i) for i in range(1, 6)}); eng.emby = lambda: None
    eng.tick = lambda: None
    import app.main as M
    M.Quark = type("Q", (), {"__init__": lambda s, c="": setattr(s, "nickname", "测试用户") or setattr(s, "mparam", {}),
                              "init": lambda s: {"nickname": "测试用户"}})
    return create_app(store, eng), store, eng


if __name__ == "__main__":
    app, store, eng = build()
    from waitress import serve
    serve(app, host="127.0.0.1", port=5099)
