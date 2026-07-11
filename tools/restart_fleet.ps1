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
      0 - restart complete and verified (task Running + dashboard up)
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
    [string]$TaskName = 'Kythera Watchdog',
    [int]$StopTimeoutSec = 90,
    [int]$StartTimeoutSec = 240,
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
    # Runs git against the repo root and returns stdout lines; throws on a
    # non-zero exit code. stderr is deliberately NOT redirected - in PS 5.1
    # that would wrap each line in an ErrorRecord and poison $? even on
    # success.
    param([string[]]$GitArgs)
    $out = & git -C $RepoRoot @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw ("git {0} failed with exit code {1}" -f ($GitArgs -join ' '), $LASTEXITCODE)
    }
    return $out
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

$branch = [string](Invoke-Git @('rev-parse', '--abbrev-ref', 'HEAD'))
Invoke-Git @('fetch', 'origin') | Out-Null
$head = [string](Invoke-Git @('rev-parse', '--short', 'HEAD'))
$behind = [int][string](Invoke-Git @('rev-list', '--count', 'HEAD..origin/main'))
Write-Log ("Branch={0}, HEAD={1}, behind origin/main: {2} commit(s)" -f $branch, $head, $behind)
if ($behind -gt 0) {
    Invoke-Git @('log', '--oneline', 'HEAD..origin/main') |
        Select-Object -First 20 | ForEach-Object { Write-Log ("  incoming: {0}" -f $_) }
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

# Snapshot the fleet PIDs while their parent is still alive: after the stop
# the parent is dead and the fingerprint goes blind, so orphan detection MUST
# poll these concrete PIDs instead. (May include python children of a running
# trainer - they are reported, never killed.)
$task = Get-WatchdogTask
$preStopPids = @((Get-FleetBotProcesses) | ForEach-Object { [int64]$_.ProcessId })

if ($task.State -eq 'Running') {
    Write-Log ("Stopping task '{0}' ({1} fleet PIDs snapshotted)..." -f $TaskName, $preStopPids.Count)
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        Write-Log ("Stop-ScheduledTask failed: {0}" -f $_.Exception.Message) 'ERROR'
        Write-Log "Likely the task-ACL gap (stop was never exercised before T-074). Needs a one-time elevated fix; the fleet keeps running." 'ERROR'
        exit 1
    }
    $deadline = (Get-Date).AddSeconds($StopTimeoutSec)
    do {
        Start-Sleep -Seconds 5
        $task = Get-WatchdogTask
        $alive = Get-AlivePids -Pids $preStopPids
        Write-Log ("  waiting for stop - task={0}, snapshot PIDs alive={1}" -f $task.State, $alive.Count)
    } while (((Get-Date) -lt $deadline) -and (($task.State -eq 'Running') -or ($alive.Count -gt 0)))
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
} elseif ($preStopPids.Count -gt 0) {
    # The 00:32 pattern: a watchdog started OUTSIDE the task (manual console)
    # holds the fleet. Starting the task now would spawn a second watchdog
    # that instantly exits on the single-instance mutex - and the old
    # dashboard on port 5000 would fake a successful "restart" on old code.
    Write-Log ("Task is not running (State={0}) but {1} fleet process(es) are alive - the fleet runs OUTSIDE the scheduled task." -f $task.State, $preStopPids.Count) 'ERROR'
    Write-Log "No UAC-free stop path exists for that watchdog. Stop it manually (its console / elevated), then re-run. Fleet untouched." 'ERROR'
    exit 1
} else {
    Write-Log ("Task not running (State={0}) and no fleet processes found - skipping stop, going straight to start." -f $task.State) 'WARN'
}

# --- Step 3: start the fleet and verify ----------------------------------------

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
