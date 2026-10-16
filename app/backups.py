# -*- coding: utf-8 -*-
"""配置自动备份：每天把 config.json 拷一份到 config/backups/，只留最近几份；页面上可以一键恢复。
备份里有 Cookie 和各种密钥（和手动导出的备份一样），文件权限设成仅本人可读。"""
import json
import os
import re
import time

from .logbuf import log

NAME_RE = re.compile(r"^kuakego-(auto|manual|pre)-\d{8}-\d{6}\.json$")


class Backups:
    def __init__(self, store):
        self.store = store

    @property
    def dir(self):
        return os.path.join(os.path.dirname(self.store.path), "backups")

    def snapshot(self, tag="auto"):
        """写一份备份，返回文件名。tag：auto 每日自动 / manual 手动点的 / pre 恢复前自动留的。"""
        os.makedirs(self.dir, exist_ok=True)
        name = time.strftime(f"kuakego-{tag}-%Y%m%d-%H%M%S.json")
        path = os.path.join(self.dir, name)
        with self.store.lock:
            raw = json.dumps(self.store.data, ensure_ascii=False, indent=2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._rotate(tag)
        return name

    def _keep(self):
        try:
            return max(1, min(60, int(self.store.data["auto"].get("backup_keep", 7))))
        except (TypeError, ValueError):
            return 7

    def _rotate(self, tag):
        """每种各留最近 N 份（恢复前自动留的 pre 只留 5 份）。"""
        keep = 5 if tag == "pre" else self._keep()
        names = sorted((n for n in os.listdir(self.dir) if NAME_RE.match(n) and n.startswith(f"kuakego-{tag}-")), reverse=True)
        for n in names[keep:]:
            try:
                os.remove(os.path.join(self.dir, n))
            except OSError as ex:
                log.warning("清理旧备份 %s 失败：%s", n, ex)

    def list(self):
        if not os.path.isdir(self.dir):
            return []
        out = []
        for n in sorted(os.listdir(self.dir), reverse=True):
            if not NAME_RE.match(n):
                continue
            p = os.path.join(self.dir, n)
            try:
                st = os.stat(p)
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                tasks = len(d.get("tasks", [])) if isinstance(d, dict) else 0
            except (OSError, ValueError):
                continue
            out.append({"name": n, "size": st.st_size, "ts": int(st.st_mtime), "tasks": tasks, "kind": NAME_RE.match(n).group(1)})
        return sorted(out, key=lambda x: -x["ts"])

    def path(self, name):
        """只认我们自己生成的文件名，防止路径穿越。"""
        if not NAME_RE.match(name or ""):
            return None
        p = os.path.join(self.dir, name)
        return p if os.path.isfile(p) else None

    def tick(self, due):
        """维护线程每轮调用：开了自动备份、且距离上次满一天才备份。due(key, every) 是限频函数。"""
        if self.store.data["auto"].get("backup_auto", True) and due("backup", 20 * 3600):
            try:
                n = self.snapshot("auto")
                log.info("已自动备份配置：%s", n)
            except OSError as ex:
                log.warning("自动备份失败：%s", ex)
