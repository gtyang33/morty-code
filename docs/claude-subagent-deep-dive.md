# Claude Code Subagent 模块深挖

## Claude Code 的实现要点

Claude Code 的 `AgentTool` 不是普通函数工具，而是一个 agent runtime 分发器。入口 schema 接收 `description`、`prompt`、`subagent_type`、`model`、`run_in_background`、`isolation` 等参数，然后按 agent 定义、权限规则、MCP 前置条件和 feature gate 决定执行路径。

核心链路：

1. `AgentTool.tsx` 解析请求，选择 built-in / custom / plugin agent。
2. `loadAgentsDir.ts` 从 built-in、插件、用户/project/policy 配置加载 agent，并按优先级覆盖。
3. `runAgent.ts` 为子代理生成独立 `agentId`、system prompt、工具池、permission mode、MCP clients、sidechain transcript 和生命周期清理逻辑。
4. `forkSubagent.ts` 是特殊路径：继承父线程 prompt 前缀和工具定义，牺牲 agent 专属 prompt，换取 prompt cache 字节级稳定。
5. 同步 agent 可以中途转后台；后台 agent 有 task registry、progress summary、kill/notification、output file。

## 做得好的地方

- **上下文隔离清楚**：子代理有独立 agentId、read file state、tool result replacement、todo、hooks 和 transcript，避免污染父线程。
- **工具池按 agent 重建**：子代理不是简单继承父线程 allowed tools，而是按 agent 定义、权限模式、MCP 可用性重新装配。
- **缓存意识强**：fork 子代理为 prompt cache 做了专门路径，普通子代理也会记录 cache-safe 参数供后台 summarization 使用。
- **失败不破坏主链路**：MCP cleanup、sidechain 写入、metadata 写入、worktree cleanup 都尽量容错。
- **成本优化细**：Explore/Plan 可省略 CLAUDE.md 和陈旧 git status，thinking 对普通子代理默认关闭。

## 复杂和代价

- `AgentTool.tsx` 承载同步、后台、teammate、worktree、remote、fork 多条路径，单文件认知负担很高。
- custom agent frontmatter 能配置 tools、MCP、hooks、skills、memory、permission、background、isolation，能力强但调试面大。
- fork path 为 cache 命中引入了与普通 agent 不同的语义，递归 guard、tool byte stability、autocompact 后的识别都需要额外代码。

## morty-code 本轮实现范围

本轮先实现同步、短轮数、模型可调用的 `spawn_agent`：

- 新增 `AgentDefinition` / `AgentRegistry`，内置 `general-purpose`、`Explore`、`Plan`、`verification`。
- 支持读取 `.morty/agents/*.md` 的简单 frontmatter，为后续 custom agent 留接口。
- 新增 `SubagentRunner`，复用已有 `ForkedAgentRunner` clone 可变状态，但为子代理重建 system prompt 和工具 schema。
- 在本地工具开启时自动注册 `spawn_agent`，并禁止第一版递归 spawn。
- 子代理失败返回结构化 `status` / metadata，不抛到父 query loop 之外。

暂不实现：

- 后台任务、progress UI、kill/resume。
- worktree/remote isolation。
- MCP 服务器前置条件和 agent 专属 MCP。
- teammate / mailbox / SendMessage。
