<#
.SYNOPSIS
    Harden the "Kythera Watchdog" scheduled task in ONE write: remove the 72h
    ExecutionTimeLimit that force-kills the fleet (T-2026-KYT-9050-151) and arm
    restart-on-failure so a dead launcher/watchdog auto-restarts and the OUTER
    supervision net self-heals (T-2026-KYT-9050-025).

.DESCRIPTION
    (A) ExecutionTimeLimit. Get-ScheduledTask reports ExecutionTimeLimit=PT72H, but
    that was never a configured value: the task XML had NO <ExecutionTimeLimit>
    element at all and PT72H is the Windows default (P3D) which Get-ScheduledTask
    materialises (measured 2026-08-31; T-2026-KYT-9050-151 and its CHANGELOG entry
    described it as a setting someone had made - that framing was wrong). After
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

    APPLY PATH (T-2026-KYT-9050-156): Export-ScheduledTask -> edit the element in
    the XML -> Register-ScheduledTask -Xml -Force. The previous
    `Set-ScheduledTask -Settings` path CANNOT write this task and shipped green
    because a dry run never writes. It fails two ways: without a credential with
    "The user name or password is incorrect" (Windows cannot read a stored task
    password back), and with one with "The task XML is missing a required element
    or attribute.(41,8):Count:" - it rewrites the WHOLE settings object, defaults
    and all, and the provider emits XML it does not validate itself.

    The password is PROMPTED (Read-Host -AsSecureString) or passed as
    -Credential; never a plaintext parameter, so it stays out of command lines
    and shell history. The original XML is backed up to %TEMP% before the write
    because -Force replaces the task.

    Run -SelfTest to exercise the XML transformation without a task, a
    credential or a write.

    Exit codes:
      0 - already configured (no-op) OR applied+verified OR dry run printed
      1 - task not found / not readable
      2 - launcher is not v6+ (arm refused) - pull the fix first
      3 - apply failed, OR an invalid parameter (RestartCount/RestartInterval out
          of range); password-logon tasks may need -User/-Password
      4 - applied but verification did not read back the expected values, OR the
          principal (LogonType/UserId) changed - re-apply with -User/-Password

.EXAMPLE
    # 0. self-test - pure XML transformation, no task, no credential, no write
    powershell -ExecutionPolicy Bypass -File tools\watchdog_selfheal_task.ps1 -SelfTest

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
    [string]$ExecutionTimeLimit = 'PT0S',
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$SkipRestartOnFailure,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LauncherPath = Join-Path $RepoRoot 'launch_watchdog.cmd'

function Write-Line { param([string]$m, [string]$lvl = 'INFO'); Write-Host ("{0} - {1}" -f $lvl, $m) }

function ConvertTo-DurationKey {
    # Task Scheduler expresses "no run-time limit" as PT0S OR as an empty/absent
    # value depending on how the definition was written and read back. Comparing
    # the raw strings would report a MISMATCH on a SUCCESSFUL apply and tell the
    # operator the change failed - so both spellings collapse to one key.
    param([string]$Duration)
    if ([string]::IsNullOrWhiteSpace($Duration) -or $Duration -eq 'PT0S') { return '<unlimited>' }
    return $Duration
}

function Format-Duration {
    param([string]$Duration)
    if ([string]::IsNullOrWhiteSpace($Duration)) { return '<none>' }
    return $Duration
}

function Set-TaskXmlSetting {
    <#
    .SYNOPSIS
        Insert or replace one element inside a task XML's <Settings> block.

    .DESCRIPTION
        Pure string transformation - no I/O, no Task Scheduler call - so -SelfTest
        can exercise it without touching the live task. That separation is the
        point of T-2026-KYT-9050-156: the previous CIM apply path shipped with a
        green dry run precisely because a dry run never writes, and it then failed
        twice against the real task.

        Placement: an existing element keeps its position and only its content
        changes; a new element is appended just before </Settings>. Windows
        normalises the order when Register-ScheduledTask re-serialises the
        definition - observed 2026-08-31 on this very task, where an element
        inserted after </IdleSettings> came back sorted ahead of
        <MultipleInstancesPolicy> and was accepted without complaint.
    #>
    param(
        [Parameter(Mandatory)][string]$Xml,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][string]$InnerXml
    )
    $element = "<{0}>{1}</{0}>" -f $Name, $InnerXml
    if ($Xml -match ("<{0}>" -f [regex]::Escape($Name))) {
        $pattern = "<{0}>.*?</{0}>" -f [regex]::Escape($Name)
        return [regex]::Replace($Xml, $pattern, $element, [System.Text.RegularExpressions.RegexOptions]::Singleline)
    }
    if ($Xml -notmatch '</Settings>') { throw "task XML has no </Settings> block - refusing to guess" }
    return $Xml -replace '\s*</Settings>', ("`r`n    " + $element + "`r`n  </Settings>")
}

function Invoke-SelfTest {
    # Exercises the transformation against the real task's XML shape. No writes,
    # no credentials, no scheduler calls - runnable anywhere.
    $sample = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Settings>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <IdleSettings>
      <Duration>PT10M</Duration>
    </IdleSettings>
  </Settings>
</Task>
'@
    $fail = 0
    function Assert([bool]$cond, [string]$what) {
        if ($cond) { Write-Line ("  PASS  {0}" -f $what) }
        else { Write-Line ("  FAIL  {0}" -f $what) 'ERROR'; $script:selfTestFailures++ }
    }
    $script:selfTestFailures = 0

    # 1. absent element gets inserted, exactly one line longer
    $out = Set-TaskXmlSetting -Xml $sample -Name 'ExecutionTimeLimit' -InnerXml 'PT0S'
    Assert ($out -match '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>') 'absent element is inserted'
    $before = ($sample -split "`r?`n").Count
    $after = ($out -split "`r?`n").Count
    Assert ($after -eq $before + 1) ("insert adds exactly one line (was {0}, now {1})" -f $before, $after)
    Assert ($out -match '</IdleSettings>[\s\S]*<ExecutionTimeLimit>') 'inserted before </Settings>, after the existing block'

    # 2. present element is replaced in place, no line growth
    $out2 = Set-TaskXmlSetting -Xml $out -Name 'ExecutionTimeLimit' -InnerXml 'PT72H'
    Assert ($out2 -match '<ExecutionTimeLimit>PT72H</ExecutionTimeLimit>') 'existing element is replaced'
    Assert (-not ($out2 -match 'PT0S')) 'old value is gone'
    Assert ((($out2 -split "`r?`n").Count) -eq $after) 'replace does not add a line'

    # 3. idempotent - applying the same value twice changes nothing
    $out3 = Set-TaskXmlSetting -Xml $out2 -Name 'ExecutionTimeLimit' -InnerXml 'PT72H'
    Assert ($out3 -eq $out2) 'applying the same value twice is a no-op'

    # 4. a nested element survives being written as inner XML
    $rof = Set-TaskXmlSetting -Xml $sample -Name 'RestartOnFailure' -InnerXml '<Interval>PT1M</Interval><Count>3</Count>'
    Assert ($rof -match '<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>') `
        'RestartOnFailure keeps Interval AND Count (the CIM path dropped Count)'

    # 5. refuses to guess when there is no Settings block
    $threw = $false
    try { Set-TaskXmlSetting -Xml '<Task></Task>' -Name 'X' -InnerXml 'y' | Out-Null } catch { $threw = $true }
    Assert $threw 'XML without </Settings> throws instead of guessing'

    # 6. other settings are untouched
    Assert ($out -match '<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>') 'unrelated settings survive'

    if ($script:selfTestFailures -gt 0) {
        Write-Line ("SELF-TEST FAILED: {0} assertion(s)." -f $script:selfTestFailures) 'ERROR'
        return 5
    }
    Write-Line "SELF-TEST OK - the XML transformation behaves as documented."
    return 0
}

if ($SelfTest) { exit (Invoke-SelfTest) }

if ($RestartIntervalMinutes -lt 1 -or $RestartIntervalMinutes -gt 30) {
    Write-Line "RestartIntervalMinutes must be between 1 and 30 (Task Scheduler bound)." 'ERROR'
    exit 3
}
if ($RestartCount -lt 1) {
    Write-Line "RestartCount must be >= 1." 'ERROR'
    exit 3
}
# A malformed duration would be written verbatim and silently mis-cap the task.
# The lookahead rejects the degenerate 'P' and 'PT', which the component groups
# alone would accept because every one of them is optional.
if ($ExecutionTimeLimit -notmatch '^P(?=\d|T\d)(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$') {
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
        (Format-Duration $task.Settings.ExecutionTimeLimit), $task.Settings.RestartCount, `
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
$etlOk = (ConvertTo-DurationKey $task.Settings.ExecutionTimeLimit) -eq (ConvertTo-DurationKey $ExecutionTimeLimit)
# With -SkipRestartOnFailure those fields are not part of this run, so they must
# not keep the idempotency check from short-circuiting - otherwise the task gets
# re-registered for nothing.
$rofOk = $SkipRestartOnFailure -or `
    (($task.Settings.RestartCount -eq $RestartCount) -and ($task.Settings.RestartInterval -eq $RestartInterval))
if ($etlOk -and $rofOk) {
    if ($SkipRestartOnFailure) {
        Write-Line ("Already configured: ExecutionTimeLimit={0} (restart-on-failure skipped) - nothing to do." -f $ExecutionTimeLimit)
    } else {
        Write-Line ("Already configured: ExecutionTimeLimit={0}, RestartCount={1}, RestartInterval={2} - nothing to do." -f `
                $ExecutionTimeLimit, $RestartCount, $RestartInterval)
    }
    exit 0
}

Write-Line "PLAN (one Register-ScheduledTask -Xml write, all other settings preserved):"
if ($etlOk) {
    Write-Line ("  [ok]     ExecutionTimeLimit already {0}" -f $ExecutionTimeLimit)
} else {
    Write-Line ("  [CHANGE] ExecutionTimeLimit : {0} -> {1}   (removes the 72h kill)" -f `
            (Format-Duration $task.Settings.ExecutionTimeLimit), $ExecutionTimeLimit)
}
if ($SkipRestartOnFailure) {
    Write-Line "  [skip]   restart-on-failure not touched (-SkipRestartOnFailure)"
} elseif ($rofOk) {
    Write-Line ("  [ok]     restart-on-failure already {0}/{1}" -f $RestartCount, $RestartInterval)
} else {
    Write-Line ("  [CHANGE] RestartCount/Interval : {0}/{1} -> {2}/{3}   (crash net only, NOT the 72h kill)" -f `
            $task.Settings.RestartCount, $task.Settings.RestartInterval, $RestartCount, $RestartInterval)
}

if (-not $Apply) {
    Write-Line "DRY RUN - nothing changed. Re-run ELEVATED with -Apply to make the change." 'WARN'
    Write-Line "-Apply exports the task XML, edits the element, and re-registers via" 'INFO'
    Write-Line "Register-ScheduledTask -Xml. It prompts for the Windows password (the task uses" 'INFO'
    Write-Line "LogonType=Password and Windows cannot read the stored one back), backs the original" 'INFO'
    Write-Line "XML up first, and verifies the principal afterwards." 'INFO'
    Write-Line "NOTE: this write neither stops nor starts anything - the running fleet is untouched," 'WARN'
    Write-Line "      and a running instance survives the re-registration (observed 2026-08-31)." 'WARN'
    Write-Line "      UNVERIFIED whether the scheduler re-reads ExecutionTimeLimit for the ALREADY" 'WARN'
    Write-Line "      RUNNING instance. Guaranteed path: apply, then one tools/restart_fleet.ps1." 'WARN'
    exit 0
}

# --- Apply --------------------------------------------------------------------
# T-2026-KYT-9050-156: this used to go through `Set-ScheduledTask -Settings`.
# That path cannot write THIS task, proven twice on 2026-08-31: it rewrites the
# WHOLE settings object, Get-ScheduledTask materialises every Windows default
# into it, and the CIM provider then emits XML it does not validate itself
# ("The task XML is missing a required element or attribute.(41,8):Count:" -
# naming a line 41 that does not exist in the real 35-line task). We export the
# XML, change the one element, and re-register.
#
# Snapshot the principal so the read-back can prove it was NOT altered: a
# botched write can convert a password logon to S4U, which silently breaks
# RunLevel Highest / boot start and would only surface at the next reboot.
$beforeLogon = $task.Principal.LogonType
$beforeUser = $task.Principal.UserId
$beforeLevel = $task.Principal.RunLevel

# Windows cannot read a stored task password back, so any rewrite has to
# re-supply it. Prompted here rather than taken as a plaintext parameter, so it
# never reaches a command line, a script file or shell history.
$cred = $Credential
if (-not $cred) {
    Write-Line ("Task uses LogonType={0}; the credential for '{1}' must be re-supplied." -f $beforeLogon, $beforeUser)
    $sec = Read-Host -AsSecureString ("Windows password for {0}" -f $beforeUser)
    if (-not $sec -or $sec.Length -eq 0) {
        Write-Line "No password entered - aborting. Nothing was changed." 'ERROR'
        exit 3
    }
    $cred = New-Object System.Management.Automation.PSCredential($beforeUser, $sec)
}

try {
    $xmlBefore = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Line ("Export-ScheduledTask failed: {0}" -f $_.Exception.Message) 'ERROR'
    exit 3
}

# -Force REPLACES the task, so keep the original recoverable before touching it.
$backupPath = Join-Path $env:TEMP ("kythera_watchdog_task_{0}.xml" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
try {
    $xmlBefore | Out-File -FilePath $backupPath -Encoding unicode -ErrorAction Stop
    Write-Line ("Original task XML backed up to {0}" -f $backupPath)
} catch {
    Write-Line ("Could not write the XML backup ({0}) - refusing to replace the task without one." -f $_.Exception.Message) 'ERROR'
    exit 3
}

try {
    $xmlAfter = Set-TaskXmlSetting -Xml $xmlBefore -Name 'ExecutionTimeLimit' -InnerXml $ExecutionTimeLimit
    if (-not $SkipRestartOnFailure) {
        # Written as XML on purpose: the CIM path dropped <Count> and produced
        # invalid XML. Restart-on-failure does NOT cover the ExecutionTimeLimit
        # kill (that termination ends success-class, SCHED_S_TASK_TERMINATED) -
        # it is the net for genuine crashes.
        $rof = "<Interval>{0}</Interval><Count>{1}</Count>" -f $RestartInterval, $RestartCount
        $xmlAfter = Set-TaskXmlSetting -Xml $xmlAfter -Name 'RestartOnFailure' -InnerXml $rof
    }
} catch {
    Write-Line ("XML transformation failed: {0} - task untouched." -f $_.Exception.Message) 'ERROR'
    exit 3
}

try {
    Register-ScheduledTask -TaskName $TaskName -Xml $xmlAfter `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
        -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Line ("Register-ScheduledTask failed: {0}" -f $_.Exception.Message) 'ERROR'
    Write-Line ("Restore the original with: Register-ScheduledTask -TaskName '{0}' -Xml (Get-Content '{1}' -Raw) -User '{2}' -Password <pw> -Force" -f `
            $TaskName, $backupPath, $beforeUser) 'ERROR'
    exit 3
}

# --- Verify -------------------------------------------------------------------
$after = Get-ScheduledTask -TaskName $TaskName
Write-Line ("After: ExecutionTimeLimit={0}, RestartCount={1}, RestartInterval={2}, MultipleInstances={3}, LogonType={4}, User={5}" -f `
        (Format-Duration $after.Settings.ExecutionTimeLimit), $after.Settings.RestartCount, $after.Settings.RestartInterval, `
        $after.Settings.MultipleInstances, $after.Principal.LogonType, $after.Principal.UserId)
$fieldsOk = ((ConvertTo-DurationKey $after.Settings.ExecutionTimeLimit) -eq (ConvertTo-DurationKey $ExecutionTimeLimit))
if (-not $SkipRestartOnFailure) {
    $fieldsOk = $fieldsOk -and ($after.Settings.RestartCount -eq $RestartCount) -and `
        ($after.Settings.RestartInterval -eq $RestartInterval)
}
# UserId may legitimately read back as the SID or the account name depending on
# how it was stored; compare the resolved account, not the spelling.
$sameUser = ($after.Principal.UserId -eq $beforeUser)
$principalOk = ($after.Principal.LogonType -eq $beforeLogon) -and $sameUser -and `
    ($after.Principal.RunLevel -eq $beforeLevel)
if ($fieldsOk -and $principalOk) {
    Write-Line "Applied and verified (principal preserved): 72h kill removed, crash net armed." 'INFO'
    Write-Line "The running instance may still hold its old deadline - do one tools/restart_fleet.ps1 to be sure." 'WARN'
    exit 0
} elseif (-not $principalOk) {
    Write-Line ("Principal CHANGED (LogonType {0}->{1}, User {2}->{3}, RunLevel {4}->{5}) - the password logon was dropped." -f `
            $beforeLogon, $after.Principal.LogonType, $beforeUser, $after.Principal.UserId, `
            $beforeLevel, $after.Principal.RunLevel) 'ERROR'
    Write-Line ("Restore the original: Register-ScheduledTask -TaskName '{0}' -Xml (Get-Content '{1}' -Raw) -User '{2}' -Password <pw> -Force" -f `
            $TaskName, $backupPath, $beforeUser) 'ERROR'
    exit 4
} else {
    Write-Line "Verification MISMATCH - the task did not read back the expected values. Inspect manually." 'ERROR'
    exit 4
}
