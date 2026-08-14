# 用 remotinator 控制 Terminator（agent 指南）

本分支为 Terminator 增加了面向 agent 的控制接口。接口分为两类：

- GUI pane：由 Terminator/VTE 持有，通过会话 D-Bus 读写、识别、编排和截图；
- headless Session：由 tmux 作为过渡 PTY 后端持有，不创建 GTK Window、Tab 或 VTE，
  Terminator 退出后仍可继续运行，并可按需 attach 到 GUI。

`agent_label` 是稳定身份，不会被 shell、Claude、Codex 等程序发出的 OSC 0/2
标题覆盖；`session_title` 始终保留前台程序给 VTE 的原始动态标题。

## 前提

- GUI 命令必须连接本仓库启动的补丁版 Terminator；先彻底退出系统旧版，再运行
  本仓库的 `./terminator`。
- `remotinator` 与 Terminator 必须处于同一 DISPLAY 和 D-Bus session。
- headless 命令不依赖 Terminator/GTK，但当前过渡实现要求安装 tmux 3.x。
- 指定 GUI pane 的命令使用 `-u/--uuid`；指定 headless Session 使用
  `-i/--session` 或唯一的 `--label`。

## 推荐的可靠工作流

```bash
# 一次获取所有 pane 身份，不再逐个猜 UUID
remotinator list_terminals --json

# 设置不会被 OSC 标题覆盖的稳定标签
remotinator set_terminal_label -u <UUID> --label 'Claude-1 / 主审'

# 原子执行“输入、确认回显、发送 Enter”
remotinator send --label 'Claude-1 / 主审' \
  --text '继续审计这个组件' --submit --verify-echo

# 等待屏幕稳定；也可等待指定文本
remotinator wait_idle --label 'Claude-1 / 主审' \
  --stable-ms 2000 --timeout 1800
remotinator wait_idle -u <UUID> --contains 'All todos complete' --timeout 1800
```

`send --verify-echo` 的顺序固定为：写入文本、确认回显、发送 Enter、观察提交后的
屏幕变化。回显失败时不会发送 Enter，命令返回非零退出码及结构化错误。返回示例：

```json
{
  "ok": true,
  "terminal_uuid": "urn:uuid:...",
  "bytes_written": 27,
  "enter_sent": true,
  "echo_observed": true,
  "input_cleared": true,
  "state_transition": "idle->busy",
  "screen_revision_before": 953,
  "screen_revision_after": 957,
  "sequence_id": 128
}
```

如需强制观察 busy 转换，可增加 `--wait-busy 5`。短命令可能在轮询前已经完成，
这种情况下接口会以 `BUSY_NOT_OBSERVED` 明确失败；一般任务不必使用该选项。

## 身份与聚合信息

```bash
remotinator get_terminal_title -u <UUID>
remotinator get_terminal_info -u <UUID>
remotinator set_terminal_label -u <UUID> --label 'GLM-2 / 内核审计'
remotinator clear_terminal_label -u <UUID>
remotinator list_terminals --json
```

聚合信息包含：

- `terminal_uuid`、`agent_label`、原始 `session_title`；
- 稳定的 `tab_uuid`、`tab_title`、`window_uuid`、`window_title`；
- `shell_pid`、`foreground_pid`、`foreground_process`、`foreground_argv`；
- `cwd`、`last_activity_at`、`activity_state`、`screen_revision`；
- 原有行列、光标、scrollback、窗口/显示器几何和 `screenshot_ready` 字段。

显示标题优先级为：

```text
agent_label > session_title > foreground command
```

允许不同 pane 设置相同 label，但 `list_terminals` 会返回 `DUPLICATE_LABEL`
警告；任何按 label 写入的操作遇到重复 label 都会以 `AMBIGUOUS_LABEL` 拒绝，
绝不会任选一个目标。

## revision 与增量读屏

```bash
remotinator get_terminal_text -u <UUID>              # 当前可见屏幕
remotinator get_terminal_text -u <UUID> -n 200       # 最近 200 行
remotinator get_terminal_text -u <UUID> -S           # 整个保留 scrollback
remotinator get_terminal_text -u <UUID> --since-revision 953
```

VTE 内容变化会递增 `screen_revision`。每个 pane 保留最近 128 个可见屏幕快照；
若旧 revision 仍在且新屏幕是追加内容，返回真正的字符增量。TUI 原地重绘或旧快照
已淘汰时返回当前完整屏幕，并标记 `full_snapshot=true`，避免把重绘误报为追加文本。

当前 VTE 没有稳定的公开 API 判断 alternate screen，因此 `screen_mode` 暂时返回
`unknown`；调用者不应据此推断主/备用屏幕。

## 明确的按键输入

旧接口继续兼容：

```bash
remotinator feed_terminal -u <UUID> -s 'ls -la\r'
remotinator feed_terminal -u <UUID> -s '\x03'
remotinator feed_terminal -u <UUID> -s 'ls -la' --enter
```

支持的转义为 `\r`、`\n`、`\t`、`\e`、`\xHH` 和 `\\`。中文可直接传入。
新代码优先使用 `send --submit --verify-echo`，旧接口只保证字节已写给 PTY。

## pane 租约

```bash
remotinator acquire_session -u <UUID> --owner codex-root --ttl 600
remotinator acquire_session --label 'Claude-1' --owner codex-root --ttl 600
remotinator release_session -u <UUID> --owner codex-root
```

读操作不要求租约。存在有效租约时，`send` 只有携带相同 `--owner` 才能写入；
其他 owner 会得到 `SESSION_LEASED`、当前 owner 和过期时间。租约到期自动失效。

## 原子分屏与布局树

旧的 `hsplit/vsplit/new_tab/new_window` 仍可使用。统一分屏接口可同时指定位置、
比例、cwd、命令、profile、label 和焦点：

```bash
remotinator split -u <TARGET_UUID> \
  --orientation vertical --side right --ratio 0.42 \
  --cwd /home/workstation/Desktop/security \
  --execute 'claude' --label 'Claude-2' --focus false
```

- `vertical` 表示左右布局，side 只能是 `left/right`；
- `horizontal` 表示上下布局，side 只能是 `top/bottom`；
- ratio 是新 pane 所占比例，范围 `0.1..0.9`；
- 返回值原子包含 `new_terminal_uuid`，无需重新枚举猜测新 pane。

```bash
remotinator get_layout --json
remotinator get_layout --window-uuid <WINDOW_UUID> --json
remotinator resize_pane -u <UUID> --ratio 0.60
remotinator focus_terminal -u <UUID> --raise-window
```

`get_layout` 返回真实 Window → Notebook/Tab → HPaned/VPaned → Terminal 树、分割
比例和稳定 tab UUID，不是平铺列表。

## 真正的 headless Session

创建、列出、读写、等待和终止均不创建 GUI：

```bash
remotinator create_session --headless \
  --label 'GLM-2 / Avahi 审计' \
  --cwd /home/workstation/Desktop/security \
  --execute 'claude'

remotinator list_sessions --json
remotinator get_session_text -i <SESSION_ID> -n 200
remotinator feed_session -i <SESSION_ID> \
  --text '继续分析' --submit --verify-echo
remotinator wait_session -i <SESSION_ID> --stable-ms 2000 --timeout 1800
remotinator wait_session -i <SESSION_ID> --contains '完成' --timeout 1800
```

`list_sessions` 在补丁版 GUI 可用时合并 GUI 与 headless Session；没有 GUI/D-Bus
时仍可独立列出 headless Session。

需要人工查看或截图时再 attach：

```bash
remotinator attach_session -i <SESSION_ID> --new-tab -u <REFERENCE_UUID>
remotinator attach_session -i <SESSION_ID> --split-right \
  -u <TARGET_UUID> --ratio 0.5

remotinator detach_session -i <SESSION_ID>
remotinator terminate_session -i <SESSION_ID> --signal TERM
```

attach 只启动 tmux view，不重启 Session；detach 会断开所有 view，但 Session 和
scrollback 继续存在。`terminate_session` 向进程组发送指定信号并确认 tmux Session
已消失。当前 tmux 后端是独立 PTY 的过渡实现，未来可替换为 `terminator-agentd`
而不改变这些 CLI 语义。

限制：一个 tmux Session 同时 attach 到多个可见 pane 时，所有 view 共享同一终端
尺寸和输入流；需要独占操作时应配合 label/owner 约定。

## 截图

```bash
remotinator get_terminal_info -u <UUID>
remotinator screenshot_terminal -u <UUID> -f /tmp/pane.png
remotinator screenshot_terminal -u <UUID> -f /tmp/window.png -w
remotinator screenshot_terminal -u <UUID> -f /tmp/raw.png --no-ratio
remotinator screenshot_terminal -u <UUID> -f /tmp/background-pane.png \
  --activate --restore --wait-frame
remotinator scrollshot_terminal -u <UUID> -f /tmp/long.png -n 1000
```

默认截图只补黑边到 16:9，不缩放、不裁剪；`--no-ratio` 保留原始比例。
普通截图仍要求 GTK 控件已 realized。`--activate` 会选择目标标签、恢复最小化窗口并
等待一帧，`--restore` 在截图后恢复原焦点/最小化状态。

## JSON 错误、退出码与审计

新 agent 接口全部返回 JSON。失败时 `ok=false` 且 remotinator 退出码非零，例如：

```json
{
  "ok": false,
  "code": "AMBIGUOUS_LABEL",
  "message": "label matches multiple terminals",
  "retryable": false,
  "matches": ["urn:uuid:...", "urn:uuid:..."]
}
```

主要错误码包括 `TERMINAL_NOT_FOUND`、`SESSION_NOT_FOUND`、
`AMBIGUOUS_LABEL`、`SESSION_LEASED`、`INPUT_NOT_ECHOED`、
`BUSY_NOT_OBSERVED`、`WAIT_TIMEOUT`、`INVALID_LAYOUT`、
`INVALID_CWD`、`PROCESS_STILL_RUNNING` 和 `HEADLESS_BACKEND_ERROR`。

操作元数据写入：

```text
${XDG_STATE_HOME:-~/.local/state}/terminator/agent-control.jsonl
```

文件权限固定为 `0600`。日志记录时间、目标、label/owner、操作类型、Enter 状态、
前后 revision、文本 UTF-8 字节数和 SHA-256；不保存输入正文，避免泄露口令。
