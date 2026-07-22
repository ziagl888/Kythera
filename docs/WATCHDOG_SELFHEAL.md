# Watchdog launcher crash + outer-net self-heal (T-2026-KYT-9050-025)

**Status:** code fixes in this PR; the scheduled-task change is an **elevated,
Michi-gated operator action** (see §4). Deploy = fleet restart, also Michi-gated.

---

## 1. Symptom

`logs/watchdog_launch.log` shows the launcher process dying with exit
`-1073741819` = `0xC0000005` (ACCESS_VIOLATION, a native segfault):

```
Sun 07/19/2026 20:08:18.49 launcher v5: exit -1073741819
Wed 07/22/2026 12:50:54.20 launcher v5: exit -1073741819
```

When the watchdog process dies, the "Kythera Watchdog" scheduled task flips
`Running → Ready`, but the fleet bots it spawned keep running **detached as
orphans** (Windows keeps the dead parent PID). The **outer supervision net is
then gone**: if a bot or the fleet later dies, nothing auto-restarts it, and the
fleet silently decays until a manual recovery. On 2026-07-22 this left an
unsupervised orphan fleet for ~1h and needed a manual `Start-ScheduledTask`.

## 2. Root cause (native crash)

`launch_watchdog.cmd` runs python with `-X faulthandler`, which caught the crash.
Both `0xC0000005` deaths in the ledger carry the **identical** faulthandler stack
(`logs/watchdog_debug_20260719_193847.log`, `..._20260722_122939.log`):

```
Windows fatal exception: access violation

Thread 0x…… (most recent call first):
  File "…/psutil/_pswindows.py", line 978 in open_files
  File "…/psutil/__init__.py",   line 1224 in open_files
  File "…/main_watchdog.py",      line 333 in _resolve_heartbeat_log
  File "…/main_watchdog.py",      line 363 in check_heartbeat
  File "…/main_watchdog.py",      line 674 in main
```

`_resolve_heartbeat_log` (part of the P2.47 hang-detection) calls
`psutil.Process(pid).open_files()` to find a bot's own log handle. On this
Windows / Python 3.13 box that native psutil call **intermittently
access-violates**. `open_files()` enumerates and duplicates handles from another
process and queries them via `NtQueryObject`; a race or a hostile handle type
segfaults inside the C extension. A native crash **cannot be caught with
try/except**, so it killed the whole watchdog process.

Timing corroborates: the crashes were ~20 min into a run, matching
`HANG_LIMIT_S = 20*60` — the first time `check_heartbeat` resolves each bot's log
after its grace window (resolution is cached, so it runs once per bot lifetime).

## 3. Fixes in this PR (code — deploy is a fleet restart, Michi-gated)

### 3a. `main_watchdog.py` — isolate the crash-prone native call
The `open_files()` enumeration now runs in a **throwaway child process**
(`_probe_open_log_files` → `subprocess.run([...python -c...])`). A native crash
in the child surfaces to the parent only as a **non-zero return code**, and a
hang is bounded by a **10 s timeout**; either way the parent treats the process
as *unresolvable → exempt from hang-detection*, exactly like a bot with no
observable log. The supervisor process can no longer be taken down by this call.
Behaviour is otherwise unchanged (still mapping-free, still prefers `logs/`).
Covered by `backtest/test_watchdog_hang.py` (crash-exit, timeout, spawn-failure →
exempt).

### 3b. `launch_watchdog.cmd` — propagate the python exit code (v5 → v6)
v5's last line was the ledger `echo`, which returns 0, so a python crash reached
the task as `LastTaskResult = 0x0` ("success"). v6 captures `%ERRORLEVEL%` before
the echo and `exit /b %WD_EXIT%`, so the task's result reflects the real outcome.
This is a prerequisite for restart-on-failure (§4) and makes crashes visible to
monitoring. A clean exit still reports 0.

> The code fix (3a) should stop *this* crash. 3b + §4 are the **outer net**: they
> make supervision self-recover from **any** future abnormal watchdog death,
> regardless of cause.

## 4. Outer-net self-heal — scheduled-task change (ELEVATED, Michi-gated)

Current task (read-only inspection): a single `<BootTrigger/>`,
`RestartCount = 0`, `RestartInterval` unset, `MultipleInstancesPolicy = IgnoreNew`.
Nothing re-launches a dead launcher until the next reboot.

**Recommended:** add **RestartOnFailure** — restart the task when the launcher
process exits non-zero. Requires launcher v6 (3b), otherwise the task never sees a
failure. Apply with the reviewed script (dry-run first):

```powershell
# 1. dry run — prints the planned change, touches nothing
powershell -ExecutionPolicy Bypass -File tools\watchdog_selfheal_task.ps1

# 2. apply (ELEVATED)
powershell -ExecutionPolicy Bypass -File tools\watchdog_selfheal_task.ps1 -Apply
```

The script preserves every other setting, is idempotent, refuses to arm on a
pre-v6 launcher, and verifies the read-back. Defaults: `RestartCount = 3`,
`RestartInterval = PT1M`. Equivalent manual commands (elevated):

```powershell
$s = (Get-ScheduledTask -TaskName 'Kythera Watchdog').Settings
$s.RestartCount = 3; $s.RestartInterval = 'PT1M'
Set-ScheduledTask -TaskName 'Kythera Watchdog' -Settings $s
```

If `Set-ScheduledTask` demands the credential (password-logon task), re-run with
`-User 'Michael' -Password '<password>'`.

### Why RestartOnFailure (not a repetition trigger)
A **repetition trigger** ("repeat every N min") would also re-launch a dead task
and is failure-code-agnostic, but it **revives a deliberate stop** too — it would
defeat a maintenance `Stop-ScheduledTask` and fight `tools/restart_fleet.ps1`.
RestartOnFailure fires **only on a real failure** (non-zero exit); a
`Stop-ScheduledTask` ends success-class (`SCHED_S_TASK_TERMINATED`) and does **not**
trigger it — so *an operator stop stays stopped*, consistent with the rest of the
fleet's "the operator's stop wins" design.

### Composition safety (no second fighting watchdog, no double-reap)
1. `MultipleInstancesPolicy = IgnoreNew` — the scheduler will not start a second
   instance while one is running.
2. `main_watchdog._acquire_single_instance_lock()` — a `Global\` named mutex; a
   second watchdog that somehow starts exits immediately, or reaps a genuine
   orphan-watchdog and retries exactly once (T-2026-CU-9050-127).
3. A crashed watchdog's process is gone, so the OS already released its mutex; the
   restart acquires it cleanly, then `_terminate_orphan_fleet()` (P0.2) reaps the
   orphaned bots **before** spawning a fresh fleet — the manual 2026-07-22 recovery,
   now automatic. Only one process ever reaps.
4. `tools/restart_fleet.ps1` is unaffected: it pulls → stops → starts; a manual
   stop won't trip RestartOnFailure, and its explicit `Start-ScheduledTask` wins
   immediately (IgnoreNew makes any overlap a no-op).

### Verify after applying
```powershell
(Get-ScheduledTask -TaskName 'Kythera Watchdog').Settings |
  Select-Object RestartCount, RestartInterval, MultipleInstances
# expect: 3, PT1M, IgnoreNew
```
Roll back by setting `RestartCount = 0` and `RestartInterval = $null` the same way.

### Known limitation (out of scope here)
`ExecutionTimeLimit = PT72H`: after 72 h of continuous uptime the scheduler stops
the task (success-class), which RestartOnFailure does **not** cover, and the
boot-only trigger won't re-fire until reboot. In practice the fleet is
restarted/rebooted well inside 72 h. Raising `ExecutionTimeLimit` (or adding a
long repetition) is a separate operator decision, not part of this task.
