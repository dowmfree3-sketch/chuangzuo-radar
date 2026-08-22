# -*- coding: utf-8 -*-
"""
创作雷达 · 终端版智能体（可独立运行，无需网页）

运行：
    pip install -r requirements.txt   # 本项目零第三方依赖，可跳过
    python agent_cli.py

说明：
    - 复用 server.py 的 LLM 与业务函数（search / analyze / script），
      由大模型通过 function calling 自主决定每一步调用哪个工具。
    - 必须在环境变量配置 OPENROUTER_API_KEY（同 .env）。
    - 没有指定框架，纯标准库实现，便于移植到 LangChain / AutoGen / WorkBuddy。
"""
import os
import sys
import json

# 复用项目内模块（server.py 已内置 .env 加载）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S  # noqa


def run():
    if not S.OPENROUTER_API_KEY:
        print("❌ 未配置 OPENROUTER_API_KEY。请在 .env 或环境变量中设置后重试。")
        return
    print("🤖 创作雷达智能体已启动（模型：%s）" % S.OPENROUTER_MODEL)
    print("   输入创作想法开始；输入 exit / quit 退出。\n")

    ctx = {"accountContext": {}, "idea": ""}
    history = [{"role": "system", "content": S.AGENT_SYSTEM}]

    while True:
        try:
            text = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 👋"); break
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            print("再见 👋"); break

        history.append({"role": "user", "content": text})
        # 多轮工具循环（最多 6 步）
        for _ in range(6):
            try:
                msg = S.openrouter_chat(history, tools=S.AGENT_TOOLS, max_tokens=1200)
            except RuntimeError as e:
                print("⚠️ AI 调用失败：%s" % e); break
            history.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                if (msg.get("content") or "").strip():
                    print("智能体 > " + msg["content"].strip())
                break
            for tc in calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                print("  ⚙️ 调用工具 [%s] %s" % (name, json.dumps(args, ensure_ascii=False)))
                result, artifact = S._exec_agent_tool(name, args, ctx)
                if artifact:
                    if artifact["type"] == "videos":
                        print("     ↳ 搜到 %d 条视频，例如：%s" % (len(artifact["videos"]),
                              (artifact["videos"][0].get("title") if artifact["videos"] else "无")))
                    elif artifact["type"] == "analysis":
                        a = artifact["analysis"] or {}
                        print("     ↳ 拆解完成：定位=%s；钩子=%s" % (a.get("positioning",""), a.get("hook","")))
                    elif artifact["type"] == "script":
                        print("     ↳ 脚本生成：标题=%s，%d 个分镜" % (
                            (artifact["script"] or {}).get("title",""), len((artifact["script"] or {}).get("scenes",[]))))
                elif result.get("error"):
                    print("     ↳ 错误：%s" % result["error"])
                history.append({"role": "tool", "tool_call_id": tc.get("id"),
                                "content": json.dumps(result, ensure_ascii=False)})
                if name == "search_bilibili" and not ctx.get("intent", {}).get("theme"):
                    ctx["intent"] = {"theme": args.get("query", "")}
                if name == "analyze_video" and not ctx.get("idea"):
                    ctx["idea"] = args.get("title", "")
        print()


if __name__ == "__main__":
    run()
