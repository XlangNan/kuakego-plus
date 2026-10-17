"""从浏览器「复制为 cURL (bash)」的文本里提取登录参数，让程序的请求尽量和浏览器一致。
只在本机解析、写入本地配置，不会把内容发到任何地方；返回的摘要里只有字段名，没有值。"""
import codecs
import json
import re
import shlex
from urllib.parse import urlparse

_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
# 带参数、但我们不关心的选项
_SKIP_WITH_ARG = {"-b", "--cookie", "-o", "--output", "-u", "--user", "-m", "--max-time", "--connect-timeout",
                  "-x", "--proxy", "--cacert", "-w", "--write-out", "--url-query"}
_DATA_OPTS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"}
# 不导入的请求头：敏感、或由 HTTP 库自己处理
_SKIP_HEADERS = {"authorization", "cookie", "host", "content-length", "content-type", "accept", "accept-encoding",
                 "connection", "pragma", "cache-control", "priority", "dnt", "upgrade-insecure-requests",
                 "x-captcha-token", "x-client-id", "x-device-id", "user-agent"}


def _decode_ansi_c(m):
    s = m.group(1)
    try:
        s = codecs.decode(s.encode("latin-1", "backslashreplace"), "unicode_escape")
    except Exception:
        pass
    return shlex.quote(s)


def parse(text):
    """返回 [{url, method, headers(小写名), body}]。"""
    text = re.sub(r"\\\r?\n", " ", text or "")
    text = _ANSI_C.sub(_decode_ansi_c, text)
    chunks = [c for c in re.split(r"(?im)^(?=\s*curl\s)", text) if c.strip().lower().startswith("curl")]
    out = []
    for chunk in chunks:
        try:
            toks = shlex.split(chunk)
        except ValueError:
            continue
        req = {"url": "", "method": "", "headers": {}, "body": ""}
        i = 1
        while i < len(toks):
            t = toks[i]
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if t in ("-H", "--header"):
                if ":" in nxt:
                    k, v = nxt.split(":", 1)
                    req["headers"][k.strip().lower()] = v.strip()
                i += 2
            elif t in ("-X", "--request"):
                req["method"] = nxt.upper()
                i += 2
            elif t in _DATA_OPTS:
                req["body"] = nxt
                i += 2
            elif t in ("-A", "--user-agent"):
                req["headers"]["user-agent"] = nxt
                i += 2
            elif t in ("-e", "--referer"):
                req["headers"]["referer"] = nxt
                i += 2
            elif t in _SKIP_WITH_ARG:
                i += 2
            elif t.startswith("http://") or t.startswith("https://"):
                req["url"] = t
                i += 1
            else:
                i += 1
        if req["url"]:
            req["method"] = req["method"] or ("POST" if req["body"] else "GET")
            out.append(req)
    return out


def apply(reqs, acct):
    """把解析结果合并进 account 配置（原地修改 acct），返回给人看的摘要列表（只含字段名）。"""
    notes = []
    for r in reqs:
        u = urlparse(r["url"])
        if "xunlei.com" not in u.netloc:
            notes.append("忽略了不是迅雷的请求：%s" % u.netloc)
            continue
        h = r["headers"]
        got = []
        if u.path.endswith("/v1/shield/captcha/init"):
            try:
                body = json.loads(r["body"]) if r["body"] else {}
            except ValueError:
                body = {}
            if body.get("client_id"):
                acct["client_id"] = body["client_id"]
                got.append("client_id")
            if body.get("device_id"):
                acct["device_id"] = body["device_id"]
                got.append("device_id")
            action = str(body.get("action") or "")
            if ":" in action:
                acct["captcha_action_style"] = "lower" if action.split(":", 1)[0].islower() else "upper"
                got.append("action 的写法（%s）" % action.split(":", 1)[0])
            meta = body.get("meta")
            if isinstance(meta, dict):
                acct["captcha_meta"] = {k: str(v) for k, v in meta.items()}
                got.append("captcha_meta（%s）" % "、".join(meta.keys()))
                if meta.get("user_id"):
                    acct["user_id"] = str(meta["user_id"])
                for k in ("client_version", "package_name"):
                    if meta.get(k):
                        acct[k] = str(meta[k])
                if any(k in meta for k in ("captcha_sign", "timestamp")):
                    notes.append("captcha/init 的 meta 里带 captcha_sign / timestamp：这是按时间计算的签名，"
                                 "抓到的固定值只在短时间内有效；要长期可用需要在账号页填「签名算法串」让程序自己算")
            extra = {k: v for k, v in body.items() if k not in ("client_id", "device_id", "action", "meta")}
            if extra:
                acct["captcha_extra"] = extra
                got.append("申请体里的其他字段（%s）" % "、".join(extra.keys()))
            notes.append("从 captcha/init 请求导入：" + ("、".join(got) or "没有认出可用字段"))
        else:
            for src, dst in (("x-client-id", "client_id"), ("x-device-id", "device_id"),
                             ("x-captcha-token", "captcha_token"), ("user-agent", "user_agent"),
                             ("x-client-version", "client_version")):
                if h.get(src):
                    acct[dst] = h[src]
                    got.append(dst)
            extra = {k: v for k, v in h.items() if k not in _SKIP_HEADERS and not k.startswith("sec-")}
            if extra:
                acct.setdefault("extra_headers", {}).update(extra)
                got.append("额外请求头（%s）" % "、".join(extra.keys()))
            notes.append("从 %s%s 请求导入：%s" % (u.netloc, u.path, "、".join(got) or "没有认出可用字段"))
    return notes
