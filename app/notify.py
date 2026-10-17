# -*- coding: utf-8 -*-
import json
import base64
import hashlib
import hmac
import time
import urllib.parse

import requests

from .logbuf import log


def dingtalk_url(webhook, secret):
    if not secret:
        return webhook
    ts = str(round(time.time() * 1000))
    sign = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    ).decode()
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"


def send_dingtalk(cfg, title, text, force=False):
    """返回 (ok, msg)。force=True 用于"发送测试"，忽略 enabled。"""
    if not (cfg.get("enabled") or force) or not cfg.get("webhook"):
        return False, "钉钉通知未启用或未填写 Webhook"
    try:
        r = requests.post(
            dingtalk_url(cfg["webhook"], cfg.get("secret", "")),
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            timeout=10,
        ).json()
        if r.get("errcode") == 0:
            return True, "发送成功"
        return False, f"钉钉返回错误：{r.get('errmsg')}"
    except Exception as e:
        return False, f"发送失败：{e}"


def _post_ss(url, body):
    r = requests.post(url, json=body, timeout=10)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError:
        j = {}
    if isinstance(j, dict) and j.get("success") is False:
        m = str(j.get("message") or r.text[:200])
        if "驱动类型" in m:
            m += "。这个 SmartStrm 任务的驱动类型不被支持；请在订阅里取消勾选该任务"
        return False, f"SmartStrm 拒绝执行：{m}（发送：{json.dumps(body, ensure_ascii=False)}）"
    return True, f"已触发（HTTP {r.status_code}，发送：{json.dumps(body, ensure_ascii=False)}；SmartStrm 回复：{(r.text or '')[:200]}）"


def trigger_smartstrm(cfg, savepath, force=False, drive="quark"):
    if not (cfg.get("enabled") or force) or not cfg.get("webhook"):
        return False, "SmartStrm 联动未启用或未填写 Webhook"
    names = [x.strip() for x in str(cfg.get("strmtask") or "").replace("，", ",").split(",") if x.strip()]
    try:
        if (drive or "quark") != "quark" and names:
            # 其他网盘：用 SmartStrm 的「开发者 API」(a_task) 按任务名执行（带上转存目录，对不上再退回整个任务）。
            # qas_strm 事件只认夸克等内置驱动，路径对不上也会被拒绝；a_task 不需要路径。一次只能指定一个任务，逐个发
            res = []
            for n in names:
                t = {"name": n}
                if savepath:
                    t["storage_path"] = savepath      # 只生成这次转存的目录（必须是任务目录或其子目录）
                r1 = _post_ss(cfg["webhook"], {"event": "a_task", "task": t})
                if not r1[0] and savepath and "范围" in r1[1]:   # 路径对不上这个任务的目录：退一步，不限定目录执行整个任务
                    r2 = _post_ss(cfg["webhook"], {"event": "a_task", "task": {"name": n}})
                    r1 = (r2[0], ("（带路径被拒：" + r1[1][:120] + "；已改为不限定目录）" + r2[1]) if r2[0] else r1[1] + "；" + r2[1])
                res.append(r1)
            bad = [m for ok, m in res if not ok]
            return (False, "；".join(bad)) if bad else (True, "；".join(m for _, m in res))
        # 夸克：原来的 qas_strm 平铺写法；SmartStrm 必须收到 savepath，且路径要落在该任务的目录范围内
        body = {"event": cfg.get("event") or "qas_strm"}
        if savepath:
            body["savepath"] = savepath
        if names:
            body["strmtask"] = cfg["strmtask"]
        return _post_ss(cfg["webhook"], body)
    except Exception as e:
        log.warning("SmartStrm 触发失败: %s", e)
        return False, f"触发失败：{e}"


def test_smartstrm(cfg):
    """「测试触发」：没有真实的转存目录，用一个占位路径。SmartStrm 回复「路径不在任务范围内」说明请求已送达、任务名也认得，算连通。"""
    pre = [x.strip() for x in str(cfg.get("path_prefix") or "").replace("，", ",").split(",") if x.strip()]
    ok, msg = trigger_smartstrm(cfg, pre[0] if pre else "/", force=True)
    if not ok and "范围" in msg:
        return True, "连通正常：SmartStrm 收到了请求，也认得这个任务名（测试用的路径不在任务目录内，所以没有真正执行，这是正常的；实际转存会带上真实目录）。原始回复：" + msg
    return ok, msg


def clean_ss(it, old=None):
    """整理一套 SmartStrm 的配置（webhook 留空 = 沿用已保存的）。"""
    import secrets
    old = old or {}
    pf = ",".join(x.strip().rstrip("/") for x in str(it.get("path_prefix") or "").replace("，", ",").split(",") if x.strip())
    try:
        delay = max(0, min(60, int(it.get("delay", 3))))
    except (TypeError, ValueError):
        delay = 3
    return {"id": it.get("id") or secrets.token_hex(4), "name": str(it.get("name") or "").strip(),
            "enabled": bool(it.get("enabled")), "webhook": (it.get("webhook") or "").strip() or old.get("webhook", ""),
            "strmtask": str(it.get("strmtask") or "").strip(), "event": str(it.get("event") or "").strip() or "qas_strm",
            "delay": delay, "send_savepath": bool(it.get("send_savepath", True)),
            "target": it.get("target") if it.get("target") in ("emby", "fn") else "", "path_prefix": pf,
            "drive": it.get("drive") if it.get("drive") in ("quark", "xunlei") else ""}


def strm_items(cfg, drive=None):
    """可供任务选择的 SmartStrm 任务：每套（主 + 其他）已启用的 SmartStrm，按它填的「STRM 任务名」拆成一项一项；
    没填任务名的整套算一项。drive 指定时，只列出适用这个网盘的（实例的「适用网盘」留空 = 都适用）。"""
    out = []
    for iid, inst in [("main", cfg["smartstrm"])] + [(i.get("id"), i) for i in cfg.get("smartstrm_more", {}).get("items", [])]:
        if not (inst.get("enabled") and inst.get("webhook") and iid):
            continue
        if drive and inst.get("drive") and inst["drive"] != drive:
            continue
        nm = inst.get("name") or ("SmartStrm（主）" if iid == "main" else "SmartStrm")
        names = [x.strip() for x in str(inst.get("strmtask") or "").replace("，", ",").split(",") if x.strip()]
        base = {"inst": iid, "drive": inst.get("drive") or "", "target": inst.get("target") or ""}
        if names:
            out += [{**base, "key": f"{iid}|{n}", "task": n, "label": f"{nm}：{n}"} for n in names]
        else:
            out.append({**base, "key": iid, "task": "", "label": nm})
    return out


def ss_matches(inst, dest):
    """这套 SmartStrm 要不要处理转存到 dest 的这次更新（path_prefix 为空 = 都处理）。"""
    pf = [x.strip().rstrip("/") for x in (inst.get("path_prefix") or "").replace("，", ",").split(",") if x.strip()]
    d = (dest or "").rstrip("/")
    return not pf or any(d == x or d.startswith(x + "/") for x in pf)
