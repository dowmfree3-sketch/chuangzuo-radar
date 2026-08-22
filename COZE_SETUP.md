# 创作雷达 · Coze 智能体搭建指南（纯 Coze，老师点链接即用）

> 目标：在 Coze（推荐国内版 coze.cn）搭一个智能体，老师点你分享的链接就能对话跑通
> 「搜 B站视频 → 拆解爆点 → 生成脚本」全流程。**完全不依赖你的 OpenRouter/Render AI 额度**：
> 大脑用 Coze 自带模型（豆包），搜索走你 Render 后端的免 AI 接口。

---

## 一、整体架构
```
老师 ──点击分享链接──> Coze 智能体（豆包模型当大脑）
                          │
                          ├─ 工具1 搜索视频 ──> 你的 Render: POST /api/search-xhs-videos（不调AI，永远能用）
                          ├─ 工具2 拆解视频 ──> Coze LLM 节点（用 prompt 生成，不耗你的额度）
                          └─ 工具3 生成脚本 ──> Coze LLM 节点（同上）
```
> 为什么搜索走你的 Render？因为 `/api/search-xhs-videos` 只调 B站、**不调 AI**，所以你的 AI 额度耗尽也照常工作。
> 拆解/脚本是 AI 生成内容，改用 Coze 自带模型，**完全不碰你的 OpenRouter**。

---

## 二、第一步：创建智能体与人设
1. 打开 https://www.coze.cn → 登录 → 「创建智能体」
2. 名称：`创作雷达`；简介：`说一个创作想法，我自主搜视频、拆解爆点、生成短视频脚本`
3. **人设与回复逻辑（Persona）**——复制下面整段填进「人设与回复逻辑」框：

```
你是「创作雷达」内容创作智能体。用户给你一个模糊的创作想法，你要自主完成三步：
1. 调用工具 search_bilibili 搜索相关视频，用一句话告诉用户你为什么搜这个词；
2. 从结果里挑 1 条最值得参考的视频，调用工具 analyze_video 做拆解（一句话定位/开头钩子/内容结构/可借鉴爆点/受众洞察/复刻切入点/注意事项）；
3. 调用工具 generate_script 基于拆解生成可执行的短视频脚本（含分镜：口播/镜头/剪辑）。

规则：
- 每一步都用「创作者口吻」自然语言解释你的决策，像真人创作者一样。
- 如果用户想法太模糊，先追问一个关键问题，不要乱搜。
- 绝不编造播放量、点赞、评论等数据；只基于工具返回的真实标题/作者/链接分析。
- 所有内容用中文，具体可执行，不要空话套话。
```

---

## 三、第二步：配置三个插件（工具）

### 方式 A（推荐）：搜索走你的 Render 后端，拆解/脚本用 Coze LLM 节点

#### 工具1：search_bilibili（自定义 API 插件）
1. Coze 左侧「插件」→「创建插件」→ 选「API 插件」
2. 填写：
   - 插件名：`search_bilibili`
   - 描述：`根据关键词在 B站搜索相关视频`
3. 「添加接口」，按下面填：

**请求**
- 方法：`POST`
- URL：`https://chuangzuo-radar.onrender.com/api/search-xhs-videos`
- Headers：`Content-Type: application/json`
- Body（JSON）：
```json
{
  "queries": ["{{query}}"],
  "intent": {"theme": "{{query}}"},
  "accountContext": {}
}
```
**入参（Input）**
| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| query | string | 是 | 检索关键词 |

**出参（Output）**——Coze 里配「返回数据结构」，示例：
```json
{
  "code": "ok",
  "count": 6,
  "videos": [
    {"title":"视频标题","author":"作者","url":"https://b23.tv/xxx","cover":"封面图URL"}
  ]
}
```
4. 保存→测试（输入 `游戏`，应返回若干视频）→ 发布插件。

#### 工具2 & 3：analyze_video / generate_script
**最省事的做法：不做成 API 插件，直接用 Coze 的「LLM 节点」写在「工作流」里**（见第三步）。
因为这两步是纯 AI 生成，Coze 自己的模型就能做，不必走你的后端（省额度）。

> 如果你想让拆解/脚本也走你的后端（统一逻辑），可同样创建 API 插件：
> - `POST https://chuangzuo-radar.onrender.com/api/analyze-video`，body `{"video":{"title":"{{title}}","author":"{{author}}","url":"{{url}}"},"intent":{},"accountContext":{},"idea":""}`
> - `POST https://chuangzuo-radar.onrender.com/api/generate-script`，body `{"video":{"title":"{{title}}"},"analysis":{{analysis}},"intent":{},"accountContext":{},"idea":"{{idea}}"}`

---

## 四、第三步：用「工作流」串起全流程（推荐）
Coze 左侧「工作流」→ 新建，拖 3 个节点：

### 节点1：搜索（插件节点）
- 调用 `search_bilibili`，入参 `query` = 用户输入（或 LLM 从用户想法提取的关键词）

### 节点2：拆解（LLM 节点，模型选 豆包 Function/poetry 等）
- 系统提示：
```
你是短视频拆解专家。基于下面这条 B站视频的真实标题与作者做拆解，输出 JSON：
{"positioning":"一句话定位","hook":"开头钩子（引用标题真实措辞）","structure":["结构点1","结构点2"],
 "borrowable":["可借鉴爆点1","爆点2"],"audience_insight":"受众洞察","your_angle":"复刻切入点","caveats":"注意事项"}
只基于标题/作者分析，不编造数据。只输出 JSON。
```
- 用户输入：`视频标题：{{节点1.videos[0].title}}  作者：{{节点1.videos[0].author}}  用户想法：{{用户输入}}`

### 节点3：脚本（LLM 节点）
- 系统提示：
```
你是短视频脚本专家。基于拆解结论 + 用户想法，生成内容策略与可执行脚本，输出 JSON：
{"content_strategy":{"positioning":"","key_message":"","target_audience":"","tone":"","format":"","posting_suggestions":[]},
 "script":{"title":"","total_time":"60-90秒","scenes":[{"no":1,"stage":"开头钩子","time":"0-5s","oral":"口播","shot":"镜头","move":"剪辑"}]}}
口播要口语化，每个 scene 必须含 no/stage/time/oral/shot/move。只输出 JSON。
```
- 用户输入：`拆解结论：{{节点2输出}}  用户想法：{{用户输入}}  参考视频标题：{{节点1.videos[0].title}}`

### 结束节点
把节点3的脚本结构化输出（Coze 支持卡片/Markdown 渲染）。

---

## 五、第四步：发布并获取分享链接
1. 智能体页面右上角「发布」→ 选择「Bot Store / API / Web SDK」均可，**最简单选「发布到 Coze」**
2. 发布后点「分享」→ 复制链接（形如 `https://www.coze.cn/store/bot/xxx`）
3. **把这个链接发给老师**，老师打开即可对话，无需注册你任何服务

---

## 六、老师那边看到的效果
- 打开链接 → 输入「帮我做个关于大学生实习的短视频」
- 智能体：先说「我去 B站搜一下相关视频」→ 调 search_bilibili → 返回视频列表
- 智能体：选一条 → 拆解 → 出脚本
- 全程 Coze 模型 + 你的 Render 搜索，**不消耗你 OpenRouter 一分钱额度**

---

## 七、兜底：如果 Render 也挂了（纯 Coze 自给自足）
把「节点1 搜索」换成 Coze 自带的搜索插件（如「头条搜索」「网页搜索」），搜索词用 `site:bilibili.com 关键词`。
这样**完全不依赖你的任何服务器**，老师只要能打开 Coze 就能跑通。

---

## 八、验收对照（作业提交用）
| 验收点 | Coze 实现 |
|---|---|
| 自主调 ≥2 工具 | search_bilibili 插件 + 拆解/脚本 LLM 节点（或全部做成插件） |
| LLM 当大脑 | Coze 豆包模型驱动决策 |
| 多轮对话 | Coze Bot 原生支持 |
| 解释决策 | 人设里要求每步用自然语言说明 |
| 老师点链接即用 | Coze 分享链接，无需注册你的服务 |
