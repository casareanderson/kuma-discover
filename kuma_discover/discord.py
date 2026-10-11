"""Minimal Discord REST client for the optional ✅/❌ approval flow (bot token, one channel)."""
from __future__ import annotations

import urllib.parse

import requests

API = "https://discord.com/api/v10"
APPROVE, REJECT = "✅", "❌"


class Discord:
    def __init__(self, token: str, channel_id: str, http=None):
        self.h = {"Authorization": f"Bot {token}", "User-Agent": "kuma-discover (github.com/casareanderson/kuma-discover)"}
        self.channel = channel_id
        self.http = http or requests

    def post(self, content: str, reply_to: str | None = None) -> str:
        body = {"content": content[:1990], "allowed_mentions": {"parse": []}}
        if reply_to:
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        r = self.http.post(f"{API}/channels/{self.channel}/messages", headers=self.h, json=body, timeout=30)
        r.raise_for_status()
        return r.json()["id"]

    def react(self, message_id: str, emoji: str) -> None:
        e = urllib.parse.quote(emoji)
        self.http.put(f"{API}/channels/{self.channel}/messages/{message_id}/reactions/{e}/@me",
                      headers=self.h, timeout=30)

    def reactors(self, message_id: str, emoji: str) -> list[str] | None:
        """User ids that reacted with emoji; None if the message is gone."""
        e = urllib.parse.quote(emoji)
        r = self.http.get(f"{API}/channels/{self.channel}/messages/{message_id}/reactions/{e}?limit=100",
                          headers=self.h, timeout=30)
        if r.status_code == 404:
            try:
                code = r.json().get("code")
            except ValueError:
                code = None
            return None if code == 10008 else []     # 10008 Unknown Message; 10014 = nobody reacted yet
        r.raise_for_status()
        return [u["id"] for u in r.json()]
