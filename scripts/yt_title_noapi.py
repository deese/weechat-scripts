# -*- coding: utf-8 -*-
import weechat
import re
import json
import urllib.parse

SCRIPT_NAME = "yt_title_noapi"
SCRIPT_AUTHOR = "DeeSe"
SCRIPT_VERSION = "1.1"
SCRIPT_LICENSE = "GPL3"
SCRIPT_DESC = "Retrieve youtube video title without requiring an API key."

RE_YOUTUBE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:\S*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)"
    r"([\w-]{11})",
    re.I,
)

script_nick = "YT"

DEFAULTS = {
    "enabled_channels": ("", "comma separated list of channels, or * for all"),
    "max_per_message": ("3", "max youtube links resolved per message"),
    "format": ("<{nick}> Title: {title}", "output format ({nick}, {title})"),
    "send_to_channel": ("off", "say the title in the channel instead of printing locally"),
}

CACHE = {}
CACHE_MAX = 100
# Partial process output, keyed by request id.
PENDING = {}
_req_id = 0


def say(s, buffer=""):
    """Display message."""
    weechat.prnt(buffer, "%s\t%s" % (script_nick, s))


def channel_enabled(channel):
    enabled = [
        c.strip().lower()
        for c in weechat.config_get_plugin("enabled_channels").split(",")
        if c.strip()
    ]
    return "*" in enabled or channel.lower() in enabled


def show(buffer, nick, title):
    fmt = weechat.config_get_plugin("format") or DEFAULTS["format"][0]
    text = fmt.format(nick=nick, title=title)
    if weechat.config_string_to_boolean(weechat.config_get_plugin("send_to_channel")):
        weechat.command(buffer, "/say %s" % text)
    else:
        say(text, buffer)


def title_cb(data, command, rc, out, err):
    req = PENDING.get(data)
    if req is None:
        return weechat.WEECHAT_RC_OK
    req["out"] += out or ""
    if rc == weechat.WEECHAT_HOOK_PROCESS_RUNNING:
        return weechat.WEECHAT_RC_OK
    del PENDING[data]
    if rc != 0:
        return weechat.WEECHAT_RC_OK
    try:
        title = json.loads(req["out"])["title"].replace("\n", " ").strip()
    except (ValueError, KeyError, TypeError):
        # oEmbed answers with plain text errors for private/removed videos.
        return weechat.WEECHAT_RC_OK
    if not title:
        return weechat.WEECHAT_RC_OK
    if len(CACHE) >= CACHE_MAX:
        CACHE.pop(next(iter(CACHE)))
    CACHE[req["vid"]] = title
    show(req["buffer"], req["nick"], title)
    return weechat.WEECHAT_RC_OK


def request_title(buffer, nick, vid):
    global _req_id
    _req_id += 1
    key = str(_req_id)
    PENDING[key] = {"buffer": buffer, "nick": nick, "vid": vid, "out": ""}
    video_url = "https://www.youtube.com/watch?v=%s" % vid
    url = "url:https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"format": "json", "url": video_url}
    )
    weechat.hook_process(url, 10000, "title_cb", key)


def url_msg(data, signal, message):
    try:
        msg = weechat.info_get_hashtable("irc_message_parse", {"message": message})
        channel = msg.get("channel", "")
        text = msg.get("text", "")
        if not channel or not channel_enabled(channel) or "http" not in text.lower():
            return weechat.WEECHAT_RC_OK

        try:
            limit = int(weechat.config_get_plugin("max_per_message"))
        except ValueError:
            limit = 3
        ids = list(dict.fromkeys(RE_YOUTUBE.findall(text)))[:limit]
        if not ids:
            return weechat.WEECHAT_RC_OK

        server = signal.partition(",")[0]
        buffer = weechat.info_get("irc_buffer", "%s,%s" % (server, channel))
        nick = msg.get("nick", "")
        for vid in ids:
            if vid in CACHE:
                show(buffer, nick, CACHE[vid])
            else:
                request_title(buffer, nick, vid)
    except Exception as e:
        say("Error: %s" % e)

    return weechat.WEECHAT_RC_OK


if __name__ == "__main__":
    if weechat.register(
        SCRIPT_NAME, SCRIPT_AUTHOR, SCRIPT_VERSION, SCRIPT_LICENSE, SCRIPT_DESC, "", ""
    ):
        for option, (default, desc) in DEFAULTS.items():
            if not weechat.config_is_set_plugin(option):
                weechat.config_set_plugin(option, default)
            weechat.config_set_desc_plugin(option, desc)
        if not weechat.config_get_plugin("enabled_channels"):
            say(
                "enabled_channels is empty: set it with "
                "/set plugins.var.python.%s.enabled_channels #chan1,#chan2 (or *)"
                % SCRIPT_NAME
            )
        weechat.hook_signal("*,irc_in2_PRIVMSG", "url_msg", "")
