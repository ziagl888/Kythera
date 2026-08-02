<#
.SYNOPSIS
    UAC-free Kythera fleet restart cycle: git pull, then stop + start the
    "Kythera Watchdog" scheduled task (T-2026-CU-9050-074).

.DESCRIPTION
    Runs entirely WITHOUT elevation. The scheduled task "Kythera Watchdog"
    (registered 2026-07-11, T-2026-CU-9050-068: user Michael, password logon,
    RunLevel Highest) is start-/stoppable by its own user; the Task Scheduler
    applies the elevated token. The script never kills processes itself:
    stopping goes through Stop-ScheduledTask, and any fleet process that
    survives the tree-stop as an orphan is reaped by the next watchdog start
    (main_watchdog._terminate_orphan_fleet, P0.2 - runs elevated).

    Order is pull-first: if the pull fails (diverged checkout, upstream change
    colliding with a runtime-written file like coins.json), the fleet keeps
    running untouched and the script aborts.

    Operator tool - run it deliberately; a fleet restart is an operator
    decision (OPUS-HANDOFF section 6). The first real run doubles as the test
    of the so-far-unexercised Stop-ScheduledTask ACL path: if it throws
    "Access denied", the task ACL needs a one-time elevated fix, and the
    fleet keeps running.

.PARAMETER DryRun
    Preflight only: task state, fleet process count, and the commits a pull
    would bring in. Does not pull, stop, or start anything.

.PARAMETER SkipPull
    Restart the fleet on the code that is already checked out. Also the only
    way to restart from a non-main checkout.

.NOTES
    Exit codes (load-bearing for the operator):
      0 - restart complete and verified (task Running + dashboard up,
          re-confirmed after 15s), or -DryRun preflight OK
      1 - aborted before any stop: preflight, pull failure, non-main checkout,
          stop-ACL failure, or a fleet running OUTSIDE the scheduled task -
          the fleet keeps running untouched
      2 - start verification failed (task not Running / dashboard not up) -
          fleet state unclear, inspect logs\watchdog_debug.log + watchdog.log
      3 - stop did not take effect (task still Running) - nothing restarted
      4 - fleet was stopped but Start-ScheduledTask FAILED - the fleet is
          DOWN; run  Start-ScheduledTask -TaskName 'Kythera Watchdog'
          manually

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\restart_fleet.ps1 -DryRun

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\restart_fleet.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipPull,
    # T-2026-KYT-9050-071: recycle the fleet through the watchdog's own restart
    # markers instead of stopping and starting the scheduled task. Works
    # UNELEVATED and is the only path that exists when the fleet runs detached
    # from the task (State=Ready + processes alive) - see the abort branch below.
    # Restarts the 40 core.fleet.FLEET bots ONLY: not the watchdog itself, and
    # not dashboard.py (main_watchdog starts that through start_dashboard()).
    [switch]$MarkerRestart,
    [string]$TaskName = 'Kythera Watchdog',
    [int]$StopTimeoutSec = 90,
    [int]$StartTimeoutSec = 240,
    [int]$MarkerTimeoutSec = 180,
    [int]$DashboardPort = 5000
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("fleet_restart_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

function Write-Log {
    # Add-Content with explicit UTF8 - Tee-Object would write UTF-16 LE in
    # PS 5.1 and break grep/findstr over logs/.
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} - {1} - {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-Git {
    # Runs git against the repo root and returns its STDOUT lines; throws only
    # on a non-zero exit code.
    #
    # stderr handling is the load-bearing part (T-2026-KYT-9050-009). git
    # reports ordinary progress on stderr - "From https://github.com/...",
    # "* branch main -> FETCH_HEAD" - and PowerShell 5.1 converts native stderr
    # into ErrorRecords as soon as the stream is merged into the pipeline. That
    # merge is not under this script's control: it happens whenever the
    # OPERATOR invokes the script with `2>&1`, a reflex when capturing output.
    # With $ErrorActionPreference = 'Stop' the first progress line then
    # terminates, and the pull's catch reported
    #   "Pull failed: From https://github.com/ziagl888/Kythera"
    # for a pull that had in fact fast-forwarded the checkout - twice, in
    # logs/fleet_restart_20260726_232251.log and _20260801_192843.log, each
    # time aborting a restart that had to be re-run by hand.
    #
    # So: merge stderr explicitly (deterministic across hosts), demote errors
    # for the duration of the call, and let the EXIT CODE be the only verdict.
    # Progress lines are logged instead of discarded.
    param([string[]]$GitArgs)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $merged = & git -C $RepoRoot @GitArgs 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    $stdoutLines = @()
    $stderrLines = @()
    foreach ($record in @($merged)) {
        if ($null -eq $record) { continue }
        if ($record -is [System.Management.Automation.ErrorRecord]) {
            # Read the message off the exception: [string] on an ErrorRecord
            # that wraps an EMPTY stderr line renders the type name
            # ("System.Management.Automation.RemoteException") instead of the
            # blank line, which would end up in the operator-facing log.
            $text = if ($record.Exception) { [string]$record.Exception.Message } else { [string]$record }
            $stderrLines += $text
        } else {
            $stdoutLines += [string]$record
        }
    }
    foreach ($line in $stderrLines) {
        if ($line.Trim()) { Write-Log ("  git: {0}" -f $line.Trim()) }
    }

    if ($exitCode -ne 0) {
        $detail = if ($stderrLines) { " - " + (($stderrLines | ForEach-Object { $_.Trim() }) -join ' | ') } else { '' }
        throw ("git {0} failed with exit code {1}{2}" -f ($GitArgs -join ' '), $exitCode, $detail)
    }
    return $stdoutLines
}

function Get-FleetBotProcesses {
    # Fleet fingerprint: python processes whose PARENT is a LIVE python
    # process (the watchdog). PID/ParentPID/CreationDate are readable without
    # elevation even for the elevated fleet; CommandLine is not. Structurally
    # blind to orphans (a dead parent drops the child out of this set), which
    # is why stop-verification works on a PID snapshot taken BEFORE the stop,
    # not on this fingerprint. Never used for killing.
    $all = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'")
    $byPid = @{}
    foreach ($p in $all) { $byPid[[int64]$p.ProcessId] = $true }
    return @($all | Where-Object { $byPid.ContainsKey([int64]$_.ParentProcessId) })
}

function Get-AlivePids {
    # Which of the given PIDs still exist as python processes. Works
    # unelevated and keeps working after the parent died (unlike the
    # fingerprint above).
    param([int64[]]$Pids)
    if (-not $Pids -or $Pids.Count -eq 0) { return @() }
    $alive = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
        Where-Object { $Pids -contains [int64]$_.ProcessId })
    return @($alive | ForEach-Object { [int64]$_.ProcessId })
}

function Get-WatchdogTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}

# --- Preflight ----------------------------------------------------------------

Write-Log ("=== Kythera fleet restart (T-2026-CU-9050-074) - repo: {0} ===" -f $RepoRoot)

try {
    $task = Get-WatchdogTask
} catch {
    Write-Log ("Scheduled task '{0}' not found/readable: {1}" -f $TaskName, $_.Exception.Message) 'ERROR'
    Write-Log "Without the task there is no UAC-free stop/start path - aborting." 'ERROR'
    exit 1
}
Write-Log ("Task '{0}': State={1}, User={2}, RunLevel={3}" -f $TaskName, $task.State, $task.Principal.UserId, $task.Principal.RunLevel)

$bots = Get-FleetBotProcesses
Write-Log ("Fleet bot processes (python-with-python-parent): {0}" -f $bots.Count)

try {
    $branch = [string](Invoke-Git @('rev-parse', '--abbrev-ref', 'HEAD'))
    Invoke-Git @('fetch', 'origin') | Out-Null
    $head = [string](Invoke-Git @('rev-parse', '--short', 'HEAD'))
    $behind = [int][string](Invoke-Git @('rev-list', '--count', 'HEAD..origin/main'))
} catch {
    Write-Log ("Git preflight failed: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Log "Fleet untouched - nothing was stopped or started." 'ERROR'
    exit 1
}
Write-Log ("Branch={0}, HEAD={1}, behind origin/main: {2} commit(s)" -f $branch, $head, $behind)
if ($behind -gt 0) {
    Invoke-Git @('log', '--oneline', 'HEAD..origin/main') |
        Select-Object -First 20 | ForEach-Object { Write-Log ("  incoming: {0}" -f $_) }
}

# Code-age canary (T-2026-KYT-9050-071): is what is RUNNING older than what is
# CHECKED OUT? "behind origin/main" above answers a different question - the
# checkout can sit on HEAD while the fleet still runs last week's processes,
# which is what a reboot leaves behind (it starts the fleet without pulling).
# Advisory only: it never changes the exit code, it just makes the state loud.
try {
    # --repo explicitly: run from a worktree the canary would otherwise compare
    # the LIVE processes against the WORKTREE's HEAD and read as stale.
    $ageOut = & python (Join-Path $RepoRoot 'tools\ops\fleet_code_age.py') '--repo' $RepoRoot 2>&1
    foreach ($line in @($ageOut)) { if ("$line".Trim()) { Write-Log ("  age: {0}" -f "$line".Trim()) } }
} catch {
    Write-Log ("  age: canary could not run ({0}) - not treated as a verdict." -f $_.Exception.Message) 'WARN'
}

if ($DryRun) {
    Write-Log ("DryRun - nothing pulled, stopped, or started. Log: {0}" -f $LogFile)
    exit 0
}

# --- Step 1: pull (fleet still running - a failure here changes nothing) ------

if ($SkipPull) {
    Write-Log ("SkipPull set - restarting on the checked-out code ({0} @ {1})." -f $branch, $head)
} elseif ($branch -ne 'main') {
    # Checked BEFORE the behind-count: a feature branch that already contains
    # origin/main reports behind=0, and the fleet must never be silently
    # restarted on non-main code.
    Write-Log ("Checkout is on '{0}', not 'main' - refusing. Fix the checkout, or force with -SkipPull." -f $branch) 'ERROR'
    exit 1
} elseif ($behind -eq 0) {
    Write-Log "Checkout already at origin/main - nothing to pull."
} else {
    try {
        Invoke-Git @('pull', '--ff-only', 'origin', 'main') | ForEach-Object { Write-Log ("  {0}" -f $_) }
    } catch {
        Write-Log ("Pull failed: {0}" -f $_.Exception.Message) 'ERROR'
        Write-Log "Fleet untouched. Resolve the checkout manually (untracked collision? diverged?), then re-run." 'ERROR'
        exit 1
    }
    $head = [string](Invoke-Git @('rev-parse', '--short', 'HEAD'))
    Write-Log ("Pulled - HEAD now {0}" -f $head)
}

# --- Step 2: stop the fleet via the scheduled task -----------------------------

# --- Step 2a: marker restart (T-2026-KYT-9050-071) ------------------------------
#
# The UAC-free path the task-based stop/start cannot offer: write one
# control/restart/<script> marker per FLEET entry and let the RUNNING watchdog
# recycle each bot on its next cycle. This is exactly what the dashboard's
# restart button does (dashboard.restart_all -> core.process_control.request_restart).
#
# Deliberately NOT calling unpark() as restart_process() does - a parked bot is
# parked on purpose, and quietly re-arming it during a code rollout is the kind
# of surprise this script exists to avoid. A parked bot simply never consumes
# its marker (main_watchdog checks is_parked BEFORE consume_restart), which
# leaves a harmless leftover file.
#
# LIMITS, both real: the watchdog does not restart ITSELF, so changes to
# main_watchdog.py or core/fleet.py still need the task; and dashboard.py is
# started through main_watchdog.start_dashboard(), not from core.fleet.FLEET, so
# it keeps running whatever it booted with.
if ($MarkerRestart) {
    Write-Log "MarkerRestart: recycling the FLEET bots through control/restart markers (unelevated, watchdog-driven)."
    $before = @{}
    foreach ($p in (Get-FleetBotProcesses)) { $before[[int64]$p.ProcessId] = $p.CreationDate }
    Write-Log ("  {0} fleet process(es) before" -f $before.Count)

    $py = Join-Path $RepoRoot 'tools\ops\_write_restart_markers.py'
    $written = & python $py 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log ("Writing restart markers failed: {0}" -f ($written -join ' | ')) 'ERROR'
        Write-Log "Fleet untouched." 'ERROR'
        exit 1
    }
    foreach ($line in @($written)) { if ("$line".Trim()) { Write-Log ("  {0}" -f "$line".Trim()) } }

    $markerDir = Join-Path $RepoRoot 'control\restart'
    $deadline = (Get-Date).AddSeconds($MarkerTimeoutSec)
    do {
        Start-Sleep -Seconds 5
        $left = @(Get-ChildItem -Path $markerDir -File -ErrorAction SilentlyContinue).Count
        Write-Log ("  waiting for the watchdog to consume markers - {0} left" -f $left)
    } while (((Get-Date) -lt $deadline) -and ($left -gt 0))

    if ($left -gt 0) {
        Write-Log ("{0} marker(s) still unconsumed after {1}s - the watchdog is not cycling. Some bots may already have restarted; check logs\watchdog.log." -f $left, $MarkerTimeoutSec) 'ERROR'
        exit 2
    }

    # Consumed markers alone are not proof: verify the PIDs actually turned over.
    $after = @(Get-FleetBotProcesses)
    $survivors = @($after | Where-Object { $before.ContainsKey([int64]$_.ProcessId) })
    Write-Log ("All markers consumed. {0} process(es) now, {1} of the old PIDs still alive." -f $after.Count, $survivors.Count)
    if ($survivors.Count -gt 0) {
        Write-Log ("  still on old PIDs (expected for parked bots, the watchdog itself and dashboard.py): {0}" -f (($survivors | ForEach-Object { $_.ProcessId }) -join ', ')) 'WARN'
    }
    Write-Log "MarkerRestart done. Watchdog and dashboard.py were NOT restarted - see the header."
    Write-Log ("Log: {0}" -f $LogFile)
    exit 0
}

# Snapshot the fleet PIDs while their parent is still alive: after the stop
# the parent is dead and the fingerprint goes blind, so orphan detection MUST
# poll these concrete PIDs instead. (May include python children of a running
# trainer - they are reported, never killed.)
$task = Get-WatchdogTask
$preStopPids = @((Get-FleetBotProcesses) | ForEach-Object { [int64]$_.ProcessId })
$stopVerified = $false

if ($task.State -eq 'Running') {
    Write-Log ("Stopping task '{0}' ({1} fleet PIDs snapshotted)..." -f $TaskName, $preStopPids.Count)
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        Write-Log ("Stop-ScheduledTask failed: {0}" -f $_.Exception.Message) 'ERROR'
        Write-Log "Likely the task-ACL gap (stop was never exercised before T-074). Needs a one-time elevated fix; the fleet keeps running." 'ERROR'
        exit 1
    }
    # From here on the stop has been issued: any unexpected error must NOT
    # surface as exit 1 ("fleet untouched") - it lands on exit 2 (unclear).
    try {
        $deadline = (Get-Date).AddSeconds($StopTimeoutSec)
        do {
            Start-Sleep -Seconds 5
            $task = Get-WatchdogTask
            $alive = Get-AlivePids -Pids $preStopPids
            Write-Log ("  waiting for stop - task={0}, snapshot PIDs alive={1}" -f $task.State, $alive.Count)
        } while (((Get-Date) -lt $deadline) -and (($task.State -eq 'Running') -or ($alive.Count -gt 0)))
    } catch {
        Write-Log ("Polling failed after the stop was issued: {0}" -f $_.Exception.Message) 'ERROR'
        Write-Log "Fleet state UNCLEAR - check task state and processes manually, then start the task if needed." 'ERROR'
        exit 2
    }
    if ($task.State -eq 'Running') {
        # Without this guard the script would fall through to Start-ScheduledTask
        # (a no-op on a running task), find the never-stopped dashboard on port
        # 5000, and report a restart that never happened.
        Write-Log ("Task still Running after {0}s - the stop did not take effect. Aborting; nothing was restarted." -f $StopTimeoutSec) 'ERROR'
        exit 3
    }
    if ($alive.Count -gt 0) {
        Write-Log ("{0} snapshot PID(s) still alive after the tree-stop: {1}." -f $alive.Count, ($alive -join ', ')) 'WARN'
        Write-Log "Fleet orphans among them are reaped by the next watchdog start (_terminate_orphan_fleet, P0.2); unrelated python children (e.g. a trainer's workers) are left alone." 'WARN'
    } else {
        Write-Log "All snapshotted fleet PIDs are gone - fleet stopped."
    }
    $stopVerified = $true
} elseif ($preStopPids.Count -gt 0) {
    # Either the 00:32 pattern - a watchdog started OUTSIDE the task holds the
    # fleet (starting the task now would spawn a second watchdog that instantly
    # exits on the single-instance mutex, and the old dashboard on port 5000
    # would fake a successful "restart" on old code) - or a trainer's python
    # workers. The fingerprint cannot tell them apart.
    Write-Log ("Task is not running (State={0}) but {1} python parent/child pair(s) are alive (PIDs: {2})." -f $task.State, $preStopPids.Count, ($preStopPids -join ', ')) 'ERROR'
    Write-Log "Either a watchdog running OUTSIDE the scheduled task (no UAC-free stop path exists for it) or a trainer's workers - identify the PIDs before stopping ANYTHING, then re-run. Fleet untouched." 'ERROR'
    # T-2026-KYT-9050-071: this state is not exotic - it is what a REBOOT leaves
    # behind (observed 2026-08-02: task Ready with LastTaskResult=15, watchdog
    # alive and supervising 41 children). Refusing was correct but left no way
    # forward, so 45 merged commits sat undeployed for 13 h, a money-path fix
    # among them. There IS an unelevated path: the running watchdog consumes
    # control/restart/<script> markers within one cycle. Name it here instead of
    # making the next operator rediscover it.
    # Root cause, measured in T-2026-KYT-9050-076: the boot-time clock correction
    # makes the Task Scheduler fire the boot trigger twice; the second watchdog
    # reaps the first through the documented mutex recovery (psutil terminate =
    # exit 15 on Windows), and the task records the reaped instance. Nothing is
    # broken - only the Scheduler's ownership of the survivor is lost. The
    # permanent fix is a boot-trigger delay; see the doc.
    Write-Log "This is the known clock-jump pattern (LastTaskResult=15) - docs\WATCHDOG_TASK_DETACH.md" 'ERROR'
    Write-Log "Two ways forward:" 'ERROR'
    Write-Log ("  (a) UNELEVATED, recycles the {0} FLEET bots on the pulled code (NOT the watchdog, NOT dashboard.py):" -f $preStopPids.Count) 'ERROR'
    Write-Log ("      powershell -ExecutionPolicy Bypass -File tools\restart_fleet.ps1 -MarkerRestart") 'ERROR'
    Write-Log "  (b) ELEVATED, restores task ownership - watchdog FIRST, else it respawns the children:" 'ERROR'
    Write-Log "      Stop-Process -Id <watchdog-pid> -Force" 'ERROR'
    Write-Log "      Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ? ParentProcessId -eq <watchdog-pid> | % { Stop-Process -Id `$_.ProcessId -Force }" 'ERROR'
    Write-Log ("      Start-ScheduledTask -TaskName '{0}'" -f $TaskName) 'ERROR'
    Write-Log "      (the watchdog is the python process with the most python children; its CommandLine is unreadable unelevated)" 'ERROR'
    exit 1
} else {
    Write-Log ("Task not running (State={0}) and no fleet processes found - skipping stop, going straight to start." -f $task.State) 'WARN'
}

# --- Step 3: start the fleet and verify ----------------------------------------

# Soundness check for the success criterion: if port 5000 is ALREADY bound
# and this run did not just verify a stop, an unmanaged (orphan/manual)
# dashboard holds it - it would fake the success gate below, and the mutex
# would turn the task start into a no-op anyway. Refuse rather than guess.
$portBusy = Test-NetConnection -ComputerName 'localhost' -Port $DashboardPort -InformationLevel Quiet -WarningAction SilentlyContinue
if ($portBusy -and -not $stopVerified) {
    Write-Log ("Port {0} is already in use, but no stop was verified in this run - an unmanaged dashboard holds it and would fake the success check. Aborting; nothing was started." -f $DashboardPort) 'ERROR'
    exit 1
}
if ($portBusy) {
    # Corrected 2026-08-02 (T-2026-KYT-9050-071): dashboard.py is NOT in
    # core.fleet.FLEET - main_watchdog starts it through its own
    # start_dashboard(). The gate below still holds (the watchdog does bring the
    # dashboard back), but the reason stated here was wrong, and it matters:
    # because dashboard.py is outside FLEET, the -MarkerRestart path does not
    # recycle it.
    Write-Log ("Port {0} is still bound after the stop (orphan dashboard) - the watchdog start reaps it (main_watchdog.start_dashboard, NOT core.fleet.FLEET); relying on the re-confirmation below." -f $DashboardPort) 'WARN'
}

Write-Log ("Starting task '{0}'..." -f $TaskName)
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    # The one window where the script has made things worse than it found
    # them - say so loudly, in the log file the operator will read.
    Write-Log ("Start-ScheduledTask FAILED: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Log ("THE FLEET IS STOPPED AND WAS NOT RESTARTED. Run  Start-ScheduledTask -TaskName '{0}'  manually (or start it elevated)." -f $TaskName) 'ERROR'
    exit 4
}

try {
    $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
    $verified = $false
    do {
        Start-Sleep -Seconds 10
        $task = Get-WatchdogTask
        $bots = Get-FleetBotProcesses
        $dashboardUp = Test-NetConnection -ComputerName 'localhost' -Port $DashboardPort -InformationLevel Quiet -WarningAction SilentlyContinue
        # Port 5000 alone is not proof: an orphaned OLD dashboard can hold the
        # port while the new watchdog import-crashed (task flips back to Ready).
        # Success = task still Running AND the dashboard answers.
        $verified = ($dashboardUp -and ($task.State -eq 'Running'))
        Write-Log ("  waiting for start - task={0}, bots={1}, dashboard:{2}={3}" -f $task.State, $bots.Count, $DashboardPort, $dashboardUp)
    } while (((Get-Date) -lt $deadline) -and (-not $verified))

    if ($verified) {
        # Re-confirm: a watchdog that import-crashes AFTER the first positive
        # poll (dotenv/PYTHONPATH, the known failure class) flips the task back
        # to Ready within seconds - catch that before declaring success.
        Start-Sleep -Seconds 15
        $task = Get-WatchdogTask
        if ($task.State -ne 'Running') {
            Write-Log ("Re-confirmation FAILED - task fell back to {0} after the first positive poll (watchdog died right after start)." -f $task.State) 'ERROR'
            $verified = $false
        }
    }
} catch {
    Write-Log ("Polling failed during start verification: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Log "Fleet state UNCLEAR - verify manually: task state, dashboard, watchdog.log." 'ERROR'
    exit 2
}

if ($verified) {
    $leftover = Get-AlivePids -Pids $preStopPids
    if ($leftover.Count -gt 0) {
        Write-Log ("{0} pre-stop PID(s) still alive: {1} - fleet orphans get reaped by the watchdog start; anything left is not fleet (e.g. trainer workers)." -f $leftover.Count, ($leftover -join ', ')) 'WARN'
    }
    Write-Log ("Task is Running and the dashboard is up. Bots start staggered over a few minutes - check watchdog.log for 'Alle Systeme started'.")
    Write-Log ("=== Restart complete - fleet on {0}. Log: {1} ===" -f $head, $LogFile)
    exit 0
} else {
    Write-Log ("Start not verified after {0}s (task={1}, dashboard up={2})." -f $StartTimeoutSec, $task.State, $dashboardUp) 'ERROR'
    Write-Log "If the task is not Running the watchdog likely died at startup - check logs\watchdog_debug.log and watchdog.log (dotenv/PYTHONPATH are the known failure class)." 'ERROR'
    exit 2
}
