---
name: external-model-worker
description: Route explicitly requested external-model subagents through the installed native Codex adapter.
---

# Native external-model subagent

- Use this only when the user explicitly requests an external-model subagent or chooses
  the external review mode. A model mention alone does not authorize delegation.
- Call `native_delegate_task` with the exact model ID exposed by the tool, a unique
  `task_name`, and readable `task_text` containing context, authorized file paths, and
  acceptance criteria. Do not hardcode a provider list.
- DeepSeek defaults to `model=deepseek-flash` and `reasoning_effort=high`; an explicit
  `low`, `medium`, `high`, or `xhigh` choice overrides that default. For other configured
  models, use their configured default effort unless the user chooses one.
- Use `native_followup_task` for later work and `native_message_task` for a running child.
  Use native collaboration lifecycle tools to wait, inspect, or stop it.
- If an alias tool or requested model ID is absent, report that the active backend has not
  loaded the integration. Do not silently substitute a model, CLI process, or transport.
- The primary agent independently verifies returned files, tests, and review findings.
  Never print credentials or persist task text in route logs.
