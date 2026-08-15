#!/usr/bin/env python3
"""Terminagent guide MCP server.

Lightweight MCP server (JSON-RPC over stdio, stdlib only) that tells a coding
agent how to drive the patched Terminator via `remotinator`. It exposes no
functional tools; it only teaches usage. Read the guide docs from the deb
install location, falling back to embedded summaries.

Run:  mcp-server.py   (stdio transport)
"""

import json
import os
import re
import sys

DOC_DIR = "/usr/share/doc/terminator"
AGENTS_MD = os.path.join(DOC_DIR, "AGENTS.md")
USAGE_MD = os.path.join(DOC_DIR, "REMOTINATOR_USAGE.md")
CONTROL_MD = os.path.join(DOC_DIR, "AGENT_CONTROL.md")


def read_doc(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- guide text

GUIDE = {
    "usage": """\
Terminagent 是 Terminator 2.1.5 的补丁版，通过 `remotinator` CLI 供 coding agent
控制终端。能力：读屏(get_terminal_text)、远程输入(send --verify-echo)、终端发现
(list_terminals --json)、布局编排(split/get_layout)、headless 会话(create_session)、
写租约(acquire_session/release_session)。

关键约束：
1. GUI 命令必须与补丁版 Terminator 处于同一 DISPLAY 和 D-Bus session；
   headless 命令不依赖 GUI。
2. 装完 DEB 或升级后，先完全退出旧 Terminator 进程再重启，否则可能连到旧
   D-Bus 服务报 UnknownMethod。
3. 先 `remotinator list_terminals --json` 确认目标，再用唯一 --label 或 -u <UUID>
   操作，不要靠窗口标题猜。
4. send 返回成功 JSON 且 echo_observed=true、enter_sent=true 才算已提交；
   回显失败不会发 Enter，不要盲目重发。
5. 多 agent 写同一 pane 前用 acquire_session --owner <ID> --ttl 600 拿租约，
   用完 release_session。

快速工作流：
  remotinator list_terminals --json
  remotinator set_terminal_label -u <UUID> --label '<LABEL>'
  remotinator send --label '<LABEL>' --text '<CMD>' --submit --verify-echo
  remotinator wait_idle --label '<LABEL>' --stable-ms 2000 --timeout 1800
  remotinator get_terminal_text -u <UUID> -n 200
""",
    "commands": """\
remotinator 主要子命令（* 表示需要 -u/--uuid）：
  list_terminals*  枚举全部 pane 身份 (JSON)
  get_terminal_text*  读取终端文本 (-n 行数, --since-revision 增量)
  send  原子提交：写入→验证回显→发送 Enter (--submit --verify-echo)
  feed_terminal*  发送原始按键 (Esc/方向键/Ctrl-C 等)
  wait_idle  等待终端稳定 (--stable-ms) 或出现文本 (--contains)
  set_terminal_label* / clear_terminal_label*  稳定标签管理
  get_layout  获取窗口/标签/面板布局树
  split*  分屏 (--orientation --side --ratio --cwd --execute --label)
  resize_pane* / focus_terminal*  布局与焦点
  screenshot_terminal* / scrollshot_terminal*  截图 (-f 输出, -w 整窗)
  create_session  创建 headless tmux 会话
  list_sessions / get_session_text / feed_session / wait_session
  attach_session* / detach_session / terminate_session
  acquire_session / release_session  写租约 (多 agent 协作)

查看完整命令：remotinator --help
""",
    "workflow": """\
可靠工作流（GUI pane）：
1. remotinator list_terminals --json    # 获取全部 pane 身份
2. remotinator set_terminal_label -u <UUID> --label '<UNIQUE_LABEL>'  # 稳定标签
3. remotinator send --label '<LABEL>' --text '<TEXT>' --submit --verify-echo
   # 返回 echo_observed=true 且 enter_sent=true 才算提交成功
4. remotinator wait_idle --label '<LABEL>' --stable-ms 2000 --timeout 1800
   # 或用 --contains '<EXPECTED_TEXT>' 等待指定结果
5. remotinator get_terminal_text -u <UUID> -n 200   # 读屏判断结果

多 agent 并发：先 acquire_session --label '<LABEL>' --owner <ID> --ttl 600，
send 携带相同 --owner，完成 release_session --label '<LABEL>' --owner <ID>。

headless 会话（无 GUI）：
  remotinator create_session --headless --label '<LABEL>' --cwd <DIR> --execute '<CMD>'
  remotinator feed_session -i <SESSION_ID> --text '<TEXT>' --submit --verify-echo
  remotinator wait_session -i <SESSION_ID> --stable-ms 2000 --timeout 1800
  remotinator attach_session -i <SESSION_ID>  # 挂回 GUI 观看
  remotinator terminate_session -i <SESSION_ID>
""",
    "constraints": """\
1. GUI 命令必须与补丁版 Terminator 处于同一 DISPLAY 和 D-Bus session；
   headless Session 不依赖 GUI。
2. 遇到 D-Bus UnknownMethod：先完全退出并重启 Terminator，排除旧版进程。
3. 先用 list_terminals --json 确认目标，再设置唯一且稳定的 agent_label；
   不要根据焦点、动态窗口标题或面板顺序猜目标。
4. send --submit --verify-echo 成功 = echo_observed=true + enter_sent=true；
   回显失败时不会发送 Enter，按结构化错误处理，不要盲目重发可能已提交的任务。
5. 以 wait_idle 成功或预期文本出现作为完成依据；超时后先读屏判断。
6. 读操作优先 get_terminal_text；只有颜色/布局/图形需要视觉确认时才截图。
7. 多 agent 可能同时写同一 pane 时，先 acquire_session 获取租约，
   完成后用同一 label/owner 调用 release_session。
""",
}


def get_guide(name):
    """Return the requested guide section, preferring the installed docs."""
    if name == "usage":
        text = read_doc(AGENTS_MD)
        if text:
            return text
    if name == "commands":
        text = read_doc(USAGE_MD)
        if text:
            m = re.search(r"## 命令参考(.*?)(?=\n## |\Z)", text, re.S)
            if m:
                return m.group(1).strip()
    if name == "workflow":
        text = read_doc(CONTROL_MD)
        if text:
            m = re.search(r"## 推荐的可靠工作流(.*?)(?=\n## |\Z)", text, re.S)
            if m:
                return m.group(1).strip()
    return GUIDE.get(name, "未知指南，可用: " + ", ".join(GUIDE))


TOOLS = [
    {
        "name": "get_usage_guide",
        "description": "Terminagent 使用总览：能力、关键约束、快速工作流。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_command_reference",
        "description": "remotinator 全部主要子命令速查。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_workflow",
        "description": "按场景返回可靠操作流程（GUI pane / headless 会话 / 多 agent 租约）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_constraints",
        "description": "操作 Terminagent 时必须遵守的约束清单。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# ------------------------------------------------------------ stdio framing


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    body = sys.stdin.buffer.read(length)
    return json.loads(body)


def write_message(payload):
    body = json.dumps(payload).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def call_tool(name, arguments):
    if name == "get_usage_guide":
        return get_guide("usage")
    if name == "get_command_reference":
        return get_guide("commands")
    if name == "get_workflow":
        return get_guide("workflow")
    if name == "get_constraints":
        return get_guide("constraints")
    raise ValueError(f"unknown tool: {name}")


def main():
    while True:
        msg = read_message()
        if msg is None:
            break
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": msg.get("params", {}).get(
                        "protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "terminagent-guide",
                                   "version": "2.1.5-agent4"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            write_message({
                "jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS},
            })
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            try:
                text = call_tool(name, args)
                content = [{"type": "text", "text": text}]
                result = {"content": content}
            except Exception as exc:  # noqa: BLE001 - protocol boundary
                result = {"isError": True,
                          "content": [{"type": "text",
                                       "text": f"error: {exc}"}]}
            write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "ping":
            write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})


if __name__ == "__main__":
    main()
