# 创作雷达（Creation Radar）

一个面向**小红书创作者**的 AI 内容策略助手 MVP：用真实大模型理解你的创作想法，
去**真实的小红书视频笔记**里检索相关内容，再用 AI 筛选出最值得参考的 Top5，并给出推荐理由。

> 设计铁律：**真实数据、真实 AI、零 Mock、零付费**。任何环节拿不到真实数据都如实报错，绝不用假数据顶替。

---

## 架构

```
浏览器 ──> 本后端(server.py) ──> OpenRouter(免费 LLM)
                            └──> 小红书 Adapter(tavily 搜索 API)
```

- **前端**：单个 HTML（`22-app-merged+workflow.html`，由后端托管在 `/`）
- **后端**：`server.py`，Python 标准库实现，**零第三方依赖**
- **数据层**：`xiaohongshu_adapter.py`，可插拔 provider（none / rest / tavily）
- **密钥**：只存在于后端 `.env`，绝不出现在前端、绝不回传、绝不写入日志

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ai_status` | 检查 AI / 数据源是否就绪 |
| POST | `/api/understand` | 用户输入 → AI 意图理解 + 检索 Query |
| POST | `/api/search-xhs-videos` | Query → 小红书视频检索 + 双重视频过滤 |
| POST | `/api/rank-xhs-videos` | 候选视频 + 用户需求 → AI 排序筛选 Top5 |

---

## 环境变量（`.env`）

```bash
# 真实大模型（只用免费模型，任何失败都明确报错，绝不 fallback 到付费模型）
OPENROUTER_API_KEY=sk-or-v1-xxxx          # 注册 https://openrouter.ai/keys
OPENROUTER_MODEL=google/gemma-4-26b-a4b-it:free

# 小红书数据源：tavily（推荐，免费合规）/ rest（自建合规接口）/ none（关闭）
XHS_PROVIDER=tavily
TAVILY_API_KEY=tvly-xxxx                   # 注册 https://app.tavily.com

PORT=8000
```

`.env.example` 是模板，复制为 `.env` 填入真实值即可（`.env` 不提交）。

---

## 如何获取免费数据源（Tavily）

1. 打开 **https://app.tavily.com** → 用 GitHub/Google 登录（**无需绑卡**）
2. 左侧 **API Keys** → 复制 `tvly-` 开头的密钥
3. 填入 `.env` 的 `TAVILY_API_KEY`
4. 免费档：**1000 积分/月**，1 次检索 = 1 积分，SOC2、零数据留存，合规

> 备选 `XHS_PROVIDER=rest`：当你有能返回 `type=video` 字段的合规小红书内容接口时，
> 设置 `XHS_REST_BASE` / `XHS_REST_KEY` 指向它，adapter 顶部 CONTRACT 约定了字段格式。

---

## 如何启动

```bash
cd <本目录>
python3 server.py
# 打开 http://127.0.0.1:8000
```

启动后日志会显示模型与数据源状态；若 key 缺失会明确 WARN（但不会用 Mock 顶替）。

---

## 如何测试

```bash
# 1) 状态
curl http://127.0.0.1:8000/api/ai_status

# 2) AI 理解（真实免费模型，零费用）
curl -s -m 90 -X POST http://127.0.0.1:8000/api/understand \
  -H "Content-Type: application/json" \
  -d '{"idea":"我想做一个关于AI产品经理面试经验分享的视频"}'

# 3) 真实小红书视频检索
curl -s -m 60 -X POST http://127.0.0.1:8000/api/search-xhs-videos \
  -H "Content-Type: application/json" \
  -d '{"queries":["AI产品经理面试经验分享"],"intent":{"theme":"AI产品经理面试经验分享"}}'

# 4) AI 排序 Top5（把第3步的 results 作为 candidates 传入）
curl -s -m 120 -X POST http://127.0.0.1:8000/api/rank-xhs-videos \
  -H "Content-Type: application/json" \
  -d '{"candidates":[...],"intent":{"theme":"AI产品经理面试经验分享"}}'
```

---

## 诚实声明（务必了解）

1. **视频判定**：Tavily 返回的是网页搜索结果，不是小红书开放平台的结构化元数据。
   本产品利用小红书分享 URL 自带的 `type=video` / `type=normal` 参数做**高可信视频判定**；
   无该参数时回退到标题/摘要的图文信号。可靠性高，但非「官方 API 逐条核验」。
   若要 100% 类型确定，请改用 `XHS_PROVIDER=rest` + 自建合规接口。
2. **免费模型延迟**：`gemma-4-26b-a4b-it:free` 免费档会排队，单次 AI 调用约 20–90s，
   整条链路偏慢但**零费用、余额不扣**。要更快可在 `.env` 换一个更快的 `:free` 模型 id。
3. **额度**：Tavily 免费档 1000 积分/月；OpenRouter 免费模型按模型额度。超限会报错（诚实提示）。
4. **绝无 Mock**：数据源或 AI 不可用时，前端会显示「数据源未连接 / AI 暂时没有回应」，
   不会用任何假数据、假 URL、假作者填充。

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `server.py` | 零依赖 Python 后端，4 个接口 + 静态托管 |
| `xiaohongshu_adapter.py` | 数据层，统一 `search_videos()` + 双重视频过滤 + 可插拔 provider |
| `.env` / `.env.example` | 后端密钥与环境（key 仅在此处） |
| `22-app-merged+workflow.html` | 前端单页（由后端托管） |
| `index.html` | 工作区内的前端副本，部署用（与后端同源） |
| `Procfile` / `runtime.txt` / `Dockerfile` / `requirements.txt` | 部署配置（Render / HF Spaces / 容器通用） |

---

## 部署到稳定网址（推荐 Hugging Face Spaces）

> CloudStudio / 纯静态托管**跑不了本后端**（密钥与真实检索都在后端），必须部署到能运行 Python 后端的平台。

### 方案 A：Hugging Face Spaces（免费、稳定、无需信用卡）
1. 注册 https://huggingface.co （免费）。
2. 生成令牌：右上角头像 → **Settings → Access Tokens → New token**， scope 选 **write**。
3. 把令牌给 AI（或直接用 `huggingface_hub` 执行创建 Space + 上传 + 配置密匙）。
4. Space 用 **Docker SDK**，监听 `$PORT`（默认 7860），本仓库 `Dockerfile` 已写好。
5. 在 Space 的 **Settings → Secrets** 里配置 4 个环境变量（等同于本地 `.env`，**不要写进代码**）：
   - `OPENROUTER_API_KEY=sk-or-...`
   - `OPENROUTER_MODEL=google/gemma-4-26b-a4b-it:free`
   - `XHS_PROVIDER=tavily`
   - `TAVILY_API_KEY=tvly-...`
6. 部署完成后得到稳定网址 `https://你的名-创作雷达.hf.space`。

### 方案 B：Render（免费、稳定 *.onrender.com，需 GitHub）
1. 把本目录推到 GitHub 仓库。
2. https://render.com → New → Web Service → 连 GitHub 仓库。
3. Build Command 留空 / `pip install -r requirements.txt`，Start Command：`python server.py`。
4. 在 Environment 里设置上面 4 个变量（含 `PORT` 由 Render 自动注入）。
5. 得到稳定网址 `https://创作雷达.onrender.com`（免费档空闲后会冷启动，约 30–50s）。

### 本地 / 部署都适用的要点
- 前端 `API_BASE` 已做同源兼容：用 `file://` 打开才回退到 `127.0.0.1:8000`，经 http/https 访问自动走同源，部署无需改前端。
- 后端始终从环境变量读密钥；部署平台设置 Secrets/Environment 即可，`.env` 已被 `.gitignore` 排除，不会上传。

