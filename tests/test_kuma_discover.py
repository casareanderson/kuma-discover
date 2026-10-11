import subprocess
from types import SimpleNamespace

import pytest

from kuma_discover import cli, core

DOCKER_PS = ("web|0.0.0.0:8080->80/tcp, [::]:8080->80/tcp\n"
             "db|127.0.0.1:5432->5432/tcp\n"
             "uptime-kuma|0.0.0.0:3001->3001/tcp\n"
             "photos|0.0.0.0:2283->2283/tcp\n"
             "worker|\n")
PROXMOX = "101|dns|running|192.0.2.53\n102|old|stopped|-\n103|vpn|running|198.51.100.7\n"


class FakeKuma:
    instances = []

    def __init__(self, url, user, pw, monitors=None):
        self.monitors_list = list(monitors if monitors is not None else FakeKuma.start)
        self.added = []
        FakeKuma.instances.append(self)

    def monitors(self):
        return self.monitors_list

    def add(self, m, notification_ids, interval=60):
        self.added.append((m, notification_ids, interval))
        self.monitors_list.append(dict(m))
        return 100 + len(self.added)

    def close(self):
        pass


FakeKuma.start = [{"name": "Svc: photos", "hostname": "192.0.2.10", "port": 2283}]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("KUMA_USERNAME", "admin")
    monkeypatch.setenv("KUMA_PASSWORD", "pw")
    FakeKuma.instances.clear()


@pytest.fixture
def cfg(tmp_path):
    c = cli.load_config(None)
    c["scan"]["state_dir"] = str(tmp_path / "state")
    c["docker_hosts"] = [{"name": "nas", "ssh": "you@192.0.2.10", "address": "192.0.2.10"}]
    c["proxmox_nodes"] = [{"ssh": "root@192.0.2.20", "ip_prefix": "192.0.2."}]
    return c


@pytest.fixture
def fake_ssh(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw.get("input")))
        out = PROXMOX if argv[-2:] == ["bash", "-s"] else DOCKER_PS
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(core.subprocess, "run", run)
    return seen


def test_docker_discovery_published_tcp_only(fake_ssh):
    items = core.discover_docker({"name": "nas", "ssh": "you@192.0.2.10", "address": "192.0.2.10"}, {"uptime-kuma"})
    names = {i["name"]: i for i in items}
    assert set(names) == {"web", "photos"}               # db is loopback-only, worker has no port, kuma skipped
    assert names["web"]["ports"] == [8080] and names["web"]["monitor"]["type"] == "port"
    assert fake_ssh[0][0][-2] == "you@192.0.2.10"


def test_docker_local_runs_without_ssh(fake_ssh):
    core.discover_docker({"ssh": "local", "address": "127.0.0.1"}, set())
    assert fake_ssh[0][0][0] == "docker"


def test_proxmox_discovery_sends_script_on_stdin(fake_ssh):
    items = core.discover_proxmox({"ssh": "root@192.0.2.20", "ip_prefix": "192.0.2."})
    assert [i["name"] for i in items] == ["dns"]          # stopped and off-prefix CTs dropped
    assert "pct list" in fake_ssh[0][1]


def test_source_failure_is_skipped_not_fatal(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(core.subprocess, "run", boom)
    assert core.discover_docker({"ssh": "x@y", "address": "y"}, set()) == []


def test_cloudflare_reads_ingress_hostnames():
    class H:
        def get(self, url, **kw):
            if url.endswith("is_deleted=false"):
                return SimpleNamespace(json=lambda: {"result": [{"id": "t1"}]})
            return SimpleNamespace(json=lambda: {"result": {"config": {"ingress": [
                {"hostname": "app.example.com", "service": "http://192.0.2.10:8080"}, {"service": "http_status:404"}]}}})
    items = core.discover_cloudflare("acct", "tok", http=H())
    assert [i["name"] for i in items] == ["app.example.com"] and items[0]["monitor"]["type"] == "http"


def test_coverage_rules():
    cov = core.coverage([{"name": "Node: dns", "hostname": "192.0.2.99"},
                         {"name": "x", "url": "https://app.example.com/"},
                         {"name": "y", "hostname": "192.0.2.10", "port": 8080}])
    web = {"kind": "docker", "name": "web", "ip": "192.0.2.10", "ports": [8080]}
    assert core.covered(web, cov)                                             # same host:port
    assert core.covered({"kind": "ct", "name": "dns", "ip": "192.0.2.53"}, cov)  # same name
    assert core.covered({"kind": "host", "name": "app.example.com"}, cov)
    other = {"kind": "docker", "name": "grafana", "ip": "192.0.2.10", "ports": [3000]}
    assert not core.covered(other, cov)
    tunnel = [{"name": "app.example.com", "origin": "http://192.0.2.10:3000"}]
    assert core.covered(other, cov, tunnel)                                   # watched through its public hostname


def test_scan_proposes_gaps_and_never_adds(cfg, fake_ssh, capsys):
    assert cli.cmd_scan(cfg, SimpleNamespace(dry_run=False), kuma_cls=FakeKuma) == 0
    out = capsys.readouterr().out
    assert "PROPOSED docker:nas:web" in out and "PROPOSED ct:192.0.2.20:101" in out
    assert "photos" not in out                            # already watched
    assert FakeKuma.instances[0].added == []              # scan never adds anything
    # a second scan does not re-propose pending items
    cli.cmd_scan(cfg, SimpleNamespace(dry_run=False), kuma_cls=FakeKuma)
    assert "PROPOSED" not in capsys.readouterr().out


def test_dry_run_writes_nothing(cfg, fake_ssh, capsys):
    cli.cmd_scan(cfg, SimpleNamespace(dry_run=True), kuma_cls=FakeKuma)
    assert "DRY" in capsys.readouterr().out
    with core.ledger(cfg["scan"]["state_dir"]) as d:
        assert d["items"] == {}


def test_approve_adds_and_reject_sticks(cfg, fake_ssh, capsys):
    cli.cmd_scan(cfg, SimpleNamespace(dry_run=False), kuma_cls=FakeKuma)
    assert cli.cmd_decide(cfg, ["docker:nas:web"], True, kuma_cls=FakeKuma) == 0
    assert cli.cmd_decide(cfg, ["ct:192.0.2.20:101"], False, kuma_cls=FakeKuma) == 0
    added = [k for inst in FakeKuma.instances for k in inst.added]
    assert added[0][0]["name"] == "Svc: web (nas)"
    with core.ledger(cfg["scan"]["state_dir"]) as d:
        assert d["items"]["docker:nas:web"]["status"] == "added"
        assert d["items"]["ct:192.0.2.20:101"]["status"] == "rejected"
    capsys.readouterr()
    cli.cmd_scan(cfg, SimpleNamespace(dry_run=False), kuma_cls=FakeKuma)    # rejected never comes back
    assert "ct:192.0.2.20:101" not in capsys.readouterr().out


def test_approve_unknown_key(cfg, capsys):
    assert cli.cmd_decide(cfg, ["nope"], True, kuma_cls=FakeKuma) == 1


def test_review_interactive(cfg, fake_ssh):
    cli.cmd_scan(cfg, SimpleNamespace(dry_run=False), kuma_cls=FakeKuma)
    answers = iter(["y", "n"])
    cli.cmd_review(cfg, None, kuma_cls=FakeKuma, ask=lambda _: next(answers))
    with core.ledger(cfg["scan"]["state_dir"]) as d:
        st = sorted(i["status"] for i in d["items"].values())
    assert st == ["added", "rejected"]


class FakeDiscord:
    def __init__(self, yes=(), no=()):
        self.yes, self.no, self.posts = list(yes), list(no), []

    def reactors(self, mid, emoji):
        return self.yes if emoji == "✅" else self.no

    def post(self, content, reply_to=None):
        self.posts.append(content); return "m2"

    def react(self, *a):
        pass


def test_poll_only_owner_counts(cfg, fake_ssh):
    cli.cmd_scan(cfg, SimpleNamespace(dry_run=False), kuma_cls=FakeKuma)
    cfg["discord"].update(enabled=True, owner_id="42")
    with core.ledger(cfg["scan"]["state_dir"]) as d:
        for i in d["items"].values():
            i["message_id"] = "m1"
    cli.cmd_poll(cfg, None, kuma_cls=FakeKuma, dc=FakeDiscord(yes=["7"]))    # stranger's ✅ ignored
    with core.ledger(cfg["scan"]["state_dir"]) as d:
        assert {i["status"] for i in d["items"].values()} == {"pending"}
    dc = FakeDiscord(yes=["42"])
    cli.cmd_poll(cfg, None, kuma_cls=FakeKuma, dc=dc)
    with core.ledger(cfg["scan"]["state_dir"]) as d:
        assert {i["status"] for i in d["items"].values()} == {"added"}
    assert all(p.startswith("✅") for p in dc.posts)


def test_config_file_and_missing_env(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[kuma]\nurl = "http://kuma:3001"\n[[docker_hosts]]\nssh = "local"\naddress = "127.0.0.1"\n')
    c = cli.load_config(str(p))
    assert c["kuma"]["url"] == "http://kuma:3001" and c["kuma"]["interval"] == 60 and len(c["docker_hosts"]) == 1
    monkeypatch.delenv("KUMA_PASSWORD")
    with pytest.raises(SystemExit):
        cli.env("KUMA_PASSWORD")
    with pytest.raises(SystemExit):
        cli.load_config(str(tmp_path / "missing.toml"))


def test_kuma_login_retries_on_timeout():
    from kuma_discover.kuma import Kuma

    class TimeoutError_(Exception):
        pass
    TimeoutError_.__name__ = "TimeoutError"
    calls = []

    class Api:
        def __init__(self, url, timeout):
            pass

        def login(self, u, p):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError_()

        def disconnect(self):
            pass

    Kuma("http://k", "u", "p", api_cls=Api)
    assert len(calls) == 3
    calls.clear()

    class Bad(Api):
        def login(self, u, p):
            calls.append(1)
            raise ValueError("Incorrect username or password.")
    with pytest.raises(ValueError):
        Kuma("http://k", "u", "p", api_cls=Bad)
    assert len(calls) == 1                          # wrong password is not retried
