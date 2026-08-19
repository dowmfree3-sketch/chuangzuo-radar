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

环境变量：
    OPENROUTER_API_KEY    (必填) OpenRouter Key
    OPENROUTER_MODEL      (可选) 模型，默认 meta-llama/llama-3.1-8b-instruct:free
    XHS_PROVIDER          (可选) 数据源：none(默认) | rest | tavily
    XHS_REST_BASE         (rest 时必填) 你的合规小红书内容接口基址
    XHS_REST_KEY          (可选) 接口鉴权
    TAVILY_API_KEY        (tavily 时必填) Tavily 搜索 API 密钥（tvly- 开头，免费档可用）
    APP_HTML              (可选) 要托管的 HTML 路径，默认取同目录或桌面文件
    PORT                  (可选) 默认 8000
"""

import os
import sys
import json
import math
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
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free").strip()
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


def openrouter_json(system_prompt, user_prompt, expect="object", max_tokens=1500):
    """调用 OpenRouter，要求模型只返回 JSON。失败即抛错，绝不降级到付费模型。"""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("AI 服务尚未配置（缺少 OPENROUTER_API_KEY）")

    log("AI REQUEST", "model=%s expect=%s" % (OPENROUTER_MODEL, expect))
    body = {
        "model": OPENROUTER_MODEL,
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
        # 明确失败，不自动切换付费模型
        raise RuntimeError("AI 服务暂时不可用（HTTP %s）" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("AI 服务暂时不可用（网络错误）")

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("AI 返回格式异常")

    try:
        parsed = _parse_json_from_text(content)
    except Exception:
        raise RuntimeError("AI 输出不是合法 JSON")
    if expect == "array" and not isinstance(parsed, list):
        raise RuntimeError("AI 输出不是数组")
    if expect == "object" and not isinstance(parsed, dict):
        raise RuntimeError("AI 输出不是对象")
    return parsed


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

    candidates = []
    seen = set()
    source_fail = None
    for q in eff_queries:
        try:
            items = xhs.search_videos(q, limit=15)
        except xhs.XHSSourceUnavailable as e:
            # 数据源未接入：如实上报，绝不以 Mock 冒充
            return {"results": [], "code": "xhs_source_unavailable", "realtime": False, "note": str(e)}
        except Exception as e:
            log("SEARCH ERROR", str(e))
            continue
        log("SEARCH RESULT COUNT", "query=%s count=%d" % (q, len(items)))
        for it in items:
            key = it.get("url") or it.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(it)

    log("VIDEO FILTER COUNT", str(len(candidates)))
    if not candidates:
        return {"results": [], "code": "no_results", "realtime": True, "source": "xiaohongshu"}
    return {"results": candidates, "code": "ok", "realtime": True, "source": "xiaohongshu"}


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
