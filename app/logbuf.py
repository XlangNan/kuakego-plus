# -*- coding: utf-8 -*-
"""日志：内存环形缓冲（给 WebUI）+ 轮转文件。"""
import collections
import itertools
import logging
import logging.handlers
import os
import threading
from datetime import datetime

_counter = itertools.count(1)


class RingHandler(logging.Handler):
    def __init__(self, capacity=2000):
        super().__init__()
        self.buf = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record):
        item = {
            "id": next(_counter),
            "ts": datetime.fromtimestamp(record.created).strftime("%m-%d %H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        with self._lock:
            self.buf.append(item)

    def since(self, last_id=0, level=None, limit=500):
        order = ["DEBUG", "INFO", "WARNING", "ERROR"]
        with self._lock:
            items = [i for i in self.buf if i["id"] > last_id]
        if level in order:
            items = [i for i in items if order.index(i["level"]) >= order.index(level)]
        return items[-limit:]

    def clear(self):
        with self._lock:
            self.buf.clear()


ring = RingHandler()
log = logging.getLogger("qp")


def setup(config_dir):
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log.addHandler(ring)
    try:
        os.makedirs(config_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(config_dir, "run.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
