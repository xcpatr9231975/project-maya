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
   escalate to SIGKILL after a grace period.  See the GUARD_* block below.
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
_PROTECT_TOKENS       = {t.strip() for t in
                         os.environ.get("GPU_MONITOR_PROTECT", "").replace(",", " ").split()
                         if t.strip()}

STATE_PATH            = os.path.join(HOME, ".gpu_monitor_guard_state")
WARN_PATH             = os.path.join(HOME, ".gpu_monitor_guard_warned")
GUARD_LOG_PATH        = os.path.join(HOME, "gpu_monitor_guard.log")


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


def memory_guard(mem, dfree, procs, psi_f=None):
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

    Victim: a GPU-pool breach (1 or 3) kills the top GPU consumer, as before.
    An OS-side-only breach (2, 4, 5) picks the biggest-RSS process instead
    (see _os_victim), because the offender there often holds no GPU memory.

    One invocation is one sample, so the two-sample confirmation has to live
    in the state file:
        armed:<ts>            first breach, waiting for a confirming sample
        termed:<pid>:<ts>     SIGTERM sent, waiting for exit / grace expiry
    """
    if not GUARD_ENABLED:
        return None

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
    swap_used = None
    st = mem.get("SwapTotal")
    sf = mem.get("SwapFree")
    if st is not None and sf is not None:
        swap_used = (st - sf) / 1048576.0
    high_swap = swap_used is not None and swap_used >= SWAP_FLOOR_GIB

    if not (low_free or low_avail or high_gpu or low_psi or high_swap):
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

    if state[0] == "armed" and (now - float(state[1])) <= GUARD_CONSEC_S:
        gpu_breach = low_free or high_gpu
        victim = None
        if gpu_breach or not OS_VICTIM_BY_RSS:
            # 2026-10-24 fix: the old condition also matched `not procs`,
            # which routed an OS-side breach with NO GPU compute processes
            # (runaway build, exactly the RSS-victim use case) into this
            # branch, picked no victim and cleared the state -- the guard
            # armed every sample and never acted.
            if procs:
                victim = max(procs, key=lambda p: p[1])          # (pid, MiB, name)
        else:
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
             f"swap {swap_used:.1f}GiB")))
    guard_log(f"LOW {why} -> armed, needs a confirming sample within "
              f"{GUARD_CONSEC_S:.0f}s")
    return "guard:armed"


def main():
    util, temp, power = gpu_row()
    procs     = compute_apps()
    mem_total = sum(p[1] for p in procs) if procs else None
    mem       = meminfo()
    psi_s, psi_f = psi_memory()
    dfree, dtotal = device_free()
    guard     = memory_guard(mem, dfree, procs, psi_f)
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

    line = (f"[{ts}] GPU: NVIDIA GB10 | U:{fmt(util, '%')} | "
            f"T:{fmt(temp, deg + 'C')} | P:{fmt(power, 'W')} | "
            f"M:{fmt(mem_total, 'MiB')} | F:{dfree_s} | A:{avail_s} | proc: {plist}")
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
