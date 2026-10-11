"""Discovery sources, coverage matching and the ledger. No network calls to Kuma here."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]

# Runs ON a Proxmox node: one line per LXC container, id|name|status|ip. Sent over ssh stdin, so nothing is
# installed on the node. Single pass so a nested ssh never eats the loop's stdin.
PROXMOX_SCRIPT = r"""
pct list 2>/dev/null | tail -n +2 | awk '{print $1}' | while read -r id; do
  [ -z "$id" ] && continue
  name=$(pct config "$id" 2>/dev/null | sed -n 's/^hostname: //p')
  status=$(pct status "$id" 2>/dev/null | sed 's/^status: //')
  ip="-"
  if [ "$status" = "running" ]; then
    ip=$(pct exec "$id" -- hostname -I </dev/null 2>/dev/null | awk '{print $1}')
    [ -z "$ip" ] && ip="-"
  fi
  printf '%s|%s|%s|%s\n' "$id" "${name:-?}" "${status:-?}" "$ip"
done
"""

PORT_RE = re.compile(r"(0\.0\.0\.0|\[::\]):(\d+)->\d+/tcp")


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def norm(s: str | None) -> str:
    s = re.sub(r"^(node|svc|web)\s*:\s*", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _run(argv: list[str], timeout: int, stdin: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                              stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None


# ── sources ───────────────────────────────────────────────────────────────────────────────────────────
def discover_docker(host: dict, skip: set[str]) -> list[dict]:
    """host = {name, ssh ('local' = run docker here), address}. Containers with a host-published TCP port."""
    label, address = host.get("name") or host["address"], host["address"]
    cmd = ["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"]
    argv = cmd if host.get("ssh", "local") == "local" else SSH + [host["ssh"], " ".join(
        ["docker", "ps", "--format", "'{{.Names}}|{{.Ports}}'"])]
    p = _run(argv, 60)
    if p is None or p.returncode != 0:
        log(f"docker {label}: {'timed out' if p is None else p.stderr.strip()[:120]} — skipped")
        return []
    out = []
    for row in p.stdout.splitlines():
        name, _, ports = row.partition("|")
        if not name or name in skip:
            continue
        hp = sorted({int(m.group(2)) for m in PORT_RE.finditer(ports)})   # loopback-bound ports are ignored
        if hp:
            out.append({"key": f"docker:{label}:{name}", "kind": "docker", "label": f"{label} container {name}",
                        "name": name, "ip": address, "ports": hp,
                        "monitor": {"type": "port", "name": f"Svc: {name} ({label})", "hostname": address,
                                    "port": hp[0]}})
    return out


def discover_proxmox(node: dict) -> list[dict]:
    """node = {ssh, ip_prefix?}. Running LXC containers with an IP (optionally only IPs starting ip_prefix)."""
    p = _run(SSH + [node["ssh"], "bash", "-s"], 120, stdin=PROXMOX_SCRIPT)
    if p is None or p.returncode != 0:
        log(f"proxmox {node['ssh']}: {'timed out' if p is None else p.stderr.strip()[:120]} — skipped")
        return []
    prefix, host = node.get("ip_prefix", ""), node["ssh"].split("@")[-1]
    out = []
    for row in p.stdout.splitlines():
        parts = [x.strip() for x in row.split("|")]
        if len(parts) < 4:
            continue
        vmid, name, status, ip = parts[:4]
        if status == "running" and ip != "-" and ip.startswith(prefix):
            out.append({"key": f"ct:{host}:{vmid}", "kind": "ct", "label": f"CT{vmid} {name} on {host}",
                        "name": name, "ip": ip, "monitor": {"type": "ping", "name": f"Node: {name}", "hostname": ip}})
    return out


def discover_cloudflare(account_id: str, token: str, http=None) -> list[dict]:
    """Public hostnames from every Cloudflare Tunnel's ingress rules (read-only API calls)."""
    import requests
    http = http or requests
    h = {"Authorization": f"Bearer {token}"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel"
    try:
        hosts: dict[str, str] = {}
        for t in http.get(base + "?is_deleted=false", headers=h, timeout=20).json()["result"]:
            cfg = http.get(f"{base}/{t['id']}/configurations", headers=h, timeout=20).json()["result"]
            for i in (cfg.get("config") or {}).get("ingress", []):
                if i.get("hostname"):
                    hosts.setdefault(i["hostname"], i.get("service", ""))
    except Exception as e:  # noqa: BLE001 — a broken source must not stop the others
        log(f"cloudflare: failed ({type(e).__name__}) — skipped")
        return []
    return [{"key": f"host:{hn}", "kind": "host", "label": f"public hostname {hn} → {svc}", "name": hn,
             "origin": svc, "monitor": {"type": "http", "name": f"Web: {hn}", "url": f"https://{hn}/"}}
            for hn, svc in sorted(hosts.items())]


# ── coverage ──────────────────────────────────────────────────────────────────────────────────────────
def coverage(monitors: list[dict]) -> dict:
    ips, hostports, hostnames, names = set(), set(), set(), set()
    for m in monitors:
        names.add(norm(m.get("name")))
        url = m.get("url") or ""
        if url and "://" in url and url != "https://":
            u = urlparse(url)
            if u.hostname:
                hostnames.add(u.hostname); ips.add(u.hostname)
                hostports.add((u.hostname, u.port or (443 if u.scheme == "https" else 80)))
        if m.get("hostname"):
            ips.add(m["hostname"]); hostnames.add(m["hostname"])
            if m.get("port"):
                hostports.add((m["hostname"], int(m["port"])))
    return {"ips": ips, "hostports": hostports, "hostnames": hostnames, "names": names}


def covered(item: dict, cov: dict, tunnel_items=()) -> bool:
    """Already watched: same IP / IP:port / hostname, a monitored tunnel hostname in front of it, or the same name."""
    if item["kind"] == "ct":
        return item["ip"] in cov["ips"] or norm(item["name"]) in cov["names"]
    if item["kind"] == "docker":
        if any((item["ip"], p) in cov["hostports"] for p in item["ports"]):
            return True
        for t in tunnel_items:
            o = urlparse(t.get("origin", ""))
            if o.hostname == item["ip"] and o.port in item["ports"] and t["name"] in cov["hostnames"]:
                return True
        return norm(item["name"]) in cov["names"]
    if item["kind"] == "host":
        return item["name"] in cov["hostnames"]
    return False


def describe(m: dict) -> str:
    if m["type"] == "ping":
        return f"ping {m['hostname']}"
    if m["type"] == "port":
        return f"TCP port {m['hostname']}:{m['port']}"
    return f"HTTP {m['url']}"


# ── ledger ────────────────────────────────────────────────────────────────────────────────────────────
@contextmanager
def ledger(state_dir: str):
    """Locked read-modify-write of ledger.json. Statuses: pending, added, rejected, failed, expired."""
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "ledger.json")
    with open(os.path.join(state_dir, ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {"items": {}}
        yield data
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
