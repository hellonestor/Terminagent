# remotinator 使用指南

`remotinator` 通过 D-Bus 控制补丁版 Terminator 的 GUI pane，也可以创建
不依赖 GUI 的 tmux-backed headless Session。它适合让 Codex、Claude Code、
Hermes 等 agent 稳定地识别、读写和编排交互式终端。

> [!IMPORTANT]
> GUI 控制只对已加载 agent-control 补丁的 Terminator 进程有效。安装或升级
> DEB 后，必须完全退出旧 Terminator 进程再重新启动。否则新 CLI 可能
> 连到仍在内存中的旧 D-Bus 服务并返回 `UnknownMethod`。

底层接口语义、JSON 字段和错误码见 [agent-control 接口说明](./AGENT_CONTROL.md)。

## 快速开始

先确认已安装的版本和 headless 后端：

```bash
dpkg-query -W -f='${Status} ${Version}\n' terminator
remotinator --version
tmux -V
```

GUI pane 和 `remotinator` 必须在同一桌面会话中，即共享 `DISPLAY` 和
D-Bus session bus。推荐按下列顺序操作。

1. 列出面板，根据 `cwd`、`foreground_process`、`session_title` 和布局信息
   人工确认目标：

   ```bash
   remotinator list_terminals --json
   ```

2. 把确认过的 `terminal_uuid` 记为 shell 变量，并设置唯一稳定标签：

   ```bash
   target_uuid='urn:uuid:12345678-1234-1234-1234-123456789abc'
   target_label='Claude-1 / 主审'
   remotinator set_terminal_label -u "$target_uuid" --label "$target_label"
   ```

   上面的 UUID 只是格式示例，必须替换为 `list_terminals` 的实际输出。
   `agent_label` 不会被 shell 或 TUI 发出的 OSC 标题覆盖。

3. 原子执行“输入→验证回显→发送 Enter”：

   ```bash
   remotinator send --label "$target_label" \
     --text '继续审计这个组件' --submit --verify-echo
   ```

4. 服务端等待屏幕稳定，然后读取结果：

   ```bash
   remotinator wait_idle --label "$target_label" \
     --stable-ms 2000 --timeout 1800
   remotinator get_terminal_text -u "$target_uuid" -n 200
   ```

`send` 返回 `ok=true`、`echo_observed=true` 且 `enter_sent=true` 才表示提交
成功。如果回显验证失败，接口不会发送 Enter。发生超时时应先读屏判断
当前状态，不要盲目重发可能已经提交的任务。

## 识别和读取 GUI pane

### 查询身份与状态

```bash
remotinator list_terminals --json
remotinator get_terminal_title -u "$target_uuid"
remotinator get_terminal_info -u "$target_uuid"
remotinator get_layout --json
```

`list_terminals` 会聚合返回 UUID、稳定标签、动态会话标题、前台进程、
cwd、活动状态、screen revision、窗口/标签页身份和截图状态。不要仅根据
当前焦点、面板顺序或动态标题猜测目标。

标签允许重复，但任何按 label 写入的操作在匹配多个 pane 时都会以
`AMBIGUOUS_LABEL` 拒绝，不会随机选择。发现 `DUPLICATE_LABEL` 警告后应先重命名：

```bash
remotinator set_terminal_label -u "$target_uuid" --label 'Claude-1 / 主审'
remotinator clear_terminal_label -u "$target_uuid"
```

### 读取屏幕和回滚区

```bash
remotinator get_terminal_text -u "$target_uuid"
remotinator get_terminal_text -u "$target_uuid" -n 200
remotinator get_terminal_text -u "$target_uuid" -S
```

长时间跟踪时，使用 `screen_revision` 只读取变化：

```bash
revision=953
remotinator get_terminal_text -u "$target_uuid" --since-revision "$revision"
```

如果 TUI 原地重绘或请求的历史 revision 已被淘汰，返回值会标记
`full_snapshot=true`。此时应把内容当作完整屏幕，不要当作新增文本追加。

## 可靠地控制交互式 TUI

### 提交普通文本

```bash
remotinator send --label "$target_label" \
  --text '请执行测试并总结失败原因' \
  --submit --verify-echo
```

一般任务不要加 `--wait-busy`。很短的命令可能在轮询前已经完成，强制
观察 busy 转换反而会返回 `BUSY_NOT_OBSERVED`。只有业务逻辑必须证明
`idle -> busy` 时才使用：

```bash
remotinator send --label "$target_label" \
  --text '开始长时间任务' --submit --verify-echo --wait-busy 5
```

### 发送原始按键

`feed_terminal` 用于 Esc、方向键、Ctrl-C、Ctrl-D 等不适合文本提交的输入：

```bash
remotinator feed_terminal -u "$target_uuid" -s '\e'
remotinator feed_terminal -u "$target_uuid" -s '\e[A'
remotinator feed_terminal -u "$target_uuid" -s '\x03'
remotinator feed_terminal -u "$target_uuid" -s 'ls -la' --enter
```

| 转义 | 按键 |
| --- | --- |
| `\r` | Enter |
| `\n` | 换行 |
| `\t` | Tab |
| `\e` | Escape |
| `\e[A/B/C/D` | 上/下/右/左 |
| `\x03` / `\x04` / `\x1a` | Ctrl-C / Ctrl-D / Ctrl-Z |
| `\\` | 字面反斜杠 |

`feed_terminal` 只保证字节已写入 PTY，不保证应用已处理。需要提交普通
文本时优先使用 `send --submit --verify-echo`。

### 等待完成

等待屏幕连续稳定一段时间：

```bash
remotinator wait_idle --label "$target_label" \
  --stable-ms 2000 --timeout 1800
```

如果任务有明确结束标记，直接等待该文本更可靠：

```bash
remotinator wait_idle --label "$target_label" \
  --contains 'All todos complete' --timeout 1800
```

`WAIT_TIMEOUT` 不等于任务未启动或必然失败。超时后读取当前屏幕和
`list_terminals` 中的 `activity_state`、`last_activity_at`、`screen_revision`，再决定
继续等待、发送中断键或报告阻塞。

## 防止多 agent 串写

读操作不需要租约。多个 agent 可能同时控制一个 pane 时，在写入前获取
限时租约：

```bash
agent_owner='codex-root'
remotinator acquire_session --label "$target_label" \
  --owner "$agent_owner" --ttl 600

remotinator send --label "$target_label" --owner "$agent_owner" \
  --text '继续执行' --submit --verify-echo

remotinator release_session --label "$target_label" --owner "$agent_owner"
```

持有期间如需延长时间，以相同 owner 再次调用 `acquire_session`。其他 owner
写入时会收到 `SESSION_LEASED`；租约过期后自动失效。

## 创建和编排 pane

统一的 `split` 能在一次操作中设置方向、位置、比例、cwd、命令、
label 和焦点，返回值直接包含 `new_terminal_uuid`：

```bash
remotinator split -u "$target_uuid" \
  --orientation vertical --side right --ratio 0.42 \
  --cwd /home/workstation/Desktop/security \
  --execute 'claude' --label 'Claude-2 / 复核' --focus false
```

`vertical` 是左右布局，只能搭配 `left/right`；`horizontal` 是上下布局，
只能搭配 `top/bottom`。`ratio` 是新 pane 占比，范围为 `0.1..0.9`。

查看真实布局树、调整比例和聚焦目标：

```bash
remotinator get_layout --json
remotinator resize_pane -u "$target_uuid" --ratio 0.60
remotinator focus_terminal -u "$target_uuid" --raise-window
```

旧的 `hsplit`、`vsplit`、`new_tab` 和 `new_window` 仍可用；新工作流优先使用
`split`，避免分屏后重新枚举和猜测新 pane。

## 运行 headless Session

headless Session 不创建 GTK Window、Tab 或 VTE，Terminator 退出后仍可继续运行。
当前后端需要 tmux 3.x。

### 创建并控制 Session

```bash
remotinator create_session --headless \
  --label 'Claude-headless / 审计' \
  --cwd /home/workstation/Desktop/security \
  --execute 'claude'

remotinator list_sessions --json
```

从创建结果或 `list_sessions` 复制实际 `session_id`，然后读写和等待：

```bash
session_id='terminator-agent-example'

remotinator feed_session -i "$session_id" \
  --text '继续分析' --submit --verify-echo
remotinator wait_session -i "$session_id" \
  --stable-ms 2000 --timeout 1800
remotinator get_session_text -i "$session_id" -n 200
```

上面的 `session_id` 是格式示例，必须替换为创建结果中的实际值。如果 label
唯一，headless 的读写和等待命令也可使用 `--label`。

### 按需显示或终止 Session

需要人工观看或截图时，把 Session attach 到新标签页或现有 pane 旁边：

```bash
remotinator attach_session -i "$session_id" \
  --new-tab -u "$target_uuid"

remotinator attach_session -i "$session_id" \
  --split-right -u "$target_uuid" --ratio 0.5
```

attach 只创建 view，不会重启 Session。隐藏所有 view 或终止进程：

```bash
remotinator detach_session -i "$session_id"
remotinator terminate_session -i "$session_id" --signal TERM
```

同一 Session 同时 attach 到多个可见 pane 时，所有 view 共享终端尺寸和输入流。
需要独占操作时，不要保留多个可写 view。

## 截图和长截图

仅在需要确认颜色、布局、TUI 绘制或图形输出时截图。纯文本任务优先
使用 `get_terminal_text`。

```bash
remotinator get_terminal_info -u "$target_uuid"
remotinator screenshot_terminal -u "$target_uuid" -f /tmp/pane.png
remotinator screenshot_terminal -u "$target_uuid" -f /tmp/window.png -w
remotinator screenshot_terminal -u "$target_uuid" -f /tmp/raw.png --no-ratio
remotinator scrollshot_terminal -u "$target_uuid" -f /tmp/long.png -n 1000
```

截取隐藏标签页或最小化窗口时，让 Terminator 激活目标、等待 GTK 渲染并在
完成后恢复原状态：

```bash
remotinator screenshot_terminal -u "$target_uuid" \
  -f /tmp/background-pane.png --activate --restore --wait-frame
```

默认 pane 截图只补黑边到 16:9，不缩放、不裁剪。`--no-ratio` 保留原始比例。
长截图超过像素上限时，减小 `-n`。

## 错误处理

新 agent 接口返回 JSON。成功时 `ok=true`；失败时 `ok=false`、进程退出码
非零，并包含机器可读的 `code`。

| 错误或现象 | 处理 |
| --- | --- |
| D-Bus `UnknownMethod` | 完全退出所有 Terminator 进程，再启动已安装的补丁版。 |
| 无 D-Bus owner / 连接失败 | 确认 GUI 命令与 Terminator 处于同一用户、`DISPLAY` 和 D-Bus session。 |
| `AMBIGUOUS_LABEL` | 从 `matches` 找到各 UUID，为目标设置唯一 label。 |
| `INPUT_NOT_ECHOED` | Enter 未发送。读屏确认当前 TUI 模式和光标位置后再决定是否重试。 |
| `BUSY_NOT_OBSERVED` | 短任务可能已完成。读屏确认；非必要不使用 `--wait-busy`。 |
| `WAIT_TIMEOUT` | 读取屏幕和 activity/revision，不要直接重发。 |
| `SESSION_LEASED` | 等待租约过期，或由当前 owner 释放；不要抢写。 |
| `INVALID_LAYOUT` | 检查 orientation/side 组合和 `0.1..0.9` 的 ratio。 |
| `HEADLESS_BACKEND_ERROR` | 运行 `tmux -V`，并检查 cwd、命令和 tmux Session 状态。 |
| 截图空白或未 realized | 查 `get_terminal_info`；对后台目标使用 `--activate --restore --wait-frame`。 |
| 长截图过大 | 减小 `scrollshot_terminal -n` 的行数。 |

完整错误码和返回字段见 [agent-control 接口说明](./AGENT_CONTROL.md#json-错误退出码与审计)。

## 审计日志

写操作元数据保存在：

```text
${XDG_STATE_HOME:-~/.local/state}/terminator/agent-control.jsonl
```

权限固定为 `0600`。日志包含时间、目标、label/owner、操作类型、Enter 状态、
前后 revision、文本字节数和 SHA-256，不保存输入正文。

## 命令参考

查看当前安装版本支持的所有命令和参数：

```bash
remotinator --help
```

接口语义以本仓库的 [AGENT_CONTROL.md](./AGENT_CONTROL.md) 和安装包随附文档
`/usr/share/doc/terminator/AGENT_CONTROL.md` 为准。
