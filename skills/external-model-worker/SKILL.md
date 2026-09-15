---
name: external-model-worker
description: Route explicit DS or DeepSeek subagent requests through the installed native Codex adapter.
---

# Native DeepSeek subagent

- 用户明确要求 DS/DeepSeek 子代理时，调用 `native_delegate_task`，使用
  `model=deepseek-flash`、唯一 `task_name`，并在明文 `task_text` 中写清上下文、
  授权文件范围和验收标准。
- 默认 `reasoning_effort=high`；用户显式指定 `low`、`medium`、`high` 或
  `xhigh` 时，以用户选择为准。
- 后续任务使用 `native_followup_task`；运行中消息使用 `native_message_task`。
  等待、查看和停止使用原生 collaboration 工具。
- 适配器创建真正的 `collaboration.spawn_agent`，DS 直连官方 API；Astra 继续走
  原有上游。主代理必须独立核验文件和测试结果。
- 明文别名缺失或模型未知时，说明当前 Codex 后端尚未加载接入。彻底退出 Codex，
  再从 **Codex（原生 DS）** 启动；不要改用密文调用或冒充原生子代理。
- 不打印凭据，不把任务正文或文件内容写入路由日志。
