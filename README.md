# Terminagent

[English](README.md) | [中文](README.zh.md)

Terminagent is a fork of [Terminator](https://github.com/gnome-terminator/terminator) (GPL v2), based on its 2.1.5 release. It keeps all upstream features (multi-tab/multi-pane, grouping, layouts, config files, etc.) intact and adds an agent-oriented remote control layer (`remotinator` CLI + D-Bus): screen reading, remote input, layout orchestration, and headless sessions.

Credit goes to the upstream Terminator and its maintainers (Chris Jones, Stephen Boddy, Matt Rose) and all contributors — everything in this project builds on their years of work.

## What's new

**Remote control (remotinator)**
- Discovery & targeting: `list_terminals --json`, `get_terminal_info`, stable labels `set_terminal_label`
- Layout orchestration: `split` (side/ratio/cwd/command/label), `get_layout`, `resize_pane`, `focus_terminal`
- Remote input: `send` (submit with atomic echo verification), `feed_terminal` (raw keys)
- Screen reading: `get_terminal_text`, `screenshot_terminal` (16:9 PNG), `scrollshot_terminal` (tall scrollback PNG)
- Status waiting: `wait_idle` (stability detection), `get_window_title`, `get_tab_title`
- Multi-agent coordination: `acquire_session` / `release_session` write leases

**Headless sessions (tmux backend)**
- `create_session` — persistent PTY session without GUI
- `list_sessions` / `get_session_text` / `feed_session` / `wait_session`
- `attach_session` — attach back to a GUI tab; `detach_session` / `terminate_session`

**Other**
- auto_theme plugin (`terminatorlib/plugins/auto_theme.py`)
- Reproducible Debian packaging (`packaging/build-deb.sh`, artifact `dist/*.deb`)
- Terminal usability improvements (search bar, popup menu, window behavior)

## Usage

```bash
# Install
sudo dpkg -i dist/terminator_2.1.5-agent4_all.deb
```

### MCP auto-registration

The deb automatically registers a lightweight **guide MCP server**
(`/usr/bin/terminagent-mcp-server`, stdlib-only, no runtime deps) into every
local user's Claude Code (`~/.claude.json`), Codex (`~/.codex/config.toml`)
and MiMoCode (`~/.config/mimocode/mimocode.jsonc`) configs. It exposes four
tools that teach the agent how to drive the terminal:

- `get_usage_guide` — capabilities, constraints, quick workflow
- `get_command_reference` — remotinator subcommand cheatsheet
- `get_workflow` — reliable GUI-pane / headless / lease workflows
- `get_constraints` — mandatory operational rules

Registration is idempotent (skips existing entries). To register manually:

```bash
# Claude Code
claude mcp add terminagent -- /usr/bin/terminagent-mcp-server
```

### Recommended: start Terminator on login

Agent GUI control needs the patched Terminator running. Recommended
autostart (per user):

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

Alternatively as a systemd user service (`~/.config/systemd/user/terminator.service`):

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

Agent-side control:

```bash
remotinator list_terminals --json
remotinator set_terminal_label -u <UUID> --label 'work'
remotinator send --label 'work' --text 'ls -la' --submit --verify-echo
remotinator get_terminal_text -u <UUID> -n 200
```

Full protocol and workflows: `AGENT_CONTROL.md`, `REMOTINATOR_USAGE.md`.

## Installing

Besides the deb above, you can install from source per upstream INSTALL.md: `python3 setup.py install`.

## License & upstream

GPL v2 (same as upstream).

- Upstream: https://github.com/gnome-terminator/terminator
- This repository: https://github.com/hellonestor/Terminagent
