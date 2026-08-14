# 用 remotinator 远程控制 Terminator(agent 指南)

本仓库的 Terminator 在原版基础上新增了一组 D-Bus 接口,使外部 agent 可以
**读取**、**写入**、**截图**和**查询几何状态**任意已打开的终端面板(pane),
配合原有接口即可完全控制 Terminator——包括控制面板里正在运行的交互式
程序(claude、codex 等 TUI)。

> 前提:必须运行本仓库打过补丁的 Terminator(先彻底退出系统旧版,再启动
> 本仓库的 `./terminator`),并且 remotinator 与 Terminator 在同一桌面
> 会话(相同 DISPLAY、相同 DBus session bus)下执行。

## 命令总览

```bash
remotinator get_terminals                 # 列出所有终端的 UUID(忽略最后一行 None)
remotinator get_focused_terminal          # 当前聚焦终端的 UUID
remotinator get_terminal_text -u <UUID>   # 读取该终端当前可见屏幕内容
remotinator get_terminal_text -u <UUID> -n 200    # 读取缓冲区最后 200 行
remotinator get_terminal_text -u <UUID> -S        # 读取整个回滚缓冲区
remotinator feed_terminal -u <UUID> -s '<文本>'   # 向该终端注入按键
remotinator get_terminal_info -u <UUID>           # 查询尺寸/位置/可截图状态(JSON)
remotinator screenshot_terminal -u <UUID> -f /tmp/shot.png      # 该面板截图(PNG,默认补成16:9)
remotinator screenshot_terminal -u <UUID> -f /tmp/shot.png -w   # 截面板所在的整个窗口(默认补成16:9)
remotinator screenshot_terminal -u <UUID> -f /tmp/raw.png --no-ratio  # 原始比例截图
remotinator scrollshot_terminal -u <UUID> -f /tmp/long.png -n 1000     # 回滚区长截图(最近1000行)
remotinator hsplit -u <UUID>              # 水平分屏(返回新终端 UUID)
remotinator vsplit -u <UUID>              # 垂直分屏(返回新终端 UUID)
remotinator new_tab -u <UUID>             # 新标签页(返回新终端 UUID)
remotinator new_window                    # 新窗口(返回新终端 UUID)
```

## feed_terminal 的按键转义

`-s` 参数支持以下转义序列,可组合出任意控制键:

| 写法 | 含义 |
|------|------|
| `\r` | 回车(Enter)——TUI 程序里提交输入用它,不是 `\n` |
| `\n` | 换行 |
| `\t` | Tab |
| `\e` | Escape(`\e[A`/`\e[B`/`\e[C`/`\e[D` = 上/下/右/左方向键) |
| `\xHH` | 任意字节,如 `\x03` = Ctrl-C,`\x04` = Ctrl-D,`\x1a` = Ctrl-Z |
| `\\` | 字面反斜杠 |

中文等多字节字符可直接写在 `-s` 里,不会被转义破坏。

## 控制 claude / codex 等 TUI 的典型流程

1. 找到目标面板:`get_terminals` 列出 UUID,逐个 `get_terminal_text`
   查看屏幕内容,识别哪个面板在跑 claude/codex。
2. 输入一条指令并提交:

   ```bash
   remotinator feed_terminal -u <UUID> -s '帮我重构这个函数\r'
   ```

3. 轮询读屏,等待对方输出稳定:

   ```bash
   remotinator get_terminal_text -u <UUID>
   ```

   判定"回答完成"的可靠方法(实测有效):claude 在生成回答期间屏幕上会
   显示 `esc to interrupt` 字样,codex 也有类似的忙碌提示。轮询时同时满足
   以下两个条件才视为完成:
   - 屏幕内容不再包含忙碌提示词(如 `esc to interrupt`);
   - 连续两次读屏内容完全相同(防止 spinner 闪烁误判)。

4. 需要确认/取消时发送相应按键,例如回车 `\r`、Esc `\e`、Ctrl-C `\x03`。

## 截图

`screenshot_terminal` 把终端渲染后的画面存为 PNG,成功时输出保存路径
(自动补 `.png` 后缀),失败时输出 `ERROR: ...`。默认规则是输出图片保持
16:9:原始画面不缩放、不裁剪,只在左右或上下补黑边;确实需要原始控件比例时
加 `--no-ratio`。

读文本用 `get_terminal_text`(快、可解析);需要"看见"颜色、TUI 布局或图形
输出时用截图。截图取自 GTK 控件的渲染缓冲,目标面板必须真实显示在屏幕上
(所在标签页处于前台、窗口未最小化),否则可能截到空白或报错;而
`feed_terminal`/`get_terminal_text` 没有这个限制。

截图前可先调用:

```bash
remotinator get_terminal_info -u <UUID>
```

返回 JSON 里重点看这些字段:

- `screenshot_ready`: 推荐的总判断,为 `true` 时窗口/面板已 realized、viewable,
  且完整落在显示器范围内。
- `window_fully_on_monitor` / `terminal_fully_on_monitor`: 窗口/面板是否完整在
  显示器几何范围内。
- `window_fully_in_workarea` / `terminal_fully_in_workarea`: 是否完整在工作区内
  (避开系统面板/dock)。
- `window_rect` / `terminal_rect` / `monitor_geometry` / `monitor_workarea`: 具体
  坐标与尺寸,用于 agent 自己判断是否需要移动或调整窗口。

`scrollshot_terminal` 生成回滚区长截图,默认截最近 2000 行,可用 `-n/--lines`
改行数。长截图不会补成 16:9,因为它的目标是保留连续回滚内容;请求过大时会
返回 `ERROR: Requested scrollshot too large`,此时减小 `-n`。

注意:
- 写入是直接送进程序的标准输入(pty),无需该面板获得焦点。
- `feed_terminal` 成功返回 `OK`,UUID 不存在时返回 `ERROR: ...`。
- 部分 TUI 对粘贴式整段输入的处理与逐键输入不同;如遇问题可把文本与
  `\r` 分成两次 `feed_terminal` 发送。
