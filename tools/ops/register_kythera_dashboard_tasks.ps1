<#
.SYNOPSIS
    Registers the two Windows Scheduled Tasks that run the Z1 analytics stack
    on the VPS: the read-only dashboard (autostart) and the incremental
    DuckDB analytics export (every 30 min).

.DESCRIPTION
    Reproducible, committed replacement for the ad-hoc registration that stood
    up the Z1 dashboard. Registers:

      (A) "Kythera Z1 Dashboard"      - waitress serving the Flask dashboard,
          read-only, bound to 127.0.0.1:8098, restarted on boot and on crash.
          Runs under LogonType=Password (michael's credentials), because the
          long-lived waitress server does NOT come up under S4U (see .NOTES).
      (B) "Kythera Analytics Export"  - `python -m tools.analytics_export`,
          incremental (watermark-driven) DuckDB/Parquet export every 30 min.
          Runs under S4U (short batch job; no stored password needed).

    Both run as `srv02\michael`. The export writes into a persistent BUILD DB
    (`analytics.duckdb.build`) and publishes atomically onto the served file, so
    the dashboard's read-only opens are never blocked by the export's write lock
    (T-2026-CU-9050-163, Teil 5).

.NOTES
    ELEVATION REQUIRED - run from an elevated PowerShell (Right-click ->
    "Run as Administrator"). Register-ScheduledTask with a named principal /
    stored password needs admin rights.

    DASHBOARD NEEDS PASSWORD LOGON (verified live 2026-07-19) - S4U works for
    the short export batch, but the long-lived dashboard server never binds
    port 8098 under S4U in the session-0 service context. The fleet watchdog
    uses LogonType=Password for the same reason. This script therefore prompts
    once for michael's Windows password and registers the dashboard task with
    -User/-Password (Task Scheduler stores it encrypted). The export task stays
    on S4U (no password). Task number: T-2026-CU-9050-170.

    RunLevel=Highest on the dashboard mirrors the fleet watchdog's principal and
    is what the live cutover verified. The dashboard only binds the non-privileged
    loopback port 8098, so RunLevel=Limited would likely suffice too, but that was
    not separately re-verified; Highest is kept as the known-good value.

    A .cmd ACTION MUST GO THROUGH cmd.exe - a scheduled-task action cannot
    CreateProcess a .cmd file directly (it silently does not start). The
    dashboard action is therefore `cmd.exe /c "<launcher.cmd>"`; the launcher
    redirects stdout/stderr to a log so a startup crash is diagnosable.

    REGISTRATION-ONLY - this script ONLY registers the two task definitions
    (and writes the dashboard launcher .cmd). It does NOT stop any process,
    does NOT start either task, and does NOT touch the running fleet. Cutover
    (stopping a manual dashboard instance and starting the scheduled one) is a
    SEPARATE, deliberate operator step, kept out of this artifact on purpose
    (CLAUDE.md hard rule 1: no live intervention / fleet restart from a
    committed dev artifact). After registration: a reboot picks up the
    dashboard via its AtStartup trigger, and the export fires at its next
    30-min tick.

    OPERATOR ACTION - registering these tasks is itself a deliberate operator
    step on the live VPS. It is intentionally NOT part of any build/dev session.

    Idempotent: `-Force` overwrites an existing task of the same name.
#>

$ErrorActionPreference = "Stop"

# --- Config ------------------------------------------------------------------
$Repo         = "C:\Users\Michael\Documents\Kythera"   # repo root = task WorkingDirectory
$Py           = "C:\Windows\py.exe"                     # the py launcher (selects -3.13)
$Cmd          = "$env:SystemRoot\System32\cmd.exe"      # runs the .cmd launcher
$User         = "srv02\michael"                         # principal for both tasks
$Port         = 8098                                    # dashboard listen port (127.0.0.1)
$DashLauncher = "C:\Users\Michael\Documents\kythera_dashboard_launch.cmd"
$DashLog      = "$Repo\staging_models\analytics\dashboard_scheduled.log"

if (-not (Test-Path $Py))  { throw "py launcher not found at $Py" }
if (-not (Test-Path $Cmd)) { throw "cmd.exe not found at $Cmd" }
if (-not (Test-Path $Repo)) { throw "repo root not found at $Repo" }

# --- (A) Dashboard autostart -------------------------------------------------
# waitress @127.0.0.1:8098, restarted on boot; on crash restart x3 @1 min;
# no execution time limit (long-lived service); MultipleInstances IgnoreNew so
# a boot trigger never spawns a second listener onto the same port.
# LogonType=Password + action via cmd.exe /c a logging launcher (see .NOTES).

# Logging launcher .cmd (captures the server's stdout/stderr so a startup
# crash is visible in $DashLog instead of vanishing).
$launcherLines = @(
    "@echo off",
    ('cd /d "' + $Repo + '"'),
    ('"' + $Py + '" -3.13 -m tools.dashboard.app --host 127.0.0.1 --port ' + $Port + ' >> "' + $DashLog + '" 2>&1')
)
# Ensure the log directory exists: a `>> "<log>"` redirect cannot create a missing
# directory (it fails silently before py starts, defeating the launcher's diagnostic
# purpose). Live it exists (the export creates it), but on a fresh VPS the dashboard
# task can fire at boot before any export has run.
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DashLog) | Out-Null
Set-Content -Path $DashLauncher -Value $launcherLines -Encoding ASCII
Write-Host "[..] dashboard logging launcher written: $DashLauncher"

# Prompt once for michael's Windows password (stored encrypted by Task Scheduler).
$sec  = Read-Host "Windows password for $User (dashboard task, Password logon)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$pw   = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)   # zero/free the unmanaged password buffer

$aDash = New-ScheduledTaskAction -Execute $Cmd `
    -Argument ('/c "' + $DashLauncher + '"') `
    -WorkingDirectory $Repo
$tDash = New-ScheduledTaskTrigger -AtStartup
$sDash = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
Register-ScheduledTask -TaskName "Kythera Z1 Dashboard" `
    -Action $aDash -Trigger $tDash -Settings $sDash `
    -User $User -Password $pw -RunLevel Highest `
    -Description "Z1 read-only analytics dashboard (waitress, 127.0.0.1:$Port). Reboot-safe (Password logon, AtStartup), restart x3/1min. T-2026-CU-9050-170." `
    -Force | Out-Null
Write-Host "[OK] registered 'Kythera Z1 Dashboard' (AtStartup, Password logon, cmd.exe /c launcher, restart x3/1min)"

# --- (B) Analytics export every 30 min ---------------------------------------
# MultipleInstances IgnoreNew = no overlapping export (a run holds the build-DB
# write lock); ExecutionTimeLimit 2h kills a hung run so it cannot pin the lock.
# S4U is fine here: a short batch job (no long-lived socket) runs under it.
$aExp = New-ScheduledTaskAction -Execute $Py `
    -Argument "-3.13 -m tools.analytics_export" `
    -WorkingDirectory $Repo
$tExp = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$pExp = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
$sExp = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
Register-ScheduledTask -TaskName "Kythera Analytics Export" `
    -Action $aExp -Trigger $tExp -Principal $pExp -Settings $sExp `
    -Description "Incremental DuckDB export (watermark) every 30 min for the Z1 dashboard. Build-DB + atomic publish. T-2026-CU-9050-163." `
    -Force | Out-Null
Write-Host "[OK] registered 'Kythera Analytics Export' (every 30 min, S4U, no-overlap, 2h limit)"

# --- Registration complete (no live cutover performed) -----------------------
# This script deliberately does NOT stop any process or start either task.
# Cutting over the running dashboard is a separate, manual operator decision.
Write-Host ""
Write-Host "[OK] Both tasks registered. No process was stopped or started."
Write-Host "     Starting/cutover is a SEPARATE deliberate operator step:"
Write-Host "       - a reboot picks up the dashboard via its AtStartup trigger;"
Write-Host "       - the export runs at its next 30-min tick."
Write-Host "     To cut over the running dashboard NOW (manual): stop the manual"
Write-Host "     instance on port $Port, then:  Start-ScheduledTask 'Kythera Z1 Dashboard'"
Write-Host "     If it does not bind afterwards, check the launcher log for the error:"
Write-Host "       $DashLog"
Write-Host "     Verify: Get-ScheduledTaskInfo 'Kythera Z1 Dashboard','Kythera Analytics Export'"
