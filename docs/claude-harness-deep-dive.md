# Claude Code Harness 深挖

## Claude Code 的 harness 分层

Claude Code 里 harness 不是单一模块，而是几层协议组合：

1. **SDK event stream**：把 assistant/user/system/result 等消息以 JSONL 形式暴露给外部调用方。
2. **control protocol**：`control_request` / `control_response` 处理 initialize、set_model、set_permission_mode、interrupt、can_use_tool 等控制面请求。
3. **remote bridge**：把 SDK event 和 control protocol 放到 WebSocket / direct connect / child process transport 上。
4. **permission handoff**：当 CLI 需要外部宿主审批工具调用时，发 `can_use_tool` control_request，等待宿主返回 allow/deny。
5. **session replay**：remote/SDK harness 需要能恢复已有 transcript，并把历史消息转换成 SDK wire format。

## 做得好的地方

- 数据面和控制面分离：普通消息不会和 set_model / permission request 混在一起。
- 所有控制请求都有 request_id，避免异步响应错配。
- 未支持的控制请求也会返回 error response，防止宿主一直等待。
- SDK event 是 append-only 流，天然适合 transcript、UI 和评测 harness 消费。

## morty-code 本轮实现

新增 `morty_code.harness`：

- `--input-format stream-json` 启动本地 NDJSON harness。
- stdin 接收：
  - `{"type":"user","message":{"content":"hello"}}`
  - `{"type":"control_request","request_id":"1","request":{"subtype":"initialize"}}`
  - `set_model`
  - `set_permission_mode`
  - `interrupt`
  - `get_status`
- stdout 输出：
  - `system/harness_initialized`
  - `assistant`
  - `user`
  - `system`
  - `result`
  - `control_response`
- 当工具权限策略返回 `ask` 时，harness 会输出：

```json
{"type":"control_request","request_id":"...","request":{"subtype":"can_use_tool","tool_name":"bash","input":{"command":"git push"},"tool_use_id":"..."}}
```

宿主需要回写：

```json
{"type":"control_response","response":{"subtype":"success","request_id":"...","response":{"behavior":"allow"}}}
```

或：

```json
{"type":"control_response","response":{"subtype":"success","request_id":"...","response":{"behavior":"deny","message":"not allowed"}}}
```

当前 deliberately 不实现远程 WebSocket。原因是 morty-code 的权限层刚刚补齐，先把本地稳定 JSONL 协议跑通，再接远端传输更稳。

## 后续深挖方向

1. **session replay**：启动 harness 时可选择 replay 当前 transcript。
2. **remote bridge**：在本地 stream-json 之上加 WebSocket transport。
3. **interrupt 真取消**：把 QueryEngine turn 变成可取消任务，而不是当前同步占用。
4. **tool progress event**：工具开始、结束、失败都输出结构化事件，方便 UI 展示。
5. **permission update persistence**：宿主批准后可选择写入 session / local permission rule。
