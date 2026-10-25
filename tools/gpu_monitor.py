#!/usr/bin/env python3
"""
gpu_monitor.py -- DGX Spark (GB10) GPU sampler, driven by gpu_monitor.timer.

One invocation = one sample:
  * util / temp / power  via `nvidia-smi --query-gpu=...`
  * GPU memory + procs   via `nvidia-smi --query-compute-apps=...`
    (GB10's memory.used query returns [N/A] -- never rely on it)
  * appends ONE compact line to ~/gpu_monitor_log.txt
  * writes ONE report file to ~/gpu_reports/
 * self-prunes ~/gpu_reports/ older than REPORT_KEEP_D days
 * samples MemAvailable / MemFree / PSI from /proc and runs the unified-memory
 * guard: when allocatable memory drops under the floor (or GPU memory passes
 * the ceiling) on two consecutive samples, SIGTERM the top GPU consumer and
 *   escalate to SIGKILL after a grace period.  See the GUARD_* block below.
 * counts NV_ERR_NO_MEMORY lines the kernel log gained since the previous
 *   sample -- literal evidence the driver is refusing allocations.  Joins
 *   the breach set with the normal two-sample confirmation, so a lone
 *   transient refusal recovers instead of killing.  Technique credited to
 *   MiaAI Lab's memwatch.sh (AGPL-3.0-or-later,
 *   github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark);
 *   reimplemented from scratch, no code taken.
    (2026-09-20 fix: old build wrote an unbounded report every run -> 1.45 M
    files / 5.7 GB; also old regexes matched the May nvidia-smi layout and
    logged only N/A ever since)

Failure mode: if every GPU query fails the sample line still lands (all N/A)
but exit code is 1 -> systemd Restart=on-failure retries in 60 s.
Never use Restart=always with a script that exits 0 immediately: it becomes
a hot self-restarting loop.

This unit uses Restart=always + RestartSec=10 to get a ~10 s sampling cadence;
gpu_monitor.timer's 30 min OnUnitInactiveSec never fires because the service
never stays inactive that long.
"""

import ctypes
import fcntl
import glob
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

HOME         = "/home/cpatriaw"
LOG_PATH     = os.path.join(HOME, "gpu_monitor_log.txt")
REPORT_DIR   = os.path.join(HOME, "gpu_reports")
REPORT_KEEP_D = 7                      # days of per-sample reports to keep

# --- unified-memory guard (2026-10-24) --------------------------------------
# GB10 has ONE memory pool: the GPU allocates from the same DRAM the CPU
# uses, and the NVIDIA driver does not charge those pages to the allocating
# process's cgroup (maya holds ~90 GiB while its cgroup reports ~4 GiB).  A
# cgroup MemoryMax therefore cannot see this coming -- only the real
# MemAvailable reading can.  When the driver runs out of pages,
# _memdescAllocInternal returns NV_ERR_NO_MEMORY and its error path
# deadlocks: the box hard-locks with nothing on the console, and since the
# kernel watchdog is disabled ("NMI not fully supported") it has to be
# power-cycled.  Cheaper to kill one server than to lose the whole uptime.
#
# MemFree is NOT a usable trigger: the kernel pins it at ~1.2 GiB by design
# and pushes everything else into page cache.  MemAvailable is the kernel's
# own estimate of what is allocatable without swapping.
#
# Two consecutive breaches inside GUARD_CONSEC_S are required, so a
# transient dip never costs a running server.  GPU_MONITOR_GUARD=0 disarms
# the whole thing without editing this file.
#
# THE SIGNAL THAT MATTERS IS DEVICE-FREE, NOT MemAvailable.  On GB10 the
# driver allocates GPU memory out of the same DRAM the CPU uses, so when the
# engine has taken 115 of 119.7 GiB the OS still reports a comfortable
# MemAvailable (~20 GiB, mostly page cache) while the driver is in fact a
# couple of GiB from NV_ERR_NO_MEMORY.  nvidia-smi and NVML both answer
# [N/A] for memory.total/used/free on this part, and the engine prints its
# own "N MiB of VRAM free" exactly once (glm_pack_generate runs a single
# time), so cudaMemGetInfo through ctypes is the only cheap live source.
# Measured: ~0.3 s and ~170 MiB of device memory per call, released at once.
#
# DEVICE_FREE_FLOOR_MIB 3072 sits above the engine's own "LOW" warning
# (256 MiB) and above the ~1021 MiB the box was at when it hard-locked.
GUARD_ENABLED         = os.environ.get("GPU_MONITOR_GUARD", "1") not in ("0", "no", "false")
DEVICE_FREE_FLOOR_MIB = float(os.environ.get("GPU_MONITOR_DEVICE_FREE_FLOOR_MIB", "3072"))
MEM_FLOOR_GIB         = float(os.environ.get("GPU_MONITOR_MEM_FLOOR_GIB", "6"))
GPU_CEIL_MIB          = float(os.environ.get("GPU_MONITOR_GPU_CEIL_MIB", "112640"))
WARN_FREE_MIB         = float(os.environ.get("GPU_MONITOR_WARN_FREE_MIB", "4096"))
WARN_FLOOR_GIB        = float(os.environ.get("GPU_MONITOR_WARN_FLOOR_GIB", "12"))
WARN_EVERY_S          = float(os.environ.get("GPU_MONITOR_WARN_EVERY_S", "300"))
GUARD_CONSEC_S        = float(os.environ.get("GPU_MONITOR_GUARD_CONSEC_S", "30"))
GUARD_GRACE_S         = float(os.environ.get("GPU_MONITOR_GUARD_GRACE_S", "30"))

# 2026-10-25: two guards against concurrent invocations (see
# docs/2026-10-25-guard-concurrency.md).  A second invocation running at the
# same time as the first used to read the first's freshly-written armed state
# as its own confirming sample, collapsing the two-sample rule into a
# one-sample kill.  (1) A whole-run exclusive flock: a concurrent invocation
# skips sampling and guard logic entirely.  (2) An armed-age floor: a
# confirming sample must observe an armed state at least this old, i.e.
# written by an EARLIER invocation, never a simultaneous one.
MIN_CONFIRM_AGE_S     = float(os.environ.get("GPU_MONITOR_MIN_CONFIRM_AGE_S", "8"))
LOCK_PATH             = os.path.join(HOME, ".gpu_monitor_guard_lock")

# 2026-10-24 additions: two more breach signals and smarter victim choice.
# PSI memory "full" avg10 is the kernel saying allocation is already
# stalling -- the earliest hard-lock warning there is, visible before any
# floor in this file trips.  Swap growth is the thrash signature that
# precedes a lock.  And an OS-side breach (low MemAvailable / high swap /
# PSI) is often caused by something that holds no GPU memory at all -- a
# runaway build, say -- in which case killing the engine is wrong: pick the
# biggest-RSS process instead.  GPU_MONITOR_PROTECT is a comma list of pids
# or /proc names the OS-side victim pick will always skip.
PSI_FULL_FLOOR        = float(os.environ.get("GPU_MONITOR_PSI_FULL_FLOOR", "5.0"))
SWAP_FLOOR_GIB        = float(os.environ.get("GPU_MONITOR_SWAP_FLOOR_GIB", "8"))
OS_VICTIM_BY_RSS      = os.environ.get("GPU_MONITOR_OS_VICTIM", "1") not in ("0", "no", "false")

# 2026-10-25: RSS-growth attribution for device-free breaches.  On GB10 the
# device pool IS system RAM, so a process that holds no GPU memory at all --
# an unwrapped build, say -- can drive device_free below the floor and the
# old victim pick (top GPU consumer) would kill the engine for it.  Every
# healthy sample writes a pid->RSS baseline; on a device-free breach the
# victim is the process whose RSS grew the most since the previous sample,
# and only when nothing grew does the old top-GPU-consumer pick apply (the
# engine's own CUDA allocations never show in RSS).  RSS_GROW_MIN_KIB keeps
# page-noise from framing a bystander; RSS_BASE_MAX_AGE_S discards a stale
# baseline (monitor was down) rather than mis-attribute.
RSS_BASE_PATH         = os.path.join(HOME, ".gpu_monitor_guard_rss")
RSS_BASE_MAX_AGE_S    = float(os.environ.get("GPU_MONITOR_RSS_BASE_MAX_AGE_S", "300"))
RSS_GROW_MIN_KIB      = float(os.environ.get("GPU_MONITOR_RSS_GROW_MIN_KIB", "262144"))
_PROTECT_TOKENS       = {t.strip() for t in
                         os.environ.get("GPU_MONITOR_PROTECT", "").replace(",", " ").split()
                         if t.strip()}

STATE_PATH            = os.path.join(HOME, ".gpu_monitor_guard_state")
WARN_PATH             = os.path.join(HOME, ".gpu_monitor_guard_warned")
GUARD_LOG_PATH        = os.path.join(HOME, "gpu_monitor_guard.log")

# NV_ERR_NO_MEMORY journal watch (2026-10-25, technique credited to MiaAI
# Lab's memwatch.sh in the header): cursor file keeps the last window's
# timestamp so the one-shot process can count only NEW kernel-log lines.
NVRM_PATH             = os.path.join(HOME, ".gpu_monitor_guard_nvrm")
NVRM_JOURNAL_TIMEOUT  = 10


def _q(args, timeout=15):
    try:
        r = subprocess.run(["nvidia-smi"] + args,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        return out if out else None
    except Exception:
        return None


def gpu_row():
    """(util, temp, power) from one --query-gpu call (fields proven on GB10)."""
    out = _q(["--query-gpu=utilization.gpu,temperature.gpu,power.draw",
              "--format=csv,noheader"])
    if not out:
        return None, None, None
    parts = [p.strip() for p in out.split(",")]

    def num(i):
        if i >= len(parts):
            return None
        m = re.match(r"(\d+(?:\.\d+)?)", parts[i])
        return float(m.group(1)) if m else None

    return num(0), num(1), num(2)


def compute_apps():
    """[(pid, MiB, name)] via --query-compute-apps (GB10-safe memory source)."""
    out = _q(["--query-compute-apps=pid,used_gpu_memory,process_name",
              "--format=csv,noheader"])
    if not out:
        return []
    procs = []
    for line in out.splitlines():
        if "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        m = re.match(r"(\d+(?:\.\d+)?)", parts[1]) if len(parts) > 1 else None
        if not m:
            continue
        pid   = parts[0] if re.match(r"\d+$", parts[0]) else "?"
        mib   = float(m.group(1))
        pname = parts[2] if len(parts) > 2 else "?"
        procs.append((pid, mib, pname))
    return procs


def fmt(v, unit=""):
    if v is None:
        return "N/A"
    if float(v).is_integer():
        return f"{int(v)}{unit}"
    return f"{v}{unit}"


def prune_old_reports():
    cutoff = datetime.now().timestamp() - REPORT_KEEP_D * 86400
    n = 0
    try:
        names = os.listdir(REPORT_DIR)
    except OSError:
        return 0
    for fn in names:
        if not (fn.startswith("gpu_report_") and fn.endswith(".txt")):
            continue
        fp = os.path.join(REPORT_DIR, fn)
        try:
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                n += 1
        except OSError:
            pass
    return n


def kib(k, unit="MiB"):
    """KiB -> human string, or N/A."""
    if k is None:
        return "N/A"
    return f"{k / 1024:.1f}{unit}"


_CUDART = None
_CUDART_TRIED = False


def device_free():
    """(free MiB, total MiB) from cudaMemGetInfo, or (None, None) if unavailable.

    This is the pool the NVIDIA driver actually allocates from.  On GB10 it is
    system DRAM, so it is the number that ran out before the hard lockup - and
    the only one MemAvailable gets wrong, because MemAvailable counts the
    page cache as reclaimable while the driver could not reclaim it in time.
    """
    global _CUDART, _CUDART_TRIED
    if not _CUDART_TRIED:
        _CUDART_TRIED = True
        cands = sorted(glob.glob("/usr/local/cuda*/lib64/libcudart.so*"), reverse=True)
        cands.append("libcudart.so")
        for c in cands:
            try:
                lib = ctypes.CDLL(c)
                lib.cudaMemGetInfo.restype = ctypes.c_int
                lib.cudaMemGetInfo.argtypes = [ctypes.POINTER(ctypes.c_size_t)] * 2
                _CUDART = lib
                break
            except OSError:
                continue
    if _CUDART is None:
        return None, None
    free = ctypes.c_size_t(0)
    total = ctypes.c_size_t(0)
    if _CUDART.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)) != 0:
        return None, None
    return free.value >> 20, total.value >> 20


def meminfo():
    """{key: value_in_KiB} from /proc/meminfo."""
    d = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                d[k.strip()] = int(v.strip().split()[0])
    except (OSError, ValueError):
        pass
    return d


def psi_memory():
    """(some avg10, full avg10) memory stall percentages, or (None, None)."""
    try:
        with open("/proc/pressure/memory") as f:
            txt = f.read()
    except OSError:
        return None, None
    out = []
    for key in ("some", "full"):
        m = re.search(rf"^{key}\s+avg10=([\d.]+)", txt, re.M)
        out.append(float(m.group(1)) if m else None)
    return out[0], out[1]


def nvrm_errors():
    """Count of NV_ERR_NO_MEMORY lines the kernel log gained since the
    previous sample, or None when the journal is unavailable.

    Windows are adjacent [last, now] via epoch cursors in NVRM_PATH.
    journalctl's --since/--until are inclusive at the boundary instant, so a
    line stamped exactly at the boundary can double-count -- harmless for a
    trigger, and preferred over a since=last+eps window that could silently
    drop a line arriving within the epsilon.  The first run only
    initialises the cursor: there is no lower bound, so counting then could
    replay an old flood as a fresh breach.

    The cursor advances only AFTER a successful query: a timed-out or
    failed journalctl leaves it at `last`, so the next sample re-covers the
    whole [last, next] window and no lines are lost -- a long journal
    outage counts its lines once, on the first sample that succeeds.
    """
    try:
        with open(NVRM_PATH) as f:
            last = float(f.read().strip())
    except (OSError, ValueError):
        last = None
    now = time.time()
    if last is None:
        try:
            with open(NVRM_PATH, "w") as f:
                f.write(f"{now}")
        except OSError:
            pass
        return 0
    if last >= now:
        return 0
    try:
        r = subprocess.run(
            ["journalctl", "-k", "-q", "--no-pager",
             f"--since=@{last}", f"--until=@{now}"],
            capture_output=True, text=True, timeout=NVRM_JOURNAL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        with open(NVRM_PATH, "w") as f:
            f.write(f"{now}")
    except OSError:
        pass
    return sum(1 for ln in r.stdout.splitlines() if "NV_ERR_NO_MEMORY" in ln)


def guard_log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(GUARD_LOG_PATH, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def _state():
    try:
        with open(STATE_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def _set_state(s):
    try:
        with open(STATE_PATH, "w") as f:
            f.write(s)
    except OSError:
        pass


def _warn_due(now):
    """True at most once per WARN_EVERY_S, so a long stretch in the warning
    band does not flood the guard log (it samples every ~10 s)."""
    try:
        with open(WARN_PATH) as f:
            last = float(f.read().strip() or 0)
    except (OSError, ValueError):
        last = 0.0
    if now - last < WARN_EVERY_S:
        return False
    try:
        with open(WARN_PATH, "w") as f:
            f.write(f"{now}")
    except OSError:
        pass
    return True


def _clear_warn():
    try:
        if os.path.exists(WARN_PATH):
            os.remove(WARN_PATH)
    except OSError:
        pass


def _alive(pid):
    # 2026-10-24: a killed-but-unreaped victim is a zombie -- /proc/<pid>
    # still exists, so a pure existence check treated it as alive and the
    # guard kept "draining" and eventually SIGKILLed a process that was
    # already dead.  /proc/<pid>/stat state 'Z' is the reliable answer.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _os_victim(self_pid):
    """(pid, name, rss_kib) of the biggest-RSS process, or None.

    Used when the breach is OS-side only (nothing ran out of the GPU pool):
    the offender is then likelier to be a plain memory hog than the engine.
    Skips the monitor itself, pid 1/2 (init, kthreadd) and anything in
    GPU_MONITOR_PROTECT (pid or /proc Name).  Kernel threads report no
    VmRSS and are skipped by the same check.
    """
    best = None
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for d in entries:
        if not d.isdigit():
            continue
        pid = int(d)
        if pid in (self_pid, 1, 2):
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                txt = f.read()
        except OSError:
            continue
        m = re.search(r"^VmRSS:\s+(\d+) kB", txt, re.M)
        if not m:
            continue
        rss = int(m.group(1))
        if rss <= 0:
            continue
        nm = ""
        mm = re.search(r"^Name:\s+(.+)", txt, re.M)
        if mm:
            nm = mm.group(1).strip()
        if f"{pid}" in _PROTECT_TOKENS or nm in _PROTECT_TOKENS:
            continue
        if best is None or rss > best[2]:
            best = (pid, nm, rss)
    return best


def _signal_pid(pid, sig):
    """True if the signal was delivered (the process may still be dying)."""
    try:
        os.kill(int(pid), sig)
        return True
    except (ProcessLookupError, OSError, ValueError):
        return False


_LAST_RSS = None      # {pid: rss_kib} of this sample, written by memory_guard
_REBASE_OK = False    # True only on a fully healthy sample -> main() rebases


def _rss_map():
    """{pid: rss_kib} for every process with resident pages.

    Kernel threads report no VmRSS and are absent.  One /proc/*/status read
    each, a few ms for a few hundred processes -- negligible next to the
    nvidia-smi call in the same sample.
    """
    d = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return d
    for e in entries:
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/status") as f:
                txt = f.read()
        except OSError:
            continue
        m = re.search(r"^VmRSS:\s+(\d+) kB", txt, re.M)
        if m:
            rss = int(m.group(1))
            if rss > 0:
                d[int(e)] = rss
    return d


def _starttime(pid):
    """Clock-tick start time of pid (stat field 22), or None when it died.

    Field 22 sits after the last ')' (comm may contain them), at index 19 of
    the remaining tokens (index i is field 3+i; _alive uses the same trick).
    Constant for the process's lifetime within one boot, so a baseline entry
    whose start time no longer matches belongs to a REUSED pid.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _load_rss_base():
    """(timestamp, {pid: (rss_kib, starttime)}) of the last baseline, or
    (0, {}).  Tolerates the pre-starttime two-token format (pid:rss) as a
    same-process delta with no identity check."""
    try:
        with open(RSS_BASE_PATH) as f:
            parts = f.read().split()
        ts = float(parts[0])
        base = {}
        for tok in parts[1:]:
            bits = tok.split(":")
            if len(bits) == 3:            # pid:starttime:rss
                base[int(bits[0])] = (int(bits[2]), int(bits[1]))
            elif len(bits) == 2:         # legacy pid:rss
                base[int(bits[0])] = (int(bits[1]), None)
        return ts, base
    except (OSError, ValueError, IndexError):
        return 0.0, {}


def _write_rss_base(cur):
    if not cur:
        return
    try:
        with open(RSS_BASE_PATH, "w") as f:
            f.write(f"{time.time()} " + " ".join(
                f"{p}:{_starttime(p)}:{r}"
                for p, r in sorted(cur.items()) if _starttime(p) is not None
            ) + "\n")
    except OSError:
        pass


def _rss_grow_victim(cur, self_pid):
    """(pid, name, growth_kib) of the process whose RSS grew the most since
    the previous sample, or None.

    Only called on a device-free breach: the baseline is deliberately NOT
    rewritten while a breach is active (main() rebases on healthy samples
    only), so growth keeps accumulating against the pre-breach snapshot and
    a slow leak is caught on the confirming sample too.  Requires a fresh
    baseline and growth above RSS_GROW_MIN_KIB; honours the protect list.

    Baseline entries carry the process start time (stat field 22), so a
    pid that died and was REUSED inside the 300 s window cannot inherit the
    old tenant's RSS: a mismatched start time means a fresh process whose
    full RSS counts as growth.  A pid with no /proc/<pid>/stat any more died
    between the sample and the pick and is skipped.
    """
    ts, base = _load_rss_base()
    if not base or time.time() - ts > RSS_BASE_MAX_AGE_S:
        return None
    best_pid, best_kib = None, 0
    for pid, rss in cur.items():
        if pid in (self_pid, 1, 2):
            continue
        st = _starttime(pid)
        if st is None:
            continue            # died between the sample and the pick
        b = base.get(pid)
        base_rss = 0
        if b is not None:
            brss, bst = b
            if bst is None or bst == st:
                base_rss = brss  # same process (or legacy entry): delta
            # else: pid reused -- full RSS counts as growth
        grow = rss - base_rss
        if grow > best_kib:
            best_pid, best_kib = pid, grow
    if best_pid is None or best_kib < RSS_GROW_MIN_KIB:
        return None
    nm = ""
    try:
        with open(f"/proc/{best_pid}/status") as f:
            txt = f.read()
        mm = re.search(r"^Name:\s+(.+)", txt, re.M)
        if mm:
            nm = mm.group(1).strip()
    except OSError:
        return None            # died between the scan and the pick
    if f"{best_pid}" in _PROTECT_TOKENS or nm in _PROTECT_TOKENS:
        return None
    return best_pid, nm, best_kib


_LOCK_FD = None


def _try_lock():
    """True if this invocation holds the whole-run guard lock.

    The lock makes every state-file and baseline read-modify-write
    single-threaded across invocations.  flock is released by the kernel
    when the fd is closed or the holder dies, so a SIGKILLed run can never
    wedge the next sample.
    """
    global _LOCK_FD
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return True          # cannot lock -> run unlocked rather than blind
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return False
    _LOCK_FD = fd
    return True


def _unlock():
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


def memory_guard(mem, dfree, procs, psi_f=None, nvrm=None):
    """Kill the offending process before unified-memory exhaustion hard-locks
    the box.  Returns a short tag for the sample line, or None.

    Triggers, in the order they matter:
      1. device-free below DEVICE_FREE_FLOOR_MIB - the pool the driver
         allocates from, and the thing that actually ran out.  This is the
         only signal that saw the failure coming: MemAvailable still read a
         healthy ~20 GiB while the driver was ~1 GiB from NV_ERR_NO_MEMORY.
      2. MemAvailable below MEM_FLOOR_GIB - OS-side exhaustion.
      3. GPU compute-app total above GPU_CEIL_MIB - runaway backstop, mostly
         useful when the cudaMemGetInfo probe is unavailable.
      4. PSI memory full avg10 at/above PSI_FULL_FLOOR for two consecutive
         samples - the kernel saying allocation is already stalling.
      5. swap used at/above SWAP_FLOOR_GIB - the pre-lock thrash signature.
      6. NV_ERR_NO_MEMORY lines new in the kernel log since the previous
         sample - the driver literally refusing an allocation.  Arms like
         any breach; the two-sample confirmation still applies, so one
         transient refusal that the engine retried past recovers.

    Victim: a GPU-pool breach (1, 3 or 6) kills the top GPU consumer, as
    before -- with 2026-10-25 growth attribution first, since on GB10 the
    pool IS system RAM and an RSS hog can cause (1) and (6) without holding
    any GPU memory.  An OS-side-only breach (2, 4, 5) picks the biggest-RSS
    process instead (see _os_victim), because the offender there often
    holds no GPU memory.

    One invocation is one sample, so the two-sample confirmation has to live
    in the state file:
        armed:<ts>            first breach, waiting for a confirming sample
        termed:<pid>:<ts>     SIGTERM sent, waiting for exit / grace expiry
    """
    if not GUARD_ENABLED:
        return None

    global _LAST_RSS, _REBASE_OK
    _REBASE_OK = False
    cur_rss = _rss_map()
    _LAST_RSS = cur_rss
    now    = time.time()
    avail  = mem.get("MemAvailable")
    availg = avail / 1048576 if avail is not None else None
    gpu    = sum(p[1] for p in procs)
    state  = _state().split(":")

    # 1. finish a kill started on an earlier sample -- never block this run
    if state[0] == "termed":
        pid = state[1]
        if not _alive(pid):
            guard_log(f"pid {pid} exited after SIGTERM")
            _set_state("")
            return None
        if now - float(state[2]) >= GUARD_GRACE_S:
            if _signal_pid(pid, signal.SIGKILL):
                guard_log(f"pid {pid} still alive after "
                          f"{GUARD_GRACE_S:.0f}s -> SIGKILL")
            _set_state("")
            return "guard:killed"
        return "guard:draining"

    low_free = dfree is not None and dfree < DEVICE_FREE_FLOOR_MIB
    low_avail = availg is not None and availg < MEM_FLOOR_GIB
    high_gpu = gpu > GPU_CEIL_MIB
    low_psi = psi_f is not None and psi_f >= PSI_FULL_FLOOR
    nvrm_hit = nvrm is not None and nvrm > 0
    swap_used = None
    st = mem.get("SwapTotal")
    sf = mem.get("SwapFree")
    if st is not None and sf is not None:
        swap_used = (st - sf) / 1048576.0
    high_swap = swap_used is not None and swap_used >= SWAP_FLOOR_GIB
    pool_breach = low_free or nvrm_hit      # driver-pool family: (1), (3), (6)

    if not (low_free or low_avail or high_gpu or low_psi or high_swap
            or nvrm_hit):
        _REBASE_OK = True
        if state[0] == "armed":
            _set_state("")
            guard_log(f"recovered: device_free={dfree}MiB "
                      f"mem_available={availg if availg is None else round(availg, 1)}GiB "
                      f"gpu={int(gpu)}MiB")
            _clear_warn()
        elif _warn_due(now) and (
                (dfree is not None and dfree < WARN_FREE_MIB) or
                (availg is not None and availg < WARN_FLOOR_GIB)):
            if dfree is not None and dfree < WARN_FREE_MIB:
                guard_log(f"WARN device_free={dfree}MiB (kill floor "
                          f"{int(DEVICE_FREE_FLOOR_MIB)}MiB) gpu={int(gpu)}MiB")
            else:
                guard_log(f"WARN mem_available={availg:.1f}GiB gpu={int(gpu)}MiB "
                          f"(kill floor {MEM_FLOOR_GIB:.0f}GiB)")
        elif dfree is None and _warn_due(now):
            guard_log("WARN cudaMemGetInfo unavailable - falling back to "
                      "MemAvailable and the GPU ceiling only")
        return None

    armed_age = None
    if state[0] == "armed":
        armed_age = now - float(state[1])

    if armed_age is not None and armed_age < MIN_CONFIRM_AGE_S:
        # An armed state this fresh was written by a CONCURRENT invocation
        # seconds ago; treating it as this sample's confirmation would turn
        # the two-sample rule into a one-sample kill.  Keep the original
        # timestamp untouched so the honest first sighting survives.
        guard_log(f"armed state is only {armed_age:.1f}s old (< "
                  f"{MIN_CONFIRM_AGE_S:g}s) -- concurrent writer? not "
                  "confirming on it")
        return "guard:pending"

    if armed_age is not None and armed_age <= GUARD_CONSEC_S:
        victim = None
        if high_gpu:
            # GPU compute-app ceiling: whoever holds that memory is the
            # culprit by definition.
            if procs:
                victim = max(procs, key=lambda p: p[1])          # (pid, MiB, name)
        elif pool_breach and OS_VICTIM_BY_RSS:
            # 2026-10-25: device memory IS RAM on GB10, so a device-free
            # breach can be caused by a plain RSS hog that holds no GPU
            # memory at all (an unwrapped build, say) -- killing the engine
            # for it is exactly the failure this guard exists to prevent.
            # Kill whoever actually grew since the previous sample.  The
            # engine's CUDA allocations never show in RSS, so genuine
            # engine growth falls through to the old GPU-consumer pick.
            v = _rss_grow_victim(cur_rss, os.getpid())
            if v is not None:
                vk, vn, vgrow = v
                victim = (str(vk), vgrow / 1024.0, vn or "?")
                guard_log(f"victim by RSS growth: {vn}(pid {vk}, "
                          f"+{vgrow / 1048576:.1f}GiB since last sample)")
            elif procs:
                victim = max(procs, key=lambda p: p[1])
        elif pool_breach:
            # growth attribution disabled -> old behaviour
            if procs:
                victim = max(procs, key=lambda p: p[1])
        elif OS_VICTIM_BY_RSS:
            # OS-side-only breach (mem_available / PSI / swap): the offender
            # often holds no GPU memory -- pick the biggest-RSS process.
            # (2026-10-24 fix: the old condition also matched `not procs`,
            # which routed exactly this case into the GPU-consumer pick,
            # found no victim and cleared the state -- armed forever.)
            v = _os_victim(os.getpid())
            if v is not None:
                vk, vn, vrss = v
                victim = (str(vk), vrss / 1024.0, vn or "?")
                guard_log(f"victim by RSS: {vn}(pid {vk}, {vrss} kB) "
                          f"over GPU consumers (OS-side breach)")
            elif procs:
                victim = max(procs, key=lambda p: p[1])
                guard_log("no RSS victim found (protected/none) -> "
                          "falling back to the top GPU consumer")
        if victim is None:
            _set_state("")
            return None
        pid, mib, name = victim
        _set_state(f"termed:{pid}:{now}")
        why = []
        if low_free:
            why.append(f"device_free {dfree}MiB < {int(DEVICE_FREE_FLOOR_MIB)}MiB")
        if low_avail:
            why.append(f"mem_available {availg:.1f}GiB < {MEM_FLOOR_GIB:.0f}GiB")
        if high_gpu:
            why.append(f"gpu {int(gpu)}MiB > {int(GPU_CEIL_MIB)}MiB")
        if low_psi:
            why.append(f"psi_full {psi_f}% >= {PSI_FULL_FLOOR}%")
        if high_swap:
            why.append(f"swap {swap_used:.1f}GiB >= {SWAP_FLOOR_GIB:.0f}GiB")
        if nvrm_hit:
            why.append(f"nvrm {nvrm} NV_ERR_NO_MEMORY since last sample")
        guard_log(f"LOW {'; '.join(why)} -> SIGTERM "
                  f"{name}(pid {pid}, {int(mib)}MiB)")
        if _signal_pid(pid, signal.SIGTERM):
            return "guard:terming"
        _set_state("")
        return "guard:term-failed"

    _set_state(f"armed:{now}")
    why = ("device_free " + str(dfree) + "MiB") if low_free else \
          (f"mem_available {availg:.1f}GiB" if low_avail else
           (f"gpu {int(gpu)}MiB" if high_gpu else
            (f"psi_full {psi_f}%" if low_psi else
             (f"nvrm {nvrm} since last sample" if nvrm_hit else
              f"swap {swap_used:.1f}GiB"))))
    guard_log(f"LOW {why} -> armed, needs a confirming sample within "
              f"{GUARD_CONSEC_S:.0f}s")
    return "guard:armed"


def main():
    # Whole-run exclusive lock (2026-10-25): if another invocation is
    # mid-sample, it owns this instant -- skip everything (no sample line,
    # no report, no guard decision) and exit 0 quietly.
    if not _try_lock():
        return 0
    try:
        return _main_locked()
    finally:
        _unlock()


def _main_locked():
    util, temp, power = gpu_row()
    procs     = compute_apps()
    mem_total = sum(p[1] for p in procs) if procs else None
    mem       = meminfo()
    psi_s, psi_f = psi_memory()
    dfree, dtotal = device_free()
    nvrm_new  = nvrm_errors()
    guard     = memory_guard(mem, dfree, procs, psi_f, nvrm_new)
    # Rebase the RSS baseline only on a healthy sample: mid-breach the
    # pre-breach snapshot is what makes growth attribution work on the
    # confirming sample, so it must survive.
    if _REBASE_OK and _LAST_RSS:
        _write_rss_base(_LAST_RSS)
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp     = datetime.now().strftime("%Y%m%d_%H%M%S")

    deg = "\N{DEGREE SIGN}"
    top = sorted(procs, key=lambda p: -p[1])[:3]
    plist = " | ".join(f"{n}(pid {pid}, {int(m)}MiB)" for pid, m, n in top) \
        if top else "none"

    avail   = mem.get("MemAvailable")
    avail_s = f"{avail / 1048576:.1f}GiB" if avail is not None else "N/A"
    dfree_s = f"{dfree / 1024:.2f}GiB" if dfree is not None else "N/A"
    psi_s   = f"{fmt(psi_s, '%')}/{fmt(psi_f, '%')}"
    nvrm_s  = "N/A" if nvrm_new is None else str(nvrm_new)

    line = (f"[{ts}] GPU: NVIDIA GB10 | U:{fmt(util, '%')} | "
            f"T:{fmt(temp, deg + 'C')} | P:{fmt(power, 'W')} | "
            f"M:{fmt(mem_total, 'MiB')} | F:{dfree_s} | A:{avail_s} | "
            f"N:{nvrm_s} | proc: {plist}")
    if guard:
        line += f" | {guard}"
    line += "\n"

    with open(LOG_PATH, "a") as f:
        f.write(line)
    print(line.rstrip())

    os.makedirs(REPORT_DIR, exist_ok=True)
    rp = os.path.join(REPORT_DIR, f"gpu_report_{stamp}.txt")
    with open(rp, "w") as f:
        bar = "=" * 64
        f.write(f"{bar}\nDGX Spark GPU snapshot -- {ts}\n{bar}\n")
        f.write(f"{'Metric':<14}{'Value':>16}\n{'-' * 30}\n")
        f.write(f"{'Utilization':<14}{fmt(util, '%'):>16}\n")
        f.write(f"{'Temperature':<14}{fmt(temp, deg + 'C'):>16}\n")
        f.write(f"{'Power':<14}{fmt(power, 'W'):>16}\n")
        f.write(f"{'Mem used':<14}{fmt(mem_total, 'MiB'):>16}\n")
        f.write(f"{'DeviceFree':<14}{dfree_s:>16}\n")
        f.write(f"{'DeviceTotal':<14}{(f'{dtotal / 1024:.2f}GiB' if dtotal else 'N/A'):>16}\n")
        f.write(f"{'MemAvailable':<14}{avail_s:>16}\n")
        f.write(f"{'MemFree':<14}{kib(mem.get('MemFree')):>16}\n")
        f.write(f"{'PSI some/full':<14}{psi_s:>16}\n")
        f.write(f"{'NV_ERR_NO_MEMORY':<14}{nvrm_s:>16}\n")
        f.write("-" * 30 + "\n")
        for pid, m, n in sorted(procs, key=lambda p: -p[1]):
            f.write(f"{n[:20]:<20} pid {pid:<9} {int(m):>8} MiB\n")
        if not procs:
            f.write("(no GPU compute processes)\n")
        if guard:
            f.write(f"{'GUARD':<14}{guard:>16}\n")
        f.write(f"{bar}\n")
    print(f"report: {rp}")

    rm = prune_old_reports()
    if rm:
        print(f"pruned {rm} report(s) older than {REPORT_KEEP_D}d")

    if util is None and mem_total is None:
        print("WARN: all GPU queries failed -- logged as N/A, exit 1",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
