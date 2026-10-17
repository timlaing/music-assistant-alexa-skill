import os
import signal


def _forward_signal_to_proc(proc, sig):
    try:
        pid = getattr(proc, "pid", None)
        if not pid:
            return
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, sig)
        except Exception:
            os.kill(pid, sig)
    except Exception:
        pass


def _terminate_proc(proc):
    """Best-effort terminate, then kill, a still-running child."""
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _shutdown_one(proc, signum):
    """Signal one child and wait briefly for a graceful exit."""
    if not proc or getattr(proc, "poll", lambda: 1)() is not None:
        return
    try:
        _forward_signal_to_proc(proc, signum)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        _terminate_proc(proc)


def _close_master_fd(procs_info):
    master_fd = procs_info.get("master_fd")
    if not master_fd:
        return
    try:
        os.close(master_fd)
    except Exception:
        pass


def _shutdown_children(get_procs, signum, _frame):
    try:
        procs_info = get_procs() or {}
    except Exception:
        # Best-effort: a failing procs provider must not break shutdown.
        return
    for name in ("_setup_auth_proc", "_setup_proc"):
        try:
            _shutdown_one(procs_info.get(name), signum)
        except Exception:
            # ignore errors per best-effort shutdown
            pass
    # Close pty master fd if provided
    _close_master_fd(procs_info)


def register_signal_handlers(get_procs_callable):
    try:
        signal.signal(
            signal.SIGINT, lambda s, f: _shutdown_children(get_procs_callable, s, f)
        )
        signal.signal(
            signal.SIGTERM, lambda s, f: _shutdown_children(get_procs_callable, s, f)
        )
    except Exception:
        # Best-effort: some environments may not allow signal registration
        pass
