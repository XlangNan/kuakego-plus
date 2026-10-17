# -*- coding: utf-8 -*-
"""分享链接文本解析 + 文件名集数解析。"""
import os
import re

# GuessIt 是可选依赖：装了就在内置规则解析不出时兜底，没装 / 设了 QP_NO_GUESSIT=1 就只用内置规则
try:
    if os.environ.get("QP_NO_GUESSIT"):
        raise ImportError
    from guessit import guessit as _guessit
except Exception:  # noqa: BLE001  任何导入问题都不应影响主程序
    _guessit = None

# ---------------------------------------------------------------- 分享链接
_URL_RE = re.compile(
    r"(?:https?://)?pan\.quark\.cn/s/(\w+)(?:\?[^\s#]*)?(?:#/list/share[^\s]*)?", re.I
)
_PWD_RE = re.compile(r"(?:提取码|访问码|密码|pwd|passcode)\s*[:：=]?\s*([A-Za-z0-9]{4,8})", re.I)


_XL_RE = re.compile(r"(?:https?://)?pan\.xunlei\.com/s/([A-Za-z0-9_\-]+)(?:\?[^\s#]*)?(?:#[^\s]*)?", re.I)


def parse_share_text(text):
    """
    从任意粘贴文本里识别 [{url, pwd_id, passcode, drive}]，支持多段，夸克和迅雷链接都认。
    提取码优先取 url 里的 pwd=，其次取紧跟其后的"提取码：xxxx"，再次取其前面的。
    """
    text = text or ""
    matches = sorted([(m, "quark") for m in _URL_RE.finditer(text)] + [(m, "xunlei") for m in _XL_RE.finditer(text)],
                     key=lambda x: x[0].start())
    out, seen = [], set()
    for i, (m, drive) in enumerate(matches):
        raw = m.group(0)
        pwd_id = m.group(1)
        q = re.search(r"pwd=(\w+)", raw)
        passcode = q.group(1) if q else ""
        if not passcode:
            end = matches[i + 1][0].start() if i + 1 < len(matches) else len(text)
            after = _PWD_RE.search(text[m.end():end])
            if after:
                passcode = after.group(1)
            else:
                start = matches[i - 1][0].end() if i > 0 else 0
                before = _PWD_RE.search(text[max(start, m.start() - 30):m.start()])
                if before:
                    passcode = before.group(1)
        frag = re.search(r"#/list/share.*", raw) if drive == "quark" else None
        url = f"https://pan.{'quark.cn' if drive == 'quark' else 'xunlei.com'}/s/{pwd_id}"
        if passcode:
            url += f"?pwd={passcode}"
        if frag:
            url += frag.group(0)
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "pwd_id": pwd_id, "passcode": passcode, "drive": drive})
    return out


# ---------------------------------------------------------------- 集数解析
VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".rmvb", ".webm", ".iso",
}

_CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s):
    """一 ~ 九十九 的中文数字转换；失败返回 None。"""
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN_NUM and c != "十" for c in s):
        return None
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CN_NUM[a] if a else 1) * 10 + (_CN_NUM[b] if b else 0)
    return _CN_NUM[s] if len(s) == 1 else None


_NUM = r"(\d{1,4}|[零〇一二两三四五六七八九十]{1,3})"
_RE_SXXEXX = re.compile(
    r"(?<![A-Za-z0-9])[Ss](\d{1,2})[\s._-]*[Ee][Pp]?(\d{1,4})"
    r"(?:(?:-\s*[Ee]?[Pp]?|[Ee][Pp]?)(\d{1,4}))?(?!\d)"
)
_RE_NXNN = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})[xX](\d{2,3})(?!\d)")
_RE_CN_EP = re.compile(r"第\s*" + _NUM + r"\s*[集话話期章回]")
_RE_E_ONLY = re.compile(r"(?<![A-Za-z])[Ee][Pp]?[\s._-]?(\d{1,4})(?!\d)")
# 数字前面可以是分隔符，也可以直接贴在汉字后面（"某某剧01.mp4" 很常见）
_RE_BARE = re.compile(r"(?:^|[\[\s._\-(（【]|(?<=[\u4e00-\u9fff]))(\d{1,3})(?:[vV]\d)?(?=$|[\]\s._\-)）】])")
_RE_N_JI = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,4})\s*[集话話期](?![\u4e00-\u9fff])")  # "01集"（没写"第"）
_RE_SEASON_CN = re.compile(r"第\s*" + _NUM + r"\s*季")
_RE_SEASON_EN = re.compile(r"(?<![A-Za-z])(?:[Ss]eason|[Ss])[\s._-]?(\d{1,2})(?![\dA-Za-z])")
_RE_AUDIO = re.compile(r"(?<!\d)[257][.\-][01](?!\d)")


def is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def season_from_text(text, default=None):
    m = _RE_SEASON_CN.search(text)
    if m:
        n = cn2int(m.group(1))
        if n is not None:
            return n
    m = _RE_SEASON_EN.search(text)
    if m:
        return int(m.group(1))
    return default


def _as_ints(v):
    if isinstance(v, bool) or v is None:
        return []
    if isinstance(v, int):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, int) and not isinstance(x, bool)]
    return []


def guess_episodes(filename, default_season=1):
    """用 GuessIt 兜底。没装、出错、结果可疑时一律返回 []。"""
    if _guessit is None:
        return []
    try:
        g = _guessit(filename, {"type": "episode"})
    except Exception:  # noqa: BLE001
        return []
    eps = _as_ints(g.get("episode"))
    if not eps or len(eps) > 30 or any(not 0 < e < 1000 for e in eps):
        return []
    seasons = _as_ints(g.get("season"))
    season = seasons[0] if seasons else season_from_text(os.path.splitext(filename)[0], default_season)
    return [(season, e) for e in eps]


def parse_episodes(filename, default_season=1):
    """
    文件名 -> [(season, episode), ...]；解析不出返回 []。
    调用方对 [] 应当放行，退回到"按文件名判重"。
    先用内置规则，解析不出再交给 GuessIt（如果装了）。
    """
    if not filename or re.search(r"\{I+\}", filename):
        return []
    return _builtin_episodes(filename, default_season) or guess_episodes(filename, default_season)


def _builtin_episodes(filename, default_season=1):
    stem = os.path.splitext(filename)[0]

    m = _RE_SXXEXX.search(stem)
    if m:
        s, e1 = int(m.group(1)), int(m.group(2))
        e2 = int(m.group(3)) if m.group(3) else e1
        if e2 < e1 or e2 - e1 > 20:
            e2 = e1
        return [(s, e) for e in range(e1, e2 + 1)]

    m = _RE_NXNN.search(stem)
    if m:
        return [(int(m.group(1)), int(m.group(2)))]

    season = season_from_text(stem, default_season)

    m = _RE_CN_EP.search(stem)
    if m:
        n = cn2int(m.group(1))
        if n is not None:
            return [(season, n)]

    m = _RE_N_JI.search(stem)
    if m:
        return [(season, int(m.group(1)))]

    m = _RE_E_ONLY.search(stem)
    if m:
        return [(season, int(m.group(1)))]

    cleaned = _RE_AUDIO.sub(" ", stem)
    cands = [int(x) for x in _RE_BARE.findall(cleaned) if 0 < int(x) < 1000]
    if cands:
        return [(season, cands[-1])]  # 标题在前、集数在后，取最后一个
    return []


# ---------------------------------------------------------------- 画质识别
_RES_RULES = [("4k", re.compile(r"2160[pi]|(?<![a-z0-9])4k(?![a-z0-9])|(?<![a-z])uhd(?![a-z])|3840x2160", re.I)),
              ("1080p", re.compile(r"1080[pi]|1920x1080|(?<![a-z])fhd(?![a-z])", re.I)),
              ("720p", re.compile(r"720[pi]|1280x720", re.I))]
# 注意：只认「杜比视界」，不认单独的「杜比」——那多半是杜比音效（全景声），和画面无关
_HDR_RE = re.compile(r"(?<![a-z])hdr|dolby[\s._-]?vision|(?<![a-z])dovi(?![a-z])|(?<![a-z])dv(?![a-z])|(?<![a-z])hlg(?![a-z])|杜比视界", re.I)
_SDR_RE = re.compile(r"(?<![a-z])sdr(?![a-z])", re.I)
# 枪版 / 抢先版（TC、TS、CAM 等）。文件名要先去掉扩展名再找：.ts 是常见的视频容器扩展名，不能算枪版
_CAM_RE = re.compile(r"(?<![a-z0-9])(?:hd)?(?:cam|tc|ts)(?![a-z0-9])|telesync|telecine|枪版|抢先版|抢鲜版", re.I)
RES_LABEL = {"4k": "4K", "1080p": "1080P", "720p": "720P"}
SRC_LABEL = {"name": "文件名", "folder": "所在文件夹名", "title": "分享标题"}


def _res_of(s, multi_unknown=False):
    hits = [k for k, rx in _RES_RULES if rx.search(s or "")]
    if multi_unknown and len(hits) > 1:
        return None   # 标题 / 文件夹名里同时列了好几种分辨率（「4K 1080P 都有」），说不准是哪一种
    return hits[0] if hits else None


def quality_tags(name, folders=(), title=""):
    """
    文件 -> {"res": "4k"|"1080p"|"720p"|None, "hdr": bool, "cam": bool, "src": {...}}。
    folders 是所在的各层文件夹名（从近到远），title 是分享标题：文件名自己没写的，沿着文件夹往上找，最后才用分享标题。
    越靠近文件的信息越优先；文件夹 / 标题里同时出现矛盾信息（HDR 和 SDR、好几种分辨率）时视为没写。
    src 记录每项是从哪里认出来的（name / folder / title），用来在原因里说明白。
    """
    name = name or ""
    srcs = [(name, "name")] + [(c, "folder") for c in folders if c] + ([(title, "title")] if title else [])
    res, hdr, cam, src = None, None, False, {}
    for text, where in srcs:
        own = where == "name"
        if res is None:
            r = _res_of(text, multi_unknown=not own)
            if r:
                res, src["res"] = r, where
        if hdr is None:
            h, sd = bool(_HDR_RE.search(text)), bool(_SDR_RE.search(text))
            if h and not (sd and not own):
                hdr, src["hdr"] = True, where
            elif sd and not h:
                hdr, src["hdr"] = False, where
        if not cam and _CAM_RE.search(os.path.splitext(text)[0] if own else text):
            cam, src["cam"] = True, where
    return {"res": res, "hdr": bool(hdr), "cam": cam, "src": src}


def quality_reason(want_res, want_hdr, name, no_cam=False, folders=(), title=""):
    """
    按用户选的画质要求检查一个文件。符合返回 ""，不符合返回原因。
    - 分辨率：写明了分辨率、且不在想要的范围内才算不符合；没写的无法判断，放行。
    - HDR "yes"：HDR 版一定会标出来，所以文件名 / 文件夹名 / 分享标题里都没有 HDR / 杜比视界 标记的视为 SDR，不符合。
    - HDR "no"：任何一处（文件名、所在文件夹名、分享标题）标了 HDR 的不符合。
    """
    t = quality_tags(name, folders, title)
    where = lambda k: SRC_LABEL.get(t["src"].get(k), "文件名")
    if no_cam and t["cam"]:
        return f"疑似枪版 / 抢先版（TC、TS、CAM；见于{where('cam')}）"
    if want_res and t["res"] and t["res"] not in want_res:
        return f"分辨率是 {RES_LABEL[t['res']]}（见于{where('res')}），不在你要的范围（{'、'.join(RES_LABEL[r] for r in want_res if r in RES_LABEL)}）内"
    if want_hdr == "yes" and not t["hdr"]:
        return "你要 HDR，但文件名、所在文件夹名和分享标题里都没有 HDR / 杜比视界 标记"
    if want_hdr == "no" and t["hdr"]:
        return f"你不要 HDR，但{where('hdr')}里标了 HDR"
    return ""


def norm_title(s):
    """去掉空格和标点、转小写，用来判断「搜索结果的标题里有没有这部剧的名字」。"""
    return re.sub(r"[\W_]+", "", (s or "").lower())


def title_seasons(title, name=""):
    """标题里提到的季号：第二季 / S2 / Season 2 / 名字后面紧跟的数字（庆余年2）。"""
    out = set()
    for m in _RE_SEASON_CN.finditer(title or ""):
        n = cn2int(m.group(1))
        if n is not None:
            out.add(n)
    for m in re.finditer(r"(?<![A-Za-z])(?:[Ss]eason[\s._-]?|[Ss])(\d{1,2})(?![\dA-Za-z])", title or ""):
        out.add(int(m.group(1)))
    n = norm_title(name)
    if n:
        for m in re.finditer(re.escape(n) + r"(\d{1,2})(?![\dA-Za-z])", norm_title(title)):
            out.add(int(m.group(1)))
    return out


def safe_name(s):
    """去掉文件系统不允许的字符，连续空白压成一个空格。"""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", s or "")).strip()


def emby_folder_name(name, year=0, tmdb_id=0):
    """Emby 推荐的文件夹命名：觉醒年代 (2021) {tmdb-117954}。年份 / TMDB 编号没有就省略。"""
    out = safe_name(name)
    if year:
        out += f" ({year})"
    if tmdb_id:
        out += f" {{tmdb-{int(tmdb_id)}}}"
    return out


def fmt_eps(eps):
    """[(1,6),(1,7),(1,8),(2,1)] -> 'S01E06-E08, S02E01'"""
    eps = sorted(set(eps))
    out, i = [], 0
    while i < len(eps):
        j = i
        while j + 1 < len(eps) and eps[j + 1][0] == eps[i][0] and eps[j + 1][1] == eps[j][1] + 1:
            j += 1
        s, a = eps[i]
        out.append(f"S{s:02d}E{a:02d}" if i == j else f"S{s:02d}E{a:02d}-E{eps[j][1]:02d}")
        i = j + 1
    return ", ".join(out)
