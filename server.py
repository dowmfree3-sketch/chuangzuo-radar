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
    POST /api/generate-script       拆解结果 + 用户意图 -> 内容策略 + 可执行脚本

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
                # 免费层每日总配额耗尽：所有 free 模型共享，重试/换模型都没用，
                # 直接失败，让上层尽快走降级逻辑（避免无谓地烧掉重试次数）。
                if "free-models-per-day" in msg or "Rate limit" in msg:
                    raise RuntimeError("AI 免费额度已耗尽（每日配额）")
                # 限流 / 5xx / 网络 / 超时：短暂等待后重试同一模型
                if "429" in msg or msg.startswith("AI HTTP 5") or "网络" in msg or "超时" in msg:
                    time.sleep(3)
                    continue
                # 其他错误（格式异常/返回空）直接换下一个模型
                break
    raise RuntimeError("AI 服务暂时不可用（所有免费模型均失败：%s）" % last_err)


def openrouter_chat(messages, tools=None, max_tokens=1500):
    """通用对话补全（支持 OpenAI 风格 tools/function calling）。

    返回原始 choices[0].message（含可能的 tool_calls）。失败抛 RuntimeError。
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("AI 服务尚未配置（缺少 OPENROUTER_API_KEY）")
    chain = _ai_model_chain()
    last_err = None
    for model in chain:
        for attempt in range(2):
            try:
                body = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": max_tokens}
                if tools:
                    body["tools"] = tools
                    body["tool_choice"] = "auto"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + OPENROUTER_API_KEY,
                    "HTTP-Referer": "https://creation-radar.local",
                    "X-Title": "CreationRadar",
                }
                req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode("utf-8"),
                                             headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                body_err = b""
                try:
                    body_err = e.read()
                except Exception:
                    pass
                if "free-models-per-day" in str(body_err) or "Rate limit" in str(body_err):
                    raise RuntimeError("AI 免费额度已耗尽（每日配额）")
                last_err = RuntimeError("AI HTTP %s" % e.code)
                if e.code == 429:
                    last_err = RuntimeError("AI 限流(429)")
                time.sleep(3); continue
            except (urllib.error.URLError, socket.timeout) as e:
                last_err = RuntimeError("AI 网络错误(%s)" % e.reason)
                time.sleep(3); continue
    raise last_err or RuntimeError("AI 服务暂时不可用")


# ----------------------------- 智能体（Agent）编排层 -----------------------------
# 让 LLM 充当"大脑"，自主决定调用哪些工具，串起"理解想法 -> 搜 B站视频 -> 拆解 -> 出脚本"全流程。
# 工具直接复用本文件的业务函数（handle_search / handle_analyze_video / handle_generate_script）。

AGENT_SYSTEM = (
    "你是「创作雷达」内容创作智能体。你的任务是帮助用户把一个模糊的创作想法，"
    "自主完成：① 在 B站搜索相关视频；② 挑选一条最值得参考的视频做拆解（爆点/结构分析）；"
    "③ 基于拆解生成可执行的短视频脚本与内容策略。\n"
    "你拥有以下工具，请像真人创作者一样自主判断每一步该调用哪个工具，并在回复里用自然语言"
    "向用户解释你为什么要这么做（例如：『我先去 B站搜一下「游戏」相关的视频』）。\n"
    "工作流程建议：先调用 search_bilibili 搜索；拿到结果后挑选一条最相关的视频，"
    "调用 analyze_video 拆解它；最后调用 generate_script 产出脚本。\n"
    "如果用户想法太模糊，可以先向用户追问一个关键问题（用普通文字回复，不调工具）。\n"
    "所有回复用中文，简洁、有「创作者口吻」。不要编造视频数据。"
)

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_bilibili",
            "description": "根据关键词在 B站搜索相关视频，返回视频列表（标题、作者、链接、封面等）。当用户给出创作方向/关键词时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，例如『游戏』『大学生实习 vlog』"},
                    "limit": {"type": "integer", "description": "返回条数，默认 6"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_video",
            "description": "对单条视频做拆解分析：一句话定位、开头钩子、内容结构、可借鉴爆点、受众洞察、复刻切入点、注意事项。输入视频的标题/作者/链接/摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "视频标题"},
                    "author": {"type": "string", "description": "作者"},
                    "url": {"type": "string", "description": "视频链接"},
                    "description": {"type": "string", "description": "视频公开摘要（可选）"}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_script",
            "description": "基于某条视频的拆解结论 + 用户创作方向，生成内容策略与可执行短视频脚本（分镜：口播/镜头/剪辑）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "参考视频标题"},
                    "analysis": {"type": "object", "description": "analyze_video 返回的拆解结论对象"},
                    "idea": {"type": "string", "description": "用户原始创作想法"}
                },
                "required": ["title", "analysis"]
            }
        }
    },
]


def _exec_agent_tool(name, args, ctx):
    """执行智能体工具，返回 (tool_result_dict, artifact)。artifact 用于前端结构化展示。"""
    if name == "search_bilibili":
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "缺少 query"}, None
        res = handle_search({"queries": [q], "intent": {"theme": q}, "accountContext": ctx.get("accountContext", {})})
        vids = res.get("results", [])
        slim = [{"id": v.get("id"), "title": v.get("title"), "author": v.get("author"),
                 "url": v.get("url"), "cover": v.get("cover")} for v in vids[:8]]
        return {"code": res.get("code", "ok"), "count": len(slim), "videos": slim,
                "note": res.get("note", "")}, {"type": "videos", "videos": slim}
    if name == "analyze_video":
        res = handle_analyze_video({
            "video": {"title": args.get("title", ""), "author": args.get("author", ""),
                      "url": args.get("url", ""), "description": args.get("description", "")},
            "intent": ctx.get("intent", {}),
            "accountContext": ctx.get("accountContext", {}),
            "idea": ctx.get("idea", ""),
        })
        if res.get("code") == "ai_unavailable":
            return {"error": "AI 拆解失败：" + res.get("error", "")}, None
        return {"analysis": res.get("analysis"), "video": res.get("video")}, \
               {"type": "analysis", "analysis": res.get("analysis"), "video": res.get("video")}
    if name == "generate_script":
        res = handle_generate_script({
            "video": {"title": args.get("title", "")},
            "analysis": args.get("analysis", {}),
            "intent": ctx.get("intent", {}),
            "accountContext": ctx.get("accountContext", {}),
            "idea": args.get("idea", ""),
        })
        if res.get("code") == "ai_unavailable":
            return {"error": "AI 脚本生成失败：" + res.get("error", "")}, None
        return {"content_strategy": res.get("content_strategy"), "script": res.get("script")}, \
               {"type": "script", "content_strategy": res.get("content_strategy"), "script": res.get("script")}
    return {"error": "未知工具 " + name}, None


def handle_agent_chat(data):
    """智能体对话入口：让 LLM 自主调工具，返回逐步事件流。

    返回 { events: [...] }。前端按事件类型渲染（思考/工具调用/结果/最终回复）。
      {"type":"thought","text":"..."}          模型自然语言回复（含决策解释）
      {"type":"tool","name":"search_bilibili","args":{...}}  即将调用工具
      {"type":"tool_result","name":...,"result":{...},"artifact":{...}}  工具结果
      {"type":"error","text":"..."}
    """
    if not OPENROUTER_API_KEY:
        return {"events": [{"type": "error", "text": "AI 服务尚未配置（缺少 OPENROUTER_API_KEY），智能体无法运行。"}], "done": True}

    messages = list(data.get("messages") or [])
    if not messages:
        return {"events": [{"type": "error", "text": "缺少对话内容"}], "done": True}
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": AGENT_SYSTEM}] + messages

    events = []
    ctx = {"accountContext": data.get("accountContext") or {}, "idea": data.get("idea") or ""}
    max_rounds = 6  # 防止无限工具循环
    for _ in range(max_rounds):
        try:
            msg = openrouter_chat(messages, tools=AGENT_TOOLS, max_tokens=1200)
        except RuntimeError as e:
            events.append({"type": "error", "text": "AI 调用失败：" + str(e)})
            break
        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = (msg.get("content") or "").strip()
            if content:
                events.append({"type": "thought", "text": content})
            break
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            events.append({"type": "tool", "name": name, "args": args})
            result, artifact = _exec_agent_tool(name, args, ctx)
            events.append({"type": "tool_result", "name": name, "result": result, "artifact": artifact})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": json.dumps(result, ensure_ascii=False),
            })
            if name == "search_bilibili" and not ctx.get("intent", {}).get("theme"):
                ctx["intent"] = {"theme": args.get("query", "")}
            if name == "analyze_video" and not ctx.get("idea"):
                ctx["idea"] = args.get("title", "")
    else:
        events.append({"type": "thought", "text": "（已达到最大步骤数，流程自动结束。你可以继续提问。）"})

    return {"events": events, "done": True}


# ----------------------------- 业务逻辑 -----------------------------
# 理解结果缓存：相同想法不重复消耗 AI 配额（免费层每日仅 50 次，很珍贵）。
_UNDERSTAND_CACHE = {}
_UNDERSTAND_CACHE_TTL = 6 * 3600  # 6 小时


def handle_understand(data):
    idea = (data.get("idea") or "").strip()
    if not idea:
        return {"error": "请输入你的想法"}
    account_ctx = data.get("accountContext") or {}
    account = account_ctx.get("account") or ""

    # 缓存命中（相同想法 + 相同账号方向）：直接返回，省一次 AI 配额
    cache_key = (idea, account)
    hit = _UNDERSTAND_CACHE.get(cache_key)
    if hit and (time.time() - hit[0]) < _UNDERSTAND_CACHE_TTL:
        log("UNDERSTAND CACHE HIT", "idea=<{:.20}>".format(idea))
        return hit[1]

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
        # AI 不可用（如免费额度耗尽/限流）：降级为用原始输入作为检索词，
        # 保证搜索流程仍可继续，前端会提示「AI 离线，已按原词检索」。
        # 注意：降级结果不进缓存，AI 恢复后应重新走正常理解。
        log("UNDERSTAND DEGRADE", "AI 不可用，使用原始输入检索：%s" % str(e)[:80])
        return {
            "intent": {"theme": idea, "scene": "", "audience": "", "content_goal": ""},
            "queries": [idea],
            "need_clarify": False,
            "clarify_question": "",
            "clarify_options": [],
            "ai_unavailable": True,
        }

    # 容错：确保关键字段存在
    intent = d.get("intent") or {}
    if not intent.get("theme"):
        intent["theme"] = idea
    queries = d.get("queries") or []
    if not isinstance(queries, list):
        queries = [str(queries)]
    res = {
        "intent": intent,
        "queries": queries[:5],
        "need_clarify": bool(d.get("need_clarify")),
        "clarify_question": d.get("clarify_question") or "",
        "clarify_options": d.get("clarify_options") or [],
    }
    _UNDERSTAND_CACHE[cache_key] = (time.time(), res)
    return res


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
                    "免费检索源收录有限，当前仅找到少量相关视频。"
                    "如需稳定检索更多相关视频，建议接入 XHS_PROVIDER=rest 并配置 XHS_REST_BASE。"
                )
            return res
        if src_unavailable:
            return {"results": [], "code": "xhs_source_unavailable", "realtime": False,
                    "note": "所有检索词的视频数据源当前都不可用（B站/Tavily 免费层可能限流，或暂未收录该主题）。可稍后重试，"
                            "或接入 XHS_PROVIDER=rest 并配置 XHS_REST_BASE 指向合规内容接口以稳定检索。"}
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


def handle_generate_script(data):
    """基于视频拆解结果 + 用户意图，生成内容策略与可执行视频脚本。

    诚实原则：
    - 只依据前端传入的真实拆解字段与用户意图进行策略推导；
    - 严禁编造原始播放量、点赞、评论、作者背景等数据；
    - 脚本应为参考模板，用户需结合自身真实素材使用。
    """
    video = data.get("video") or {}
    analysis = data.get("analysis") or {}
    intent = data.get("intent") or {}
    account_ctx = data.get("accountContext") or {}
    idea = (data.get("idea") or "").strip()

    title = (video.get("title") or "").strip()
    if not title:
        return {"error": "缺少待生成脚本的视频信息", "code": "bad_input"}

    system = (
        "你是一个短视频内容策略与脚本创作专家。用户已经选中了一条参考视频并完成了拆解，"
        "现在需要你基于拆解结论和用户自身账号方向，输出一份【内容策略】+【可执行视频脚本】。"
        "你必须只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。结构：\n"
        "{\n"
        '  "content_strategy": {\n'
        '    "positioning": "给这条新内容的一句话定位",\n'
        '    "key_message": "核心要传递的信息/卖点（一句话）",\n'
        '    "target_audience": "明确的目标受众与他们的痛点/需求",\n'
        '    "tone": "整体风格调性（如：真诚分享 / 干货紧凑 / 轻松共情 / 高能反转）",\n'
        '    "format": "建议的视频形式与时长（如：口播 60-90 秒 / vlog 切片 45 秒）",\n'
        '    "posting_suggestions": ["发布建议1", "发布建议2"]\n'
        '  },\n'
        '  "script": {\n'
        '    "title": "为新脚本取的标题（不要照抄原视频标题，要结合用户想法差异化）",\n'
        '    "total_time": "建议总时长，如 60-90 秒",\n'
        '    "scenes": [\n'
        '      {"no": 1, "stage": "开头钩子", "time": "0-5s", "oral": "口播文案", "shot": "画面/镜头建议", "move": "剪辑/动效建议"},\n'
        '      {"no": 2, "stage": "主体内容", "time": "5-40s", "oral": "口播文案", "shot": "画面/镜头建议", "move": "剪辑/动效建议"},\n'
        '      {"no": 3, "stage": "结尾转化", "time": "40-60s", "oral": "口播文案", "shot": "画面/镜头建议", "move": "剪辑/动效建议"}\n'
        '    ]\n'
        '  }\n'
        "}\n"
        "硬性规则：\n"
        "- 必须基于参考视频拆解结论进行创作，但不要照搬原视频；要结合用户的账号方向做差异化。\n"
        "- 口播文案要口语化、自然，适合短视频口播，避免书面化长句。\n"
        "- 每个 scene 必须包含 no / stage / time / oral / shot / move 六个字段。\n"
        "- 不要编造原视频没有的播放量、点赞、评论、作者背景等数据。\n"
        "- 所有内容用中文。"
    )
    user = (
        "用户账号方向：%s\n用户原始想法：%s\n意图理解：%s\n"
        "参考视频标题：%s\n"
        "拆解结论：%s"
        % (
            account_ctx.get("account") or "未指定",
            idea or "（未提供）",
            json.dumps(intent, ensure_ascii=False),
            title,
            json.dumps(analysis, ensure_ascii=False),
        )
    )
    try:
        d = openrouter_json(system, user, expect="object", max_tokens=2500)
    except RuntimeError as e:
        return {"error": str(e), "code": "ai_unavailable"}

    strategy = d.get("content_strategy") or {}
    script = d.get("script") or {}
    # 确保 scenes 字段完整
    scenes = script.get("scenes") or []
    cleaned_scenes = []
    for s in scenes:
        if isinstance(s, dict):
            cleaned_scenes.append({
                "no": s.get("no") or (len(cleaned_scenes) + 1),
                "stage": s.get("stage") or "",
                "time": s.get("time") or "",
                "oral": s.get("oral") or "",
                "shot": s.get("shot") or "",
                "move": s.get("move") or "",
            })
    script["scenes"] = cleaned_scenes

    return {
        "content_strategy": {
            "positioning": strategy.get("positioning") or "",
            "key_message": strategy.get("key_message") or "",
            "target_audience": strategy.get("target_audience") or "",
            "tone": strategy.get("tone") or "",
            "format": strategy.get("format") or "",
            "posting_suggestions": strategy.get("posting_suggestions") or [],
        },
        "script": {
            "title": script.get("title") or (title + " 复刻版"),
            "total_time": script.get("total_time") or "60-90 秒",
            "scenes": cleaned_scenes,
        },
        "code": "ok",
    }


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
        elif p == "/agent.html":
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
            elif p == "/api/generate-script":
                self._send(200, handle_generate_script(data))
            elif p == "/api/agent-chat":
                self._send(200, handle_agent_chat(data))
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
