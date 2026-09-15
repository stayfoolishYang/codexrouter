# CodexRouter

让 Codex Desktop 通过原生子代理界面调用 DeepSeek，同时保留当前主模型上游。

## 工作方式

```text
Codex Desktop
  └─ 本地 Responses 适配器
       ├─ 主模型 → 当前 Codex provider
       └─ DS 子代理 → DeepSeek 官方 API
```

父代理调用 `native_delegate_task` 后，适配器将明文任务映射为真正的
`collaboration.spawn_agent`。子代理拥有 Codex 原生任务窗口、文件工具、终端工具和
followup；它不是独立 CLI 进程。

`native_delegate_task` 可选的模型 ID 从本地 `settings.json` 的 `routes` 动态生成。
以后增加其他 Responses 兼容路由时，审阅规则可以直接选择新模型，不需要在 Skill
里维护固定供应商列表。

DeepSeek Flash 排队时会持续返回 keep-alive。适配器先请求真实
`deepseek-flash`，超过约 20 秒仍未开始推理时切换到官方
`deepseek-v4-pro`，避免 Codex 将子代理判定为断流。路由日志只记录模型名、
排队时长和时间，不记录任务正文或密钥。

## 环境要求

- Windows 10/11
- Python 3.11+
- Codex Desktop；已验证版本 `0.154.0-alpha.6.2`
- 已配置可用的 Codex 主模型 provider（Responses API）
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

桌面的 **CodexRouter 设置** 会打开仅监听 `127.0.0.1` 的临时配置界面。可以在
其中填写 Responses 网关和 API Key、调用 `/models` 获取模型列表、添加或修改模型、
选择主代理模型，并设置审阅模式及外部子代理默认档位。API Key 只写入 Windows
用户环境变量，页面不回显，`settings.json` 只保存变量名。
界面会跟随 Windows 的浅色或深色系统主题，并在窄窗口自动切换为单列布局。

审阅模式支持：

- `Terra High`：默认新建 Terra 高档审阅子代理。
- `主代理审理`：由当前主代理直接审阅。
- `外部模型审阅`：从当前已配置的第三方模型中选择审阅子代理。

保存配置后先完成正在运行的任务，再彻底退出并重启 Codex，以加载新的主模型、
模型目录和子代理工具定义。

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

离线测试覆盖消息映射、SSE、Flash 排队回退、本地鉴权、凭据隔离、配置 UI、
配置安装、目录生成和启动修复。真实 API 可用性仍由模型服务端决定。

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
