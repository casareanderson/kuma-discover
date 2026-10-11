# Setting up kuma-discover

This takes about 15 minutes. By the end, kuma-discover scans your Docker hosts (and optionally Proxmox and Cloudflare Tunnel), tells you what Uptime Kuma isn't watching, and adds a monitor when you say yes.

**What CI proves and what it can't.** The steps marked ✅ run on every change in [`scripts/setup-check.sh`](../scripts/setup-check.sh) on a GitHub runner, against a real Uptime Kuma container (1.23 and 2.x), a Docker host reached over SSH, and real `nginx` containers. CI can't prove the Proxmox, Cloudflare and Discord parts, because they need your accounts and machines. Those steps are marked "not CI-tested".

## 1. What you need

- **Uptime Kuma** 1.23 or 2.x, and its admin username and password. kuma-discover logs in the same way the web UI does, using [uptime-kuma-api](https://github.com/lucasheld/uptime-kuma-api).
- **Python 3.11 or newer** on the machine that will run kuma-discover. Any always-on box works.
- **SSH key login** from that machine to each Docker host, with a user that can run `docker ps` without sudo. Or run it on the Docker host itself with `ssh = "local"`.

## 2. Install ✅

```bash
pipx install git+https://github.com/casareanderson/kuma-discover
kuma-discover --help
```

No `pipx`? `python3 -m venv ~/.venvs/kuma-discover && ~/.venvs/kuma-discover/bin/pip install git+https://github.com/casareanderson/kuma-discover`.

## 3. Check SSH to your Docker host ✅

This must print container names and ports, and must **not** ask for a password:

```bash
ssh -o BatchMode=yes you@your-docker-host docker ps --format '{{.Names}}|{{.Ports}}'
```

If it asks for a password, set up a key first: `ssh-keygen -t ed25519` then `ssh-copy-id you@your-docker-host`.

## 4. Write the config ✅

```bash
mkdir -p ~/.config/kuma-discover
curl -fsSL https://raw.githubusercontent.com/casareanderson/kuma-discover/main/config.example.toml \
  -o ~/.config/kuma-discover/kuma-discover.toml
```

Edit it. The parts that matter:

```toml
[kuma]
url = "http://your-kuma:3001"
notification_ids = [1]          # optional: which Kuma notification new monitors alert to

[[docker_hosts]]
name = "nas"                    # shows up in monitor names: "Svc: immich (nas)"
ssh = "you@your-docker-host"    # or "local"
address = "your-docker-host"    # what the monitor points at
```

Add one `[[docker_hosts]]` block per host. To find a notification id, open the notification in Kuma's settings; the number is in the URL.

The password never goes in the file. Put it in the environment:

```bash
export KUMA_DISCOVER_CONFIG=~/.config/kuma-discover/kuma-discover.toml
export KUMA_USERNAME=admin
export KUMA_PASSWORD='your-kuma-password'
```

## 5. First scan, changing nothing ✅

```bash
kuma-discover scan --dry-run
```

You get one `DRY` line per service Kuma isn't watching, for example:

```
DRY Kuma isn't watching: nas container immich -> proposed Svc: immich (nas) (TCP port your-docker-host:2283)
```

Only containers with a port **published on all interfaces** count. Something bound to `127.0.0.1` isn't reachable from Kuma, so it's skipped. So is the `uptime-kuma` container itself, because Kuma watching itself proves nothing.

## 6. Propose, then approve ✅

```bash
kuma-discover scan                          # records proposals as "pending", adds nothing
kuma-discover review                        # y / n / skip each one at the terminal
# or by key:
kuma-discover approve docker:nas:immich
kuma-discover reject docker:nas:test-thing  # never proposed again
kuma-discover list
```

On approve, it checks once more that nothing already watches the service, adds the monitor (ping, TCP port or HTTP, every 60 seconds, 2 retries), then reads the monitor back from Kuma before saying it's done.

## 7. Run it every day

kuma-discover only proposes. Running `scan` daily is safe, because it never re-asks about something pending, approved or rejected. With cron:

```cron
30 9 * * * KUMA_DISCOVER_CONFIG=/home/you/.config/kuma-discover/kuma-discover.toml KUMA_USERNAME=admin KUMA_PASSWORD=... /home/you/.local/bin/kuma-discover scan >> /home/you/kuma-discover.log 2>&1
```

Then run `kuma-discover review` whenever you like. If you'd rather approve from your phone, use Discord (step 8).

## 8. Optional extras (not CI-tested)

**Proxmox.** Running LXC containers with an IP get a ping monitor. kuma-discover sends a short read-only script over SSH (`pct list`, `pct config`, `pct exec … hostname -I`), so nothing is installed on the node. That needs root SSH to the node.

```toml
[[proxmox_nodes]]
ssh = "root@your-proxmox"
ip_prefix = "192.0.2."     # optional: only CTs on this subnet
```

**Cloudflare Tunnel.** Every public hostname in your tunnels' ingress rules gets an HTTPS monitor. It also counts a container as watched when its tunnel hostname is already monitored. Create an API token with **Account → Cloudflare Tunnel → Read** only, then:

```toml
[cloudflare]
enabled = true
account_id = "your-account-id"
```

```bash
export CF_API_TOKEN='your-read-only-token'
```

**Discord approvals.** Each proposal is posted to a channel with ✅ and ❌ already added, and only the owner's reaction counts. You need a bot in your server with permission to post and add reactions in that channel.

```toml
[discord]
enabled = true
channel_id = "123456789012345678"
owner_id = "your-discord-user-id"
```

```bash
export DISCORD_BOT_TOKEN='your-bot-token'
kuma-discover scan        # posts proposals to Discord
kuma-discover poll        # run every ~10 minutes: acts on your reactions, expires unanswered after 14 days
```

## Common errors

| What you see | Why | Fix |
|---|---|---|
| `set KUMA_PASSWORD` | Secret not in the environment | Export `KUMA_USERNAME` and `KUMA_PASSWORD` (or put them in your cron line / systemd `Environment=`) |
| `socketio.exceptions.TimeoutError` on login | Kuma was busy or restarting. kuma-discover already retries 3 times | Run it again. If it keeps happening, check `url`: it must be the Kuma web address, including `http://` and the port |
| `Incorrect username or password` | Wrong Kuma login, or 2FA is on for that user | Use a Kuma user without 2FA, or turn 2FA off for this account |
| `docker nas: Permission denied (publickey…) — skipped` | SSH key not installed | Step 3 |
| `docker nas: … permission denied … docker.sock` | The SSH user can't use Docker | `sudo usermod -aG docker you` on the host, then log out and in |
| `FAILED docker:nas:x: … conditions …` | A Kuma 2.x change that uptime-kuma-api doesn't know about yet | Already handled in `kuma.py`. If it still fails on a newer Kuma, open an issue with your Kuma version |
| Nothing proposed for a container you expected | Its port isn't published, or is bound to `127.0.0.1` only | Check `docker ps`: it needs `0.0.0.0:PORT->` |

## Updating

```bash
pipx upgrade kuma-discover
```

The ledger (`~/.local/state/kuma-discover/ledger.json` by default) is kept, so past decisions stick.

## Uninstalling

```bash
pipx uninstall kuma-discover
rm -rf ~/.local/state/kuma-discover ~/.config/kuma-discover
```

Monitors it added stay in Kuma. They're ordinary monitors; delete them in the Kuma UI if you want them gone.
