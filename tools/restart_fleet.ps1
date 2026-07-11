<#
.SYNOPSIS
    UAC-free Kythera fleet restart cycle: git pull, then stop + start the
    "Kythera Watchdog" scheduled task (T-2026-CU-9050-074).

.DESCRIPTION
    Runs entirely WITHOUT elevation. The scheduled task "Kythera Watchdog"
    (registered 2026-07-11, T-2026-CU-9050-068: user Michael, password logon,
    RunLevel Highest) is start-/stoppable by its own user; the Task Scheduler
    applies the elevated token for us. The script never kills processes
    itself: stopping goes through Stop-ScheduledTask, and any bot that
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
    Restart the fleet on the code that is already checked out.

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
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} - {1} - {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $line | Tee-Object -FilePath $LogFile -Append
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
    # Bots = python processes whose PARENT is also a python process (the
    # watchdog). PID/ParentPID/CreationDate are readable without elevation
    # even for the elevated fleet; CommandLine/Path are not - so this is the
    # only unelevated-safe fleet fingerprint. Python children spawned by
    # trainers would match too, which is fine: the count is used for
    # logging/waiting only, never for killing.
    $all = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'")
    $byPid = @{}
    foreach ($p in $all) { $byPid[[int64]$p.ProcessId] = $true }
    return @($all | Where-Object { $byPid.ContainsKey([int64]$_.ParentProcessId) })
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
    Write-Log ("SkipPull set - restarting on the checked-out code ({0})." -f $head)
} elseif ($behind -eq 0) {
    Write-Log "Checkout already at origin/main - nothing to pull."
} else {
    if ($branch -ne 'main') {
        Write-Log ("Checkout is on '{0}', not 'main' - refusing to pull. Fix the checkout or use -SkipPull." -f $branch) 'ERROR'
        exit 1
    }
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

if ($task.State -eq 'Running') {
    Write-Log ("Stopping task '{0}'..." -f $TaskName)
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
        $bots = Get-FleetBotProcesses
        Write-Log ("  waiting for stop - task={0}, bots={1}" -f $task.State, $bots.Count)
    } while (((Get-Date) -lt $deadline) -and (($task.State -eq 'Running') -or ($bots.Count -gt 0)))
    if ($task.State -eq 'Running') {
        # Without this guard the script would fall through to Start-ScheduledTask
        # (a no-op on a running task), find the never-stopped dashboard on port
        # 5000, and report a restart that never happened.
        Write-Log ("Task still Running after {0}s - the stop did not take effect. Aborting; nothing was restarted." -f $StopTimeoutSec) 'ERROR'
        exit 3
    }
    if ($bots.Count -gt 0) {
        Write-Log ("{0} bot process(es) survived the tree-stop as orphans - OK: the next watchdog start reaps them (_terminate_orphan_fleet, P0.2)." -f $bots.Count) 'WARN'
    } else {
        Write-Log "Fleet fully stopped."
    }
} else {
    Write-Log ("Task not running (State={0}) - skipping stop." -f $task.State) 'WARN'
}

# --- Step 3: start the fleet and verify ----------------------------------------

Write-Log ("Starting task '{0}'..." -f $TaskName)
Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds($StartTimeoutSec)
$dashboardUp = $false
do {
    Start-Sleep -Seconds 10
    $task = Get-WatchdogTask
    $bots = Get-FleetBotProcesses
    $dashboardUp = Test-NetConnection -ComputerName 'localhost' -Port $DashboardPort -InformationLevel Quiet -WarningAction SilentlyContinue
    Write-Log ("  waiting for start - task={0}, bots={1}, dashboard:{2}={3}" -f $task.State, $bots.Count, $DashboardPort, $dashboardUp)
} while (((Get-Date) -lt $deadline) -and (-not $dashboardUp))

if ($dashboardUp) {
    Write-Log ("Dashboard is up. Bots start staggered over ~215s - current count {0} keeps rising; check watchdog.log for 'Alle Systeme started'." -f $bots.Count)
    Write-Log ("=== Restart complete - fleet on {0}. Log: {1} ===" -f $head, $LogFile)
    exit 0
} else {
    Write-Log ("Dashboard not reachable after {0}s (task={1})." -f $StartTimeoutSec, $task.State) 'ERROR'
    Write-Log "Check logs\watchdog_debug.log and watchdog.log - dotenv/PYTHONPATH issues are the known failure class here." 'ERROR'
    exit 2
}
