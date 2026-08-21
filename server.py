# -*- coding: utf-8 -*-
"""
创作雷达 · 后端 API（Python 标准库，零第三方依赖）

架构：
    Browser ──> 本后端 ──> OpenRouter(免费 LLM) / 小红书 Adapter
    - API Key 只在后端环境变量，绝不出现在前端、绝不回传、绝不写入日志。
    - 只使用免费模型；任何失败都明确报错，绝不自动 fallback 到付费模型。

接口：
    GET  /api/ai_status            检查 AI / 数据源是否就绪
    POST /api/understand           用户输入 -> AI 意图理解 + 检索 Query
    POST /api/search-xhs-videos    Query -> 小红书视频检索 + 双重视频过滤
    POST /api/rank-xhs-videos       候选视频 + 用户需求 -> AI 排序筛选 Top5
    POST /api/analyze-video         单条视频笔记 -> AI 视频拆解 / 爆点分析

环境变量：
    OPENROUTER_API_KEY    (必填) OpenRouter Key
    OPENROUTER_MODEL      (可选) 模型，默认 google/gemma-4-26b-a4b-it:free
    XHS_PROVIDER          (可选) 数据源：none(默认) | rest | tavily | ddg
    XHS_REST_BASE         (rest 时必填) 你的合规小红书内容接口基址
    XHS_REST_KEY          (可选) 接口鉴权
    TAVILY_API_KEY        (tavily 时必填) Tavily 搜索 API 密钥（tvly- 开头，免费档可用）
    APP_HTML              (可选) 要托管的 HTML 路径，默认取同目录或桌面文件
    PORT                  (可选) 默认 8000

说明：免费检索源默认优先用 ddg（DuckDuckGo，无需密钥、能索引小红书单篇笔记页），
tavily 作为兜底；当配置项返回 0 / 失败时自动回退，全部失败才如实报错（绝不用 Mock）。
"""

import os
import sys
import json
import math
import time
import socket
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse

# 载入同目录 .env（若存在），不覆盖已存在的环境变量
def _load_dotenv():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k:
                    # .env 优先生效（保证使用已验证的免费模型，不被外部环境变量覆盖）
                    os.environ[k] = v
    except Exception:
        pass

_load_dotenv()

import xiaohongshu_adapter as xhs

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free").strip()
# 免费模型兜底列表（逗号分隔）；任一被限流(429)或失败会自动尝试下一个。
# 全部为 :free 模型，绝不降级到付费模型。
OPENROUTER_FALLBACK_MODELS = (os.getenv("OPENROUTER_FALLBACK_MODELS") or
    "google/gemma-4-26b-a4b-it:free,openai/gpt-oss-20b:free,z-ai/glm-5.2:free").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# ----------------------------- 日志（绝不记录 Key） -----------------------------
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

def log(tag, msg=""):
    # 任何包含 key 的可能都被屏蔽
    safe = str(msg)
    for secret in (OPENROUTER_API_KEY, TAVILY_API_KEY):
        if secret and secret in safe:
            safe = safe.replace(secret, "***")
    print("[%s] %s" % (tag, safe), flush=True)


# ----------------------------- OpenRouter（免费模型，无付费 fallback） -----------------------------
def _parse_json_from_text(text):
    """从模型输出里稳健地提取 JSON（可能是对象或数组）。"""
    if text is None:
        raise ValueError("空响应")
    t = text.strip()
    # 去掉 ```json ... ``` 代码块
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lower().startswith("json"):
            t = t[4:]
    t = t.strip()
    # 找最外层对象/数组
    start = None
    end = None
    for i, ch in enumerate(t):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError("未找到 JSON")
    opener = t[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise ValueError("JSON 不完整")
    return json.loads(t[start:end])


def _ai_model_chain():
    """返回按顺序尝试的免费模型列表：[主模型] + 去重的兜底模型。"""
    chain = [OPENROUTER_MODEL]
    for m in OPENROUTER_FALLBACK_MODELS.split(","):
        m = m.strip()
        if m and m not in chain:
            chain.append(m)
    return chain


def _call_openrouter_once(model, system_prompt, user_prompt, max_tokens, expect):
    """对单个模型发一次请求并解析 JSON。失败抛 RuntimeError（供上层重试/换模型）。"""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + OPENROUTER_API_KEY,
        "HTTP-Referer": "https://creation-radar.local",
        "X-Title": "CreationRadar",
    }
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("AI 限流(429)")
        raise RuntimeError("AI HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("AI 网络错误(%s)" % e.reason)
    except socket.timeout:
        raise RuntimeError("AI 超时")

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("AI 返回格式异常")
    if not content or not content.strip():
        raise RuntimeError("AI 返回为空")

    try:
        parsed = _parse_json_from_text(content)
    except (ValueError, Exception) as e:
        raise RuntimeError("AI 输出不是合法 JSON: %s" % e)
    if expect == "array" and not isinstance(parsed, list):
        raise RuntimeError("AI 输出不是数组")
    if expect == "object" and not isinstance(parsed, dict):
        raise RuntimeError("AI 输出不是对象")
    return parsed


def openrouter_json(system_prompt, user_prompt, expect="object", max_tokens=1500):
    """调用 OpenRouter，要求模型只返回 JSON。

    - 主模型 + 兜底免费模型按顺序尝试；单个模型遇限流(429)/5xx/网络/超时最多重试 2 次；
      其他错（如格式异常）直接换下一个模型。
    - 全部为 :free 模型，绝不降级到付费模型。
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("AI 服务尚未配置（缺少 OPENROUTER_API_KEY）")

    chain = _ai_model_chain()
    last_err = None
    for model in chain:
        for attempt in range(2):
            try:
                log("AI REQUEST", "model=%s expect=%s attempt=%d" % (model, expect, attempt + 1))
                return _call_openrouter_once(model, system_prompt, user_prompt, max_tokens, expect)
            except RuntimeError as e:
                last_err = e
                msg = str(e)
                # 限流 / 5xx / 网络 / 超时：短暂等待后重试同一模型
                if "429" in msg or msg.startswith("AI HTTP 5") or "网络" in msg or "超时" in msg:
                    time.sleep(3)
                    continue
                # 其他错误（格式异常/返回空）直接换下一个模型
                break
    raise RuntimeError("AI 服务暂时不可用（所有免费模型均失败：%s）" % last_err)


# ----------------------------- 业务逻辑 -----------------------------
def handle_understand(data):
    idea = (data.get("idea") or "").strip()
    if not idea:
        return {"error": "请输入你的想法"}
    account_ctx = data.get("accountContext") or {}
    account = account_ctx.get("account") or ""

    system = (
        "你是一个小红书内容策略 AI。用户输入一个模糊的内容想法。"
        "请理解其意图，并生成用于在小红书检索「视频笔记」的关键词。"
        "必须只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。结构：\n"
        "{\n"
        '  "intent": { "theme":"主题", "scene":"内容场景(经验分享/知识解释/测评/故事/vlog等)", '
        '"audience":"目标受众", "content_goal":"内容目标" },\n'
        '  "queries": ["检索词1","检索词2","检索词3","检索词4"],\n'
        '  "need_clarify": false,\n'
        '  "clarify_question": "",\n'
        '  "clarify_options": []\n'
        "}\n"
        "规则：\n"
        "- queries 是给小红书用的视频检索关键词，结合主题+场景+受众，生成 3-5 个。\n"
        "- 若用户输入极其模糊（例如「想做点 AI 相关内容」）导致无法判断场景或受众，"
        "则 need_clarify=true，clarify_question 用中文最多问一个问题，"
        "clarify_options 给 2-4 个中文选项（须包含「让 AI 判断」）。\n"
        "- 不要编造任何数据、URL 或作者。"
    )
    user = "用户账号方向：%s\n用户输入：%s" % (account or "未指定", idea)
    try:
        d = openrouter_json(system, user, expect="object")
    except RuntimeError as e:
        return {"error": str(e), "code": "ai_unavailable"}

    # 容错：确保关键字段存在
    intent = d.get("intent") or {}
    if not intent.get("theme"):
        intent["theme"] = idea
    queries = d.get("queries") or []
    if not isinstance(queries, list):
        queries = [str(queries)]
    return {
        "intent": intent,
        "queries": queries[:5],
        "need_clarify": bool(d.get("need_clarify")),
        "clarify_question": d.get("clarify_question") or "",
        "clarify_options": d.get("clarify_options") or [],
    }


def handle_search(data):
    queries = data.get("queries") or []
    intent = data.get("intent") or {}
    clarification = (data.get("clarification") or "").strip()

    if not queries:
        theme = intent.get("theme")
        if theme:
            queries = [theme]
    if not queries:
        return {"results": [], "code": "no_results", "realtime": False}

    # 把用户的补充澄清拼进检索词，提升召回
    eff_queries = []
    for q in queries[:5]:
        eff_queries.append(q)
        if clarification and clarification not in q:
            eff_queries.append((q + " " + clarification).strip())
    eff_queries = eff_queries[:6]

    def _run():
        candidates = []
        seen = set()
        source = None
        src_unavailable = False
        for q in eff_queries:
            try:
                items = xhs.search_videos(q, limit=15)
                source = xhs.LAST_SOURCE
            except xhs.XHSSourceUnavailable as e:
                # 单个检索词的数据源异常：跳过该词继续其他词；
                # 只有全部词都失败才如实上报，绝不以 Mock 冒充。
                src_unavailable = True
                log("SEARCH SRC UNAVAILABLE", str(e)[:120])
                continue
            except Exception as e:
                log("SEARCH ERROR", str(e))
                continue
            log("SEARCH RESULT COUNT", "query=%s count=%d source=%s" % (q, len(items), xhs.LAST_SOURCE))
            for it in items:
                key = it.get("url") or it.get("id")
                if not key or key in seen:
                    continue
                seen.add(key)
                candidates.append(it)
        if candidates:
            res = {"results": candidates, "code": "ok", "realtime": True, "source": source or "unknown"}
            # 召回很少时诚实提示用户：这是免费源的客观覆盖限制，不是 bug。
            if len(candidates) < 3:
                res["note"] = (
                    "免费检索源对小红书单篇笔记收录有限，当前仅找到少量相关视频；"
                    "部分笔记需登录小红书账号才能打开。"
                    "如需稳定检索更多相关视频，建议接入 XHS_PROVIDER=rest 并配置 XHS_REST_BASE。"
                )
            return res
        if src_unavailable:
            return {"results": [], "code": "xhs_source_unavailable", "realtime": False,
                    "note": "所有检索词的小红书视频数据源当前都不可用（Tavily 免费层可能限流，或暂未收录该主题）。可稍后重试，"
                            "或接入 XHS_PROVIDER=rest 并配置 XHS_REST_BASE 指向合规的小红书内容接口以稳定检索。"}
        return {"results": [], "code": "no_results", "realtime": True, "source": source or "unknown"}

    # 免费检索源偶发限流（429）时，整体等几秒后重试一次，扛过限流窗口
    res = _run()
    if res.get("code") == "xhs_source_unavailable":
        log("SEARCH RETRY after throttle", "wait 4s")
        time.sleep(4)
        res = _run()
    return res


def _batch_engagement_score(candidates):
    """跨候选池归一化互动表现，返回 {id: 0-100 或 None}。"""
    maxv = {"likes": 0, "collects": 0, "comments": 0}
    for c in candidates:
        for k in maxv:
            v = c.get(k)
            if isinstance(v, (int, float)) and v > maxv[k]:
                maxv[k] = v
    out = {}
    for c in candidates:
        parts = []
        for k in maxv:
            v = c.get(k)
            if isinstance(v, (int, float)) and v > 0 and maxv[k] > 0:
                parts.append(min(v / maxv[k], 1.0))
        if parts:
            out[c.get("id")] = (sum(parts) / len(parts)) * 100.0
        else:
            out[c.get("id")] = None
    return out


def handle_rank(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return {"results": [], "code": "no_results"}
    intent = data.get("intent") or {}
    account_ctx = data.get("accountContext") or {}

    # 免费模型输出 token 有限，送入 AI 排序的候选数需封顶，避免 JSON 被截断
    MAX_RANK = 15
    rank_pool = candidates[:MAX_RANK]

    # 只把安全字段（不要求模型编造数据）传给 AI 做判断
    safe = []
    for c in rank_pool:
        safe.append({
            "id": c.get("id"),
            "title": c.get("title"),
            "author": c.get("author"),
            "type": c.get("type"),
            "description": (c.get("description") or "")[:120],
        })

    system = (
        "你是小红书内容筛选 AI。给定用户意图与一组「视频笔记」候选，"
        "请对每条做相关性评估，并只返回一个 JSON 数组，不要解释、不要 markdown。结构：\n"
        "[ { "
        '"id":"对应id", "relevance_score":0-100, "scene_match":0-100, '
        '"audience_match":0-100, "borrowability":0-100, '
        '"why_recommend":"一句话中文推荐理由" }, ... ]\n'
        "规则：\n"
        "- 必须包含全部候选的 id，不要新增或遗漏。\n"
        "- relevance_score 综合主题/场景/受众/可借鉴性。\n"
        "- why_recommend 说明「为什么值得研究 / 值得参考」，中文一句。\n"
        "- 绝对不要编造 URL、作者、点赞数等原始数据，原样保留传入字段。"
    )
    user = (
        "用户意图：%s\n账号方向：%s\n候选：%s"
        % (json.dumps(intent, ensure_ascii=False),
           json.dumps(account_ctx, ensure_ascii=False),
           json.dumps(safe, ensure_ascii=False))
    )
    try:
        ai = openrouter_json(system, user, expect="array", max_tokens=3000)
    except RuntimeError as e:
        return {"error": str(e), "code": "ai_unavailable"}

    score_map = {}
    for row in ai:
        if isinstance(row, dict) and row.get("id") is not None:
            score_map[str(row.get("id"))] = row

    eng = _batch_engagement_score(rank_pool)

    # 合并 AI 评分 + 真实数据，计算综合分（动态权重，缺失项自动重分配）
    def final_score(c):
        w = {"relevance": 0.4, "scene": 0.2, "audience": 0.15, "engagement": 0.15, "borrow": 0.1}
        s = score_map.get(str(c.get("id"))) or {}
        parts = {
            "relevance": float(s.get("relevance_score") or 0),
            "scene": float(s.get("scene_match") or 0),
            "audience": float(s.get("audience_match") or 0),
            "borrow": float(s.get("borrowability") or 0),
        }
        e = eng.get(c.get("id"))
        if e is not None:
            parts["engagement"] = e
        else:
            w.pop("engagement")
        tot = sum(w.values()) or 1
        return sum(parts[k] * w[k] for k in parts) / tot

    for c in rank_pool:
        s = score_map.get(str(c.get("id"))) or {}
        c["relevance_score"] = int(round(float(s.get("relevance_score") or 0)))
        c["scene_match"] = int(round(float(s.get("scene_match") or 0)))
        c["audience_match"] = int(round(float(s.get("audience_match") or 0)))
        c["borrowability"] = int(round(float(s.get("borrowability") or 0)))
        c["why_recommend"] = s.get("why_recommend") or "和你的主题相关，可作为内容参考。"
        c["final_score"] = round(final_score(c), 2)

    rank_pool.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    log("AI RANKING", "top=%d" % min(len(rank_pool), 5))
    return {"results": rank_pool[:5], "code": "ok"}


def handle_analyze_video(data):
    """视频拆解 / 爆点分析：基于单条笔记的「标题 + 公开摘要 + 链接」做策略拆解。

    诚实原则：
    - 只依据前端传入的真实字段（标题/摘要/链接/真实互动数）进行分析；
    - 严禁编造播放量、点赞、评论、作者背景等任何原始数据；
    - 这是「策略拆解」而非「视频逐帧稿」，必须在 based_on 里说明依据。
    """
    video = data.get("video") or {}
    intent = data.get("intent") or {}
    account_ctx = data.get("accountContext") or {}
    idea = (data.get("idea") or "").strip()

    title = (video.get("title") or "").strip()
    if not title:
        return {"error": "缺少待拆解的视频信息", "code": "bad_input"}
    desc = (video.get("description") or "").strip()
    url = (video.get("url") or "").strip()
    author = (video.get("author") or "").strip()

    def fmt_num(v):
        return v if isinstance(v, (int, float)) and v else "暂无数据"

    system = (
        "你是一个小红书内容策略专家。用户选中了一条小红书视频笔记，"
        "希望你基于它的「标题」和「公开摘要」做一次【视频拆解 / 爆点分析】。"
        "你必须只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。结构：\n"
        "{\n"
        '  "positioning": "一句话定位：这条笔记在讲什么、为什么容易火",\n'
        '  "hook": "它的开头钩子/标题钩子是怎么抓人的（必须引用原笔记标题里的真实措辞，不要凭空发明金句）",\n'
        '  "structure": ["内容结构要点1","内容结构要点2","内容结构要点3"],\n'
        '  "borrowable": ["可借鉴的爆点/手法1","可借鉴的爆点/手法2"],\n'
        '  "audience_insight": "从标题/摘要推断它打动了哪类人、戳中了什么情绪或需求",\n'
        '  "your_angle": "结合用户的账号方向，给出 1-2 个可以复刻或差异化的切入点",\n'
        '  "caveats": "做类似内容时要注意的风险或前提（如需要特定场景/能力/合规）",\n'
        '  "based_on": "说明本次拆解依据的数据（例如：仅依据标题与公开摘要，并非视频逐帧）"\n'
        "}\n"
        "硬性规则：\n"
        "- 只能基于下面提供的「标题 / 公开摘要 / 链接」进行分析，"
        "严禁编造播放量、点赞数、评论数、作者背景、发布时间等任何原始数据；"
        "若提供的值写的是「暂无数据」，就如实写「暂无数据」，不要替用户补一个数字。\n"
        "- hook 必须引用标题/摘要里的真实措辞。\n"
        "- 所有分析用中文，具体、可执行，不要空话套话。\n"
    )
    user = (
        "用户账号方向：%s\n用户原始想法：%s\n意图理解：%s\n"
        "待拆解视频笔记（真实数据，请勿编造）：\n"
        "标题：%s\n作者：%s\n链接：%s\n点赞：%s\n收藏：%s\n评论：%s\n公开摘要：%s"
        % (
            account_ctx.get("account") or "未指定",
            idea or "（未提供）",
            json.dumps(intent, ensure_ascii=False),
            title,
            author or "暂无数据",
            url or "暂无数据",
            fmt_num(video.get("likes")),
            fmt_num(video.get("collects")),
            fmt_num(video.get("comments")),
            desc or "（无公开摘要）",
        )
    )
    try:
        d = openrouter_json(system, user, expect="object", max_tokens=2000)
    except RuntimeError as e:
        return {"error": str(e), "code": "ai_unavailable"}

    # 容错：确保字段存在
    analysis = {
        "positioning": d.get("positioning") or "",
        "hook": d.get("hook") or "",
        "structure": d.get("structure") or [],
        "borrowable": d.get("borrowable") or [],
        "audience_insight": d.get("audience_insight") or "",
        "your_angle": d.get("your_angle") or "",
        "caveats": d.get("caveats") or "",
        "based_on": d.get("based_on") or "依据标题与公开摘要的策略拆解。",
    }
    return {"analysis": analysis, "video": video, "code": "ok"}


def handle_ai_status():
    return {
        "online": bool(OPENROUTER_API_KEY),
        "model": OPENROUTER_MODEL if OPENROUTER_API_KEY else "",
        "xhs_source": (os.getenv("XHS_PROVIDER") or "none").strip().lower() != "none",
    }


# ----------------------------- HTTP 服务 -----------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, cors=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path):
        # 解析要托管的 HTML 文件
        html_path = os.getenv("APP_HTML") or ""
        if not html_path or not os.path.exists(html_path):
            here = os.path.dirname(os.path.abspath(__file__))
            for cand in ("index.html", "22-app-merged+workflow.html",
                         os.path.expanduser("~/Desktop/22-app-merged+workflow.html")):
                p = cand if os.path.isabs(cand) else os.path.join(here, cand)
                if os.path.exists(p):
                    html_path = p
                    break
        if not html_path or not os.path.exists(html_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"HTML not found")
            return
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send_html(p)
        elif p == "/api/ai_status":
            self._send(200, handle_ai_status())
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self._send(204, {})

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}

        try:
            if p == "/api/understand":
                self._send(200, handle_understand(data))
            elif p == "/api/search-xhs-videos":
                self._send(200, handle_search(data))
            elif p == "/api/rank-xhs-videos":
                self._send(200, handle_rank(data))
            elif p == "/api/analyze-video":
                self._send(200, handle_analyze_video(data))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e), "code": "server_error"})

    def log_message(self, fmt, *args):
        # 静默默认访问日志，避免噪声
        pass


def main():
    port = int(os.getenv("PORT") or 8000)
    log("START", "创作雷达后端启动 port=%d model=%s" % (port, OPENROUTER_MODEL or "(未配置)"))
    if not OPENROUTER_API_KEY:
        log("WARN", "OPENROUTER_API_KEY 未配置，AI 接口将返回「AI 服务尚未配置」")
    if (os.getenv("XHS_PROVIDER") or "none").strip().lower() == "none":
        log("WARN", "XHS_PROVIDER=none，小红书检索将返回「缺少真实数据源」（不会用 Mock 冒充）")
    elif (os.getenv("XHS_PROVIDER") or "").strip().lower() == "tavily" and not os.getenv("TAVILY_API_KEY"):
        log("WARN", "XHS_PROVIDER=tavily 但未设置 TAVILY_API_KEY，检索将返回「缺少真实数据源」")
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except KeyboardInterrupt:
        log("STOP", "已停止")


if __name__ == "__main__":
    main()
