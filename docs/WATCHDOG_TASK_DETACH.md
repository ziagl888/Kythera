# Why the watchdog task ends with `LastTaskResult=15` and leaves the watchdog detached

**T-2026-KYT-9050-076.** Root cause of the state that cost 13 hours of undeployed
code on 2026-08-02: the scheduled task reads `State=Ready` while a live watchdog
supervises the fleet, so `Stop-ScheduledTask` — the only UAC-free stop path —
grabs nothing.

**Short version: nothing is broken.** Exit 15 is the *correct* propagation of a
*deliberate* kill. The boot-time clock correction makes the Task Scheduler start
the task twice; the second watchdog reaps the first exactly as designed, and the
task records the reaped one's exit code. What is lost is only the Scheduler's
ownership of the surviving process.

## The evidence chain (measured 2026-08-02/03, read-only)

| # | Fact | Source |
|---|---|---|
| 1 | Boot at **05:29:03** local | `Win32_OperatingSystem.LastBootUpTime` |
| 2 | At **05:29:44** the system clock jumps **+3 h**: UTC `2026-08-01T23:29:43Z` → `2026-08-02T02:29:44Z` | System log, Kernel-General **event 1** |
| 3 | Launcher run #1 starts at clock-**02:29:34** (pre-jump) and creates `logs/watchdog_debug_20260802_022934.log` | `logs/watchdog_launch.log` |
| 4 | The Task Scheduler (`svchost` PID 1748) starts `launch_watchdog.cmd` **again** as cmd PID 6232 at **05:29:44** — the same second as the jump | `Win32_Process` parent chain |
| 5 | Watchdog #2 = python PID **7016**, child of 6232, starts 05:29:46 and supervises 41 bots | `Win32_Process` |
| 6 | Launcher #1 records **`exit 15`** at 05:29:52 | `logs/watchdog_launch.log` |
| 7 | `main_watchdog.py` has **no** `sys.exit(15)` — its own exits are 0 and 1 (mutex conflict) | source |
| 8 | `psutil.Process.terminate()` on Windows yields exit code **15** — measured on this host, psutil 7.2.2 | empirical probe |

## What actually happened

1. The box boots with a clock that is three hours behind. The boot trigger fires
   and launcher #1 starts watchdog #1.
2. Windows time sync corrects the clock forward by three hours.
3. **The forward jump makes the Task Scheduler re-fire the boot trigger.**
   `MultipleInstances=IgnoreNew` does not suppress it — the jump moved the
   Scheduler's notion of "now" past the point where it considered the trigger
   pending again.
4. Watchdog #2 starts, finds the `Global\KytheraWatchdog` mutex held by
   watchdog #1, and runs the documented mutex-deadlock recovery from
   T-2026-CU-9050-127: `_acquire_single_instance_lock` → `_reap_orphans` →
   `psutil.Process.terminate()`.
5. Watchdog #1 dies with exit code 15. Launcher v6 propagates it faithfully
   (`exit /b %WD_EXIT%`, the T-2026-KYT-9050-025 change), so the task records
   `LastTaskResult=15`.
6. The task instance the Scheduler tracked was run #1. It exited, so `State`
   falls back to `Ready` — while run #2's watchdog keeps running, **detached**.

**The self-healing worked.** Exactly one fleet came up, no double Cornix signals
(P0.2 held). The single-instance guard did its job. The only casualty is the
ownership link, and with it the UAC-free stop path.

## What this does NOT establish

**That it happens at every reboot.** 2026-08-02 is the only boot since
2026-07-08 (`Kernel-Boot` event 20), so there is exactly one observation. The
mechanism is boot-specific and will recur whenever the boot-time clock
correction is large enough to re-fire the trigger — but "every reboot" is not
measured, and a small correction may well not trigger it.

Also unresolved, and deliberately left so: watchdog #2's output lands in run
#1's debug log, and no `watchdog_debug_20260802_052944.log` exists. The
redirect-onto-a-locked-file failure mode from launcher v5 is the obvious
suspect, but it is not proven and nothing depends on it.

## Not the launcher's fault — it already blocks

An obvious suspicion is that the launcher fires the watchdog off and returns, so
the task loses it. It does not. `launch_watchdog.cmd` runs
`python.exe … main_watchdog.py` in the **foreground** and only then evaluates
`%ERRORLEVEL%`; the proof is in the evidence above — launcher #1 wrote
`exit 15`, which it could only do by having waited for its python to die.

So there is nothing to make "more blocking", and no task setting would help:
the task *did* hold its watchdog as a child. It held the **wrong one** — the
instance that was reaped — because the Scheduler started the task a second time.
The fix therefore belongs at the trigger, not at the launcher.

## The fix: give the boot trigger a delay

The trigger has **no delay** (`Delay` is empty). Firing at boot+2 min lets the
time service settle *before* the task starts, so no jump happens during or after
the task start and there is nothing to re-fire.

Elevated, one-time (the task runs `RunLevel=Highest`, so re-registration needs
elevation):

```powershell
$t = Get-ScheduledTask -TaskName "Kythera Watchdog"
$t.Triggers[0].Delay = "PT2M"
Set-ScheduledTask -TaskName "Kythera Watchdog" -Trigger $t.Triggers
```

Cost: the fleet starts two minutes later after a reboot. That is not a
regression — the DB and network are more likely to be ready, and the bots'
own `start_delay` staggering already spans ~5 minutes.

**This command has not been executed.** It needs elevation, which the session
that wrote this doc did not have, so it is derived from the task definition
(`Delay` is a real, currently empty property of the boot trigger) and not from a
successful run. Verify the trigger afterwards:

```powershell
(Get-ScheduledTask -TaskName "Kythera Watchdog").Triggers |
    Select-Object -Property Delay, Enabled
```

**Careful:** `Set-ScheduledTask` silently drops the Principal
(T-2026-KYT-9050-025 / T-025) — verify `RunLevel=Highest` and
`UserId=Michael` afterwards, or the task comes back unelevated and the fleet
starts without the rights it needs:

```powershell
(Get-ScheduledTask -TaskName "Kythera Watchdog").Principal |
    Select-Object UserId, RunLevel, LogonType
```

## If it happens anyway

The state is survivable without elevation — `tools/restart_fleet.ps1` prints
both routes when it detects it (T-2026-KYT-9050-071):

* `-MarkerRestart` recycles the FLEET bots through the running watchdog's
  restart markers. Covers every bot-code rollout.
* Restoring task ownership needs elevation: kill the watchdog **first**
  (otherwise it respawns the children it just lost), then its python children,
  then `Start-ScheduledTask`.

And `tools/ops/fleet_code_age.py` reports when the running fleet is older than
`HEAD`, which is what makes the detached state *visible* instead of silent.
