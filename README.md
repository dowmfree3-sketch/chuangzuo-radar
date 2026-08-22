# 创作雷达 · AI 创作智能体

一个**由大模型驱动的创作智能体（Agent）**：你只要说一个模糊的创作想法，它会**自主调用工具**，完成
「在 B站搜索相关视频 → 挑选一条拆解爆点 → 生成可执行的短视频脚本与内容策略」的完整流程，并在每一步用自然语言解释自己的决策。

> 这是一个**真正的智能体**：大脑是 LLM（OpenRouter 上的免费模型），它自己决定「先搜什么、拆哪条、怎么出脚本」，
> 而不是前端按钮硬编码的流水线。支持**多轮对话**与**工具调用**。

---

## 一、核心能力（验收点对照）

| 验收点 | 实现 |
|---|---|
| 自主调用 ≥2 个工具 | 内置 3 个工具：`search_bilibili`、`analyze_video`、`generate_script`，由 LLM 通过 Function Calling 自主编排 |
| 以 LLM 为大脑 | 后端 `openrouter_chat` 走 OpenAI 风格 tools 调用，模型自己决定下一步 |
| 多轮对话 / 记忆 | 前端与 CLI 都维护 `messages` 上下文，支持追问 |
| 解释决策 | 系统提示要求模型用「创作者口吻」说明每一步为什么这么做 |
| 不编造数据 | 拆解/脚本严格基于真实视频标题与公开摘要，缺失即写「暂无数据」 |

---

## 二、运行方式（老师电脑上也能跑）

### 方式 A：网页智能体（推荐演示）
```bash
# 1. 准备 key（任选其一填到 .env）
#    OPENROUTER_API_KEY=sk-or-xxxx        # 在 https://openrouter.ai/keys 免费申请
#    XHS_PROVIDER=bilibili                # 已默认，使用 B站作为视频数据源
pip install -r requirements.txt          # 本项目零第三方依赖，可跳过
python server.py                          # 启动后端（默认 8000 端口）
```
浏览器打开 `http://localhost:8000/agent.html` 即可对话。

### 方式 B：终端智能体（最轻量，无需浏览器）
```bash
python agent_cli.py
```
直接在命令行输入想法，智能体会打印每一步工具调用与最终结果。

### 方式 C：原「按钮式」创作流程（保留）
浏览器打开 `http://localhost:8000/`（即 `index.html`）——这是之前的可视化分步版本，搜视频已改为纯关键词、不依赖 AI。

---

## 三、环境变量（`.env`）
```
OPENROUTER_API_KEY=sk-or-xxxx      # 必填，LLM 大脑
OPENROUTER_MODEL=google/gemma-4-26b-a4b-it:free   # 可选，免费模型
XHS_PROVIDER=bilibili              # 视频数据源
TAVILY_API_KEY=                    # 可选，B站源兜底
PORT=8000
```
> AI 用的是 OpenRouter **免费模型**，每天有额度上限；额度耗尽时智能体会明确报错，
> 不会编造结果。需要稳定使用可在 OpenRouter 充值少量 credits（免费额度升至 1000/天）。

---

## 四、接入 WorkBuddy（作为专家 / 技能）

本智能体**不绑定任何框架**，便于挂入 WorkBuddy。两种接法：

### 1. 作为「专家」（最直接）
把下面这段作为专家的系统设定 + 工具描述注册到 WorkBuddy：
- **系统提示（system）**：见 `server.py` 中的 `AGENT_SYSTEM` 常量。
- **工具（tools）**：见 `server.py` 中的 `AGENT_TOOLS`（search_bilibili / analyze_video / generate_script）。
- **工具实现**：直接复用 `server.py` 的 `handle_search` / `handle_analyze_video` / `handle_generate_script`。
- **编排循环**：见 `handle_agent_chat`——标准 function-calling 多轮循环，可直接搬进专家的执行逻辑。

### 2. 作为「技能 / Skill」
把 `agent_cli.py` 的 `run()` 或一个 `/api/agent-chat` 的 HTTP 调用封装成 Skill：
- 触发词：「帮我做个短视频」「拆解一个视频」「生成脚本」「创作雷达」。
- Skill 内部只需 `POST /api/agent-chat` 并逐步渲染 `events`（thought / tool / tool_result）。

---

## 五、项目结构
```
server.py         后端：LLM 调用 + 业务函数 + Agent 编排（/api/agent-chat）
agent.html        网页版对话智能体界面
agent_cli.py      终端版智能体（独立可跑）
index.html        原分步式创作流程（保留）
xiaohongshu_adapter.py  B站/检索数据源适配器
requirements.txt  依赖（本项目为零依赖，仅占位）
README.md         本文件
```

## 六、诚信说明（作业提交建议）
- 智能体的「搜索」来自真实 B站数据，「拆解/脚本」由 LLM 基于真实标题与摘要生成，**不伪造播放量等数据**。
- 若老师要求「必须看到模型调用」，网页版 `/agent.html` 的每一步工具调用都有可视化卡片，可现场演示。
