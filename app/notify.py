# -*- coding: utf-8 -*-
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


def trigger_smartstrm(cfg, savepath, force=False):
    if not (cfg.get("enabled") or force) or not cfg.get("webhook"):
        return False, "SmartStrm 联动未启用或未填写 Webhook"
    body = {"event": cfg.get("event") or "qas_strm"}
    if savepath and cfg.get("send_savepath", True):
        body["savepath"] = savepath  # 只触发转存的目标文件夹，秒级生成
    if cfg.get("strmtask"):
        body["strmtask"] = cfg["strmtask"]
    try:
        r = requests.post(cfg["webhook"], json=body, timeout=10)
        r.raise_for_status()
        return True, f"已触发（HTTP {r.status_code}）"
    except Exception as e:
        log.warning("SmartStrm 触发失败: %s", e)
        return False, f"触发失败：{e}"
