# Terminagent

[English](README.md) | [中文](README.zh.md)

Terminagent 是 [Terminator](https://github.com/gnome-terminator/terminator)（GPL v2）的 fork，基于其 2.1.5 版本。在完整保留上游全部功能（多标签/多面板、分组、布局、配置文件等）的基础上，新增了面向 AI Agent 的远程控制层（`remotinator` CLI + D-Bus），支持读屏、远程输入、布局编排与 headless 会话。

对上游 Terminator 及其维护者（Chris Jones、Stephen Boddy、Matt Rose）和全部贡献者致谢——本项目的一切能力都建立在他们多年的工作之上。

## 新增能力

**远程控制（remotinator）**
- 终端发现与定位：`list_terminals --json`、`get_terminal_info`、稳定标签 `set_terminal_label`
- 布局编排：`split`（side/ratio/cwd/command/label）、`get_layout`、`resize_pane`、`focus_terminal`
- 远程输入：`send`（提交并原子验证回显）、`feed_terminal`（原始按键）
- 读屏：`get_terminal_text`、`screenshot_terminal`（16:9 PNG）、`scrollshot_terminal`（长截图）
- 状态等待：`wait_idle`（稳定/闲置检测）、`get_window_title`、`get_tab_title`
- 多 agent 协作：`acquire_session` / `release_session` 写租约

**Headless 会话（tmux 后端）**
- `create_session` 创建无 GUI 的持久 PTY 会话
- `list_sessions` / `get_session_text` / `feed_session` / `wait_session`
- `attach_session` 挂回 GUI 标签页、`detach_session` 脱离、`terminate_session` 销毁

**其他**
- auto_theme 自动主题插件（`terminatorlib/plugins/auto_theme.py`）
- 可复现 Debian 打包（`packaging/build-deb.sh`，产物 `dist/*.deb`）
- 终端易用性改进（搜索栏、弹出菜单、窗口行为）

## 使用

```bash
# 安装补丁版
sudo dpkg -i dist/terminator_2.1.5-agent4_all.deb
```

### MCP 自动注册

deb 安装时自动向本机各用户的 coding agent 配置注册**轻量指南 MCP server**
（`/usr/bin/terminagent-mcp-server`，纯标准库实现、无运行时依赖）：
Claude Code（`~/.claude.json`）、Codex（`~/.codex/config.toml`）、
MiMoCode（`~/.config/mimocode/mimocode.jsonc`）。它暴露 4 个"教学"工具，
告诉 agent 如何驱动本终端：

- `get_usage_guide` — 能力、约束、快速工作流
- `get_command_reference` — remotinator 子命令速查
- `get_workflow` — GUI pane / headless / 租约的可靠流程
- `get_constraints` — 必须遵守的操作规则

注册幂等（已存在则跳过）。手动注册：

```bash
# Claude Code
claude mcp add terminagent -- /usr/bin/terminagent-mcp-server
```

### 推荐：登录时自启 Terminagent

Agent 的 GUI 控制依赖补丁版 Terminator 处于运行状态。推荐自启（按用户）：

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/terminator.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Terminagent
Exec=/usr/bin/terminator
X-GNOME-Autostart-enabled=true
EOF
```

或用 systemd 用户服务（`~/.config/systemd/user/terminator.service`）：

```ini
[Unit]
Description=Terminagent (patched Terminator)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/terminator --no-sandbox
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now terminator.service
```

Agent 侧控制示例：

```bash
remotinator list_terminals --json
remotinator set_terminal_label -u <UUID> --label 'work'
remotinator send --label 'work' --text 'ls -la' --submit --verify-echo
remotinator get_terminal_text -u <UUID> -n 200
```

完整协议与工作流见 `AGENT_CONTROL.md`、`REMOTINATOR_USAGE.md`。

## 安装

除上述 deb 外，亦可按上游 INSTALL.md 从源码安装：`python3 setup.py install`。

## 许可与上游

GPL v2（与上游一致）。

- 上游：https://github.com/gnome-terminator/terminator
- 本仓库：https://github.com/hellonestor/Terminagent
