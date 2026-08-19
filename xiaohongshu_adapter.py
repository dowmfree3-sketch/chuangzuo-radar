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
import json
import urllib.request
import urllib.parse
import urllib.error


class XHSSourceUnavailable(Exception):
    """真实小红书数据源不可用 / 未配置。"""
    pass


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

_NON_VIDEO_HINTS = ["图文", "图片", "纯文字", "图文笔记", "图集", "九宫格"]


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
    # 视频偏向：检索词强制带「视频」，并限定域名
    q = (query + " 视频").strip()
    payload = {
        "api_key": key,
        "query": q,
        "search_depth": "basic",
        "max_results": min(int(limit) * 2, 20),
        "include_domains": ["xiaohongshu.com"],
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise XHSSourceUnavailable("Tavily 请求失败（HTTP %s）" % e.code)
    except Exception as e:
        raise XHSSourceUnavailable("Tavily 暂时不可用：%s" % e)

    raw_items = data.get("results") or []
    out = []
    for it in raw_items:
        url = (it.get("url") or "").strip()
        # 只保留真实笔记页 URL，丢弃列表/搜索/聚合页
        if "/explore/" not in url and "/discovery/item/" not in url:
            continue
        title = (it.get("title") or "").strip()
        if title.endswith(" - 小红书"):
            title = title[: -len(" - 小红书")]
        # 跳过已被删除/不存在的笔记页（搜索结果残留的死链）
        if any(h in title for h in ("页面不见了", "笔记不存在", "内容已删除", "该笔记不存在", "笔记已删除", "你访问的页面")):
            continue
        content = (it.get("content") or "").strip()
        low = (title + " " + content).lower()

        # 类型判定（按可信度从高到低）：
        #   1) URL 查询串里的 type=video / type=normal 是小红书分享链接自带的高可信信号
        #   2) 否则用标题/摘要里的图文/纯文字信号做规则判定
        qp = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        url_type = (qp.get("type") or [""])[0].lower()
        if url_type == "video":
            ntype = "video"
        elif url_type == "normal":
            ntype = "image"  # 图文/文字笔记，数据层丢弃
        else:
            ntype = "image" if any(h in low for h in _NON_VIDEO_HINTS) else "video"

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


PROVIDERS = {
    "rest": _provider_rest,
    "tavily": _provider_tavily,
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


def _looks_non_video(u):
    """规则层：明显是图文/纯文字且无视频信号 -> 视为非视频。"""
    text = ((u.get("title") or "") + " " + (u.get("description") or "")).lower()
    if any(h in text for h in _NON_VIDEO_HINTS):
        # 只有当完全没有视频信号时才丢弃；有 videoUrl 则保留
        if not (u.get("videoUrl") or u.get("cover")):
            return True
    return False


def search_videos(query, limit=15):
    """统一检索入口：返回已过滤的「视频笔记」列表（统一结构）。

    第一层过滤（数据层）：type 必须是 "video"。
    第二层过滤（规则层）：排除明显图文/纯文字。
    """
    provider = (os.getenv("XHS_PROVIDER") or "none").strip().lower()
    if provider == "none" or provider not in PROVIDERS:
        raise XHSSourceUnavailable(
            "当前环境缺少真实小红书数据源：请配置 XHS_PROVIDER=tavily 并提供 TAVILY_API_KEY，"
            "或配置 XHS_PROVIDER=rest 并提供 XHS_REST_BASE / XHS_REST_KEY 指向合规的小红书内容接口。"
        )

    raw = PROVIDERS[provider](query, limit)
    items = _extract_items(raw)

    out = []
    for it in items:
        u = _normalize(it)
        # 第一层：数据层只保留视频
        if u.get("type") != "video":
            continue
        # 第二层：规则层确认非图文/纯文字
        if _looks_non_video(u):
            continue
        out.append(u)
    return out
