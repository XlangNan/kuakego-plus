# -*- coding: utf-8 -*-
"""夸克网盘 API 客户端。

接口封装改写自 Cp0204/quark-auto-save（AGPL-3.0），仅保留本项目用到的部分。
"""
import re
import random
import time
import urllib.parse
from datetime import datetime

import requests


class QuarkError(Exception):
    pass


class Quark:
    BASE_URL = "https://drive-pc.quark.cn"
    BASE_URL_APP = "https://drive-m.quark.cn"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
    )

    def __init__(self, cookie=""):
        self.cookie = (cookie or "").strip()
        self.nickname = ""
        self.mparam = self._match_mparam(self.cookie)

    # ---------------- 基础 ----------------
    @staticmethod
    def _match_mparam(cookie):
        out = {}
        for key in ("kps", "sign", "vcode"):
            m = re.search(rf"(?<!\w){key}=([a-zA-Z0-9%+/=]+)[;&]?", cookie)
            if m:
                out[key] = m.group(1).replace("%25", "%")
        return out if len(out) == 3 else {}

    def _send(self, method, url, **kwargs):
        headers = {
            "cookie": self.cookie,
            "content-type": "application/json",
            "user-agent": self.USER_AGENT,
        }
        if "headers" in kwargs:
            headers = kwargs.pop("headers")
        # 有 kps/sign/vcode 时，分享相关接口走移动端域名（与上游一致）
        if self.mparam and "share" in url and self.BASE_URL in url:
            url = url.replace(self.BASE_URL, self.BASE_URL_APP)
            kwargs.setdefault("params", {}).update(
                {
                    "device_model": "M2011K2C", "entry": "default_clouddrive",
                    "_t_group": "0%3A_s_vp%3A1", "dmn": "Mi%2B11", "fr": "android",
                    "pf": "3300", "bi": "35937", "ve": "7.4.5.680", "ss": "411x875",
                    "mi": "M2011K2C", "nt": "5", "nw": "0", "kt": "4", "pr": "ucpro",
                    "sv": "release", "dt": "phone", "data_from": "ucapi",
                    "kps": self.mparam["kps"], "sign": self.mparam["sign"],
                    "vcode": self.mparam["vcode"], "app": "clouddrive", "kkkk": "1",
                }
            )
            headers.pop("cookie", None)
        try:
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            return resp.json()
        except Exception as e:  # 网络异常统一成 status=500，交给调用方判断
            return {"status": 500, "code": 1, "message": f"request error: {e}"}

    # ---------------- 账号 ----------------
    def init(self):
        data = self._send(
            "GET", "https://pan.quark.cn/account/info", params={"fr": "pc", "platform": "pc"}
        ).get("data")
        if data:
            self.nickname = data.get("nickname", "")
        return data or False

    def growth_info(self):
        if not self.mparam:
            return False
        return self._send(
            "GET", f"{self.BASE_URL_APP}/1/clouddrive/capacity/growth/info",
            headers={"content-type": "application/json"},
            params={"pr": "ucpro", "fr": "android", **self.mparam},
        ).get("data") or False

    def growth_sign(self):
        if not self.mparam:
            return False, "Cookie 中没有 kps/sign/vcode，无法签到（需从手机端抓取）"
        r = self._send(
            "POST", f"{self.BASE_URL_APP}/1/clouddrive/capacity/growth/sign",
            headers={"content-type": "application/json"},
            params={"pr": "ucpro", "fr": "android", **self.mparam},
            json={"sign_cyclic": True},
        )
        if r.get("data"):
            return True, r["data"]["sign_daily_reward"]
        return False, r.get("message", "签到失败")

    # ---------------- 分享 ----------------
    @staticmethod
    def extract_url(url):
        m = re.search(r"/s/(\w+)", url)
        pwd_id = m.group(1) if m else None
        m = re.search(r"pwd=(\w+)", url)
        passcode = m.group(1) if m else ""
        matches = re.findall(r"/(\w{32})-?([^/]+)?", url)
        paths = [
            {"fid": g[0], "name": urllib.parse.unquote(g[1] or "").replace("*101", "-")}
            for g in matches
        ]
        pdir_fid = paths[-1]["fid"] if matches else "0"
        return pwd_id, passcode, pdir_fid, paths

    def get_stoken(self, pwd_id, passcode=""):
        return self._send(
            "POST", f"{self.BASE_URL}/1/clouddrive/share/sharepage/token",
            params={"pr": "ucpro", "fr": "pc"},
            json={"pwd_id": pwd_id, "passcode": passcode},
        )

    def get_detail(self, pwd_id, stoken, pdir_fid):
        merged, page = [], 1
        while True:
            r = self._send(
                "GET", f"{self.BASE_URL}/1/clouddrive/share/sharepage/detail",
                params={
                    "pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
                    "pdir_fid": pdir_fid, "force": "0", "_page": page, "_size": "50",
                    "_fetch_banner": "0", "_fetch_share": 0, "_fetch_total": "1",
                    "_sort": "file_type:asc,updated_at:desc", "ver": "2",
                    "fetch_share_full_path": 0,
                },
            )
            if r.get("code") != 0:
                raise QuarkError(r.get("message", "获取分享文件列表失败"))
            items = r["data"]["list"]
            if not items:
                break
            merged += items
            page += 1
            if len(merged) >= r["metadata"]["_total"]:
                break
        return merged

    # ---------------- 自己的网盘 ----------------
    def get_fids(self, paths):
        out = []
        paths = list(paths)
        while paths:
            r = self._send(
                "POST", f"{self.BASE_URL}/1/clouddrive/file/info/path_list",
                params={"pr": "ucpro", "fr": "pc"},
                json={"file_path": paths[:50], "namespace": "0"},
            )
            if r.get("code") != 0:
                raise QuarkError(f"获取目录ID失败: {r.get('message')}")
            out += r["data"]
            paths = paths[50:]
        return out

    def ls_dir(self, pdir_fid):
        merged, page = [], 1
        while True:
            r = self._send(
                "GET", f"{self.BASE_URL}/1/clouddrive/file/sort",
                params={
                    "pr": "ucpro", "fr": "pc", "uc_param_str": "", "pdir_fid": pdir_fid,
                    "_page": page, "_size": "50", "_fetch_total": "1",
                    "_fetch_sub_dirs": "0", "_sort": "file_type:asc,updated_at:desc",
                    "_fetch_full_path": 0, "fetch_all_file": 1, "fetch_risk_file_name": 1,
                },
            )
            if r.get("code") != 0:
                raise QuarkError(r.get("message", "读取目录失败"))
            items = r["data"]["list"]
            if not items:
                break
            merged += items
            page += 1
            if len(merged) >= r["metadata"]["_total"]:
                break
        return merged

    def mkdir(self, dir_path):
        r = self._send(
            "POST", f"{self.BASE_URL}/1/clouddrive/file",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            json={"pdir_fid": "0", "file_name": "", "dir_path": dir_path, "dir_init_lock": False},
        )
        if r.get("code") != 0:
            raise QuarkError(f"创建文件夹失败: {r.get('message')}")
        return r["data"]["fid"]

    def ensure_dir(self, path):
        """返回目录 fid，不存在则创建。"""
        path = re.sub(r"/{2,}", "/", "/" + path.strip())
        if path == "/":
            return "0"
        found = self.get_fids([path])
        return found[0]["fid"] if found else self.mkdir(path)

    def rename(self, fid, name):
        return self._send(
            "POST", f"{self.BASE_URL}/1/clouddrive/file/rename",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            json={"fid": fid, "file_name": name},
        )

    def save_file(self, fid_list, fid_token_list, to_pdir_fid, pwd_id, stoken):
        r = self._send(
            "POST", f"{self.BASE_URL}/1/clouddrive/share/sharepage/save",
            params={
                "pr": "ucpro", "fr": "pc", "uc_param_str": "", "app": "clouddrive",
                "__dt": int(random.uniform(1, 5) * 60 * 1000),
                "__t": datetime.now().timestamp(),
            },
            json={
                "fid_list": fid_list, "fid_token_list": fid_token_list,
                "to_pdir_fid": to_pdir_fid, "pwd_id": pwd_id, "stoken": stoken,
                "pdir_fid": "0", "scene": "link",
            },
        )
        if r.get("code") != 0:
            raise QuarkError(f"转存失败: {r.get('message')}")
        return r["data"]["task_id"]

    def wait_task(self, task_id, timeout=120):
        deadline, retry = time.time() + timeout, 0
        while time.time() < deadline:
            r = self._send(
                "GET", f"{self.BASE_URL}/1/clouddrive/task",
                params={
                    "pr": "ucpro", "fr": "pc", "uc_param_str": "", "task_id": task_id,
                    "retry_index": retry, "__dt": int(random.uniform(1, 5) * 60 * 1000),
                    "__t": datetime.now().timestamp(),
                },
            )
            if r.get("status") != 200:
                raise QuarkError(r.get("message", "查询转存任务失败"))
            if r["data"]["status"] == 2:
                return r
            retry += 1
            time.sleep(0.5)
        raise QuarkError("等待转存任务超时")
