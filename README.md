# Next Sentinel

[English](README.en.md)

macOS 状态栏工具，让 Codex 长程任务按"实现、审查、修复、发布"的节奏自动推进，同时降低 token 空转成本。

## 为什么需要它

Codex 长任务自动推进有两个实际问题：

1. **token 空转** — 定时心跳每跑一次都要恢复会话、读上下文、做判断，任务越长成本越高。
2. **推进失准** — 自动化看到的是一段历史输出，没有明确协议时，容易把该审查的任务继续实现，把该修复的任务直接发布。

Next Sentinel 的做法：hooks 做主链路，自动化只做重启后的兜底。日常推进靠 `SessionStart` 和 `Stop` hook 事件驱动，不轮询。

## 功能特性

- macOS 状态栏常驻，显示为 `NEXT`
- 监听 Codex App 启动（`com.openai.codex`），60 秒后自动触发兜底
- 菜单查看 hooks 状态、自动化状态、调度信息、最近动作
- 支持手动启动/停止 NEXT、立即触发兜底、打开日志
- 完整 hook 脚本：`next_session_start.py`、`next_stop_router.py`、`next_ctl.py`
- 提供安装脚本、重启 Codex 脚本、配置示例、测试用例

## 系统要求

- macOS 13+
- Xcode Command Line Tools（`swiftc`、`iconutil`）
- Python 3
- Pillow（图标渲染）

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/ouyanggai/next-sentinel.git
cd next-sentinel
```

### 2. 安装 hook 脚本

```bash
./scripts/install-hooks.sh
```

脚本会复制 `next_ctl.py`、`next_session_start.py`、`next_stop_router.py` 到 `~/.codex/hooks/`。

自定义 Codex 目录：

```bash
CODEX_HOME="$HOME/.codex" ./scripts/install-hooks.sh
```

### 3. 配置 Codex hooks

将 `examples/config.toml.snippet` 合并到 `~/.codex/config.toml`：

```toml
[features]
codex_hooks = true

[[hooks.SessionStart]]
matcher = "startup|resume|clear"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python3 ~/.codex/hooks/next_session_start.py"
timeout = 10
statusMessage = "Loading NEXT protocol"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "python3 ~/.codex/hooks/next_stop_router.py"
timeout = 30
statusMessage = "Routing NEXT"
```

### 4. 配置目标会话

编辑 `~/.codex/hooks/next_router_config.json`：

```json
{
  "target_sessions": ["replace-with-target-session-id"],
  "target_cwds": ["/absolute/path/to/your/project"],
  "max_auto_continuations": 24,
  "skill_root": "~/.codex/skills"
}
```

`target_sessions` 锁定会话，`target_cwds` 锁定项目目录，命中任一条件即启用 NEXT 路由。

### 5. 配置 automation-2

```bash
mkdir -p ~/.codex/automations/automation-2
```

参考 `examples/automation-2.toml` 创建 `~/.codex/automations/automation-2/automation.toml`。

关键点：`status` 默认 `PAUSED`，兜底处理完后自动暂停。

### 6. 构建状态栏 App

```bash
python3 -m pip install pillow
./build.sh
```

启动：

```bash
open "$HOME/Applications/Next Sentinel.app"
```

安装到其他目录：

```bash
NEXT_SENTINEL_INSTALL_DIR="/Applications" ./build.sh
```

## 使用

### 常用命令

```bash
# 查看状态
python3 ~/.codex/hooks/next_ctl.py status

# 启用 NEXT
python3 ~/.codex/hooks/next_ctl.py start

# 暂停 NEXT
python3 ~/.codex/hooks/next_ctl.py stop

# 手动触发兜底
python3 ~/.codex/hooks/next_ctl.py trigger
```

### NEXT 协议

每轮任务结束时，Agent 写一个标识：

```text
NEXT: 继续    → 发送纯文字"继续"
NEXT: 实现    → 发送 incremental-implementation + test-driven-development 技能
NEXT: 修复    → 发送 incremental-implementation + test-driven-development 技能
NEXT: 审查    → 发送 code-review-and-quality 技能
NEXT: 发布    → 发送 shipping-and-launch 技能
NEXT: 停止    → 不再发送
```

技能来自 [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)，Next Sentinel 将它们接入 Codex 会话生命周期，按 `NEXT:` 协议自动衔接。

## 工作原理

```text
Codex 会话启动/恢复
      ↓
SessionStart hook 注入 NEXT 协议
      ↓
Agent 执行任务，末尾写 NEXT 标识
      ↓
Stop hook 读取最后一个明确 NEXT 标识
      ↓
按标识发送继续文字或技能引用
      ↓
下一轮按对应技能继续执行
```

重启兜底链路：

```text
Cockpit Tools 切号（可选）
      ↓
重启 Codex App
      ↓
Next Sentinel 监听到启动
      ↓
等待 60 秒
      ↓
next_ctl.py trigger 单次调度 automation-2
      ↓
automation-2 恢复目标会话，按 NEXT 标识分发
      ↓
automation-2 回到 PAUSED
```

详见 [docs/usage-associations.md](docs/usage-associations.md)。

## 目录结构

```text
next-sentinel/
├── Assets/           # 状态栏和 App 图标素材
├── Sources/          # AppKit 状态栏应用
├── hooks/            # Codex hook 和控制脚本
├── scripts/          # 安装 hooks、重启 Codex 的辅助脚本
├── examples/         # config、automation、cockpit 状态示例
├── docs/             # 使用关联和流程说明
├── test/             # hook 路由与 automation 单次触发测试
├── build.sh
└── README.md
```

构建产物生成到 `build/` 和 `dist/`，不纳入 Git。

## 状态说明

`next_ctl.py status` 输出示例：

```text
NEXT hooks: ACTIVE
codex_hooks feature: True
SessionStart hook: True
Stop hook: True
automation-2 toml: PAUSED
automation-2 db: PAUSED
```

- `NEXT hooks: ACTIVE` — NEXT 路由启用
- `NEXT hooks: STOPPED` — 通过 `NEXT_ROUTER_DISABLED` 暂停
- `automation-2 db: PAUSED` — 兜底自动化空闲
- `automation-2 db: ACTIVE` — 正在等待单次触发执行

## 测试

```bash
python3 -m unittest discover -s test -p 'test_*.py'
```

覆盖场景：
- `NEXT:` 标识后有其他上下文时仍正确路由
- 多个 `NEXT:` 时使用最后一个明确标识
- 协议说明行不被误判
- `实现`/`修复` 发送两个技能引用
- `automation-2` 单次触发后回到 `PAUSED`

## Cockpit 自动切号

Cockpit Tools 是外部工具，本仓库不调用未公开 API，不保存真实认证内容。

示例状态文件：`examples/cockpit_codex_auth.example.json`

切号后重启 Codex：

```bash
./scripts/restart-codex.sh
```

## 安全说明

- 不提交 `~/.codex/.cockpit_codex_auth.json`
- 不提交 Codex 的 `auth.json`、sqlite 数据库、真实 session 状态和日志
- `examples/` 里的 session、cwd、账号字段都是占位示例
- hook 脚本支持 `CODEX_HOME`、`NEXT_HOOKS_DIR`、`NEXT_SKILL_ROOT` 环境变量覆盖

## 致谢

TDD、增量实现、审查和发布阶段参考了 [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) 的 skills 设计。本地技能生态兼容宝玉大神 Jim Liu 的 [baoyu-skills](https://github.com/jimliu/baoyu-skills)。

## License

MIT
