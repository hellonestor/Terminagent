# AGENTS.md

本仓库是 **Terminagent**——Terminator 2.1.5 的 fork，为 AI coding agent 增加了远程控制能力。作为 coding agent，你可以通过 `remotinator` CLI 稳定地识别、读写、编排本机运行的 Terminator 终端，以及创建不依赖 GUI 的 headless 会话。

## 你能用它做什么

- **读屏**：`get_terminal_text` 读取终端内容（支持按 revision 增量读取）
- **远程输入**：`send --submit --verify-echo` 原子提交（写入→验证回显→发送 Enter）；`feed_terminal` 发原始按键
- **终端发现**：`list_terminals --json` 枚举全部 pane；`set_terminal_label` 设置不被 OSC 标题覆盖的稳定标签
- **状态等待**：`wait_idle` 等待终端稳定或出现指定文本（`--contains`）
- **布局编排**：`split`（side/ratio/cwd/command/label）、`get_layout`、`resize_pane`、`focus_terminal`
- **Headless 会话**：`create_session` 无 GUI 持久 PTY（tmux 后端），`feed_session`/`get_session_text`/`wait_session` 读写，`attach_session` 挂回 GUI
- **多 agent 协作**：`acquire_session`/`release_session` 写租约，避免并发冲突

## 关键约束

1. GUI 命令必须与补丁版 Terminator 处于同一 `DISPLAY` 和 D-Bus session；headless 命令不依赖 GUI。
2. 装完 DEB 或升级后，先完全退出旧 Terminator 进程再重启，否则可能连到旧 D-Bus 服务报 `UnknownMethod`。
3. 先 `list_terminals --json` 确认目标，再用唯一 `--label` 或 `-u <UUID>` 操作，不要靠窗口标题猜。
4. `send` 返回成功 JSON 且 `echo_observed=true`、`enter_sent=true` 才算已提交；回显失败不会发 Enter，不要盲目重发。
5. 多 agent 写同一 pane 前用 `acquire_session --owner <ID> --ttl 600` 拿租约，用完 `release_session`。

## 快速工作流

```bash
remotinator list_terminals --json
remotinator set_terminal_label -u <UUID> --label '<LABEL>'
remotinator send --label '<LABEL>' --text '<CMD>' --submit --verify-echo
remotinator wait_idle --label '<LABEL>' --stable-ms 2000 --timeout 1800
remotinator get_terminal_text -u <UUID> -n 200
```

## 详细文档

- `REMOTINATOR_USAGE.md` — 任务式使用指南（含 headless 会话、截图、租约全流程）
- `AGENT_CONTROL.md` — 底层接口语义、JSON 字段、错误码

## 安装

```bash
sudo dpkg -i dist/terminator_2.1.5-agent4_all.deb
```
