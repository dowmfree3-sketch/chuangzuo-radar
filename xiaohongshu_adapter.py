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
- 双重视频过滤：
    1) 数据层：只保留 type == "video"
    2) 规则层：标题/描述明显为「图文 / 图片 / 纯文字」且无视频信号时丢弃
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
    for attempt in range(2):
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
            if e.code == 429:
                time.sleep(2)
                continue
            raise XHSSourceUnavailable(last_err)
        except Exception as e:
            last_err = "Tavily 暂时不可用：%s" % e
            time.sleep(1)
            continue
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
        "platform": "xiaohongshu",
        "title": item.get("title") or "",
        "author": item.get("author") or item.get("nickname") or "",
        "url": item.get("url") or item.get("note_url") or "",
        "cover": item.get("cover") or item.get("cover_url") or "",
        "type": "video" if ntype in ("video", "vlog", "短视频") else ntype,
        "publishedAt": item.get("publishedAt") or item.get("publish_time") or "",
        "likes": num(item.get("likes")),
        "collects": num(item.get("collects") or item.get("favs")),
        "comments": num(item.get("comments") or item.get("comment_count")),
        "videoUrl": item.get("videoUrl") or item.get("video_url") or "",
        "description": item.get("description") or item.get("desc") or "",
    }


def search_videos(query, limit=15):
    """统一检索入口：返回已过滤的「视频笔记」列表（统一结构）。

    数据源策略：
      - XHS_PROVIDER=none       -> 如实报错（未配置真实数据源，绝不用 Mock 冒充）。
      - XHS_PROVIDER=rest       -> 只用用户自己的合规内容接口。
      - 其余（tavily / ddg / 未知）-> 优先用配置项，若该源返回 0 / 失败，
        自动回退到 ddg -> tavily，最大化「真实拿到视频笔记」的概率；
        全部失败才如实上报。LAST_SOURCE 记录最终真正出数据的源。
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

    # 免费检索源：配置项优先，失败/空则自动回退
    order = []
    if provider in ("tavily", "ddg"):
        order = [provider, "ddg", "tavily"]
    else:
        order = ["ddg", "tavily"]
    order = [p for p in order if p in PROVIDERS]

    last_err = None
    for p in order:
        try:
            raw = PROVIDERS[p](query, limit)
            items = _extract_items(raw)
            vis = [u for u in (_normalize(it) for it in items)
                   if u.get("type") == "video"]
            if vis:
                # 先抓真实笔记标题（DDG 锚点文本是站点名，须取页面 <title>），
                # 再用真实标题做相关性闸门，避免站点级噪声描述误命中。
                _enrich_titles(vis, query)
                # 丢弃无真实标题的条目（如 DDG 返回的无关热门页抓不到标题），
                # 再用真实标题做相关性闸门，避免无关内容冒充相关结果。
                vis = [u for u in vis if not u.get("_no_real_title")]
                vis = [u for u in vis if _relevant_to_query(u, query)]
                if vis:
                    LAST_SOURCE = p
                    for u in vis:
                        u.pop("_no_real_title", None)
                    return vis
        except XHSSourceUnavailable as e:
            last_err = e
            continue
        except Exception as e:  # 任何异常都不伪装成数据
            last_err = e
            continue

    if last_err:
        raise last_err
    raise XHSSourceUnavailable(
        "未在真实数据源中找到与「%s」相关的小红书视频笔记。"
        "免费检索源（Tavily / DuckDuckGo）当前对小红书单篇笔记的覆盖与相关性都很有限："
        "Tavily 仅收录聚合页、DDG 仅返回少量热门笔记且不按关键词检索。"
        "如需稳定检索相关视频，请接入 XHS_PROVIDER=rest 并配置 XHS_REST_BASE 指向合规的小红书内容接口。"
        % (query or "").strip()
    )
