from dataclasses import dataclass

import pytest

import macblock.daemon as daemon
from macblock.errors import MacblockError
from macblock.exec import RunResult
from macblock.state import State, load_state


@dataclass
class _ServiceInfo:
    name: str
    device: str | None = None


def test_marker_files_written_atomically(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "daemon.pid"
    ready_path = tmp_path / "daemon.ready"
    last_apply_path = tmp_path / "daemon.last_apply"

    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_PID", pid_path)
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_READY", ready_path)
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_LAST_APPLY", last_apply_path)

    monkeypatch.setattr(daemon.os, "getpid", lambda: 1234)
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)

    calls: list[tuple[object, object, object]] = []

    def _fake_atomic_write_text(path, text, mode=None):
        calls.append((path, text, mode))

    monkeypatch.setattr(daemon, "atomic_write_text", _fake_atomic_write_text)

    daemon._write_pid_file()
    daemon._write_ready_file()
    daemon._write_last_apply_file()

    assert calls == [
        (pid_path, "1234\n", 0o644),
        (ready_path, "1000\n", 0o644),
        (last_apply_path, "1000\n", 0o644),
    ]


def test_seconds_until_resume_none_when_disabled(monkeypatch: pytest.MonkeyPatch):
    st = State(
        schema_version=2,
        enabled=False,
        resume_at_epoch=None,
        blocklist_source=None,
        dns_backup={},
        managed_services=[],
    )
    assert daemon._seconds_until_resume(st) is None


def test_seconds_until_resume_counts_down(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)
    st = State(
        schema_version=2,
        enabled=True,
        resume_at_epoch=1030,
        blocklist_source=None,
        dns_backup={},
        managed_services=[],
    )
    assert daemon._seconds_until_resume(st) == 30.0


def test_update_upstreams_writes_defaults_and_per_domain(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    upstream_conf = tmp_path / "upstream.conf"
    upstream_info = tmp_path / "upstream.info.json"
    monkeypatch.setattr(daemon, "VAR_DB_UPSTREAM_CONF", upstream_conf)
    monkeypatch.setattr(daemon, "VAR_DB_UPSTREAM_INFO", upstream_info)
    monkeypatch.setattr(daemon, "_load_exclude_services", lambda: set())

    monkeypatch.setattr(
        daemon,
        "_collect_upstream_defaults",
        lambda _state, _exc: daemon.UpstreamDefaultsPlan(
            defaults=["1.1.1.1"],
            source="scutil",
            default_route_interface=None,
            fallbacks=["1.1.1.1", "1.0.0.1"],
            resolvers=daemon.Resolvers(
                defaults=["9.9.9.9"],
                per_domain={"corp.example": ["10.0.0.1", "127.0.0.1"]},
            ),
        ),
    )

    st = State(
        schema_version=2,
        enabled=False,
        resume_at_epoch=None,
        blocklist_source=None,
        dns_backup={},
        managed_services=[],
    )

    changed = daemon._update_upstreams(st)
    assert changed is True

    text = upstream_conf.read_text(encoding="utf-8")
    assert "server=1.1.1.1\n" in text
    assert "server=/corp.example/10.0.0.1\n" in text
    assert "127.0.0.1" not in text

    assert (upstream_conf.stat().st_mode & 0o777) == 0o644


def test_apply_state_enables_blocking_and_persists_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    state_file = tmp_path / "state.json"
    upstream_conf = tmp_path / "upstream.conf"

    monkeypatch.setattr(daemon, "SYSTEM_STATE_FILE", state_file)
    monkeypatch.setattr(daemon, "VAR_DB_UPSTREAM_CONF", upstream_conf)

    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)

    monkeypatch.setattr(
        daemon,
        "compute_managed_services",
        lambda exclude=None: [_ServiceInfo("Wi-Fi", "en0")],
    )
    dns_by_service = {"Wi-Fi": ["8.8.8.8"]}

    def _get_dns(service: str):
        return dns_by_service.get(service, [])

    monkeypatch.setattr(daemon, "get_dns_servers", _get_dns)
    monkeypatch.setattr(daemon, "get_search_domains", lambda _svc: ["corp"])
    monkeypatch.setattr(daemon, "read_dhcp_nameservers", lambda _dev: ["1.1.1.1"])

    set_calls = []

    def _set_dns(service: str, servers):
        dns_by_service[service] = list(servers) if servers is not None else []
        set_calls.append((service, servers))
        return True

    monkeypatch.setattr(daemon, "set_dns_servers", _set_dns)

    monkeypatch.setattr(daemon, "_update_upstreams", lambda _state: False)
    monkeypatch.setattr(daemon, "_hup_dnsmasq", lambda: True)

    run_calls: list[tuple[list[str], float | None]] = []

    def _run(cmd: list[str], *, timeout: float | None = 10.0) -> RunResult:
        run_calls.append((cmd, timeout))
        return RunResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "run", _run)

    daemon.save_state_atomic(
        state_file,
        State(
            schema_version=2,
            enabled=True,
            resume_at_epoch=None,
            blocklist_source=None,
            dns_backup={},
            managed_services=[],
        ),
    )

    ok, issues = daemon._apply_state()
    assert ok is True
    assert issues == []
    assert set_calls == [("Wi-Fi", ["127.0.0.1"])]

    assert [cmd for cmd, _timeout in run_calls] == [
        ["/usr/bin/dscacheutil", "-flushcache"],
        ["/usr/bin/killall", "-HUP", "mDNSResponder"],
    ]
    assert [timeout for _cmd, timeout in run_calls] == [5.0, 5.0]

    st2 = load_state(state_file)
    assert st2.managed_services == ["Wi-Fi"]
    assert "Wi-Fi" in st2.dns_backup


def test_apply_state_paused_restores_dns(tmp_path, monkeypatch: pytest.MonkeyPatch):
    state_file = tmp_path / "state.json"
    upstream_conf = tmp_path / "upstream.conf"

    monkeypatch.setattr(daemon, "SYSTEM_STATE_FILE", state_file)
    monkeypatch.setattr(daemon, "VAR_DB_UPSTREAM_CONF", upstream_conf)
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)

    monkeypatch.setattr(
        daemon,
        "compute_managed_services",
        lambda exclude=None: [_ServiceInfo("Wi-Fi", "en0")],
    )

    dns_by_service = {"Wi-Fi": ["127.0.0.1"]}

    def _get_dns(service: str):
        return dns_by_service.get(service, [])

    monkeypatch.setattr(daemon, "get_dns_servers", _get_dns)

    restore_calls = []

    def _set_dns(service: str, servers):
        dns_by_service[service] = list(servers) if servers is not None else []
        restore_calls.append((service, servers))
        return True

    def _set_search(service: str, search):
        restore_calls.append((service, search))
        return True

    monkeypatch.setattr(daemon, "set_dns_servers", _set_dns)
    monkeypatch.setattr(daemon, "set_search_domains", _set_search)
    monkeypatch.setattr(daemon, "_update_upstreams", lambda _state: False)
    monkeypatch.setattr(daemon, "_hup_dnsmasq", lambda: True)

    run_calls: list[tuple[list[str], float | None]] = []

    def _run(cmd: list[str], *, timeout: float | None = 10.0) -> RunResult:
        run_calls.append((cmd, timeout))
        return RunResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon, "run", _run)

    daemon.save_state_atomic(
        state_file,
        State(
            schema_version=2,
            enabled=True,
            resume_at_epoch=2000,
            blocklist_source=None,
            dns_backup={
                "Wi-Fi": {"dns": ["9.9.9.9"], "search": ["corp"], "dhcp": None}
            },
            managed_services=["Wi-Fi"],
        ),
    )

    ok, issues = daemon._apply_state()
    assert ok is True
    assert issues == []
    assert ("Wi-Fi", ["9.9.9.9"]) in restore_calls
    assert ("Wi-Fi", ["corp"]) in restore_calls

    assert [cmd for cmd, _timeout in run_calls] == [
        ["/usr/bin/dscacheutil", "-flushcache"],
        ["/usr/bin/killall", "-HUP", "mDNSResponder"],
    ]
    assert [timeout for _cmd, timeout in run_calls] == [5.0, 5.0]


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _mk_state(*, enabled: bool, resume_at_epoch: int | None) -> State:
    return State(
        schema_version=2,
        enabled=enabled,
        resume_at_epoch=resume_at_epoch,
        blocklist_source=None,
        dns_backup={},
        managed_services=[],
    )


def test_should_wait_for_network_before_apply(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(daemon.time, "time", lambda: 1000.0)

    assert (
        daemon._should_wait_for_network_before_apply(
            _mk_state(enabled=False, resume_at_epoch=None)
        )
        is False
    )
    assert (
        daemon._should_wait_for_network_before_apply(
            _mk_state(enabled=True, resume_at_epoch=None)
        )
        is True
    )
    assert (
        daemon._should_wait_for_network_before_apply(
            _mk_state(enabled=True, resume_at_epoch=2000)
        )
        is False
    )
    assert (
        daemon._should_wait_for_network_before_apply(
            _mk_state(enabled=True, resume_at_epoch=900)
        )
        is True
    )


def test_wait_for_network_ready_waits_until_route_and_ip(
    monkeypatch: pytest.MonkeyPatch,
):
    clock = _FakeClock()
    monkeypatch.setattr(daemon.time, "time", clock.time)
    monkeypatch.setattr(daemon.time, "sleep", clock.sleep)
    monkeypatch.setattr(daemon, "_shutdown_requested", False)
    monkeypatch.setattr(daemon, "_trigger_apply", False)

    def _run(cmd: list[str], *, timeout: float | None = 10.0) -> RunResult:
        if cmd == ["/sbin/route", "-n", "get", "default"]:
            if clock.now < 1.0:
                return RunResult(returncode=1, stdout="", stderr="not in table")
            return RunResult(returncode=0, stdout="interface: en0\n", stderr="")

        if cmd == ["/usr/sbin/ipconfig", "getifaddr", "en0"]:
            if clock.now < 1.0:
                return RunResult(returncode=1, stdout="", stderr="")
            return RunResult(returncode=0, stdout="192.168.0.10\n", stderr="")

        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(daemon, "run", _run)

    ok = daemon._wait_for_network_ready(15.0)
    assert ok is True
    assert clock.now >= 1.0


def test_wait_for_network_ready_ipv6_only(monkeypatch: pytest.MonkeyPatch):
    clock = _FakeClock()
    monkeypatch.setattr(daemon.time, "time", clock.time)
    monkeypatch.setattr(daemon.time, "sleep", clock.sleep)
    monkeypatch.setattr(daemon, "_shutdown_requested", False)
    monkeypatch.setattr(daemon, "_trigger_apply", False)

    def _run(cmd: list[str], *, timeout: float | None = 10.0) -> RunResult:
        if cmd == ["/sbin/route", "-n", "get", "default"]:
            if clock.now < 1.0:
                return RunResult(returncode=1, stdout="", stderr="not in table")
            return RunResult(returncode=0, stdout="interface: en0\n", stderr="")

        if cmd == ["/usr/sbin/ipconfig", "getifaddr", "en0"]:
            return RunResult(returncode=1, stdout="", stderr="")

        if cmd == ["/usr/sbin/ipconfig", "getv6ifaddr", "en0"]:
            if clock.now < 1.0:
                return RunResult(returncode=1, stdout="", stderr="")
            return RunResult(returncode=0, stdout="fe80::1234%en0\n", stderr="")

        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(daemon, "run", _run)

    ok = daemon._wait_for_network_ready(15.0)
    assert ok is True
    assert clock.now >= 1.0


def _patch_run_daemon_harness(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_PID", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_READY", tmp_path / "daemon.ready")
    monkeypatch.setattr(
        daemon, "VAR_DB_DAEMON_LAST_APPLY", tmp_path / "daemon.last_apply"
    )
    monkeypatch.setattr(daemon, "SYSTEM_STATE_FILE", tmp_path / "state.json")

    monkeypatch.setattr(daemon.signal, "signal", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "_check_stale_daemon", lambda: False)
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)


def test_run_daemon_defers_apply_until_network_ready(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _patch_run_daemon_harness(tmp_path, monkeypatch)

    state = _mk_state(enabled=True, resume_at_epoch=None)
    monkeypatch.setattr(daemon, "load_state", lambda _p: state)

    wait_ready_calls = {"count": 0}

    def _wait_for_network_ready(_timeout: float) -> bool:
        wait_ready_calls["count"] += 1
        return False

    monkeypatch.setattr(daemon, "_wait_for_network_ready", _wait_for_network_ready)
    monkeypatch.setattr(
        daemon,
        "_network_ready",
        lambda: (False, "no default route", None, None),
    )

    apply_calls = {"count": 0}

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        apply_calls["count"] += 1
        return True, []

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)

    wait_calls: list[float | None] = []

    def _wait_for_network_change_or_signal(timeout: float | None) -> tuple[str, int]:
        wait_calls.append(timeout)
        daemon._shutdown_requested = True
        return "shutdown", 0

    monkeypatch.setattr(
        daemon,
        "_wait_for_network_change_or_signal",
        _wait_for_network_change_or_signal,
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()

    assert rc == 0
    assert wait_ready_calls["count"] >= 1
    assert apply_calls["count"] == 0
    assert wait_calls


def test_run_daemon_applies_once_after_becoming_ready(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _patch_run_daemon_harness(tmp_path, monkeypatch)

    state = _mk_state(enabled=True, resume_at_epoch=None)
    monkeypatch.setattr(daemon, "load_state", lambda _p: state)

    events: list[str] = []
    wait_ready_results = [False, True]

    def _wait_for_network_ready(_timeout: float) -> bool:
        events.append("wait_ready")
        if wait_ready_results:
            return wait_ready_results.pop(0)
        return True

    monkeypatch.setattr(daemon, "_wait_for_network_ready", _wait_for_network_ready)
    network_ready_results = [
        (False, "no default route", None, None),
        (True, "", "en0", "192.168.0.2"),
    ]

    def _network_ready() -> tuple[bool, str, str | None, str | None]:
        if network_ready_results:
            return network_ready_results.pop(0)
        return True, "", "en0", "192.168.0.2"

    monkeypatch.setattr(daemon, "_network_ready", _network_ready)

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        events.append("apply")
        daemon._shutdown_requested = True
        return True, []

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)

    def _wait_for_network_change_or_signal(timeout: float | None) -> tuple[str, int]:
        events.append("wait_change")
        return "timeout", 0

    monkeypatch.setattr(
        daemon,
        "_wait_for_network_change_or_signal",
        _wait_for_network_change_or_signal,
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()

    assert rc == 0
    assert events == ["wait_ready", "wait_change", "wait_ready", "apply"]


def test_run_daemon_applies_without_deferring_when_network_recovers_during_gate_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _patch_run_daemon_harness(tmp_path, monkeypatch)

    state = _mk_state(enabled=True, resume_at_epoch=None)
    monkeypatch.setattr(daemon, "load_state", lambda _p: state)
    monkeypatch.setattr(daemon, "_wait_for_network_ready", lambda _timeout: False)
    monkeypatch.setattr(
        daemon,
        "_network_ready",
        lambda: (True, "", "en0", "192.168.0.2"),
    )

    apply_calls = {"count": 0}

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        apply_calls["count"] += 1
        daemon._shutdown_requested = True
        return True, []

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)
    monkeypatch.setattr(
        daemon,
        "_wait_for_network_change_or_signal",
        lambda _timeout: (_ for _ in ()).throw(
            AssertionError("unexpected defer wait before apply")
        ),
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()

    assert rc == 0
    assert apply_calls["count"] == 1


def test_run_daemon_sigusr1_interrupt_ready_rechecks_and_applies(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _patch_run_daemon_harness(tmp_path, monkeypatch)

    state = _mk_state(enabled=True, resume_at_epoch=None)
    monkeypatch.setattr(daemon, "load_state", lambda _p: state)

    def _wait_for_network_ready(_timeout: float) -> bool:
        daemon._trigger_apply = True
        return False

    monkeypatch.setattr(daemon, "_wait_for_network_ready", _wait_for_network_ready)

    network_ready_calls = {"count": 0}

    def _network_ready() -> tuple[bool, str, str | None, str | None]:
        network_ready_calls["count"] += 1
        return True, "", "en0", "192.168.0.2"

    monkeypatch.setattr(daemon, "_network_ready", _network_ready)

    apply_calls = {"count": 0}

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        apply_calls["count"] += 1
        daemon._shutdown_requested = True
        return True, []

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)

    monkeypatch.setattr(
        daemon,
        "_wait_for_network_change_or_signal",
        lambda _timeout: (_ for _ in ()).throw(
            AssertionError("unexpected wait before apply")
        ),
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()

    assert rc == 0
    assert network_ready_calls["count"] >= 1
    assert apply_calls["count"] == 1


def test_run_daemon_sigusr1_state_flip_to_disabled_applies_restore_even_if_unready(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _patch_run_daemon_harness(tmp_path, monkeypatch)

    enabled_state = _mk_state(enabled=True, resume_at_epoch=None)
    disabled_state = _mk_state(enabled=False, resume_at_epoch=None)

    load_calls = {"count": 0}

    def _load_state(_p):
        load_calls["count"] += 1
        if load_calls["count"] == 1:
            return enabled_state
        return disabled_state

    monkeypatch.setattr(daemon, "load_state", _load_state)

    def _wait_for_network_ready(_timeout: float) -> bool:
        daemon._trigger_apply = True
        return False

    monkeypatch.setattr(daemon, "_wait_for_network_ready", _wait_for_network_ready)
    monkeypatch.setattr(
        daemon,
        "_network_ready",
        lambda: (False, "no default route", None, None),
    )

    apply_calls = {"count": 0}

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        apply_calls["count"] += 1
        daemon._shutdown_requested = True
        return True, []

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)

    monkeypatch.setattr(
        daemon,
        "_wait_for_network_change_or_signal",
        lambda _timeout: (_ for _ in ()).throw(
            AssertionError("unexpected wait before disabled/paused restore apply")
        ),
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()

    assert rc == 0
    assert load_calls["count"] >= 2
    assert apply_calls["count"] == 1


def test_run_daemon_coalesces_notify_burst_before_single_apply(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _patch_run_daemon_harness(tmp_path, monkeypatch)

    state = _mk_state(enabled=False, resume_at_epoch=None)
    monkeypatch.setattr(daemon, "load_state", lambda _p: state)

    apply_reasons: list[str] = []

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        apply_reasons.append(reason)
        if len(apply_reasons) >= 2:
            daemon._shutdown_requested = True
        return True, []

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)

    wait_calls: list[float | None] = []
    responses = [
        ("notify", 0),
        ("notify", 0),
        ("timeout", None),
    ]

    def _wait_for_network_change_or_signal(
        timeout: float | None,
    ) -> tuple[str, int | None]:
        wait_calls.append(timeout)
        if not responses:
            raise AssertionError("unexpected extra wait")
        return responses.pop(0)

    monkeypatch.setattr(
        daemon,
        "_wait_for_network_change_or_signal",
        _wait_for_network_change_or_signal,
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()

    assert rc == 0
    assert apply_reasons == ["startup", "notify"]
    assert len(wait_calls) == 3


def test_run_daemon_exits_after_consecutive_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_PID", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_READY", tmp_path / "daemon.ready")
    monkeypatch.setattr(
        daemon, "VAR_DB_DAEMON_LAST_APPLY", tmp_path / "daemon.last_apply"
    )
    monkeypatch.setattr(daemon, "SYSTEM_STATE_FILE", tmp_path / "state.json")

    monkeypatch.setattr(daemon, "_check_stale_daemon", lambda: False)
    monkeypatch.setattr(
        daemon, "_should_wait_for_network_before_apply", lambda _st: False
    )
    monkeypatch.setattr(daemon, "_wait_for_network_ready", lambda _t: False)

    monkeypatch.setattr(daemon.signal, "signal", lambda *_a, **_k: None)

    st = State(
        schema_version=2,
        enabled=False,
        resume_at_epoch=None,
        blocklist_source=None,
        dns_backup={},
        managed_services=[],
    )
    monkeypatch.setattr(daemon, "load_state", lambda _p: st)

    apply_calls = {"count": 0}

    def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
        apply_calls["count"] += 1
        return False, ["issue"]

    monkeypatch.setattr(daemon, "_apply_state", _apply_state)

    monkeypatch.setattr(daemon, "_seconds_until_resume", lambda _st: 0.0)
    monkeypatch.setattr(
        daemon, "_wait_for_network_change_or_signal", lambda _t: ("timeout", 0)
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()
    assert rc == 1
    assert apply_calls["count"] == 5


def test_run_daemon_state_load_error_returns_nonzero_and_cleans_up(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    pid_file = tmp_path / "daemon.pid"
    ready_file = tmp_path / "daemon.ready"
    last_apply_file = tmp_path / "daemon.last_apply"

    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_PID", pid_file)
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_READY", ready_file)
    monkeypatch.setattr(daemon, "VAR_DB_DAEMON_LAST_APPLY", last_apply_file)
    monkeypatch.setattr(daemon, "SYSTEM_STATE_FILE", tmp_path / "state.json")

    monkeypatch.setattr(daemon, "_check_stale_daemon", lambda: False)
    monkeypatch.setattr(daemon.signal, "signal", lambda *_a, **_k: None)
    monkeypatch.setattr(
        daemon, "_should_wait_for_network_before_apply", lambda _st: False
    )

    def _raise(_p):
        raise MacblockError("corrupt")

    monkeypatch.setattr(daemon, "load_state", _raise)

    monkeypatch.setattr(daemon, "_apply_state", lambda **_k: (False, ["issue"]))
    monkeypatch.setattr(
        daemon, "_wait_for_network_change_or_signal", lambda _t: ("timeout", 0)
    )

    daemon._shutdown_requested = False
    daemon._trigger_apply = False

    rc = daemon.run_daemon()
    assert rc == 1
    assert not pid_file.exists()
    assert not ready_file.exists()


def test_wait_for_network_ready_times_out(monkeypatch: pytest.MonkeyPatch):
    clock = _FakeClock()
    monkeypatch.setattr(daemon.time, "time", clock.time)
    monkeypatch.setattr(daemon.time, "sleep", clock.sleep)
    monkeypatch.setattr(daemon, "_shutdown_requested", False)
    monkeypatch.setattr(daemon, "_trigger_apply", False)

    def _run(cmd: list[str], *, timeout: float | None = 10.0) -> RunResult:
        return RunResult(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(daemon, "run", _run)

    ok = daemon._wait_for_network_ready(15.0)
    assert ok is False
    assert clock.now >= 15.0
