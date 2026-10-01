# -*- coding: utf-8 -*-
"""
读取 config/hosts（格式同系统 hosts），启动时写入容器的 /etc/hosts，并监视文件变化自动重新写入。
用于国内网络访问不了 TMDB 等域名时手动指定 IP。
"""
import ipaddress
import os
import threading
import time

from .logbuf import log

BEGIN = "# >>> quark-plus hosts（自动生成，请改 config/hosts，不要改这里）>>>"
END = "# <<< quark-plus hosts <<<"

TEMPLATE = """# quark-plus 自定义 hosts —— 格式和系统 hosts 一样：IP 空格 域名
# 保存后约 10 秒内自动生效，不用重启容器；也会在每次容器启动时写入。
# 以 # 开头的是注释。下面两行是示例，把 IP 换成你查到的真实 IP、去掉行首的 # 即可：
#
#1.2.3.4  api.themoviedb.org
#1.2.3.4  image.tmdb.org
#
# 怎么查 IP：在 NAS 上执行
#   curl -s -H 'accept: application/dns-json' 'https://dns.alidns.com/resolve?name=api.themoviedb.org&type=A'
# 返回结果里的 "data" 就是 IP。IP 失效（TMDB 又连不上）时回来改这里。
"""


def parse(text):
    """-> (条目列表[(ip, [域名...])], 无效行列表)"""
    entries, bad = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        try:
            ipaddress.ip_address(parts[0])
            if len(parts) < 2:
                raise ValueError
        except ValueError:
            bad.append(f"第 {n} 行：{raw.strip()}")
            continue
        entries.append((parts[0], parts[1:]))
    return entries, bad


def _strip_block(text):
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith("# >>> quark-plus hosts"):
            skip = True
            continue
        if skip:
            if line.startswith("# <<< quark-plus hosts"):
                skip = False
            continue
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def apply(config_dir, etc_hosts="/etc/hosts"):
    """返回写入的条目数；失败返回 None。可重复调用（幂等）。"""
    path = os.path.join(config_dir, "hosts")
    try:
        if not os.path.exists(path):
            os.makedirs(config_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(TEMPLATE)
            log.info("已生成 hosts 模板：config/hosts（需要时编辑它）")
        with open(path, "r", encoding="utf-8") as f:
            entries, bad = parse(f.read())
    except OSError as e:
        log.warning("读取 config/hosts 失败：%s", e)
        return None
    for b in bad:
        log.warning("config/hosts 有无效行，已忽略 —— %s", b)

    try:
        with open(etc_hosts, "r", encoding="utf-8") as f:
            cur = f.read()
        new = _strip_block(cur)
        if entries:
            lines = [f"{ip}\t{' '.join(names)}" for ip, names in entries]
            new += "\n".join([BEGIN, *lines, END]) + "\n"
        if new != cur:
            with open(etc_hosts, "w", encoding="utf-8") as f:  # 原地写：/etc/hosts 在容器里是挂载文件，不能替换
                f.write(new)
            if entries:
                log.info("已写入 hosts：%s", "；".join(f"{' '.join(n)} → {ip}" for ip, n in entries))
            else:
                log.info("config/hosts 里没有有效条目，已清除之前写入的")
    except OSError as e:
        log.warning("写入 %s 失败（需要 root 权限）：%s", etc_hosts, e)
        return None
    return len(entries)


def watch(config_dir, etc_hosts="/etc/hosts", every=10):
    """后台线程：config/hosts 被修改后自动重新应用。"""
    path = os.path.join(config_dir, "hosts")

    def mtime():
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    def loop():
        last = mtime()
        while True:
            time.sleep(every)
            cur = mtime()
            if cur != last:
                last = cur
                log.info("检测到 config/hosts 已修改，重新应用")
                apply(config_dir, etc_hosts)

    threading.Thread(target=loop, daemon=True, name="hosts-watch").start()
