# Guard concurrency hardening: two-sample confirmation collapses under parallel invocations

Date: 2026-10-25
Status: implemented -- flock whole-run lock + `GPU_MONITOR_MIN_CONFIRM_AGE_S`
(default 8 s, ~80% of the 10 s service cadence).  Verified by the sandbox
suite (38 checks) incl. a two-concurrent-invocations test: exactly one
sample line, no kill, and a later invocation confirms normally.
Affects: `tools/gpu_monitor.py` (`memory_guard`, state file handling)

## Incident

At 2026-10-25 05:16:28 the guard logged this, all within the same second:

```
[2026-10-25 05:16:28] LOW device_free 2500.0MiB -> armed, needs a confirming sample within 30s
[2026-10-25 05:16:28] LOW device_free 2500.0MiB -> armed, needs a confirming sample within 30s
[2026-10-25 05:16:28] victim by RSS growth: sleep(pid 103647, +40.0GiB since last sample)
[2026-10-25 05:16:28] LOW device_free 2500.0MiB < 3072MiB -> SIGTERM sleep(pid 103647, 40960MiB)
[2026-10-25 05:16:28] pid 103647 exited after SIGTERM
```

Two `armed` lines in the same second mean **two invocations of
`gpu_monitor.py` ran concurrently**. The systemd service samples every
~10 s via `Restart=always` + `RestartSec=10`; the second invocation here
came from outside the service (a short SSH session seconds earlier is
visible in the journal). The second invocation read the state file that
the first had *just* written as `armed:<ts>` -- and since
`now - armed_ts <= GUARD_CONSEC_S` was trivially true (0 s elapsed), it
took that state as its own **confirming sample** and killed immediately.

Net effect: the two-sample confirmation requirement, which exists so a
transient dip never costs a running process, collapsed into a
**single-sample kill**. In this incident the victim was a test dummy
(a `sleep` process planted by a concurrent session, with synthetic
breach values -- the journal's real device-free readings stayed at
6.8-7.0 GiB throughout), so no harm was done. But the same race on a
genuine one-sample dip would SIGTERM an innocent process that a
well-behaved guard would have spared.

A repeatable sequence at 05:07:23-24 shows the same double-armed race.

## The defect, precisely

`memory_guard` treats the state file as if only one process ever reads
or writes it. Consequences of two concurrent invocations A and B:

1. A writes `armed:<ts>`. B reads it moments later, computes
   `now - ts ~ 0 <= GUARD_CONSEC_S`, and confirms on B's *first*
   sighting of the breach -- one sample, not two.
2. State-file writes race each other (`armed` from A can be overwritten
   by B's `termed:<pid>` while A is still mid-decision), so the same
   victim can be signalled twice, or a `termed` entry can be lost.
3. The baseline file has the same write-race exposure.

## Proposed mechanism

Two independent guards, either of which alone is sufficient:

1. **Cross-invocation lock.** Before guard logic, take a non-blocking
   exclusive lock (`fcntl.flock(LOCK_EX | LOCK_NB)`) on a dedicated
   lockfile held for the duration of `memory_guard` and the baseline
   write. If the lock is held, the invocation **skips the guard and
   sampling entirely** (exit 0, log nothing): the holder is the live
   sampler for this instant. This makes every guard decision
   single-threaded by construction, including the state-file and
   baseline read-modify-write cycles. Lock is released in a `finally`,
   so a crashed holder cannot wedge the next sample.

2. **Armed-age floor.** Independently of the lock, refuse to confirm
   an `armed` state whose timestamp is younger than the minimum sane
   sampling interval: a confirming sample must have been produced by a
   *different, earlier* invocation. Confirm requires
   `now - armed_ts >= MIN_CONFIRM_AGE_S` (default ~8 s, just under the
   10 s service cadence) *and* `<= GUARD_CONSEC_S`. An armed state that
   is younger than that belongs to a concurrent writer and is ignored
   for confirmation purposes. This preserves the two-sample semantics
   even if someone later disables the lock or runs the script from cron
   with a different cadence.

## Acceptance criteria

- Two invocations started in the same second on a breach: exactly one
  `armed` line, no kill until a genuinely later invocation confirms.
- A confirming invocation that sees an `armed` state younger than
  `MIN_CONFIRM_AGE_S` does not kill on it (logs and skips).
- No change to the kill paths for real breaches that persist across
  two service samples (~20 s), verified by the existing sandbox suite
  plus a new two-concurrent-invocations test.
- Watchdog holder crash (SIGKILL) does not deadlock the next sample.

## Relation to the 2026-10-25 drill

The same drills proved the rest of the chain works end to end: growth
attribution picked the true offender (the RSS-growing hog, not the
engine) even inside a frozen process group, and safe-build froze before
the guard engaged once its device probe ran at 15 s. This note only
closes the confirmation-race hole that the drill's concurrent
invocations revealed.