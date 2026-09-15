# CodexRouter

让 Codex Desktop 通过原生子代理界面调用 DeepSeek，同时保留现有 Astra 上游。

## 工作方式

```text
Codex Desktop
  └─ 本地 Responses 适配器
       ├─ Astra → 原 AiMaMi 上游
       └─ DS 子代理 → DeepSeek 官方 API
```

父代理调用 `native_delegate_task` 后，适配器将明文任务映射为真正的
`collaboration.spawn_agent`。子代理拥有 Codex 原生任务窗口、文件工具、终端工具和
followup；它不是独立 CLI 进程。

DeepSeek Flash 排队时会持续返回 keep-alive。适配器先请求真实
`deepseek-flash`，超过约 20 秒仍未开始推理时切换到官方
`deepseek-v4-pro`，避免 Codex 将子代理判定为断流。路由日志只记录模型名、
排队时长和时间，不记录任务正文或密钥。

## 环境要求

- Windows 10/11
- Python 3.11+
- Codex Desktop；已验证版本 `0.154.0-alpha.6.2`
- 已运行的 AiMaMi：`http://127.0.0.1:25817/codex/router/v1`
- 用户环境变量 `DEEPSEEK_API_KEY`

项目只使用 Python 标准库。

## 安装

双击 `安装原生DS.cmd`，或在普通 PowerShell 中运行：

```powershell
python native_install.py --activate
```

安装器会备份 Codex 配置、全局规则和已安装 Skill，创建本地鉴权令牌、Windows
计划任务及桌面快捷方式。安装完成后彻底退出 Codex（包括托盘），再从
**Codex（原生 DS）** 启动。

以后明确要求“DS 子代理”时，默认使用 `high`。也可显式指定
`low`、`medium`、`high` 或 `xhigh`；适配器会映射为 DeepSeek 支持的档位。

## 维护

检查服务：

```powershell
python "$env:USERPROFILE/.codex/native-model-adapter/native_launcher.py" status
```

升级已有安装前先完成所有子代理任务并停止对应计划任务，然后运行：

```powershell
python native_repair.py --activate
```

回滚：

```powershell
python "$env:USERPROFILE/.codex/native-model-adapter/native_launcher.py" rollback
```

## 测试

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

离线测试覆盖消息映射、SSE、Flash 排队回退、本地鉴权、凭据隔离、配置安装、
目录生成和启动修复。真实 API 可用性仍由 DeepSeek 服务端决定。

## 安全边界

- API Key 只从用户环境变量读取，不写入仓库或 Codex 子进程环境。
- 本地服务监听 `127.0.0.1`，并校验随机鉴权头。
- 适配器不解密原生密文，只转换自己注入的明文别名调用。
- 配置变化、未知供应商和自定义请求头会停止修复，避免覆盖用户设置。
- 子代理仍运行在同一 Windows 用户下，本项目不提供凭据隔离沙箱。

## 已验证范围

原生 DS 创建、文件读取和写入、终端操作、同一子代理 followup 均已通过真实验证。
运行中消息、取消和并发仍需更多端到端测试。接入依赖 Codex 的原生子代理协议，
Codex 升级后应重新运行测试。
