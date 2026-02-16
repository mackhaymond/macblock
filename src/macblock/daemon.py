"""
Daemon entry point for launchd. Run with: macblock daemon
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from macblock.constants import (
    DEFAULT_UPSTREAM_FALLBACKS,
    SYSTEM_DNS_EXCLUDE_SERVICES_FILE,
    SYSTEM_STATE_FILE,
    SYSTEM_UPSTREAM_FALLBACKS_FILE,
    VAR_DB_DAEMON_PID,
    VAR_DB_DAEMON_READY,
    VAR_DB_DAEMON_LAST_APPLY,
    VAR_DB_DNSMASQ_PID,
    VAR_DB_UPSTREAM_CONF,
    VAR_DB_UPSTREAM_INFO,
)
from macblock.errors import MacblockError
from macblock.exec import run
from macblock.fs import atomic_write_text
from macblock.resolvers import (
    Resolvers,
    ensure_fallback_upstreams_file,
    read_system_resolvers,
)
from macblock.state import State, load_state, replace_state, save_state_atomic
from macblock.system_dns import (
    compute_managed_services,
    get_dns_servers,
    get_search_domains,
    set_dns_servers,
    set_search_domains,
    parse_exclude_services_file,
    read_dhcp_nameservers,
)


_trigger_apply = False
_shutdown_requested = False


def _log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", file=sys.stderr, flush=True)


def _write_last_apply_file() -> None:
    atomic_write_text(VAR_DB_DAEMON_LAST_APPLY, f"{int(time.time())}\n", mode=0o644)


def _handle_sigusr1(signum: int, frame: object) -> None:
    global _trigger_apply
    _trigger_apply = True


def _handle_sigterm(signum: int, frame: object) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def _consume_trigger_apply() -> bool:
    global _trigger_apply
    was_triggered = _trigger_apply
    _trigger_apply = False
    return was_triggered


def _is_forward_ip(ip: str) -> bool:
    if not ip:
        return False
    if ip in {"127.0.0.1", "::1", "0.0.0.0", "::"}:
        return False
    return True


def _is_process_running(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid_file(path) -> int | None:
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        return pid if pid > 1 else None
    except Exception:
        return None


_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_IPV6_RE = re.compile(r"^(?=.*:)[0-9A-Fa-f:]+(%[0-9A-Za-z._-]+)?$")


def _get_default_route_interface() -> str | None:
    r = run(["/sbin/route", "-n", "get", "default"], timeout=2.0)
    if r.returncode != 0:
        return None

    for raw in r.stdout.splitlines():
        line = raw.strip()
        if not line.startswith("interface:"):
            continue
        interface = line.split(":", 1)[1].strip()
        return interface or None

    return None


def _get_interface_ipv4(interface: str) -> str | None:
    if not interface:
        return None

    r = run(["/usr/sbin/ipconfig", "getifaddr", interface], timeout=2.0)
    if r.returncode != 0:
        return None

    ip = (r.stdout or "").strip()
    return ip if _IPV4_RE.match(ip) else None


def _get_interface_ipv6(interface: str) -> str | None:
    if not interface:
        return None

    r = run(["/usr/sbin/ipconfig", "getv6ifaddr", interface], timeout=2.0)
    if r.returncode != 0:
        return None

    ip = (r.stdout or "").strip()
    return ip if _IPV6_RE.match(ip) else None


def _network_ready() -> tuple[bool, str, str | None, str | None]:
    interface = _get_default_route_interface()
    if not interface:
        return False, "no default route", None, None

    ip = _get_interface_ipv4(interface)
    if not ip:
        ip = _get_interface_ipv6(interface)

    if not ip:
        return False, f"no IP for {interface}", interface, None

    return True, "", interface, ip


def _wait_for_network_ready(max_wait_s: float = 15.0) -> bool:
    start = time.time()
    stable_good: tuple[str, str] | None = None
    stable_count = 0

    logged_start = False
    last_reason: str | None = None

    while True:
        now = time.time()
        elapsed = now - start

        if _shutdown_requested or _trigger_apply:
            return False

        if elapsed >= max_wait_s:
            if logged_start:
                print(
                    f"network not ready after {max_wait_s:.0f}s; deferring apply",
                    file=sys.stderr,
                )
            return False

        ready, reason, interface, ip = _network_ready()
        if ready and interface and ip:
            current = (interface, ip)
            if current == stable_good:
                stable_count += 1
            else:
                stable_good = current
                stable_count = 1

            if stable_count >= 2:
                if elapsed >= 0.25:
                    print(
                        f"network ready after {elapsed:.1f}s ({interface} {ip})",
                        file=sys.stderr,
                    )
                return True

        else:
            stable_good = None
            stable_count = 0

            if not logged_start:
                print(
                    f"waiting for network ready (max {max_wait_s:.0f}s): {reason}",
                    file=sys.stderr,
                )
                logged_start = True
                last_reason = reason
            elif reason != last_reason and elapsed >= 1.0:
                print(f"waiting for network ready: {reason}", file=sys.stderr)
                last_reason = reason

        time.sleep(0.25)


def _hup_dnsmasq() -> bool:
    pid = _read_pid_file(VAR_DB_DNSMASQ_PID)
    if pid is None:
        return False

    if not _is_process_running(pid):
        print(
            f"dnsmasq pid {pid} not running, removing stale pid file", file=sys.stderr
        )
        try:
            VAR_DB_DNSMASQ_PID.unlink()
        except Exception:
            pass
        return False

    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except (ProcessLookupError, PermissionError) as e:
        print(f"failed to HUP dnsmasq: {e}", file=sys.stderr)
        return False


def _flush_dns_cache_best_effort() -> None:
    for cmd in [
        ["/usr/bin/dscacheutil", "-flushcache"],
        ["/usr/bin/killall", "-HUP", "mDNSResponder"],
    ]:
        try:
            run(cmd, timeout=5.0)
        except Exception:
            continue


def _load_exclude_services() -> set[str]:
    if not SYSTEM_DNS_EXCLUDE_SERVICES_FILE.exists():
        return set()

    try:
        text = SYSTEM_DNS_EXCLUDE_SERVICES_FILE.read_text(encoding="utf-8")
        return parse_exclude_services_file(text)
    except Exception:
        return set()


@dataclass(frozen=True)
class UpstreamDefaultsPlan:
    defaults: list[str]
    source: str
    default_route_interface: str | None
    fallbacks: list[str]
    resolvers: Resolvers


def _collect_upstream_defaults(state: State, exclude: set[str]) -> UpstreamDefaultsPlan:
    fallbacks, warn = ensure_fallback_upstreams_file(
        SYSTEM_UPSTREAM_FALLBACKS_FILE, defaults=DEFAULT_UPSTREAM_FALLBACKS
    )
    if warn:
        _log(f"warning: {warn}")

    resolvers = read_system_resolvers()

    scutil_defaults: list[str] = []
    for ip in resolvers.defaults:
        if _is_forward_ip(ip) and ip not in scutil_defaults:
            scutil_defaults.append(ip)

    interface = _get_default_route_interface()
    dhcp_defaults: list[str] = []
    if interface:
        for ip in read_dhcp_nameservers(interface):
            if _is_forward_ip(ip) and ip not in dhcp_defaults:
                dhcp_defaults.append(ip)

    if dhcp_defaults:
        return UpstreamDefaultsPlan(
            defaults=dhcp_defaults,
            source="dhcp-default-route",
            default_route_interface=interface,
            fallbacks=fallbacks,
            resolvers=resolvers,
        )

    if scutil_defaults:
        return UpstreamDefaultsPlan(
            defaults=scutil_defaults,
            source="scutil",
            default_route_interface=None,
            fallbacks=fallbacks,
            resolvers=resolvers,
        )

    return UpstreamDefaultsPlan(
        defaults=fallbacks,
        source="fallbacks",
        default_route_interface=None,
        fallbacks=fallbacks,
        resolvers=resolvers,
    )


def _update_upstreams(state: State) -> bool:
    exclude = _load_exclude_services()
    plan = _collect_upstream_defaults(state, exclude)
    resolvers = plan.resolvers

    lines: list[str] = []
    for ip in plan.defaults:
        lines.append(f"server={ip}")

    for domain, ips in sorted(resolvers.per_domain.items()):
        for ip in ips:
            if _is_forward_ip(ip):
                lines.append(f"server=/{domain}/{ip}")

    conf_text = "\n".join(lines) + "\n"

    info = {
        "schema_version": 1,
        "active_defaults": plan.defaults,
        "active_source": plan.source,
        "default_route_interface": plan.default_route_interface,
        "fallbacks": plan.fallbacks,
        "fallbacks_active": plan.source == "fallbacks",
    }
    info_text = json.dumps(info, indent=2, sort_keys=True) + "\n"

    conf_changed = True
    try:
        if VAR_DB_UPSTREAM_CONF.exists():
            existing = VAR_DB_UPSTREAM_CONF.read_text(
                encoding="utf-8", errors="replace"
            )
            conf_changed = existing != conf_text
    except Exception:
        pass

    info_changed = True
    try:
        if VAR_DB_UPSTREAM_INFO.exists():
            existing_info = VAR_DB_UPSTREAM_INFO.read_text(
                encoding="utf-8", errors="replace"
            )
            info_changed = existing_info != info_text
    except Exception:
        pass

    if conf_changed:
        atomic_write_text(VAR_DB_UPSTREAM_CONF, conf_text, mode=0o644)

    if info_changed:
        atomic_write_text(VAR_DB_UPSTREAM_INFO, info_text, mode=0o644)

    return conf_changed


def _backup_service_dns(
    service: str,
    device: str | None,
    dns_backup: dict,
    *,
    current_dns: list[str] | None,
) -> None:
    if service in dns_backup:
        return

    if current_dns == ["127.0.0.1"]:
        return

    dhcp = read_dhcp_nameservers(device or "")
    dns_backup[service] = {
        "dns": current_dns,
        "search": get_search_domains(service),
        "dhcp": dhcp or None,
    }


def _enable_blocking(
    state: State, managed_infos: list
) -> tuple[State, list[str], bool]:
    dns_backup = dict(state.dns_backup)
    managed_names: list[str] = []
    failures: list[str] = []
    changed = False

    for info in managed_infos:
        managed_names.append(info.name)

        current_dns = get_dns_servers(info.name)
        _backup_service_dns(info.name, info.device, dns_backup, current_dns=current_dns)

        if current_dns == ["127.0.0.1"]:
            _log(f"apply: {info.name}: DNS already 127.0.0.1")
            continue

        if set_dns_servers(info.name, ["127.0.0.1"]):
            changed = True
            _log(f"apply: {info.name}: set DNS -> 127.0.0.1")
        else:
            failures.append(f"failed to set DNS for {info.name}")
            _log(f"apply: {info.name}: failed to set DNS -> 127.0.0.1")

    new_state = replace_state(
        state,
        dns_backup=dns_backup,
        managed_services=managed_names,
    )
    return new_state, failures, changed


def _disable_blocking(state: State, managed_names: list[str]) -> tuple[list[str], bool]:
    to_restore = state.managed_services if state.managed_services else managed_names
    failures: list[str] = []
    changed = False

    for service in to_restore:
        backup_data = state.dns_backup.get(service)
        if not isinstance(backup_data, dict):
            _log(f"apply: {service}: no DNS backup found; skipping restore")
            continue

        dns = backup_data.get("dns")
        search = backup_data.get("search")

        desired_dns = list(dns) if isinstance(dns, list) else None
        desired_search = list(search) if isinstance(search, list) else None

        current_dns = get_dns_servers(service)
        if current_dns == desired_dns:
            _log(f"apply: {service}: DNS already restored")
        else:
            if set_dns_servers(service, desired_dns):
                changed = True
                _log(f"apply: {service}: restored DNS -> {desired_dns}")
            else:
                failures.append(f"failed to restore DNS for {service}")
                _log(f"apply: {service}: failed to restore DNS -> {desired_dns}")

        current_search = get_search_domains(service)
        if current_search == desired_search:
            _log(f"apply: {service}: search domains already restored")
        else:
            if set_search_domains(service, desired_search):
                changed = True
                _log(f"apply: {service}: restored search domains -> {desired_search}")
            else:
                failures.append(f"failed to restore search domains for {service}")
                _log(
                    f"apply: {service}: failed to restore search domains -> {desired_search}"
                )

    return failures, changed


def _verify_dns_state(
    state: State, managed_infos: list, should_be_localhost: bool
) -> list[str]:
    issues: list[str] = []
    for info in managed_infos:
        current = get_dns_servers(info.name)
        if should_be_localhost:
            if current != ["127.0.0.1"]:
                issues.append(f"{info.name}: expected 127.0.0.1, got {current}")
        else:
            if current == ["127.0.0.1"]:
                issues.append(f"{info.name}: still pointing to localhost")
    return issues


def _apply_state(*, reason: str = "unknown") -> tuple[bool, list[str]]:
    started = time.time()

    state = load_state(SYSTEM_STATE_FILE)
    issues: list[str] = []

    now = int(time.time())
    paused = state.resume_at_epoch is not None and state.resume_at_epoch > now

    if state.resume_at_epoch is not None and state.resume_at_epoch <= now:
        state = replace_state(state, resume_at_epoch=None)

    exclude = _load_exclude_services()
    managed_infos = compute_managed_services(exclude=exclude)
    managed_names = [info.name for info in managed_infos]

    _log(
        f"apply start (reason={reason}) enabled={state.enabled} paused={paused} managed={len(managed_names)}"
    )
    if managed_names:
        _log("apply: managed services: " + ", ".join(managed_names))
    else:
        _log("apply: no managed services detected")

    dns_changed = False

    if state.enabled and not paused:
        _log("apply: enforcing localhost DNS (127.0.0.1)")
        state, failures, dns_changed = _enable_blocking(state, managed_infos)
        issues.extend(failures)
        should_be_localhost = True
    else:
        _log("apply: restoring DNS from backup")
        failures, dns_changed = _disable_blocking(state, managed_names)
        issues.extend(failures)
        should_be_localhost = False

    save_state_atomic(SYSTEM_STATE_FILE, state)

    upstreams_changed = _update_upstreams(state)
    if upstreams_changed:
        _log("apply: upstreams updated; reloading dnsmasq")
        if _hup_dnsmasq():
            _log("apply: dnsmasq reload signal sent")
        else:
            _log("apply: failed to reload dnsmasq")
    else:
        _log("apply: upstreams unchanged; skipping dnsmasq reload")

    time.sleep(0.1)
    verification_issues = _verify_dns_state(state, managed_infos, should_be_localhost)
    issues.extend(verification_issues)

    if verification_issues:
        for issue in verification_issues:
            _log(f"apply: verify issue: {issue}")

    elapsed = time.time() - started
    ok = len(issues) == 0
    if ok and dns_changed:
        _flush_dns_cache_best_effort()
        _log("apply: flushed DNS cache")

    if ok:
        _log(f"apply done: success ({elapsed:.2f}s)")
    else:
        _log(f"apply done: {len(issues)} issue(s) ({elapsed:.2f}s)")

    return ok, issues


def _seconds_until_resume(state: State) -> float | None:
    if not state.enabled:
        return None

    if state.resume_at_epoch is None:
        return None

    now = int(time.time())
    if state.resume_at_epoch <= now:
        return 0

    return float(state.resume_at_epoch - now)


def _should_wait_for_network_before_apply(state: State) -> bool:
    if not state.enabled:
        return False

    now = int(time.time())
    paused = state.resume_at_epoch is not None and state.resume_at_epoch > now
    return not paused


def _wait_for_network_change_or_signal(
    timeout: float | None,
) -> tuple[str, int | None]:
    """Wait for network change notification or until signaled.

    Uses Popen with a poll loop so we can check for signals.
    """
    global _trigger_apply, _shutdown_requested

    cmd = ["/usr/bin/notifyutil", "-1", "com.apple.system.config.network_change"]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        time.sleep(1.0)
        return "sleep", None

    reason: str = "timeout"
    exit_code: int | None = None

    try:
        deadline = time.time() + timeout if timeout is not None else None
        poll_interval = 0.25

        while True:
            if _shutdown_requested:
                reason = "shutdown"
                break

            if _trigger_apply:
                reason = "signal"
                break

            if deadline is not None and time.time() >= deadline:
                reason = "timeout"
                break

            ret = proc.poll()
            if ret is not None:
                reason = "notify"
                exit_code = ret
                break

            time.sleep(poll_interval)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    return reason, exit_code


def _wake_reason_from_wait(wait_reason: str, exit_code: int | None) -> str:
    if wait_reason == "shutdown":
        return "shutdown"

    if wait_reason == "signal":
        _log("wake: received SIGUSR1 trigger")
        return "signal"

    if wait_reason == "notify":
        _log(f"wake: network change notification (exit_code={exit_code})")
        return "notify"

    if wait_reason == "timeout":
        _log("wake: periodic reconcile")
        return "timeout"

    _log(f"wake: notify watcher fallback ({wait_reason})")
    return wait_reason


def _coalesce_notify_wake(wake_reason: str, window_s: float = 1.0) -> str:
    if wake_reason != "notify":
        return wake_reason

    deadline = time.time() + window_s
    while True:
        if _shutdown_requested:
            return "shutdown"

        if _consume_trigger_apply():
            _log("wake: received SIGUSR1 trigger")
            return "signal"

        remaining = deadline - time.time()
        if remaining <= 0:
            return "notify"

        wait_reason, exit_code = _wait_for_network_change_or_signal(remaining)

        if wait_reason == "notify":
            _log(
                f"wake: coalesced additional network change notification (exit_code={exit_code})"
            )
            deadline = time.time() + window_s
            continue

        if wait_reason == "timeout":
            return "notify"

        return _wake_reason_from_wait(wait_reason, exit_code)


def _write_pid_file() -> None:
    atomic_write_text(VAR_DB_DAEMON_PID, f"{os.getpid()}\n", mode=0o644)


def _write_ready_file() -> None:
    atomic_write_text(VAR_DB_DAEMON_READY, f"{int(time.time())}\n", mode=0o644)


def _remove_pid_file() -> None:
    try:
        VAR_DB_DAEMON_PID.unlink()
    except Exception:
        pass


def _remove_ready_file() -> None:
    try:
        VAR_DB_DAEMON_READY.unlink()
    except Exception:
        pass


def _check_stale_daemon() -> bool:
    pid = _read_pid_file(VAR_DB_DAEMON_PID)
    if pid is None:
        return False

    if pid == os.getpid():
        return False

    if _is_process_running(pid):
        print(f"another daemon is already running (pid={pid})", file=sys.stderr)
        return True

    print(f"removing stale pid file (pid={pid} not running)", file=sys.stderr)
    _remove_pid_file()
    _remove_ready_file()
    return False


def run_daemon() -> int:
    global _trigger_apply, _shutdown_requested

    if _check_stale_daemon():
        return 1

    signal.signal(signal.SIGUSR1, _handle_sigusr1)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    _write_pid_file()

    _log(f"macblock daemon started (pid={os.getpid()})")

    consecutive_failures = 0
    max_consecutive_failures = 5

    wake_reason = "startup"

    try:
        while not _shutdown_requested:
            _consume_trigger_apply()

            try:
                state_for_wait = load_state(SYSTEM_STATE_FILE)
            except MacblockError as e:
                _log(f"failed to load state for network wait: {e}")
                state_for_wait = State(
                    schema_version=2,
                    enabled=False,
                    resume_at_epoch=None,
                    blocklist_source=None,
                    dns_backup={},
                    managed_services=[],
                )

            if _should_wait_for_network_before_apply(state_for_wait):
                ready_for_apply = _wait_for_network_ready(15.0)
                if not ready_for_apply:
                    if _shutdown_requested:
                        wake_reason = "shutdown"
                        break

                    triggered_during_gate = _consume_trigger_apply()

                    if triggered_during_gate:
                        try:
                            state_for_wait = load_state(SYSTEM_STATE_FILE)
                        except MacblockError as e:
                            _log(f"failed to load state for network wait: {e}")
                            state_for_wait = State(
                                schema_version=2,
                                enabled=False,
                                resume_at_epoch=None,
                                blocklist_source=None,
                                dns_backup={},
                                managed_services=[],
                            )

                        if _should_wait_for_network_before_apply(state_for_wait):
                            ready_now, not_ready_reason, _interface, _ip = (
                                _network_ready()
                            )
                            if not ready_now:
                                _log(f"deferring apply: {not_ready_reason}")
                                _consume_trigger_apply()
                                wait_reason, exit_code = (
                                    _wait_for_network_change_or_signal(5.0)
                                )
                                wake_reason = _wake_reason_from_wait(
                                    wait_reason, exit_code
                                )
                                if wake_reason == "shutdown":
                                    break
                                continue

                        _consume_trigger_apply()
                    else:
                        ready_now, not_ready_reason, _interface, _ip = _network_ready()
                        if not ready_now:
                            _log(f"deferring apply: {not_ready_reason}")
                            _consume_trigger_apply()
                            wait_reason, exit_code = _wait_for_network_change_or_signal(
                                5.0
                            )
                            wake_reason = _wake_reason_from_wait(wait_reason, exit_code)
                            if wake_reason == "shutdown":
                                break
                            continue

                        _consume_trigger_apply()

            ready_now, _not_ready_reason, ready_interface, ready_ip = _network_ready()
            if ready_now and ready_interface and ready_ip:
                _log(f"network ready for apply: {ready_interface} {ready_ip}")

            try:
                success, issues = _apply_state(reason=wake_reason)
                if success:
                    consecutive_failures = 0
                    _write_last_apply_file()

                    if not VAR_DB_DAEMON_READY.exists():
                        _write_ready_file()
                        _log("daemon ready")
                else:
                    consecutive_failures += 1
                    _log(f"apply returned {len(issues)} issue(s)")

                    if consecutive_failures >= max_consecutive_failures:
                        _log(
                            f"too many consecutive failures ({consecutive_failures}), exiting to let launchd restart"
                        )
                        return 1

            except Exception as e:
                consecutive_failures += 1
                _log(f"error applying state: {e}")

                if consecutive_failures >= max_consecutive_failures:
                    _log(
                        f"too many consecutive failures ({consecutive_failures}), exiting to let launchd restart"
                    )
                    return 1

            if _shutdown_requested:
                break

            try:
                state = load_state(SYSTEM_STATE_FILE)
                timeout = _seconds_until_resume(state)
            except MacblockError as e:
                _log(f"failed to load state for resume timer: {e}")
                timeout = None

            if timeout is None:
                timeout = 300.0
            elif timeout > 60:
                timeout = 60.0

            _log(f"waiting for network changes (timeout={timeout:.0f}s)")
            _consume_trigger_apply()
            wait_reason, exit_code = _wait_for_network_change_or_signal(timeout)
            wake_reason = _wake_reason_from_wait(wait_reason, exit_code)
            wake_reason = _coalesce_notify_wake(wake_reason)

            if wake_reason == "shutdown":
                break

    except KeyboardInterrupt:
        _log("daemon interrupted")
    finally:
        _log("daemon shutting down")
        _remove_ready_file()
        _remove_pid_file()

    return 0
