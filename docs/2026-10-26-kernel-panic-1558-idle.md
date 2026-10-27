# 2026-10-26 15:58 kernel panic — idle box, engine not running, first captured vmcore

Date: 2026-10-26 (written ~1 h after the event) · review due: week of 2026-11-02
Status: **INTENTIONAL — a kdump test by another agent (user-confirmed), not the
failure class.** Kept on record because it is the first panic this box has ever
captured and it proves the kdump path works end to end: vmcore saved,
auto-reboot ~90 s. Do not count it as an unexplained death.
Affects: this DGX Spark (`carlosp-nvidia-dgx`).
Companion: `docs/2026-10-26-hardlock-1521-headroom40.md` (the 15:21 freeze,
~37 min earlier) and `.opencode/skills/gb10-hardlock-triage`.

## The event

| | |
|---|---|
| Boot | `-1` 15:34:50 → 15:57:53 (the manual power-cycle after the 15:21 freeze) |
| Engine | **not running** (per the 15:21 post-mortem's "do not restart"; device held only the monitor's own 170 MiB python) |
| Last healthy sample | 15:57:45 — `U:0% T:58°C P:12.47W M:170MiB F:109.12GiB N:0` |
| Last journal entry | 15:57:53 (sshd disconnect) |
| Panic | ~15:57:53–15:58:00 |
| Recovery | **automatic** — boot `0` at 15:59:23, ~85–90 s after the panic |
| Evidence | `/var/crash/202610262258/`: `dmesg.202610262258` (134 KB) + `dump.202610262258` (**1.5 GB vmcore**), saved 15:58; `.crash` report written 15:59:36 |

`ProblemType: KernelCrash`, arm64, kernel `7.0.0-1019-nvidia`
(7.0.0-1019.19~24.04.2). kdump was armed: `crashkernel reserved: 512M` +
`crashkernel low 128M`, and kdump-tools re-armed `kexec -p` at 15:59:25 in
boot 0.

## Why this event is still worth recording

It is **not** an unexplained death — the user confirmed another agent induced it
as a kdump test. But it is the first panic this box has ever captured, and it
settles two open questions from the 15:21 post-mortem:

1. **kdump works on this box.** 512 MB crashkernel + 128 MB low, capture kernel
   ran on 1 CPU, saved a 1.5 GB vmcore and 134 KB dmesg in ~40 s, and the box
   **auto-rebooted at 15:59:23 — ~85–90 s after the panic, no human involved.**
   The 15:21 freeze needed a 13m30s manual power-cycle precisely because a hang
   does not panic and kdump never engages. So the failure series splits cleanly:
   - **hangs** (15:21 and every earlier death): no vmcore, no auto-recovery,
     manual power-cycle required;
   - **panics**: vmcore in `/var/crash/<ts>/`, auto-recovery in ~90 s.
2. **Every soft log misses a panic.** `gpu_monitor_log.txt` shows only a gap
   (15:57:45 → 15:59:30, with an ext4 delayed-allocation NUL hole between);
   `kern.log` has nothing between 15:35:14 and 15:59:25; syslog stops at
   15:57:46. **`/var/crash` is the only witness** — which is why the watch cron
   now checks it (see §5). Corollary for triage: a clean `journalctl -k -b -1`
   no longer rules out a panic; only `/var/crash` does.

Side observation, harmless: the box idled for the 23 min before the test with
the engine down (per the 15:21 post-mortem's "do not restart"), 109 GiB free,
0 % GPU — the state it was left in.

## What it does NOT tell us

Nothing about the failure class — it was induced. In particular it does not
exonerate or implicate the engine, the pool, or memory pressure for the
*unexplained* deaths; the box was simply idle when the test ran. The only
transferable facts are the kdump mechanics above and the `/var/crash`-is-the-
only-witness corollary.

## Next actions, ranked

1. **USB-UART is still the right long-term investment** (carried over from the
   15:21 post-mortem): for the *unexplained* hangs, no panic is captured at
   all, so a serial console remains the only channel that can see them. kdump
   is proven but only fires on panics.
2. **Do not restart the engine on current evidence** (unchanged from 15:21).
   If a restart is needed for other reasons, keep kdump armed (it is, and it
   demonstrably works).
3. **Note for coordination:** the vmcore (`/var/crash/202610262258/`, 1.5 GB)
   is a test artifact owned root-only. Prune it when the other agent is done
   with it, so a future real panic is not confused with this one.

## Cross-references

- `docs/2026-10-26-hardlock-1521-headroom40.md` — the 15:21 freeze; the
  unexplained-death series continues there, not here.
- `.opencode/skills/gb10-hardlock-triage` — triage procedure; §2's evidence
  order now includes `/var/crash`, and §4's "no panic ever" claim is qualified
  by this induced test.
- Monitor-log gap inventory (log spans 2026-09-20 → 2026-10-26, ~5 weeks — not
  a year): box-down gaps align with known reboots on 10-24 (×4), 10-25
  (17:11, 18:36), 10-26 (15:21). Older 3–11 min gaps on 09-20/09-28/09-29/
  10-20/10-21/10-24 predate the journal window and are unattributed.
