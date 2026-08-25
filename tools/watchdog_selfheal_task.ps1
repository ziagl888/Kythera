<#
.SYNOPSIS
    Harden the "Kythera Watchdog" scheduled task in ONE write: remove the 72h
    ExecutionTimeLimit that force-kills the fleet (T-2026-KYT-9050-151) and arm
    restart-on-failure so a dead launcher/watchdog auto-restarts and the OUTER
    supervision net self-heals (T-2026-KYT-9050-025).

.DESCRIPTION
    (A) ExecutionTimeLimit. The task carries ExecutionTimeLimit=PT72H. After
    exactly 72.0h of uptime the Task Scheduler force-terminates it: CTRL_BREAK
    reaches the whole process group, every bot dies with 0xC000013A
    (STATUS_CONTROL_C_EXIT), and the bots the watchdog restarts in its final
    seconds survive as UNSUPERVISED ORPHANS that keep trading and logging while
    the task reads State=Ready. Measured three times: 2026-08-12 15:40 -> dead
    08-15 15:41 (23.6h orphan phase), 08-17 16:57 -> dead 08-20 16:58 (15.9h),
    08-21 08:49 -> dead 08-24 08:49 (28h). Every run that stayed under 72h ended
    cleanly with a ~2min tail. This is the ROOT CAUSE; setting the limit to PT0S
    (unlimited) is the actual fix.

    Restart-on-failure below does NOT cover it: a termination via
    ExecutionTimeLimit ends success-class (SCHED_S_TASK_TERMINATED), so the task
    never reports a failure for restart-on-failure to act on.

    Both fields live on the SAME Settings object and are written by the SAME
    Set-ScheduledTask call - and that call is the step that can silently drop a
    password-logon principal. Doing both in one write halves that exposure
    compared to two separate scripts.

    UNVERIFIED, do not assume: whether the scheduler re-reads ExecutionTimeLimit
    for an ALREADY RUNNING instance. If it does not, the current run still dies
    at its old deadline. The guaranteed path is apply + one tools/restart_fleet.ps1
    at an operator-chosen time - the new run then starts under PT0S.

    (B) Restart-on-failure. The task has a single <BootTrigger/> and NO restart-on-
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

.PARAMETER ExecutionTimeLimit
    ISO-8601 duration for the task's run-time cap. Default 'PT0S' = unlimited,
    which is what removes the 72h kill. Pass the current value to leave it alone.

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
    [int]$RestartIntervalMinutes = 1,
    [string]$ExecutionTimeLimit = 'PT0S'
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
# A malformed duration would be written verbatim and silently mis-cap the task.
if ($ExecutionTimeLimit -notmatch '^P(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$') {
    Write-Line ("ExecutionTimeLimit '{0}' is not an ISO-8601 duration (e.g. PT0S, PT72H)." -f $ExecutionTimeLimit) 'ERROR'
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
Write-Line ("Current: ExecutionTimeLimit={0}, RestartCount={1}, RestartInterval={2}, MultipleInstances={3}" -f `
        $task.Settings.ExecutionTimeLimit, $task.Settings.RestartCount, `
        $task.Settings.RestartInterval, $task.Settings.MultipleInstances)

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
$etlOk = ($task.Settings.ExecutionTimeLimit -eq $ExecutionTimeLimit)
$rofOk = ($task.Settings.RestartCount -eq $RestartCount) -and ($task.Settings.RestartInterval -eq $RestartInterval)
if ($etlOk -and $rofOk) {
    Write-Line ("Already configured: ExecutionTimeLimit={0}, RestartCount={1}, RestartInterval={2} - nothing to do." -f `
            $ExecutionTimeLimit, $RestartCount, $RestartInterval)
    exit 0
}

Write-Line "PLAN (one single Set-ScheduledTask write, all other settings preserved):"
if ($etlOk) {
    Write-Line ("  [ok]     ExecutionTimeLimit already {0}" -f $ExecutionTimeLimit)
} else {
    Write-Line ("  [CHANGE] ExecutionTimeLimit : {0} -> {1}   (removes the 72h kill)" -f `
            $task.Settings.ExecutionTimeLimit, $ExecutionTimeLimit)
}
if ($rofOk) {
    Write-Line ("  [ok]     restart-on-failure already {0}/{1}" -f $RestartCount, $RestartInterval)
} else {
    Write-Line ("  [CHANGE] RestartCount/Interval : {0}/{1} -> {2}/{3}   (crash net only, NOT the 72h kill)" -f `
            $task.Settings.RestartCount, $task.Settings.RestartInterval, $RestartCount, $RestartInterval)
}

if (-not $Apply) {
    Write-Line "DRY RUN - nothing changed. Re-run ELEVATED with -Apply to make the change." 'WARN'
    Write-Line "Equivalent manual command (elevated):" 'INFO'
    Write-Line ("  `$s = (Get-ScheduledTask -TaskName '{0}').Settings" -f $TaskName)
    Write-Line ("  `$s.ExecutionTimeLimit = '{0}'" -f $ExecutionTimeLimit)
    Write-Line ("  `$s.RestartCount = {0}; `$s.RestartInterval = '{1}'" -f $RestartCount, $RestartInterval)
    Write-Line ("  Set-ScheduledTask -TaskName '{0}' -Settings `$s" -f $TaskName)
    Write-Line "NOTE: this write neither stops nor starts anything - the running fleet is untouched." 'WARN'
    Write-Line "      UNVERIFIED whether the scheduler re-reads ExecutionTimeLimit for the ALREADY" 'WARN'
    Write-Line "      RUNNING instance. Guaranteed path: apply, then one tools/restart_fleet.ps1." 'WARN'
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
$settings.ExecutionTimeLimit = $ExecutionTimeLimit
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
Write-Line ("After: ExecutionTimeLimit={0}, RestartCount={1}, RestartInterval={2}, MultipleInstances={3}, LogonType={4}, User={5}" -f `
        $after.Settings.ExecutionTimeLimit, $after.Settings.RestartCount, $after.Settings.RestartInterval, `
        $after.Settings.MultipleInstances, $after.Principal.LogonType, $after.Principal.UserId)
$fieldsOk = ($after.Settings.ExecutionTimeLimit -eq $ExecutionTimeLimit) -and `
    ($after.Settings.RestartCount -eq $RestartCount) -and ($after.Settings.RestartInterval -eq $RestartInterval)
$principalOk = ($after.Principal.LogonType -eq $beforeLogon) -and ($after.Principal.UserId -eq $beforeUser)
if ($fieldsOk -and $principalOk) {
    Write-Line "Applied and verified (principal preserved): 72h kill removed, crash net armed." 'INFO'
    Write-Line "The running instance may still hold its old deadline - do one tools/restart_fleet.ps1 to be sure." 'WARN'
    exit 0
} elseif (-not $principalOk) {
    Write-Line ("Principal CHANGED (LogonType {0}->{1}, User {2}->{3}) - Set-ScheduledTask dropped the password logon." -f `
            $beforeLogon, $after.Principal.LogonType, $beforeUser, $after.Principal.UserId) 'ERROR'
    Write-Line ("Re-apply preserving the credential: Set-ScheduledTask -TaskName '{0}' -Settings `$s -User '{1}' -Password '<password>'" -f `
            $TaskName, $beforeUser) 'ERROR'
    exit 4
} else {
    Write-Line "Verification MISMATCH - the task did not read back the expected values. Inspect manually." 'ERROR'
    exit 4
}
