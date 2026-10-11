"""Thin wrapper over uptime-kuma-api: login, list monitors, add one monitor and read it back."""
from __future__ import annotations


class Kuma:
    def __init__(self, url: str, username: str, password: str, api_cls=None, tries: int = 3):
        if api_cls is None:
            from uptime_kuma_api import UptimeKumaApi as api_cls
        # The socket.io login occasionally times out on a busy Kuma (seen 2 in 7 runs against a Kuma 2.5 on a
        # ZimaBlade). A fresh connection nearly always works, so retry before giving up.
        for attempt in range(1, tries + 1):
            self.api = api_cls(url, timeout=30)
            try:
                self.api.login(username, password)
                return
            except Exception as e:  # noqa: BLE001
                self.close()
                if attempt == tries or "Timeout" not in type(e).__name__:
                    raise

    def monitors(self) -> list[dict]:
        return self.api.get_monitors()

    def add(self, m: dict, notification_ids: list[int], interval: int = 60) -> int:
        """Add a ping / port / http monitor. Returns the new monitor id after reading it back."""
        from uptime_kuma_api import MonitorType
        kw = {"type": MonitorType(m["type"]), "name": m["name"], "interval": interval,
              "retryInterval": interval, "maxretries": 2}
        for k in ("hostname", "port", "url"):
            if k in m:
                kw[k] = m[k]
        if m["type"] == "http":
            kw["accepted_statuscodes"] = ["200-299", "300-399"]
        try:
            new_id = self.api.add_monitor(notificationIDList=list(notification_ids), **kw)["monitorID"]
        except Exception as e:  # noqa: BLE001
            # uptime-kuma-api 1.2.1 was written for Kuma 1.x. Kuma 2.x rejects its add (monitor.conditions is
            # NOT NULL), so build the same payload and send it with conditions set.
            if "conditions" not in str(e):
                raise
            data = self.api._build_monitor_data(**kw)
            data["conditions"] = "[]"
            # Kuma iterates the KEYS of this object; a plain list would add notification id "0"
            data["notificationIDList"] = {str(i): True for i in notification_ids}
            new_id = self.api._call("add", data)["monitorID"]
        back = self.api.get_monitor(new_id)                      # don't trust the add ack
        if back.get("name") != m["name"]:
            raise RuntimeError(f"read-back mismatch for monitor #{new_id}")
        return new_id

    def close(self) -> None:
        try:
            self.api.disconnect()
        except Exception:  # noqa: BLE001
            pass
