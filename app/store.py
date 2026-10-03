# -*- coding: utf-8 -*-
"""配置存储：单个 JSON 文件，原子写入。"""
import copy
import json
import os
import tempfile
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "cookie": "",
    "auto_sign": True,
    "last_sign_date": "",
    "tasks": [],
    "recent": [],          # 最近入库事件（新→旧，最多 200 条）
    "dingtalk": {
        "enabled": False, "webhook": "", "secret": "",
        "on_add": True, "on_save": True, "on_error": True,
    },
    "tmdb": {
        "api_key": "", "base_url": "https://api.themoviedb.org/3",
        "image_base": "https://image.tmdb.org/t/p/w342",
        "language": "zh-CN", "region": "CN",
    },
    "emby": {
        "enabled": False, "url": "", "api_key": "", "user_id": "",
        "only_missing": True, "refresh_after_save": True, "refresh_delay": 10,
    },
    "pansou": {
        "enabled": False, "url": "", "username": "", "password": "",
        "src": "all", "channels": "", "plugins": "", "timeout": 30,
    },
    "smartstrm": {
        "enabled": False, "webhook": "", "strmtask": "", "event": "qas_strm", "delay": 3,
        "send_savepath": True,
    },
}

# 这些字段不会通过 GET 返回明文
SECRET_FIELDS = {
    "dingtalk": ["webhook", "secret"],
    "tmdb": ["api_key"],
    "emby": ["api_key"],
    "smartstrm": ["webhook"],
    "pansou": ["password"],
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        with self.lock:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = _merge(DEFAULTS, json.load(f))

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)  # 含 cookie / token
            except OSError:
                pass

    def public_section(self, name):
        """返回某个设置分区，密钥类字段替换为 xxx_set 标记。"""
        with self.lock:
            sec = copy.deepcopy(self.data[name])
            for f in SECRET_FIELDS.get(name, []):
                sec[f + "_set"] = bool(sec.get(f))
                sec[f] = ""
            return sec

    def update_section(self, name, payload):
        """密钥字段：空字符串=保持不变，"__clear__"=清除。"""
        with self.lock:
            sec = self.data[name]
            for k, v in payload.items():
                if k not in DEFAULTS[name]:
                    continue
                if k in SECRET_FIELDS.get(name, []):
                    if v == "__clear__":
                        sec[k] = ""
                    elif v:
                        sec[k] = v.strip() if isinstance(v, str) else v
                    continue
                default = DEFAULTS[name][k]
                if isinstance(default, bool):
                    sec[k] = bool(v)
                elif isinstance(default, int):
                    try:
                        sec[k] = int(v)
                    except (TypeError, ValueError):
                        pass
                else:
                    sec[k] = (v or "").strip() if isinstance(v, str) else v
            self.save()
