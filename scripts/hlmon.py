# -*- coding: utf-8 -*-
#
# hlmon.py - Highlight monitor with a line selector (highmon.pl style)
#
# Collects highlights (and optionally private messages) in a "hlmon" buffer.
# Move the selection with the arrow keys and press Enter to open the matching
# line of the log file, with some context around it, in a new buffer.
#
# Keys in the hlmon buffer:
#   up / down        move selection
#   pgup / pgdn      move selection one page
#   home / end       first / last entry
#   enter            show log context for the selected entry
#
# Commands:
#   /hlmon                     open the monitor buffer
#   /hlmon up|down|pgup|pgdn|first|last
#   /hlmon enter [before [after]]   show context (optionally overriding size)
#   /hlmon del                 delete selected entry
#   /hlmon clear               delete all entries
#
# Options (/set plugins.var.python.hlmon.*): see DEFAULTS below.
#
# In a context buffer, type "q" in the input to close it.

import calendar
import datetime
import gzip
import io
import os
import re
import sys
import time

try:
    import weechat
    IMPORT_OK = True
except ImportError:
    print("This script must be run under WeeChat.")
    IMPORT_OK = False

SCRIPT_NAME = "hlmon"
SCRIPT_AUTHOR = "deese"
SCRIPT_VERSION = "0.15"
SCRIPT_LICENSE = "GPL3"
SCRIPT_DESC = "Highlight monitor with line selector and log context viewer"

# "log1" makes weechat-android treat our lines as messages from a person; without
# it (or irc_privmsg/irc_action) it shows them dimmed, as "non-human" lines
LINE_TAGS = "log1,no_highlight,notify_none,no_log"

MON_NAME = "hlmon"
CTX_NAME = "hlmon.context"

DEFAULTS = {
    "context_before": ("10", "log lines shown before the highlighted line"),
    "context_after": ("10", "log lines shown after the highlighted line"),
    "context_step": ("10", "lines added/removed on each side with + / - in the context buffer"),
    "context_reuse_buffer": ("on", "on: reuse one context buffer; off: open a new buffer for each lookup"),
    "wrap_nicks": ("on", "show nicks between weechat.look.nick_prefix/nick_suffix (e.g. <nick>) in the monitor and context buffers"),
    "hide_joins": ("off", "hide joins/parts/quits in the context buffer"),
    "flush_before_search": ("on", "run /logger flush before searching the log"),
    "max_entries": ("500", "maximum number of entries kept in the monitor"),
    "privates": ("on", "also monitor private messages"),
    "max_time_diff": ("300", "max seconds between highlight and log line to accept a match (0 = no limit)"),
    "time_format": ("%d/%m %H:%M:%S", "time format of entries in the monitor (strftime)"),
    "color_text": ("chat", "base text color, sent as an explicit color code like WeeChat's own messages (clients such as weechat-android may show text without one in dark gray)"),
    "color_time": ("darkgray", "color for the time"),
    "color_buffer": ("cyan", "color for the buffer name"),
    "color_selected": ("white,blue", "color for the selected line"),
    "mark_highlights": ("on", "mark other highlights of yours found in the context buffer"),
    "highlight_scope": ("nick", "what to mark in other highlights: nick (who mentions you), word (your nick in the message) or line"),
    "color_highlight": ("*yellow", "color for other highlights in the context buffer"),
    "color_match": ("yellow,red", "color for the matching line in the context buffer"),
}

entries = []
entry_counter = 0
ctx_state = {}  # context buffer pointer -> {"entry", "before", "after"}
trace_hook = ""
selected = -1
mon_buffer = ""
ctx_counter = 0

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PREFIX_VAR_RE = re.compile(r"\$\{[^}]*\}")
JOIN_RE = re.compile(r"^\S+ \([^()\s]*@[^()\s]*\) ")


def opt(name):
    return weechat.config_get_plugin(name)


def base_color():
    """Reset, then the base text color (an explicit code, as WeeChat uses for messages)."""
    return weechat.color("reset") + weechat.color(opt("color_text"))


def opt_int(name):
    try:
        return max(0, int(opt(name)))
    except ValueError:
        return int(DEFAULTS[name][0])


def key_names():
    """Key names depend on WeeChat version (new key names since 4.0)."""
    version = int(weechat.info_get("version_number", "") or 0)
    if version >= 0x04000000:
        return {
            "up": "up", "down": "down", "pgup": "pgup", "pgdn": "pgdn",
            "first": "home", "last": "end", "return": "return",
        }
    return {
        "up": "meta2-A", "down": "meta2-B", "pgup": "meta2-5~", "pgdn": "meta2-6~",
        "first": "meta2-1~", "last": "meta2-4~", "return": "ctrl-M",
    }


# ---------------------------------------------------------------- monitor buffer

def mon_open(switch=False):
    global mon_buffer
    if not mon_buffer:
        mon_buffer = weechat.buffer_new(MON_NAME, "mon_input_cb", "", "mon_close_cb", "")
        if not mon_buffer:
            return ""
        weechat.buffer_set(mon_buffer, "type", "free")
        weechat.buffer_set(mon_buffer, "title",
                           "Highlight monitor | up/down+enter, or type the entry number (. = last): log context | /help hlmon")
        weechat.buffer_set(mon_buffer, "localvar_set_hlmon", "monitor")
        weechat.buffer_set(mon_buffer, "localvar_set_no_log", "1")
        for action, key in key_names().items():
            weechat.buffer_set(mon_buffer, "key_bind_" + key, "/hlmon " + action)
        redraw()
    if switch:
        weechat.buffer_set(mon_buffer, "display", "1")
        ensure_visible()
    return mon_buffer


def mon_close_cb(data, buffer):
    global mon_buffer
    mon_buffer = ""
    return weechat.WEECHAT_RC_OK


def mon_input_cb(data, buffer, input_data):
    cmd = input_data.strip().lower()
    numbers = cmd.split()
    if numbers and numbers[0] == "." and entries:  # "." = last entry
        numbers[0] = str(entries[-1]["id"])
    if 1 <= len(numbers) <= 3 and all(n.isdigit() for n in numbers):
        open_by_id(*[int(n) for n in numbers])  # "12", "12 30" or "12 5 20"
    elif cmd in ("q", "close"):
        weechat.buffer_close(buffer)
    elif cmd == "clear":
        hlmon_cmd_cb("", buffer, "clear")
    elif cmd == "del":
        hlmon_cmd_cb("", buffer, "del")
    return weechat.WEECHAT_RC_OK


def nick_chars():
    """Characters around nicks: weechat.look.nick_prefix / nick_suffix."""
    if opt("wrap_nicks") != "on":
        return "", ""

    def get(name):
        return weechat.config_string(weechat.config_get("weechat.look." + name))
    return get("nick_prefix"), get("nick_suffix")


def wrap_nick(nick, chars, colored=False, nick_color=""):
    pre, suf = chars
    if not (pre or suf) or (pre and nick.startswith(pre)):  # none, or logger already adds them
        if not colored:
            return nick
        return (nick_color or base_color()) + nick + base_color()
    if not colored:
        return pre + nick + suf
    return "".join((weechat.color("chat_nick_prefix"), pre, nick_color or base_color(),
                    nick, weechat.color("chat_nick_suffix"), suf, base_color()))


def format_entry(index, entry, width, chars, idw):
    tm = time.strftime(opt("time_format"), time.localtime(entry["date"]))
    label = ("[%d]" % entry["id"]).rjust(idw + 2)
    name = entry["short"][:width].ljust(width)
    if index == selected:
        text = "%s %s %s %s %s" % (label, tm, name, wrap_nick(entry["nick"], chars), entry["plain"])
        return "%s%s" % (weechat.color(opt("color_selected")), text)
    nick_color = weechat.color(weechat.info_get("nick_color_name", entry["nick"]) or "default")
    return "%s%s %s %s%s %s%s %s" % (
        weechat.color(opt("color_time")), label, tm,
        weechat.color(opt("color_buffer")), name,
        wrap_nick(entry["nick"], chars, True, nick_color), base_color(), entry["message"])


def redraw():
    if not mon_buffer:
        return
    weechat.buffer_clear(mon_buffer)
    if not entries:
        weechat.prnt_y_date_tags(mon_buffer, 0, 0, LINE_TAGS,
                                 "%sNo highlights yet" % weechat.color(opt("color_time")))
        return
    width = min(20, max(len(e["short"]) for e in entries))
    chars = nick_chars()
    idw = len(str(entries[-1]["id"]))
    for i, entry in enumerate(entries):
        weechat.prnt_y_date_tags(mon_buffer, i, 0, LINE_TAGS,
                                 format_entry(i, entry, width, chars, idw))
    ensure_visible()


def ensure_visible():
    """Scroll the window showing the monitor so the selected line is visible."""
    if not mon_buffer or selected < 0:
        return
    win = weechat.window_search_with_buffer(mon_buffer)
    if not win:
        return
    infolist = weechat.infolist_get("window", win, "")
    if not infolist:
        return
    start = height = 0
    if weechat.infolist_next(infolist):
        start = weechat.infolist_integer(infolist, "start_line_y")
        height = weechat.infolist_integer(infolist, "chat_height")
    weechat.infolist_free(infolist)
    if height <= 0:
        return
    if selected < start:
        delta = selected - start
    elif selected >= start + height:
        delta = selected - start - height + 1
    else:
        return
    number = weechat.window_get_integer(win, "number")
    weechat.command(mon_buffer, "/window scroll -window %d %+d" % (number, delta))


def page_size():
    win = weechat.window_search_with_buffer(mon_buffer) if mon_buffer else ""
    if win:
        return max(1, weechat.window_get_integer(win, "win_chat_height") - 1)
    return 10


def open_by_id(entry_id, before=None, after=None):
    """Select the entry numbered entry_id and show its log context."""
    for index, entry in enumerate(entries):
        if entry["id"] == entry_id:
            select(index)
            show_context(before, before if after is None else after)
            return
    message = "%s: no entry [%d]" % (SCRIPT_NAME, entry_id)
    weechat.prnt("", weechat.prefix("error") + message)
    if mon_buffer:
        weechat.buffer_set(mon_buffer, "title", message)


def select(index):
    global selected
    if not entries:
        selected = -1
        return
    selected = max(0, min(len(entries) - 1, index))
    redraw()


# ---------------------------------------------------------------- collecting

def log_path_for(buffer):
    """Ask the logger plugin which file it uses for this buffer."""
    path = ""
    infolist = weechat.infolist_get("logger_buffer", "", "")
    if infolist:
        while weechat.infolist_next(infolist):
            if weechat.infolist_pointer(infolist, "buffer") == buffer:
                path = weechat.infolist_string(infolist, "log_filename")
                break
        weechat.infolist_free(infolist)
    return path


def print_cb(data, buffer, date, tags, displayed, highlight, prefix, message):
    if weechat.buffer_get_string(buffer, "localvar_hlmon"):
        return weechat.WEECHAT_RC_OK
    # only IRC channels and private buffers (not core, server or script buffers)
    if weechat.buffer_get_string(buffer, "localvar_type") not in ("channel", "private"):
        return weechat.WEECHAT_RC_OK
    tag_list = tags.split(",")
    if "self_msg" in tag_list:
        return weechat.WEECHAT_RC_OK
    is_private = "notify_private" in tag_list and opt("privates") == "on"
    if not (int(highlight) or is_private):
        return weechat.WEECHAT_RC_OK

    nick = ""
    for tag in tag_list:
        if tag.startswith("nick_"):
            nick = tag[5:]
            break
    if not nick:
        nick = weechat.string_remove_color(prefix, "").strip()

    add_entry(buffer, int(date), nick, message)
    return weechat.WEECHAT_RC_OK


def add_entry(buffer, date, nick, message, select_it=False):
    global selected, entry_counter
    entry_counter += 1
    short = weechat.buffer_get_string(buffer, "short_name") or weechat.buffer_get_string(buffer, "name")
    entry = {
        "id": entry_counter,
        "date": date,
        "buffer": buffer,
        "full_name": weechat.buffer_get_string(buffer, "full_name"),
        "short": short,
        "nick": nick,
        "message": message,
        "plain": weechat.string_remove_color(message, ""),
        "log": log_path_for(buffer),
        "own_nick": weechat.buffer_get_string(buffer, "localvar_nick"),
        "hl_words": ",".join(w for w in (
            weechat.buffer_get_string(buffer, "localvar_nick"),
            weechat.config_string(weechat.config_get("weechat.look.highlight")),
            weechat.buffer_get_string(buffer, "highlight_words")) if w),
    }
    follow = selected == len(entries) - 1
    entries.append(entry)
    max_entries = opt_int("max_entries")
    if max_entries and len(entries) > max_entries:
        drop = len(entries) - max_entries
        del entries[:drop]
        selected = max(0, selected - drop)
    if follow or select_it or selected < 0:
        selected = len(entries) - 1
    mon_open()
    redraw()


# ---------------------------------------------------------------- log context

def log_time_format():
    """Logger time format converted for strptime, and whether it is UTC.

    WeeChat extensions: "%@" (UTC), "%.1".."%.6" and "%f" (fraction of second).
    """
    fmt = weechat.config_string(weechat.config_get("logger.file.time_format"))
    utc = "%@" in fmt
    fmt = re.sub(r"%\.[1-6]", "%f", fmt.replace("%@", ""))
    return fmt, utc


def parse_ts(text, tfmt):
    """Convert a log timestamp to epoch seconds, None if not possible."""
    fmt, utc = tfmt
    dt = None
    try:
        dt = datetime.datetime.strptime(text, fmt)
    except ValueError:
        match = TS_RE.match(text)
        if match:
            try:
                dt = datetime.datetime(*(int(x) for x in match.groups()))
            except ValueError:
                pass
    if dt is None or dt.year < 1971:  # no usable date in the format
        return None
    try:
        if dt.tzinfo:
            return int(dt.timestamp())
        if utc:
            return calendar.timegm(dt.timetuple())
        return int(time.mktime(dt.timetuple()))
    except (OverflowError, ValueError):
        return None


def zst_open(path):
    try:
        from compression import zstd  # Python >= 3.14
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    except ImportError:
        import zstandard  # raises ImportError if not installed
        reader = zstandard.ZstdDecompressor().stream_reader(open(path, "rb"))
        return io.TextIOWrapper(reader, encoding="utf-8", errors="replace")


def read_log(path):
    """Read a log file (plain, .gz or .zst) without color codes; None on error."""
    try:
        if path.endswith(".gz"):
            f = gzip.open(path, "rt", encoding="utf-8", errors="replace")
        elif path.endswith(".zst"):
            f = zst_open(path)
        else:
            f = open(path, "r", encoding="utf-8", errors="replace")
        with f:
            data = f.read()
    except (IOError, OSError, EOFError, ImportError):
        return None
    if "\x1b" in data:  # logger.file.color_lines = on
        data = ANSI_RE.sub("", data)
    return data.splitlines()


def log_files(path):
    """Current log file followed by its rotated files (newest to oldest)."""
    files = [path]
    for num in range(1, 1000):
        for name in ("%s.%d" % (path, num), "%s.%d.gz" % (path, num), "%s.%d.zst" % (path, num)):
            if os.path.exists(name):
                files.append(name)
                break
        else:
            break
    return files


def match_key(line, entry, tfmt):
    """Sort key of a candidate line (lower is better) and its time difference.

    Order: close in time, same nick, exact text, smallest time difference.
    Returns (None, None) if the line does not contain the message.
    """
    parts = line.split("\t", 2)
    target = entry["plain"].strip()
    if len(parts) < 3 or target not in parts[2]:
        return None, None
    ts = parse_ts(parts[0], tfmt)
    diff = abs(ts - entry["date"]) if ts is not None else None
    nick = parts[1].strip().lstrip("~&@%+!")
    key = (diff is not None and diff > 60,
           nick != entry["nick"] and entry["nick"] not in parts[2].split(None, 1)[:1],
           parts[2].strip() != target,
           diff or 0)
    return key, diff


def find_line(lines, entry, tfmt):
    """Index of the line matching entry and its time difference (None if unknown)."""
    best, best_key, best_diff = -1, None, None
    for i, line in enumerate(lines):
        key, diff = match_key(line, entry, tfmt)
        if key is not None and (best_key is None or key <= best_key):
            best, best_key, best_diff = i, key, diff
    return best, best_diff


def decode_line(raw):
    line = raw.decode("utf-8", "replace").rstrip("\r\n")
    return ANSI_RE.sub("", line) if "\x1b" in line else line


def lines_before(f, offset, count):
    """Up to count lines ending just before byte offset (a line start)."""
    if count <= 0:
        return []
    chunk = 256 * (count + 1)
    while True:
        start = max(0, offset - chunk)
        f.seek(start)
        raws = f.read(offset - start).split(b"\n")
        if start > 0:
            raws = raws[1:]  # partial line
        if raws and raws[-1] == b"":
            raws.pop()
        if len(raws) >= count or start == 0:
            return [decode_line(r) for r in raws[-count:]]
        chunk *= 2


def lines_after(f, offset, count):
    """Up to count lines starting at byte offset (a line start)."""
    f.seek(offset)
    lines = []
    while len(lines) < count:
        raw = f.readline()
        if not raw:
            break
        lines.append(decode_line(raw))
    return lines


def dated_line_at(f, offset, tfmt):
    """Timestamp of the first dated line starting at or after byte offset."""
    f.seek(offset)
    if offset:
        f.readline()  # partial line
    for _ in range(50):
        raw = f.readline()
        if not raw:
            return None
        ts = parse_ts(decode_line(raw).split("\t", 1)[0], tfmt)
        if ts is not None:
            return ts
    return None


def search_plain(path, entry, tfmt, window, margin):
    """Search an uncompressed log without reading all of it.

    The log is sorted by time: bisect to (highlight - window) and scan until
    (highlight + window). Returns (lines, index, line number, diff), None if
    not found or False if timestamps can't be parsed (caller reads it all).
    """
    try:
        f = open(path, "rb")
    except (IOError, OSError):
        return None
    with f:
        if dated_line_at(f, 0, tfmt) is None:
            return False
        f.seek(0, os.SEEK_END)
        lo, hi = 0, f.tell()
        while hi - lo > 65536:
            mid = (lo + hi) // 2
            ts = dated_line_at(f, mid, tfmt)
            if ts is not None and ts < entry["date"] - window:
                lo = mid
            else:
                hi = mid
        f.seek(lo)
        if lo:
            f.readline()
        best, best_key, best_diff = -1, None, None
        while True:
            pos = f.tell()
            raw = f.readline()
            if not raw:
                break
            line = decode_line(raw)
            ts = parse_ts(line.split("\t", 1)[0], tfmt)
            if ts is not None and ts > entry["date"] + window:
                break
            key, diff = match_key(line, entry, tfmt)
            if key is not None and (best_key is None or key <= best_key):
                best, best_key, best_diff = pos, key, diff
        if best < 0:
            return None
        before = lines_before(f, best, margin)
        lines = before + lines_after(f, best, margin + 1)
    return lines, len(before), None, best_diff


def neighbor_lines(path, margin, tail):
    """Last (tail=True) or first margin lines of a log file."""
    if path.endswith((".gz", ".zst")):
        lines = read_log(path) or []
        return lines[-margin:] if tail else lines[:margin]
    try:
        with open(path, "rb") as f:
            if tail:
                f.seek(0, os.SEEK_END)
                return lines_before(f, f.tell(), margin)
            return lines_after(f, 0, margin)
    except (IOError, OSError):
        return []


def search_file(path, entry, tfmt, margin):
    """Returns (lines, index, line number or None, diff) or None."""
    window = opt_int("max_time_diff")
    if window and not path.endswith((".gz", ".zst")):
        result = search_plain(path, entry, tfmt, window, margin)
        if result is not False:
            return result
    lines = read_log(path)
    if lines is None:
        return None
    index, diff = find_line(lines, entry, tfmt)
    if index < 0:
        return None
    return lines, index, index + 1, diff


def find_in_log(entry, margin):
    """Find entry in its log (or rotated logs).

    Returns (file, lines, index, line number in file or None, error); lines
    from the neighbor files are added when the match is within margin lines
    of an edge.
    """
    path = entry["log"]
    if not path:
        buf = weechat.buffer_search("==", entry["full_name"])
        path = log_path_for(buf) if buf else ""
        entry["log"] = path
    if not path:
        return None, [], -1, -1, "no log file known for %s (is logger enabled?)" % entry["full_name"]

    # make sure the line is written to disk (logger delays writes)
    if opt("flush_before_search") == "on":
        # in the core buffer, and not through /mute: an alias named mute
        # would turn it into a message sent to the current buffer
        weechat.command(weechat.buffer_search_main(), "/logger flush")
    tfmt = log_time_format()
    files = log_files(path)
    found = None  # (file index, lines, line index, line number, time diff)
    for num, name in enumerate(files):
        result = search_file(name, entry, tfmt, margin)
        if result is None:
            continue
        lines, index, line_no, diff = result
        if found is None or (diff is not None and found[4] is not None and diff < found[4]):
            found = (num, lines, index, line_no, diff)
        if diff is None or diff <= 60:  # good enough, don't read older files
            break
    if found is None:
        return path, [], -1, -1, "line not found in %s (%d file(s) searched)" % (path, len(files))

    num, lines, index, line_no, diff = found
    max_diff = opt_int("max_time_diff")
    if max_diff and diff is not None and diff > max_diff:
        return path, [], -1, -1, (
            "line not found in %s: closest match is %s away from the highlight "
            "(not written to disk yet? check logger.file.flush_delay)"
            % (files[num], datetime.timedelta(seconds=diff)))
    if index < margin and num + 1 < len(files):
        older = neighbor_lines(files[num + 1], margin, tail=True)
        lines = older + lines
        index += len(older)
    if len(lines) - index <= margin and num > 0:
        lines = lines + neighbor_lines(files[num - 1], margin, tail=False)
    return files[num], lines, index, line_no, ""


def ctx_buffer():
    global ctx_counter
    if opt("context_reuse_buffer") == "on":
        name = CTX_NAME
    else:
        ctx_counter += 1
        name = "%s.%d" % (CTX_NAME, ctx_counter)
    buf = weechat.buffer_search("python", name)
    if not buf:
        buf = weechat.buffer_new(name, "ctx_input_cb", "", "ctx_close_cb", "")
        weechat.buffer_set(buf, "localvar_set_hlmon", "context")
        weechat.buffer_set(buf, "localvar_set_no_log", "1")
        for key in ("+", "="):
            weechat.buffer_set(buf, "key_bind_" + key, "/hlmon more")
        weechat.buffer_set(buf, "key_bind_-", "/hlmon less")
    weechat.buffer_clear(buf)
    return buf


def ctx_close_cb(data, buffer):
    ctx_state.pop(buffer, None)
    return weechat.WEECHAT_RC_OK


def ctx_input_cb(data, buffer, input_data):
    cmd = input_data.strip().lower()
    resize = re.match(r"^([+=-])(\d*)$", cmd)  # "+", "-", "+20"
    if cmd in ("q", "close"):
        weechat.buffer_close(buffer)
    elif resize:
        ctx_resize(buffer, -1 if resize.group(1) == "-" else 1, int(resize.group(2) or 0))
    return weechat.WEECHAT_RC_OK


def look_prefix(name):
    # value may hold colors: "${lightblue}-${gray}!${lightblue}-" -> "-!-"
    value = weechat.config_string(weechat.config_get("weechat.look.prefix_" + name))
    return weechat.string_remove_color(PREFIX_VAR_RE.sub("", value), "").strip()


def join_prefixes():
    """Join/quit prefixes, and those shared with network messages."""
    prefixes = set(p for p in (look_prefix("join"), look_prefix("quit")) if p)
    return prefixes, prefixes & set([look_prefix("network")])


def special_prefixes():
    """Prefixes of non-message lines (network, join, quit, action, error)."""
    names = ("error", "network", "action", "join", "quit")
    return set(p for p in (look_prefix(n) for n in names) if p)


def is_join_line(line, join_prefixes):
    prefixes, ambiguous = join_prefixes
    parts = line.split("\t", 2) + ["", ""]
    prefix = parts[1].strip()
    if prefix not in prefixes:
        return False
    if prefix in ambiguous:
        # same prefix as network messages (topic, modes, nick...): only
        # joins/parts/quits have "nick (ident@host) ..."
        return bool(JOIN_RE.match(parts[2]))
    return True


def is_own_highlight(prefix, msg, entry, special, action):
    """True if a log line is a message from someone else that highlights you."""
    words = entry.get("hl_words")
    p = prefix.strip()
    if not words or not p or (p in special and p != action):
        return False
    own = entry.get("own_nick", "").lower()
    if p == action:
        if msg.split(None, 1)[:1] == [entry.get("own_nick", "")]:  # your own /me
            return False
    elif p.lstrip("~&@%+!").lower() == own:
        return False
    return bool(weechat.string_has_highlight(msg, words))


def highlight_regex(words):
    """Regex matching highlight words (WeeChat word boundaries, * wildcard at the ends)."""
    patterns = []
    for word in words.split(","):
        word = word.strip()
        if word.strip("*"):
            patterns.append(("\\w*" if word.startswith("*") else "") + re.escape(word.strip("*"))
                            + ("\\w*" if word.endswith("*") else ""))
    if not patterns:
        return None
    return re.compile(r"(?<![\w\-|])(?:%s)(?![\w\-|])" % "|".join(patterns), re.I)


def mark_words(msg, regex, color):
    """msg with the highlight words colored, None if none is found."""
    if regex is None:
        return None
    marked = regex.sub(lambda m: color + m.group(0) + base_color(), msg)
    return marked if marked != msg else None


def show_context(before=None, after=None):
    if selected < 0 or selected >= len(entries):
        return
    before = opt_int("context_before") if before is None else before
    after = opt_int("context_after") if after is None else after
    render_context(entries[selected], before, after)


def ctx_resize(buffer, direction, step=None):
    """+ / - in a context buffer: more or less lines on each side."""
    state = ctx_state.get(buffer)
    if not state:
        return
    step = step or max(1, opt_int("context_step"))
    before = max(0, state["before"] + direction * step)
    after = max(0, state["after"] + direction * step)
    render_context(state["entry"], before, after, buffer)


def render_context(entry, before, after, buf=None):
    # generous margin: hidden join/part/quit lines also count
    margin = (max(before, after) + 1) * (5 if opt("hide_joins") == "on" else 1)
    path, lines, index, line_no, error = find_in_log(entry, margin)
    if index < 0:
        weechat.prnt("", "%s%s: %s" % (weechat.prefix("error"), SCRIPT_NAME, error))
        return
    tfmt = log_time_format()

    # (original line number, line), optionally without joins/parts/quits
    numbered = list(enumerate(lines))
    if opt("hide_joins") == "on":
        prefixes = join_prefixes()
        numbered = [(i, line) for i, line in numbered
                    if i == index or not is_join_line(line, prefixes)]
    pos = [i for i, _ in numbered].index(index)

    if buf is None:
        buf = ctx_buffer()
    else:
        weechat.buffer_clear(buf)
    ctx_state[buf] = {"entry": entry, "before": before, "after": after}
    where = "%s line %d" % (path, line_no) if line_no else path
    weechat.buffer_set(buf, "title", "%s | %s | -%d/+%d lines: + more, - less | q to close"
                       % (entry["full_name"], where, before, after))
    tags = LINE_TAGS
    match_color = weechat.color(opt("color_match"))
    chars = nick_chars()
    special = special_prefixes()
    action = look_prefix("action")
    mark = opt("mark_highlights") == "on"
    hl_color = weechat.color(opt("color_highlight"))
    scope = opt("highlight_scope")
    hl_regex = highlight_regex(entry.get("hl_words", "")) if scope == "word" else None
    for i, line in numbered[max(0, pos - before):pos + after + 1]:
        parts = line.split("\t", 2)
        if len(parts) < 3:
            weechat.prnt_date_tags(buf, 0, tags, "\t" + line)
            continue
        date = parse_ts(parts[0], tfmt) or 0
        prefix, msg = parts[1], parts[2]
        color = match_color if i == index else ""
        nick_color = ""
        if not color and mark and is_own_highlight(prefix, msg, entry, special, action):
            if scope == "word":
                marked = mark_words(msg, hl_regex, hl_color)
                if marked is None:
                    color = hl_color
                else:
                    msg = marked
            elif scope == "line":
                color = hl_color
            elif prefix.strip() == action:  # nick: in actions the nick is the first word
                msg = re.sub(r"^\S+", lambda m: hl_color + m.group(0) + base_color(), msg, 1)
            else:
                nick_color = hl_color
        if prefix.strip() and prefix.strip() not in special:
            prefix = wrap_nick(prefix, chars, colored=not color, nick_color=nick_color)
        elif not color:
            prefix = base_color() + prefix
        if color:
            prefix = color + prefix
            msg = color + msg
        else:
            msg = base_color() + msg
        weechat.prnt_date_tags(buf, date, tags, "%s\t%s" % (prefix, msg))
    weechat.buffer_set(buf, "display", "1")


def debug_entry():
    """Print why the selected entry is (not) found in its log."""
    def out(text):
        weechat.prnt("", "hlmon debug\t%s" % text)

    out("weechat %s, python %s" % (weechat.info_get("version", ""), sys.version.split()[0]))
    if selected < 0 or selected >= len(entries):
        out("no entry selected")
        return
    entry = entries[selected]
    tfmt = log_time_format()
    out("entry: date=%d (%s) nick=%r plain=%r" % (
        entry["date"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["date"])),
        entry["nick"], entry["plain"]))
    out("log=%r, logger time_format -> strptime %r, utc=%s, max_time_diff=%s"
        % (entry["log"], tfmt[0], tfmt[1], opt("max_time_diff")))
    if not entry["log"]:
        return
    try:
        with open(entry["log"], "rb") as f:
            first = f.readline()
    except (IOError, OSError) as err:
        out("cannot open log: %s" % err)
        return
    out("first line: %r -> columns=%d ts=%r" % (
        first[:150], len(decode_line(first).split("\t")),
        parse_ts(decode_line(first).split("\t", 1)[0], tfmt)))
    target = entry["plain"].strip()
    hits = []
    for i, line in enumerate(read_log(entry["log"]) or []):
        if target in line:
            ts = parse_ts(line.split("\t", 1)[0], tfmt)
            diff = abs(ts - entry["date"]) if ts is not None else None
            hits.append((diff if diff is not None else 1 << 40, i, line))
    out("%d line(s) contain %r; closest in time:" % (len(hits), target))
    for diff, i, line in sorted(hits)[:5]:
        out("  line %d diff=%s key=%r: %r" % (i + 1, diff, match_key(line, entry, tfmt)[0], line[:150]))

    # the real search used by Enter (fast path included)
    margin = (max(opt_int("context_before"), opt_int("context_after")) + 1) * \
        (5 if opt("hide_joins") == "on" else 1)
    start = time.time()
    path, lines, index, line_no, error = find_in_log(entry, margin)
    if index < 0:
        out("find_in_log: %.3fs -> ERROR: %s" % (time.time() - start, error))
        return
    out("find_in_log: %.3fs -> OK, index %d of %d lines: %r"
        % (time.time() - start, index, len(lines), lines[index][:100]))
    prefixes = join_prefixes()
    out("hide_joins=%s, join/quit prefixes=%r, shared with network=%r"
        % (opt("hide_joins"), sorted(prefixes[0]), sorted(prefixes[1])))
    hidden = sum(1 for line in lines if is_join_line(line, prefixes))
    out("%d of %d lines would be hidden by hide_joins" % (hidden, len(lines)))


# ---------------------------------------------------------------- command

def trace_cb(data, buffer, command):
    weechat.prnt("", "hlmon trace\t[%s] %s"
                 % (weechat.buffer_get_string(buffer, "full_name"), command))
    return weechat.WEECHAT_RC_OK


def hlmon_cmd_cb(data, buffer, args):
    global selected, trace_hook
    argv = args.split()
    action = argv[0].lower() if argv else ""
    if action in ("", "open"):
        mon_open(switch=True)
    elif action == "up":
        select(selected - 1)
    elif action == "down":
        select(selected + 1)
    elif action == "pgup":
        select(selected - page_size())
    elif action == "pgdn":
        select(selected + page_size())
    elif action == "first":
        select(0)
    elif action == "last":
        select(len(entries) - 1)
    elif action == "return":
        # Enter key: run what was typed in the input, or show context
        if weechat.buffer_get_string(buffer, "input"):
            weechat.command(buffer, "/input return")
        else:
            show_context()
    elif action == "enter":
        try:
            before = int(argv[1]) if len(argv) > 1 else None
            after = int(argv[2]) if len(argv) > 2 else before
        except ValueError:
            return weechat.WEECHAT_RC_ERROR
        show_context(before, after)
    elif action == "del":
        if 0 <= selected < len(entries):
            del entries[selected]
            select(selected if selected < len(entries) else len(entries) - 1)
            redraw()
    elif action == "clear":
        del entries[:]
        selected = -1
        redraw()
    elif action in ("more", "less"):
        ctx_resize(buffer, 1 if action == "more" else -1)
    elif action == "trace":
        # print every command WeeChat runs (to find who runs /msg)
        if trace_hook:
            weechat.unhook(trace_hook)
            trace_hook = ""
            weechat.prnt("", "hlmon trace\toff")
        else:
            trace_hook = weechat.hook_command_run("/*", "trace_cb", "")
            weechat.prnt("", "hlmon trace\ton: every command is printed here")
    elif action == "debug":
        debug_entry()
    elif action == "add":
        # fake highlight in current buffer: add YYYY-MM-DD HH:MM[:SS] nick message
        if len(argv) < 5 or weechat.buffer_get_string(buffer, "localvar_hlmon"):
            weechat.prnt_date_tags(buffer, 0, "no_log,notify_none",
                                   "%s%s: usage (in a channel buffer): /hlmon add "
                                   "YYYY-MM-DD HH:MM[:SS] <nick> <message>"
                                   % (weechat.prefix("error"), SCRIPT_NAME))
            return weechat.WEECHAT_RC_ERROR
        date = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                date = int(time.mktime(time.strptime("%s %s" % (argv[1], argv[2]), fmt)))
                break
            except ValueError:
                pass
        if date is None:
            weechat.prnt_date_tags(buffer, 0, "no_log,notify_none",
                                   "%s%s: invalid date/time: %s %s"
                                   % (weechat.prefix("error"), SCRIPT_NAME, argv[1], argv[2]))
            return weechat.WEECHAT_RC_ERROR
        message = args.split(None, 4)[4]
        add_entry(buffer, date, argv[3], message, select_it=True)
        weechat.prnt_date_tags(buffer, 0, "no_log,notify_none",
                               "%s: test highlight added (%s %s <%s> %s), log: %s"
                               % (SCRIPT_NAME, argv[1], argv[2], argv[3], message,
                                  entries[-1]["log"] or "none"))
        weechat.buffer_set(mon_buffer, "display", "1")
        ensure_visible()
    else:
        return weechat.WEECHAT_RC_ERROR
    return weechat.WEECHAT_RC_OK


if __name__ == "__main__" and IMPORT_OK:
    if weechat.register(SCRIPT_NAME, SCRIPT_AUTHOR, SCRIPT_VERSION, SCRIPT_LICENSE,
                        SCRIPT_DESC, "", ""):
        for name, (value, desc) in DEFAULTS.items():
            if not weechat.config_is_set_plugin(name):
                weechat.config_set_plugin(name, value)
            weechat.config_set_desc_plugin(name, desc)
        weechat.hook_command(
            SCRIPT_NAME, SCRIPT_DESC,
            "[open] || up|down|pgup|pgdn|first|last || enter [<before> [<after>]] || del || clear"
            " || more|less || debug || trace || add <YYYY-MM-DD> <HH:MM[:SS]> <nick> <message>",
            "  open: open the monitor buffer (default)\n"
            "    up/down/pgup/pgdn/first/last: move selection\n"
            " enter: show log context of the selected entry\n"
            "   del: delete selected entry\n"
            " clear: delete all entries\n"
            "  more/less: in the context buffer, add/remove lines on each side (keys + and -)\n"
            " trace: print every command WeeChat runs, on/off (to diagnose unexpected commands)\n"
            " debug: show why the selected entry is (not) found in its log\n"
            "   add: add a fake highlight for the current buffer (local time), for testing\n\n"
            "In the hlmon buffer: arrows, pgup/pgdn, home/end and enter work directly.\n"
            "Without keys (e.g. weechat-android): type the entry number in the hlmon input,\n"
            "  '12' or '12 30' (lines before and after) or '12 5 20' ('.' = last entry: '.', '. 30');\n"
            "  in the context buffer\n"
            "  type '+', '-', '+20', '-5' or 'q'.\n"
            "Context size: /set plugins.var.python.hlmon.context_before / context_after\n\n"
            "Example:\n"
            "  /hlmon add 2026-10-17 21:58:37 ElQueFaltaba DS",
            "open|up|down|pgup|pgdn|first|last|enter|del|clear|more|less|trace|debug|add",
            "hlmon_cmd_cb", "")
        weechat.hook_print("", "", "", 0, "print_cb", "")
        mon_open()
