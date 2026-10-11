"""kuma-discover: find services Uptime Kuma isn't watching, ask a human, then add the monitor.

  kuma-discover scan [--dry-run]     discover -> diff against Kuma -> new gaps become pending proposals
  kuma-discover review                go through pending proposals at the terminal (y / n / skip)
  kuma-discover approve KEY [KEY..]   add the monitor for a pending proposal
  kuma-discover reject KEY [KEY..]    never propose it again
  kuma-discover poll                  Discord mode: act on the owner's ✅ / ❌ reactions, expire old ones
  kuma-discover list                  show the ledger

Nothing is ever added without a human yes. No LLM anywhere.
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from datetime import datetime, timedelta

from . import core
from .core import log, now

DEFAULTS = {
    "kuma": {"url": "http://127.0.0.1:3001", "notification_ids": [], "interval": 60},
    "scan": {"max_new": 6, "skip_containers": ["uptime-kuma"], "skip_images": ["louislam/uptime-kuma"], "expiry_days": 14,
             "state_dir": "~/.local/state/kuma-discover"},
    "docker_hosts": [], "proxmox_nodes": [],
    "cloudflare": {"enabled": False, "account_id": ""},
    "discord": {"enabled": False, "channel_id": "", "owner_id": ""},
}


def load_config(path: str | None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULTS.items()}
    path = path or os.environ.get("KUMA_DISCOVER_CONFIG") or "kuma-discover.toml"
    if os.path.exists(path):
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    elif path != "kuma-discover.toml":
        raise SystemExit(f"kuma-discover: config file not found: {path}")
    cfg["scan"]["state_dir"] = os.path.expanduser(cfg["scan"]["state_dir"])
    return cfg


def env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"kuma-discover: set {name} (see config.example.toml)")
    return v


def connect(cfg, kuma_cls=None):
    from .kuma import Kuma
    return (kuma_cls or Kuma)(cfg["kuma"]["url"], env("KUMA_USERNAME"), env("KUMA_PASSWORD"))


def discord_client(cfg):
    if not cfg["discord"].get("enabled"):
        return None
    from .discord import Discord
    return Discord(env("DISCORD_BOT_TOKEN"), str(cfg["discord"]["channel_id"]))


def discover(cfg) -> tuple[list[dict], list[dict]]:
    skip = set(cfg["scan"]["skip_containers"])
    items: list[dict] = []
    for h in cfg["docker_hosts"]:
        items += core.discover_docker(h, skip, tuple(cfg["scan"]["skip_images"]))
    for n in cfg["proxmox_nodes"]:
        items += core.discover_proxmox(n)
    tunnel: list[dict] = []
    if cfg["cloudflare"].get("enabled"):
        tunnel = core.discover_cloudflare(cfg["cloudflare"]["account_id"], env("CF_API_TOKEN"))
    return items + tunnel, tunnel


# ── commands ──────────────────────────────────────────────────────────────────────────────────────────
def cmd_scan(cfg, a, kuma_cls=None) -> int:
    items, tunnel = discover(cfg)
    api = connect(cfg, kuma_cls)
    try:
        cov = core.coverage(api.monitors())
    finally:
        api.close()
    gaps = [i for i in items if not core.covered(i, cov, tunnel)]
    log(f"discovered {len(items)}, not watched {len(gaps)}")
    dc = None if a.dry_run else discord_client(cfg)
    with core.ledger(cfg["scan"]["state_dir"]) as data:
        led = data["items"]
        fresh = [g for g in gaps if (led.get(g["key"]) or {}).get("status") not in ("pending", "rejected", "added")]
        for g in fresh[:cfg["scan"]["max_new"]]:
            line = f"Kuma isn't watching: {g['label']} -> proposed {g['monitor']['name']} ({core.describe(g['monitor'])})"
            if a.dry_run:
                print("DRY", line)
                continue
            rec = {**g, "status": "pending", "created_at": now().isoformat()}
            if dc:
                mid = dc.post(f"🔭 **Kuma isn't watching:** {g['label']}\nProposed monitor: **{g['monitor']['name']}**"
                              f" — {core.describe(g['monitor'])}, every {cfg['kuma']['interval']}s.\n"
                              "React ✅ to add it, ❌ to never ask about it again.")
                dc.react(mid, "✅"); dc.react(mid, "❌")
                rec["message_id"] = mid
            led[g["key"]] = rec
            print(f"PROPOSED {g['key']}\n  {line}")
        held = len(fresh) - cfg["scan"]["max_new"]
        if held > 0:
            log(f"{held} more held for the next run")
        if not a.dry_run and not dc and fresh:
            print("\nApprove with:  kuma-discover approve <key>   (or: kuma-discover review)")
    return 0


def _decide(cfg, it: dict, yes: bool, api) -> str:
    """Apply one decision to a pending ledger item. Returns a one-line result."""
    if not yes:
        it.update(status="rejected", decided_at=now().isoformat())
        return f"rejected {it['key']} — won't propose it again"
    if core.covered(it, core.coverage(api.monitors())):
        it.update(status="added", decided_at=now().isoformat(), note="already covered by then")
        return f"{it['key']}: something already watches it now — nothing added"
    try:
        new_id = api.add(it["monitor"], cfg["kuma"]["notification_ids"], cfg["kuma"]["interval"])
    except Exception as e:  # noqa: BLE001
        it.update(status="failed", decided_at=now().isoformat(), note=f"{type(e).__name__}: {str(e)[:150]}")
        return f"FAILED {it['key']}: {it['note']}"
    it.update(status="added", decided_at=now().isoformat(), monitor_id=new_id)
    return f"added monitor #{new_id} {it['monitor']['name']}"


def cmd_decide(cfg, keys: list[str], yes: bool, kuma_cls=None) -> int:
    api, rc = None, 0
    try:
        with core.ledger(cfg["scan"]["state_dir"]) as data:
            for k in keys:
                it = data["items"].get(k)
                if not it or it["status"] != "pending":
                    print(f"no pending proposal {k!r} (see: kuma-discover list)"); rc = 1; continue
                if yes:
                    api = api or connect(cfg, kuma_cls)
                msg = _decide(cfg, it, yes, api)
                print(msg); rc |= msg.startswith("FAILED")
    finally:
        if api:
            api.close()
    return rc


def cmd_review(cfg, a, kuma_cls=None, ask=input) -> int:
    with core.ledger(cfg["scan"]["state_dir"]) as data:
        pend = [k for k, i in data["items"].items() if i["status"] == "pending"]
    if not pend:
        print("nothing pending"); return 0
    yes, no = [], []
    for k in pend:
        with core.ledger(cfg["scan"]["state_dir"]) as data:
            it = data["items"][k]
        ans = ask(f"{it['label']}\n  add {it['monitor']['name']} ({core.describe(it['monitor'])})? [y/n/skip] ").strip().lower()
        (yes if ans == "y" else no if ans == "n" else []).append(k)
    rc = 0
    if yes:
        rc |= cmd_decide(cfg, yes, True, kuma_cls)
    if no:
        rc |= cmd_decide(cfg, no, False, kuma_cls)
    return rc


def cmd_poll(cfg, a, kuma_cls=None, dc=None) -> int:
    dc = dc or discord_client(cfg)
    if not dc:
        raise SystemExit("kuma-discover: poll needs [discord] enabled = true")
    owner = str(cfg["discord"]["owner_id"])
    api = None
    try:
        with core.ledger(cfg["scan"]["state_dir"]) as data:
            for it in [i for i in data["items"].values() if i["status"] == "pending" and i.get("message_id")]:
                mid = it["message_id"]
                yes = dc.reactors(mid, "✅")
                if yes is None:
                    it.update(status="expired", decided_at=now().isoformat(), note="message deleted"); continue
                if owner in (dc.reactors(mid, "❌") or []):
                    dc.post("❌ " + _decide(cfg, it, False, None), reply_to=mid)
                elif owner in yes:
                    api = api or connect(cfg, kuma_cls)
                    msg = _decide(cfg, it, True, api)
                    dc.post(("🛑 " if msg.startswith("FAILED") else "✅ ") + msg, reply_to=mid)
                elif datetime.fromisoformat(it["created_at"]) + timedelta(days=cfg["scan"]["expiry_days"]) < now():
                    it.update(status="expired", decided_at=now().isoformat())
                    dc.post("⌛ Expired unanswered — I'll ask again if it's still unwatched.", reply_to=mid)
    finally:
        if api:
            api.close()
    return 0


def cmd_list(cfg, a) -> int:
    with core.ledger(cfg["scan"]["state_dir"]) as data:
        items = data["items"]
    for k, i in sorted(items.items()):
        print(f"{i['status']:9} {k:44} {i['monitor']['name']}  {i.get('note', '')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kuma-discover", description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help="TOML config (default: $KUMA_DISCOVER_CONFIG or ./kuma-discover.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="find unwatched services and propose monitors")
    s.add_argument("--dry-run", action="store_true", help="print proposals, change nothing")
    sub.add_parser("review", help="approve or reject pending proposals at the terminal")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} pending proposals by key")
        p.add_argument("keys", nargs="+")
    sub.add_parser("poll", help="Discord mode: act on ✅ / ❌ reactions")
    sub.add_parser("list", help="show every proposal and its status")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd in ("approve", "reject"):
        return cmd_decide(cfg, a.keys, a.cmd == "approve")
    return {"scan": cmd_scan, "review": cmd_review, "poll": cmd_poll, "list": cmd_list}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
