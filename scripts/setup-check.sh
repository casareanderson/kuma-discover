#!/usr/bin/env bash
# Follows docs/SETUP.md steps 2-6 on a GitHub runner against a REAL Uptime Kuma container.
# The runner is also the "Docker host", reached over SSH to localhost.
# KUMA_IMAGE picks the Kuma version. TRANSCRIPT=<file> also records the user-facing commands.
set -euo pipefail
KUMA_IMAGE="${KUMA_IMAGE:-louislam/uptime-kuma:1.23.16}"
TRANSCRIPT="${TRANSCRIPT:-/dev/null}"
say() { printf '\n== %s\n' "$*"; }
fail() { echo "SETUP-CHECK FAILED: $*" >&2; exit 1; }
show() { printf '$ %s\n' "$*" | tee -a "$TRANSCRIPT"; "$@" 2>&1 | tee -a "$TRANSCRIPT"; echo >> "$TRANSCRIPT"; }

say "Uptime Kuma ($KUMA_IMAGE) with an admin user (CI-only: in real life you made the admin in the web UI)"
docker rm -f kuma demo-web demo-api demo-local >/dev/null 2>&1 || true
docker run -d --name kuma -p 3001:3001 -e UPTIME_KUMA_DB_TYPE=sqlite "$KUMA_IMAGE" >/dev/null
for i in $(seq 1 90); do curl -fsS -o /dev/null http://127.0.0.1:3001 && break; sleep 2; done
curl -fsS -o /dev/null http://127.0.0.1:3001 || { docker logs kuma | tail -40; fail "Kuma did not come up"; }

say "install (step 2) - from this checkout so CI tests the commit"
python3 -m pip install -q pipx && python3 -m pipx install --force . >/dev/null
export PATH="$HOME/.local/bin:$PATH"
KD_PY="$(python3 -m pipx environment --value PIPX_LOCAL_VENVS)/kuma-discover/bin/python"
"$KD_PY" - <<'PY'
import time
from uptime_kuma_api import UptimeKumaApi
for i in range(30):
    try:
        api = UptimeKumaApi("http://127.0.0.1:3001", timeout=30)
        if api.need_setup():
            api.setup("admin", "ci-only-Passw0rd")
        api.login("admin", "ci-only-Passw0rd"); api.disconnect(); break
    except Exception as e:
        print("waiting for kuma:", type(e).__name__, str(e)[:80]); time.sleep(3)
else:
    raise SystemExit("could not create / log in the Kuma admin")
print("kuma admin ready")
PY
kuma-discover --help >/dev/null

say "SSH to the Docker host (step 3)"
if ! command -v sshd >/dev/null; then sudo apt-get update -qq && sudo apt-get install -y -qq openssh-server; fi
sudo mkdir -p /run/sshd && (sudo systemctl start ssh 2>/dev/null || sudo /usr/sbin/sshd)
mkdir -p ~/.ssh && chmod 700 ~/.ssh
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
docker run -d --name demo-web -p 8080:80 nginx:alpine >/dev/null
docker run -d --name demo-api -p 8081:80 nginx:alpine >/dev/null
docker run -d --name demo-local -p 127.0.0.1:8082:80 nginx:alpine >/dev/null
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$USER@localhost" \
  "docker ps --format '{{.Names}}|{{.Ports}}'" | grep -q "demo-web|0.0.0.0:8080" || fail "ssh docker ps"

say "config (step 4)"
mkdir -p ~/.config/kuma-discover
sed -e 's|^url = .*|url = "http://127.0.0.1:3001"|' \
    -e 's|^name = "nas"|name = "lab"|' \
    -e "s|^ssh = \"you@192.0.2.10\"|ssh = \"$USER@localhost\"|" \
    -e 's|^address = "192.0.2.10".*|address = "127.0.0.1"|' \
    -e "s|^state_dir = .*|state_dir = \"$HOME/.local/state/kuma-discover\"|" \
    config.example.toml > ~/.config/kuma-discover/kuma-discover.toml
export KUMA_DISCOVER_CONFIG=~/.config/kuma-discover/kuma-discover.toml KUMA_USERNAME=admin KUMA_PASSWORD=ci-only-Passw0rd
rm -rf ~/.local/state/kuma-discover

say "dry run (step 5)"
out=$(show kuma-discover scan --dry-run)
echo "$out" | grep -q "proposed Svc: demo-web (lab)" || fail "dry run did not propose demo-web"
echo "$out" | grep -q "demo-local" && fail "loopback-only container was proposed"
echo "$out" | grep -q "Svc: kuma " && fail "kuma itself was proposed (CI names it 'kuma'; it must be skipped by image)"
[ ! -s ~/.local/state/kuma-discover/ledger.json ] || grep -q '"items": {}' ~/.local/state/kuma-discover/ledger.json \
  || fail "dry run wrote proposals"

say "propose, approve, reject (step 6)"
show kuma-discover scan
show kuma-discover approve docker:lab:demo-web
show kuma-discover reject docker:lab:demo-api
show kuma-discover list
"$KD_PY" - <<'PY'
from uptime_kuma_api import UptimeKumaApi
api = UptimeKumaApi("http://127.0.0.1:3001", timeout=30); api.login("admin", "ci-only-Passw0rd")
names = [m["name"] for m in api.get_monitors()]; api.disconnect()
assert "Svc: demo-web (lab)" in names, names
assert not any("demo-api" in n for n in names), names
print("Kuma now has:", names)
PY
again=$(kuma-discover scan)
echo "$again" | grep -q PROPOSED && fail "second scan re-proposed something: $again"
docker rm -f kuma demo-web demo-api demo-local >/dev/null
say "setup-check passed"
