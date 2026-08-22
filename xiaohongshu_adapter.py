# -*- coding: utf-8 -*-
"""
创作雷达 · 小红书内容检索 Adapter（数据层）

职责：
- 对外只暴露统一接口 search_videos(query, limit)
- 返回统一结构：
    {
      id, platform:"xiaohongshu", title, author, url, cover,
      type:"video", publishedAt, likes, collects, comments,
      videoUrl, description
    }
- 三重视频过滤（规避「不存在 / 虚假」）：
    1) 数据层：只保留真实笔记页 URL（/explore/ 或 /discovery/item/）且 type == "video"
    2) 规则层：标题/描述明显为「图文 / 图片 / 纯文字」且无视频信号时丢弃
    3) 活体核验层：对每个候选真实抓取笔记页，从小红书 SSR 状态解析
       noteCard.type 确认「真实存在且确为视频」；已删除死链 / 图文冒充
       一律丢弃。抓取失败（反爬/网络）则保守保留，不误杀真实视频。
- 可插拔 provider：通过环境变量 XHS_PROVIDER 指定（默认 "none"）。
  - "none"    -> 未接入真实数据源，如实抛出 XHSSourceUnavailable（绝不返回 Mock）
  - "rest"    -> 调用受你控制的真实内容接口（XHS_REST_BASE + XHS_REST_KEY），
                 接口需返回下方 CONTRACT 约定的字段（含 type 字段）。
  - "tavily"  -> 通过合规第三方搜索 API（Tavily）在 xiaohongshu.com 域内检索，
                 再按视频偏向 + 规则过滤得到视频笔记候选（详见 _provider_tavily）。

关于 Tavily 来源的视频可信度（务必如实告知用户）：
- Tavily 返回的是「网页搜索结果」，不是小红书开放平台的结构化笔记元数据，
  因此它本身不提供 type 字段（video/image/text）。
- 本 adapter 的做法是「视频偏向检索 + 类型判定 + 规则过滤」：
    1) 检索词强制追加「视频」，且限定域名 xiaohongshu.com，只保留
       /explore/ 或 /discovery/item/ 形式的真实笔记页 URL；
    2) 类型判定按可信度从高到低：
       - 小红书分享 URL 的查询串自带 type=video / type=normal 参数
         （高可信信号）：type=video -> 视频；type=normal -> 图文/文字（丢弃）；
       - 否则回退到标题/摘要的图文信号（图文/图集/九宫格/纯文字 -> 丢弃）；
    3) 其余视为视频候选，进入候选池。
- 有 URL 的 type=video 信号支撑后，视频判定的可靠性显著高于纯启发式；
  但仍非「官方 API 逐条核验」。若要求 100% 类型确定，须接入能返回 type
  字段的官方/商业内容接口（rest provider）。
- 无论哪种来源，本文件绝不编造标题 / 作者 / URL / 点赞数等任何内容；
  无法确认是真实笔记页的内容，一律丢弃。

重要原则：
- 绝不返回 Mock。
- 只有来自真实数据源、且通过视频过滤的内容才会进入候选池。
"""

import os
import re
import json
import html
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor


class XHSSourceUnavailable(Exception):
    """真实小红书数据源不可用 / 未配置。"""
    pass


# 最近一次成功返回数据的 provider 名称（供上层如实上报来源）
LAST_SOURCE = "none"


# ---------------------------------------------------------------------------
# 外部真实数据源需要返回的字段约定（CONTRACT）
# 任意「rest」provider 只要在响应里给出下列字段（缺字段则显示「暂无数据」）：
#   items: [
#     {
#       "id":         "唯一 id（建议笔记 id）",
#       "title":      "笔记标题",
#       "author":     "作者昵称",
#       "url":        "笔记真实 URL（必须可访问）",
#       "cover":      "封面图 URL（可选）",
#       "type":       "video" | "image" | "text"，adapter 只保留 "video"
#       "publishedAt":"发布时间（可选，字符串）",
#       "likes":      数字（可选）,
#       "collects":   数字（可选）,
#       "comments":   数字（可选）,
#       "videoUrl":   "视频文件/播放页 URL（可选）",
#       "description":"正文摘要（可选）"
#     }
#   ]
# 也允许直接返回数组（顶层为 list）。
# ---------------------------------------------------------------------------

_NON_VIDEO_HINTS = ["图文", "图片", "纯文字", "图文笔记", "图集", "九宫格", "壁纸", "拼图", "手帐", "照片墙"]

# 笔记「已删除 / 不存在」死链页面会出现的文案（活体核验时据此判不存在）
_DEAD_HINTS = ("笔记不存在", "页面不见了", "内容已删除", "该笔记不存在",
               "笔记已删除", "你访问的页面", "笔记不存在或已被删除")


def _http_get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _provider_rest(query, limit):
    """调用你自己的真实内容接口（合规数据源）。

    约定：GET {XHS_REST_BASE}/search?q=<query>&type=video&limit=<limit>
    返回 CONTRACT 约定的 JSON。
    """
    base = os.getenv("XHS_REST_BASE")
    key = os.getenv("XHS_REST_KEY")
    if not base:
        raise XHSSourceUnavailable(
            "未配置真实数据源：请设置 XHS_PROVIDER=rest 与 XHS_REST_BASE / XHS_REST_KEY"
        )
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({
        "q": query, "type": "video", "limit": limit,
    })
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        return _http_get_json(url, headers, timeout=15)
    except urllib.error.HTTPError as e:
        raise XHSSourceUnavailable("真实数据源请求失败（HTTP %s）" % e.code)
    except Exception as e:  # 网络/解析等任何异常都不伪装成数据
        raise XHSSourceUnavailable("真实数据源暂时不可用：%s" % e)


_NOISE_FRAME = ("行吟信息科技", "你的生活兴趣社区", "沪ICP备", "© 2014")

def _clean_xhs_title(title, query=None):
    """清洗小红书框架噪声标题。

    仅做「去后缀 / 去纯框架噪声」的轻量清洗；若标题本身就是站点名或框架噪声，
    返回空字符串，交由 _enrich_titles 去抓真实笔记标题（绝不用检索词冒充真实标题）。
    """
    t = (title or "").strip()
    if t.endswith(" - 小红书"):
        t = t[: -len(" - 小红书")].strip()
    if not t or t == "小红书" or t == "Xiaohongshu":
        return ""
    if any(h in t for h in ("你的生活兴趣社区", "行吟信息科技")):
        return ""
    return t


_DEFA700_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _fetch_xhs_title(url):
    """抓取小红书单篇笔记页 <title> 拿到真实笔记标题（非 Mock、非检索词冒充）。

    返回清洗后的真实标题；抓取失败 / 页面无有效标题时返回空字符串。
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _DEFA700_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read().decode("utf-8", "ignore")
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        if not m:
            return ""
        t = html.unescape(m.group(1).strip())
        if t.endswith(" - 小红书"):
            t = t[: -len(" - 小红书")].strip()
        if not t or t == "小红书" or "你的生活兴趣社区" in t:
            return ""
        return t
    except Exception:
        return ""


def _parse_xhs_state(page):
    """从笔记页 HTML 解析小红书 SSR 状态。

    返回 (has_card, is_video, title)：
      - has_card=False  -> 没取到笔记数据（反爬/登录页/JS 重定向），无法核验；
      - is_video=None   -> 取到 noteCard 但段内没拿到 type 字段（用 URL 信号兜底）；
      - title           -> 解析到的真实笔记标题（优先 noteCard.title，否则 <title>）。
    """
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(.*?)</script>', page, re.S)
    is_video = None
    title = ""
    has_card = False
    if not m:
        return has_card, is_video, title
    blob = m.group(1)
    nc = re.search(r'"noteCard"\s*:\s*\{', blob) or re.search(r'"note_card"\s*:\s*\{', blob)
    if nc:
        has_card = True
        seg = blob[nc.end():nc.end() + 4000]
        tm = re.search(r'"type"\s*:\s*"(video|normal|image)"', seg)
        if tm:
            is_video = (tm.group(1) == "video")
        titm = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', seg)
        if titm:
            try:
                title = html.unescape(json.loads('"' + titm.group(1) + '"'))
            except Exception:
                title = html.unescape(titm.group(1))
    if not title:
        mm = re.search(r'<title[^>]*>(.*?)</title>', page, re.S | re.I)
        if mm:
            t = html.unescape(mm.group(1).strip())
            if t.endswith(" - 小红书"):
                t = t[: -len(" - 小红书")].strip()
            if t and "你的生活兴趣社区" not in t:
                title = t
    return has_card, is_video, title


def _verify_xhs_note(url):
    """活体核验单个候选笔记：确认它真实存在且是视频笔记。

    返回 dict：
      {
        "verified": bool,  # 是否成功抓到并解析页面（失败则保守，不丢弃）
        "alive":    bool,  # 页面真实存在（非已删除/不存在死链）
        "is_video": bool,  # 是否为视频笔记
        "title":    str,   # 解析到的真实标题（如有）
      }

    设计原则（绝不误杀真实视频，也不放过虚假）：
      - 页面含「已删除/不存在」文案 -> verified=True, alive=False，明确丢弃；
      - 抓到 noteCard 且 type=video -> 确认视频；type=normal/image -> 确认图文，丢弃；
      - 抓到 noteCard 但段内无 type -> 用 URL 信号 / <video> 标签兜底；
      - 完全抓不到 noteCard（疑似反爬/登录页）-> verified=False，保守保留，
        不因为反爬误判而把真实视频当死链丢弃。
    """
    if not url:
        return {"verified": False, "alive": False, "is_video": False, "title": ""}
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _DEFA700_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        # 404/410 是明确死链；403/5xx 或反爬则保守保留，不误杀真实视频。
        body = (e.read() or b"").decode("utf-8", "ignore")
        if e.code in (404, 410) or any(h in body for h in _DEAD_HINTS):
            return {"verified": True, "alive": False, "is_video": False, "title": ""}
        return {"verified": False, "alive": True, "is_video": True, "title": ""}
    except Exception:
        # 网络/超时/解析等失败：无法核验，保守保留
        return {"verified": False, "alive": True, "is_video": True, "title": ""}

    # 1) 已删除 / 不存在 的死链：页面含这些文案，直接判不存在
    if any(h in page for h in _DEAD_HINTS):
        return {"verified": True, "alive": False, "is_video": False, "title": ""}

    # 2) 解析 SSR 状态
    has_card, is_video, title = _parse_xhs_state(page)
    if not has_card:
        # 没拿到笔记数据（反爬/登录页/JS 重定向）：无法核验，保守保留
        return {"verified": False, "alive": True, "is_video": True, "title": title}

    # 3) 有 noteCard 但没取到 type：回退到 URL 信号 + 页面 <video> 标签
    if is_video is None:
        qp = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        ut = (qp.get("type") or [""])[0].lower()
        if ut == "video":
            is_video = True
        elif ut == "normal":
            is_video = False
        else:
            is_video = ("<video" in page) or ("og:video" in page)

    return {"verified": True, "alive": True, "is_video": bool(is_video), "title": title}


def _title_needs_fetch(title):
    t = (title or "").strip()
    if not t:
        return True
    if t in ("小红书", "Xiaohongshu", "小红书-你的生活兴趣社区"):
        return True
    if "你的生活兴趣社区" in t:
        return True
    return False


def _enrich_titles(items, query):
    """为标题缺失 / 退化为站点名的笔记并发抓取真实标题。

    仅对「标题不可用」的条目发起抓取，真实标题优先；抓取失败才用检索词生成
    兜底标题（明确标注为「相关视频笔记」，不冒充具体笔记内容）。
    """
    targets = [it for it in items if _title_needs_fetch(it.get("title"))]
    if not targets:
        return
    q = (query or "").strip()

    def _fill(it):
        real = _fetch_xhs_title(it.get("url"))
        if real:
            it["title"] = real
            it.pop("_no_real_title", None)
        else:
            # 抓不到真实标题：标记丢弃，绝不用「检索词相关视频笔记」冒充具体笔记，
            # 避免把无关热门页包装成相关结果（符合「绝不 Mock / 诚实」原则）。
            it["_no_real_title"] = True

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
        list(ex.map(_fill, targets))

def _clean_xhs_desc(desc):
    """清洗页面框架噪声摘要；无法判定为真实内容时返回空字符串。"""
    d = (desc or "").strip()
    if any(h in d for h in _NOISE_FRAME):
        return ""
    if len(d) < 30:
        return ""
    return d


def _provider_tavily(query, limit):
    """通过 Tavily 搜索 API 在 xiaohongshu.com 域内检索视频笔记候选。

    Tavily 是合规第三方搜索 API（免费档 1000 积分/月、无需信用卡、SOC2、零留存）。
    它返回的是网页结果，不提供笔记 type 字段，因此本函数做「视频偏向 + 规则过滤」：
      - 检索词追加「视频」，include_domains 限定 xiaohongshu.com
      - 只保留 /explore/ 或 /discovery/item/ 形式的真实笔记页 URL
      - 标题/摘要含图文信号 -> 标记为 image（数据层会被丢弃）
    详见文件头注释关于「视频可信度」的说明。
    """
    key = os.getenv("TAVILY_API_KEY")
    if not key or key.startswith("请替换") or not key.startswith("tvly-"):
        raise XHSSourceUnavailable(
            "Tavily 密钥未正确配置：请在 .env 的 TAVILY_API_KEY 填入以 tvly- 开头的真实密钥"
            "（注册地址 https://app.tavily.com）"
        )
    # 视频偏向：检索词强制带「视频」，并限定域名；提高召回基数以便筛出视频
    q = (query + " 视频").strip()
    payload = {
        "api_key": key,
        "query": q,
        "search_depth": "basic",
        "max_results": min(int(limit) * 2, 25),
        "include_domains": ["xiaohongshu.com"],
        "include_answer": False,
        "include_raw_content": False,
    }
    last_err = None
    data = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            last_err = None
            break
        except urllib.error.HTTPError as e:
            last_err = "Tavily 请求失败（HTTP %s）" % e.code
            if e.code == 429 and attempt < 3:
                # 免费层偶发限流：指数退避重试，扛过短期限流窗口
                time.sleep(2 * (attempt + 1))
                continue
            raise XHSSourceUnavailable(last_err)
        except Exception as e:
            last_err = "Tavily 暂时不可用：%s" % e
            if attempt < 3:
                time.sleep(1)
                continue
            raise XHSSourceUnavailable(last_err)
    if last_err:
        raise XHSSourceUnavailable(last_err)

    raw_items = data.get("results") or []
    out = []
    for it in raw_items:
        url = (it.get("url") or "").strip()
        # 只保留真实笔记页 URL，丢弃用户主页/招聘/开放平台/列表等聚合页
        if "/explore/" not in url and "/discovery/item/" not in url:
            continue
        title = _clean_xhs_title(it.get("title"), query)
        # 跳过已被删除/不存在的笔记页（搜索结果残留的死链）
        if any(h in title for h in ("页面不见了", "笔记不存在", "内容已删除", "该笔记不存在", "笔记已删除", "你访问的页面", "笔记不存在或已被删除")):
            continue
        # 摘要若为页面框架噪声则清空；真实视频笔记 URL 仍保留（用户可点开查看）
        content = _clean_xhs_desc(it.get("content"))
        # 类型判定（统一交给 _classify_xhs，按「纯度优先但不误杀」原则）
        ntype = _classify_xhs(title, url)

        out.append({
            "id": url,
            "platform": "xiaohongshu",
            "title": title,
            "author": "",
            "url": url,
            "cover": "",
            "type": ntype,
            "publishedAt": it.get("published_date") or "",
            "likes": None,
            "collects": None,
            "comments": None,
            "videoUrl": "",
            "description": content[:300],
        })
    return {"results": out}


# 检索词里的"弱意图词"，判定相关性时会被去掉，只保留核心主题词
_STOP_WORDS = ("视频", "教程", "分享", "推荐", "怎么", "如何", "怎样",
               "小技巧", "技巧", "方法", "攻略", "合集", "全集", "日常",
               "记录", "短视频", "vlog", "VLOG", "Vlog", "a", "the", "of")


def _core_query(query):
    """去掉弱意图词，提取检索核心主题串（用于相关性判定）。"""
    q = (query or "").strip()
    for s in _STOP_WORDS:
        q = q.replace(s, "")
    return q.strip()


def _relevant_to_query(item, query):
    """粗粒度相关性闸门：标题/摘要须含检索核心主题词，避免无关笔记冒充结果。

    免费检索源（Tavily / DDG）对小红书单篇笔记的覆盖与相关性都很弱，
    若不闸门会出现「搜收纳技巧却返回足球集锦」这类误导。宁可少返回，
    也绝不把无关内容当结果（符合「绝不 Mock / 诚实」原则）。
    无法判定相关性（核心词过短）时保守保留。
    """
    core = _core_query(query)
    if len(core) < 2:
        return True
    # 只用「真实笔记标题」判定（DDG 返回的 description 是小红书站点级分类噪声，
    # 含「影视/职场/健身/视频」等词会误命中，绝不能用它判相关性）。
    text = (item.get("title") or "").strip()
    if not text:
        return True
    # 中文按二元组匹配：核心词任一连续 2 字出现在真实标题即视为相关
    if len(core) >= 2:
        for i in range(len(core) - 1):
            if core[i:i + 2] in text:
                return True
    return core in text


def _filter_verified(vis, query):
    """根据活体核验结果过滤候选，规避「不存在 / 虚假」。

    规则：
      - 已核验且明确不存在（alive=False）-> 丢弃（死链）；
      - 已核验且明确非视频（is_video=False）-> 丢弃（图文冒充视频）；
      - 未核验（反爬/网络失败）-> 保守保留，不误杀真实视频；
      - 取核验得到的真实标题覆盖，无真实标题的条目丢弃（避免噪声/占位冒充）；
      - 最后用真实标题过相关性闸门。
    """
    kept = []
    for u in vis:
        v = u.get("_verify") or {}
        if v.get("verified"):
            if not v.get("alive"):
                continue
            if not v.get("is_video"):
                continue
        real_title = v.get("title") or u.get("title") or ""
        u["title"] = real_title
        if not real_title:
            continue
        if _relevant_to_query(u, query):
            kept.append(u)
    return kept


def _classify_xhs(title, url):
    """判定小红书笔记是否为视频。返回 'video' 或 'image'。

    原则（纯度优先但不误杀）：
      - URL 查询串 type=video  -> 视频（高可信信号）
      - URL 查询串 type=normal -> 图文/文字（丢弃）
      - 标题含强图文信号        -> 图文（丢弃）
      - 其余（含视频信号 / 无信号）-> 视频候选

    说明：检索已在查询里强制带「视频」并限定 xiaohongshu.com 笔记页，
    因此「无明确信号」的笔记也应视为视频候选，避免把真实视频误杀导致 0 结果。
    明确的图文（图集/九宫格/壁纸等）才丢弃，以保证不混入图文笔记。
    """
    qp = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
    url_type = (qp.get("type") or [""])[0].lower()
    if url_type == "video":
        return "video"
    if url_type == "normal":
        return "image"
    low = (title or "").lower()
    if any(h in low for h in _NON_VIDEO_HINTS):
        return "image"
    # 其余一律视为视频候选（命中视频信号 / 无信号两种情况都保留）
    return "video"


def _decode_ddg_url(href):
    """DuckDuckGo 结果链接常经过 /l/?uddg=<encoded> 重定向，解出真实地址。"""
    if "uddg=" in href:
        m = re.search(r'uddg=([^&]+)', href)
        if m:
            return urllib.parse.unquote(m.group(1))
    return href


_NON_NOTE_SEGMENTS = {
    "homepage", "feed", "search", "explore", "discovery", "user", "users",
    "topics", "topic", "page", "mobile", "question", "questions", "board",
    "channel", "activity", "notice", "login", "search_result", "guide",
}


def _is_xhs_note_url(url):
    """判断是否为小红书单篇笔记页（排除主页/话题/用户/搜索等聚合页）。"""
    path = urllib.parse.urlparse(url or "").path
    for pat in ("/explore/", "/discovery/item/"):
        if pat in path:
            seg = path.split(pat, 1)[1]
            seg = seg.split("/")[0].split("?")[0].split("#")[0]
            if not seg or seg.lower() in _NON_NOTE_SEGMENTS:
                return False
            return True
    return False


def _parse_ddg(page_html, limit):
    """从 DuckDuckGo 搜索结果页里解析出小红书「单篇笔记页」候选。"""
    out = []
    seen = set()
    # 抓取所有 <a href="...">文本</a>，覆盖 lite 与 html 两种结果格式
    anchor_re = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
    for m in anchor_re.finditer(page_html):
        href = m.group(1)
        text = html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()
        real = _decode_ddg_url(href)
        if not real or "xiaohongshu.com" not in real:
            continue
        # 只保留真实单篇笔记页（explore / discovery/item），丢弃主页/话题/用户页
        if not _is_xhs_note_url(real):
            continue
        key = re.sub(r'[?#].*$', '', real)
        if key in seen:
            continue
        seen.add(key)
        title = text or real.rsplit("/", 1)[-1]
        if title.endswith(" - 小红书"):
            title = title[: -len(" - 小红书")]
        # 跳过已被删除/不存在的死链残留
        if any(h in title for h in ("页面不见了", "笔记不存在", "内容已删除",
                                    "该笔记不存在", "笔记已删除", "你访问的页面",
                                    "笔记不存在或已被删除")):
            continue
        # 片段：取锚点后到下一个 <a 之间的纯文本作为「公开摘要」（避免吞掉下一条结果）
        after = page_html[m.end():]
        nxt = after.find("<a")
        after = after[:nxt] if nxt != -1 else after[:700]
        snippet = html.unescape(re.sub(r'<[^>]+>', ' ', after))
        snippet = re.sub(r'\s+', ' ', snippet).strip()[:300]
        ntype = _classify_xhs(title, real)
        if ntype != "video":
            continue
        out.append({
            "id": real,
            "platform": "xiaohongshu",
            "title": title,
            "author": "",
            "url": real,
            "cover": "",
            "type": "video",
            "publishedAt": "",
            "likes": None,
            "collects": None,
            "comments": None,
            "videoUrl": "",
            "description": snippet,
        })
        if len(out) >= limit:
            break
    return out


def _provider_ddg(query, limit):
    """通过 DuckDuckGo（免费、无需密钥）在 xiaohongshu.com 域内检索视频笔记候选。

    DuckDuckGo 会索引小红书被搜索引擎收录的单篇笔记页（Tavily 当前已不再索引
    小红书单篇笔记页，因此本 provider 作为更可靠的主力来源）。
    检索词强制带「视频」并加 site: 限定域名，再按 _classify_xhs 过滤图文。
    """
    q = (query + " 视频").strip()
    search_q = "site:xiaohongshu.com " + q
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(search_q)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            page_html = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        raise XHSSourceUnavailable("DuckDuckGo 检索失败：%s" % e)
    items = _parse_ddg(page_html, limit)
    # 无笔记页结果时返回空列表，交由 search_videos 统一给出「未找到相关笔记」的
    # 综合诚实说明，而不是在此抛 DDG 专属异常（避免误导用户以为是反爬）。
    return {"results": items}


PROVIDERS = {
    "rest": _provider_rest,
    "tavily": _provider_tavily,
    "ddg": _provider_ddg,
}


# ---------------------------------------------------------------------------
# Bilibili 数据源（B站官方搜索 API，返回结构化真实视频，无需活体核验兜底）
# ---------------------------------------------------------------------------
_BILI_WBI_KEYS = {"img": None, "sub": None}


def _bili_ensure_wbi():
    """拉取 B站 wbi 签名所需的 img/sub key（带模块级缓存）。"""
    if _BILI_WBI_KEYS["img"] and _BILI_WBI_KEYS["sub"]:
        return _BILI_WBI_KEYS["img"], _BILI_WBI_KEYS["sub"]
    headers = {"User-Agent": _DEFA700_UA, "Referer": "https://www.bilibili.com"}
    try:
        data = _http_get_json("https://api.bilibili.com/x/web-interface/nav", headers)
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if img and sub:
            _BILI_WBI_KEYS["img"], _BILI_WBI_KEYS["sub"] = img, sub
    except Exception:
        pass
    return _BILI_WBI_KEYS["img"], _BILI_WBI_KEYS["sub"]


_MIXIN_KEY_ENC_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 6, 60, 21, 59, 4, 58,
    1, 36, 57, 0, 25, 34, 56, 27, 20, 18, 51, 54, 0, 21, 51, 44,
    8, 58, 47, 33, 43, 38, 33, 45, 13, 38,
]


def _bili_sign(params):
    """对查询参数做 wbi 签名，返回带 w_rid/wts 的新参数字典。"""
    img, sub = _bili_ensure_wbi()
    if not (img and sub):
        # 拿不到 key 也无妨：部分接口在不签名的降级情况下仍可返回数据
        params = dict(params)
        params["wts"] = int(time.time())
        return params
    raw = img + sub
    mixin = "".join(raw[i] for i in _MIXIN_KEY_ENC_TABLE[:32])
    params = dict(params)
    params["wts"] = int(time.time())
    ordered = dict(sorted(params.items()))
    query = urllib.parse.urlencode(ordered)
    params["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return params


def _bili_strip_title(title):
    """B站搜索结果标题含 <em class="keyword"> 高亮标签，移除标签并还原文本。"""
    if not title:
        return ""
    title = re.sub(r"</?em[^>]*>", "", title)
    return html.unescape(title).strip()


def _bili_duration(sec):
    """将 B站视频时长规范化为 'm:s' 或 'h:m:s' 字符串。
    支持两种输入：
      - 数字（秒）：转为 mm:ss / h:mm:ss
      - 已经是 'mm:ss' / 'hh:mm:ss' 字符串：原样保留
    """
    if sec is None:
        return ""
    # 已经是 m:s / h:m:s 形式的字符串（部分 B站接口直接返回这种格式）
    if isinstance(sec, str):
        s = sec.strip()
        if not s:
            return ""
        # 仅含数字与冒号，认为已是规范时长
        if all(c.isdigit() or c == ":" for c in s):
            # 补齐 mm:ss 格式
            parts = s.split(":")
            if len(parts) == 2 and len(parts[0]) == 1:
                s = "0" + s
            return s
        return ""
    # 数字（秒）→ 转 mm:ss
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def _provider_bilibili(query, limit):
    """通过 B站官方搜索 API 检索视频（结构化真实数据，无需 Mock / 活体核验）。

    返回 CONTRACT 约定结构；cover 字段为 B站 pic URL（http->https 修正），
    便于前端直接展示封面。平台标记为 bilibili。
    """
    params = {
        "search_type": "video",
        "keyword": query,
        "page": 1,
        "pagesize": min(int(limit) * 2, 36),
        "order": "totalrank",
    }
    signed = _bili_sign(params)
    url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + urllib.parse.urlencode(signed)
    headers = {
        "User-Agent": _DEFA700_UA,
        "Referer": "https://search.bilibili.com",
        "Accept": "application/json",
    }
    try:
        data = _http_get_json(url, headers, timeout=15)
    except urllib.error.HTTPError as e:
        raise XHSSourceUnavailable("B站检索失败（HTTP %s）" % e.code)
    except Exception as e:
        raise XHSSourceUnavailable("B站检索暂时不可用：%s" % e)

    if (data.get("code") not in (0, None)) and "data" not in data:
        raise XHSSourceUnavailable("B站检索返回异常：%s" % (data.get("message") or data.get("code")))

    results = ((data.get("data") or {}).get("result") or [])
    out = []
    for it in results:
        bvid = it.get("bvid") or it.get("id")
        title = _bili_strip_title(it.get("title"))
        if not title:
            continue
        cover = (it.get("pic") or "").replace("http://", "https://")
        play = it.get("play") or it.get("view") or 0
        duration = _bili_duration(it.get("duration") or it.get("dur") or 0)
        pub = it.get("pubdate") or it.get("senddate") or ""
        if pub:
            try:
                import datetime
                pub = datetime.datetime.fromtimestamp(int(pub)).strftime("%Y-%m-%d")
            except Exception:
                pass
        out.append({
            "id": str(bvid or it.get("aid") or cover),
            "platform": "bilibili",
            "title": title,
            "author": it.get("author") or it.get("upname") or "",
            "url": it.get("arcurl") or ("https://www.bilibili.com/video/" + bvid if bvid else ""),
            "cover": cover,
            "type": "video",
            "publishedAt": str(pub),
            "likes": None,
            "collects": None,
            "comments": None,
            "play": play,
            "duration": duration,
            "videoUrl": "",
            "description": (it.get("description") or "")[:300],
        })
    return {"results": out}


# 注册 B站 provider（函数定义见上）；B站返回结构化真实视频，无需活体核验兜底。
PROVIDERS["bilibili"] = _provider_bilibili


def _extract_items(raw):
    """从 provider 响应里取出笔记列表。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("items", "data", "notes", "results", "list"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def _normalize(item):
    """把 provider 的单条记录映射成统一结构。"""
    def num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    ntype = (item.get("type") or "").lower()
    return {
        "id": str(item.get("id") or item.get("note_id") or item.get("url") or ""),
        "platform": item.get("platform") or "xiaohongshu",
        "title": item.get("title") or "",
        "author": item.get("author") or item.get("nickname") or "",
        "url": item.get("url") or item.get("note_url") or "",
        "cover": item.get("cover") or item.get("cover_url") or "",
        "type": "video" if ntype in ("video", "vlog", "短视频") else ntype,
        "publishedAt": item.get("publishedAt") or item.get("publish_time") or "",
        "likes": num(item.get("likes")),
        "collects": num(item.get("collects") or item.get("favs")),
        "comments": num(item.get("comments") or item.get("comment_count")),
        "play": num(item.get("play") or item.get("view")),          # B站播放量（如 B站等结构化源提供）
        "duration": item.get("duration") or "",                    # B站时长（"m:s"格式）；其他源为空字符串
        "videoUrl": item.get("videoUrl") or item.get("video_url") or "",
        "description": item.get("description") or item.get("desc") or "",
    }


def search_videos(query, limit=15):
    """统一检索入口：返回已过滤的「视频笔记」列表（统一结构）。

    数据源策略：
      - XHS_PROVIDER=none       -> 如实报错（未配置真实数据源，绝不用 Mock 冒充）。
      - XHS_PROVIDER=rest       -> 只用用户自己的合规内容接口。
      - 其余（tavily / ddg / 未知）-> 多源并行合并召回：
        同时调用配置源与 ddg/tavily 兜底源，合并去重后统一活体核验过滤；
        全部源都失败才如实上报。LAST_SOURCE 记录最终真正出数据的源。
    """
    global LAST_SOURCE
    provider = (os.getenv("XHS_PROVIDER") or "none").strip().lower()

    if provider == "none":
        raise XHSSourceUnavailable(
            "当前环境缺少真实小红书数据源：请配置 XHS_PROVIDER=tavily 或 ddg，"
            "或配置 XHS_PROVIDER=rest 并提供 XHS_REST_BASE / XHS_REST_KEY 指向合规的小红书内容接口。"
        )

    if provider == "rest":
        raw = PROVIDERS["rest"](query, limit)
        LAST_SOURCE = "rest"
        vis = [u for u in (_normalize(it) for it in _extract_items(raw))
               if u.get("type") == "video"]
        _enrich_titles(vis, query)
        vis = [u for u in vis if not u.get("_no_real_title")]
        for u in vis:
            u.pop("_no_real_title", None)
        return vis

    # 免费检索源：多源并行合并召回，不再"有一个源有结果就停"，
    # 而是把配置源 + 兜底源的结果合并去重后统一核验，最大化真实视频召回。
    order = []
    if provider in ("tavily", "ddg", "bilibili"):
        order = [provider, "bilibili", "ddg", "tavily"]
    else:
        order = ["bilibili", "ddg", "tavily"]
    order = [p for p in order if p in PROVIDERS]
    # 去重（配置源与兜底源可能相同）
    order = list(dict.fromkeys(order))

    all_items = []
    seen_urls = set()
    last_err = None
    sources_used = []
    max_pool = max(limit * 3, 30)
    per_limit = max(limit * 2, 20)

    for p in order:
        try:
            raw = PROVIDERS[p](query, per_limit)
            items = _extract_items(raw)
            src_added = 0
            for it in items:
                u = _normalize(it)
                if u.get("type") != "video":
                    continue
                key = re.sub(r'[?#].*$', '', (u.get("url") or ""))
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                u["_source"] = p
                all_items.append(u)
                src_added += 1
                if len(all_items) >= max_pool:
                    break
            if src_added:
                sources_used.append(p)
            if len(all_items) >= max_pool:
                break
        except XHSSourceUnavailable as e:
            last_err = e
            continue
        except Exception as e:  # 任何异常都不伪装成数据
            last_err = e
            continue

    if all_items:
        # 平台相关核验：小红书候选做活体核验（过滤死链/图文冒充）；
        # B站候选已是结构化真实视频，无需核验，直接保留真实元数据。
        def _verify_one(u):
            if u.get("platform") == "xiaohongshu":
                v = _verify_xhs_note(u.get("url"))
                if not v.get("verified") or not v.get("title"):
                    rt = _fetch_xhs_title(u.get("url"))
                    if rt:
                        v["title"] = rt
                u["_verify"] = v
            else:
                u["_verify"] = {"verified": True, "alive": True, "is_video": True,
                                "title": u.get("title") or ""}

        with ThreadPoolExecutor(max_workers=min(8, len(all_items))) as ex:
            list(ex.map(_verify_one, all_items))

        vis = _filter_verified(all_items, query)
        if vis:
            LAST_SOURCE = "+".join(sources_used) if sources_used else provider
            for u in vis:
                u.pop("_verify", None)
                u.pop("_source", None)
            return vis[:limit]

    if last_err:
        raise last_err
    raise XHSSourceUnavailable(
        "未在真实数据源中找到与「%s」相关的视频内容。"
        "当前检索源（B站 / Tavily / DuckDuckGo）对该主题覆盖有限："
        "如需稳定检索相关视频，可接入 XHS_PROVIDER=rest 并配置 XHS_REST_BASE 指向合规内容接口。"
        % (query or "").strip()
    )
