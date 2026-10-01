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

SHOWS = [{"id": i, "name": n, "first_air_date": d, "vote_average": v, "overview": "这是一段剧情简介，用来检查两行截断和排版效果是否正常。" * 2,
          "poster_path": None, "backdrop_path": None, "origin_country": ["CN"]}
         for i, (n, d, v) in enumerate([("庆余年 第二季", "2024-05-16", 8.3), ("长相思", "2024-07-22", 7.9), ("繁花", "2023-12-27", 8.5),
                                         ("三体", "2023-01-15", 8.1), ("漫长的季节", "2023-04-22", 9.4), ("狂飙", "2023-01-14", 8.6),
                                         ("去有风的地方", "2023-01-02", 7.7), ("莲花楼", "2023-07-19", 8.2)], 1)]


class Tmdb(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/tv/") and "/season/" in p:
            d = {"episodes": [{"episode_number": i, "air_date": "2024-05-%02d" % i if i <= 8 else "2099-01-01"} for i in range(1, 11)]}
        elif p.startswith("/tv/"):
            s = SHOWS[int(p.split("/")[2]) - 1]
            d = {**s, "genres": [{"name": "剧情"}, {"name": "古装"}], "status": "Returning Series", "vote_count": 420,
                 "number_of_seasons": 2, "number_of_episodes": 46,
                 "seasons": [{"season_number": 1, "name": "第 1 季", "episode_count": 46, "air_date": "2019-11-26"},
                             {"season_number": 2, "name": "第 2 季", "episode_count": 36, "air_date": "2024-05-16"}],
                 "credits": {"cast": [{"name": "张若昀", "character": "范闲"}, {"name": "李沁", "character": "林婉儿"}]}}
        else:
            d = {"results": SHOWS, "total_pages": 3}
        b = json.dumps(d).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)


def build():
    threading.Thread(target=HTTPServer(("127.0.0.1", 5998), Tmdb).serve_forever, daemon=True).start()
    store = Store(os.path.join(os.environ["CONFIG_DIR"], "config.json"))
    store.data["cookie"] = "fake"
    store.data["tmdb"].update(api_key="k", base_url="http://127.0.0.1:5998")
    eng = Engine(store)
    shares = {"aaa": {"files": [f"庆余年.第{i:02d}集.mp4" for i in range(1, 9)]},
              "bbb": {"files": [f"S02E{i:02d}.2160p.mkv" for i in range(1, 11)]},
              "dead": {"status": 400, "msg": "分享已取消"}}
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
