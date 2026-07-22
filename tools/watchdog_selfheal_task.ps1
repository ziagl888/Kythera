<#
.SYNOPSIS
    Configure restart-on-failure on the "Kythera Watchdog" scheduled task so a
    dead launcher/watchdog auto-restarts and the OUTER supervision net self-heals
    (T-2026-KYT-9050-025).

.DESCRIPTION
    The "Kythera Watchdog" task has a single <BootTrigger/> and NO restart-on-
    failure (RestartCount=0, RestartInterval unset). When the launcher/watchdog
    dies (e.g. the psutil open_files access-violation 0xC0000005 fixed in
    main_watchdog.py this same task), the task flips Running -> Ready and nothing
    re-launches it until the next reboot; the fleet it spawned keeps running
    DETACHED as orphans with no supervisor. On 2026-07-22 that produced an
    unsupervised orphan fleet for ~1h.

    This script adds RestartOnFailure (RestartCount + RestartInterval) to the task,
    PRESERVING every other setting (MultipleInstances=IgnoreNew, battery flags,
    principal). Restart-on-failure fires ONLY on a genuine failure (the launcher
    process exiting with a non-zero code) - it does NOT fire on a deliberate
    Stop-ScheduledTask (which ends success-class, SCHED_S_TASK_TERMINATED), so an
    operator stop still stays stopped and tools/restart_fleet.ps1 keeps working.

    REQUIRES launcher v6 (launch_watchdog.cmd propagates the python exit code).
    v5's last line was the ledger echo, which returns 0, so a python crash reached
    the task as LastTaskResult 0x0 ("success") and restart-on-failure could never
    see a failure to act on. This script refuses to arm restart-on-failure unless
    the checked-out launcher is v6 (or newer), to avoid a false sense of coverage.

    COMPOSITION SAFETY (why this cannot spawn a second fighting watchdog):
      1. MultipleInstancesPolicy=IgnoreNew - the scheduler will not start a second
         task instance while one is still running.
      2. main_watchdog._acquire_single_instance_lock() - a Global\ named mutex; a
         second watchdog that somehow starts exits immediately (or reaps a genuine
         orphan-watchdog and retries exactly once).
      3. A crashed watchdog's process is gone, so the OS has already released its
         mutex; the restart acquires it cleanly, then _terminate_orphan_fleet()
         reaps the orphaned bots BEFORE spawning a fresh fleet - exactly the manual
         recovery from 2026-07-22, now automatic. Only one process ever reaps.

.PARAMETER Apply
    Actually change the task. WITHOUT this switch the script is a DRY RUN: it
    prints the current state and the exact change it WOULD make, and touches
    nothing. Run the dry run first.

.PARAMETER TaskName
    Scheduled task to configure. Default: 'Kythera Watchdog'.

.PARAMETER RestartCount
    Number of restart attempts after a failure (Task Scheduler bounds: >= 1).
    Default 3.

.PARAMETER RestartIntervalMinutes
    Minutes between restart attempts (Task Scheduler bounds: 1..30). Default 1.

.NOTES
    Run ELEVATED (the task is RunLevel Highest, password logon). This is an
    operator action (OPUS-HANDOFF section 6) - deploy is Michi-gated.

    Exit codes:
      0 - already configured (no-op) OR applied+verified OR dry run printed
      1 - task not found / not readable
      2 - launcher is not v6+ (arm refused) - pull the fix first
      3 - apply failed, OR an invalid parameter (RestartCount/RestartInterval out
          of range); password-logon tasks may need -User/-Password
      4 - applied but verification did not read back the expected values, OR the
          principal (LogonType/UserId) changed - re-apply with -User/-Password

.EXAMPLE
    # 1. dry run - shows the planned change, changes nothing
    powershell -ExecutionPolicy Bypass -File tools\watchdog_selfheal_task.ps1

.EXAMPLE
    # 2. apply (elevated)
    powershell -ExecutionPolicy Bypass -File tools\watchdog_selfheal_task.ps1 -Apply
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$TaskName = 'Kythera Watchdog',
    [int]$RestartCount = 3,
    [int]$RestartIntervalMinutes = 1
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LauncherPath = Join-Path $RepoRoot 'launch_watchdog.cmd'

function Write-Line { param([string]$m, [string]$lvl = 'INFO'); Write-Host ("{0} - {1}" -f $lvl, $m) }

if ($RestartIntervalMinutes -lt 1 -or $RestartIntervalMinutes -gt 30) {
    Write-Line "RestartIntervalMinutes must be between 1 and 30 (Task Scheduler bound)." 'ERROR'
    exit 3
}
if ($RestartCount -lt 1) {
    Write-Line "RestartCount must be >= 1." 'ERROR'
    exit 3
}
$RestartInterval = "PT{0}M" -f $RestartIntervalMinutes

# --- Preflight: task readable? -----------------------------------------------
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Line ("Scheduled task '{0}' not found/readable: {1}" -f $TaskName, $_.Exception.Message) 'ERROR'
    exit 1
}
Write-Line ("Task '{0}': State={1}, User={2}, RunLevel={3}" -f `
        $TaskName, $task.State, $task.Principal.UserId, $task.Principal.RunLevel)
Write-Line ("Current: RestartCount={0}, RestartInterval={1}, MultipleInstances={2}" -f `
        $task.Settings.RestartCount, $task.Settings.RestartInterval, $task.Settings.MultipleInstances)

# --- Preflight: launcher must be v6+ (propagates the python exit code) --------
# Without exit-code propagation the task never sees a crash as a failure, so
# restart-on-failure would be armed but inert - refuse rather than mislead.
$launcherV6 = $false
if (Test-Path $LauncherPath) {
    $launcherText = [IO.File]::ReadAllText($LauncherPath)
    $launcherV6 = ($launcherText -match 'launcher v6') -and ($launcherText -match 'exit /b %WD_EXIT%')
} else {
    Write-Line ("launch_watchdog.cmd not found at {0}" -f $LauncherPath) 'ERROR'
    exit 2
}
if (-not $launcherV6) {
    Write-Line "launch_watchdog.cmd is not v6+ (no exit-code propagation)." 'ERROR'
    Write-Line "Pull the T-2026-KYT-9050-025 fix first; restart-on-failure would be inert on v5." 'ERROR'
    exit 2
}
Write-Line "Launcher v6 detected (exit-code propagation present)." 'INFO'

# --- Idempotency: already configured? ----------------------------------------
$already = ($task.Settings.RestartCount -eq $RestartCount) -and ($task.Settings.RestartInterval -eq $RestartInterval)
if ($already) {
    Write-Line ("Already configured: RestartCount={0}, RestartInterval={1} - nothing to do." -f `
            $RestartCount, $RestartInterval)
    exit 0
}

Write-Line ("PLAN: set RestartCount={0}, RestartInterval={1} (all other settings preserved)." -f `
        $RestartCount, $RestartInterval)

if (-not $Apply) {
    Write-Line "DRY RUN - nothing changed. Re-run ELEVATED with -Apply to make the change." 'WARN'
    Write-Line "Equivalent manual command (elevated):" 'INFO'
    Write-Line ("  `$s = (Get-ScheduledTask -TaskName '{0}').Settings" -f $TaskName)
    Write-Line ("  `$s.RestartCount = {0}; `$s.RestartInterval = '{1}'" -f $RestartCount, $RestartInterval)
    Write-Line ("  Set-ScheduledTask -TaskName '{0}' -Settings `$s" -f $TaskName)
    exit 0
}

# --- Apply --------------------------------------------------------------------
# Snapshot the principal so the read-back can prove it was NOT altered. On a
# password-logon task, Set-ScheduledTask -Settings without -User/-Password can
# succeed while silently converting the principal to an S4U/interactive-token
# logon (dropping the stored password) - which would break RunLevel Highest /
# boot start. We verify LogonType + UserId are unchanged, not just the two
# restart fields.
$beforeLogon = $task.Principal.LogonType
$beforeUser = $task.Principal.UserId
$settings = $task.Settings
$settings.RestartCount = $RestartCount
$settings.RestartInterval = $RestartInterval
try {
    Set-ScheduledTask -TaskName $TaskName -Settings $settings -ErrorAction Stop | Out-Null
} catch {
    Write-Line ("Set-ScheduledTask failed: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Line "Password-logon tasks sometimes require the credential to be re-supplied. Retry with:" 'ERROR'
    Write-Line ("  Set-ScheduledTask -TaskName '{0}' -Settings `$s -User '{1}' -Password '<password>'" -f `
            $TaskName, $beforeUser) 'ERROR'
    exit 3
}

# --- Verify -------------------------------------------------------------------
$after = Get-ScheduledTask -TaskName $TaskName
Write-Line ("After: RestartCount={0}, RestartInterval={1}, MultipleInstances={2}, LogonType={3}, User={4}" -f `
        $after.Settings.RestartCount, $after.Settings.RestartInterval, $after.Settings.MultipleInstances, `
        $after.Principal.LogonType, $after.Principal.UserId)
$fieldsOk = ($after.Settings.RestartCount -eq $RestartCount) -and ($after.Settings.RestartInterval -eq $RestartInterval)
$principalOk = ($after.Principal.LogonType -eq $beforeLogon) -and ($after.Principal.UserId -eq $beforeUser)
if ($fieldsOk -and $principalOk) {
    Write-Line "Restart-on-failure configured and verified (principal preserved). The outer supervision net now self-heals." 'INFO'
    exit 0
} elseif (-not $principalOk) {
    Write-Line ("Principal CHANGED (LogonType {0}->{1}, User {2}->{3}) - Set-ScheduledTask dropped the password logon." -f `
            $beforeLogon, $after.Principal.LogonType, $beforeUser, $after.Principal.UserId) 'ERROR'
    Write-Line ("Re-apply preserving the credential: Set-ScheduledTask -TaskName '{0}' -Settings `$s -User '{1}' -Password '<password>'" -f `
            $TaskName, $beforeUser) 'ERROR'
    exit 4
} else {
    Write-Line "Verification MISMATCH - the task did not read back the expected restart values. Inspect manually." 'ERROR'
    exit 4
}
