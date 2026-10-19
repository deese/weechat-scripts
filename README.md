# weechat-scripts

Personal scripts for [WeeChat](https://weechat.org/).

| Script | Description |
|--------|-------------|
| [`hlmon.py`](scripts/hlmon.py) | Highlight monitor with a line selector and log context viewer |

## hlmon

Collects your highlights (and, optionally, private messages) in a dedicated
`hlmon` buffer, in the style of `highmon.pl`. Select an entry with the arrow
keys and press Enter to open the matching line of the channel's log file, with
some surrounding context, in a new buffer. This lets you see what was being
said around a highlight without leaving WeeChat or scrolling back.

### Requirements

- WeeChat with the Python plugin
- The `logger` plugin enabled (the context viewer reads the log files)
- Optional: `zstandard` (Python module) to read `.zst` rotated logs on Python < 3.14.
  Plain and `.gz` logs work out of the box.

Both the new (4.0+) and the old key names are supported.

### Installation

```
cp scripts/hlmon.py ~/.local/share/weechat/python/
ln -s ../hlmon.py ~/.local/share/weechat/python/autoload/hlmon.py   # optional: autoload
```

Then, inside WeeChat:

```
/script load hlmon.py
```

(Use `~/.weechat/python/` if your WeeChat still uses the legacy home directory.)

### Usage

Open the monitor with `/hlmon`. New highlights are appended as they arrive.

Keys in the `hlmon` buffer:

| Key | Action |
|-----|--------|
| `up` / `down` | Move the selection |
| `pgup` / `pgdn` | Move the selection one page |
| `home` / `end` | First / last entry |
| `enter` | Show the log context of the selected entry |

Keys in the context buffer:

| Key | Action |
|-----|--------|
| `+` / `=` | Show more lines on each side |
| `-` | Show fewer lines on each side |

Type `q` (or `close`) in the input of either buffer to close it.

### Commands

| Command | Description |
|---------|-------------|
| `/hlmon [open]` | Open the monitor buffer |
| `/hlmon up\|down\|pgup\|pgdn\|first\|last` | Move the selection |
| `/hlmon enter [<before> [<after>]]` | Show the context, optionally overriding its size |
| `/hlmon del` | Delete the selected entry |
| `/hlmon clear` | Delete all entries |
| `/hlmon more` / `/hlmon less` | In the context buffer, add/remove lines on each side |
| `/hlmon debug` | Explain why the selected entry is (not) found in its log |
| `/hlmon trace` | Toggle printing every command WeeChat runs (diagnostics) |
| `/hlmon add <YYYY-MM-DD> <HH:MM[:SS]> <nick> <message>` | Add a fake highlight in the current buffer, for testing |

### Configuration

Options live under `plugins.var.python.hlmon.*` and can be changed with
`/set` (for example `/set plugins.var.python.hlmon.context_before 20`).

| Option | Default | Description |
|--------|---------|-------------|
| `context_before` | `10` | Log lines shown before the highlighted line |
| `context_after` | `10` | Log lines shown after the highlighted line |
| `context_step` | `10` | Lines added/removed on each side with `+` / `-` |
| `context_reuse_buffer` | `on` | `on`: reuse one context buffer; `off`: open a new one per lookup |
| `wrap_nicks` | `on` | Show nicks between `weechat.look.nick_prefix` / `nick_suffix` |
| `hide_joins` | `off` | Hide joins/parts/quits in the context buffer |
| `flush_before_search` | `on` | Run `/logger flush` before searching the log |
| `max_entries` | `500` | Maximum number of entries kept in the monitor |
| `privates` | `on` | Also monitor private messages |
| `max_time_diff` | `300` | Max seconds between highlight and log line to accept a match (`0` = no limit) |
| `time_format` | `%d/%m %H:%M:%S` | Time format of monitor entries (`strftime`) |
| `color_time` | `darkgray` | Color for the time |
| `color_buffer` | `cyan` | Color for the buffer name |
| `color_selected` | `white,blue` | Color for the selected line |
| `mark_highlights` | `on` | Mark your other highlights in the context buffer |
| `highlight_scope` | `nick` | What to mark: `nick` (who mentions you), `word` (your nick in the message) or `line` |
| `color_highlight` | `*yellow` | Color for other highlights in the context buffer |
| `color_match` | `yellow,red` | Color for the matching line in the context buffer |

### How it works

Highlights are captured with a print hook on IRC channel and private buffers
(your own messages are ignored). On Enter, hlmon asks the `logger` plugin for the
buffer's log file and looks for the line that matches the highlight by time,
nick and text. Rotated logs (`.1`, `.2.gz`, `.3.zst`, ...) are searched too, and
ANSI color codes in logs are stripped. If a match is not found, `/hlmon debug`
shows why.

## License

GPL-3.0
