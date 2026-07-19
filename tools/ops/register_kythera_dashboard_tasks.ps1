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
      (B) "Kythera Analytics Export"  - `python -m tools.analytics_export`,
          incremental (watermark-driven) DuckDB/Parquet export every 30 min.

    Both run as `srv02\michael` via S4U (LogonType=S4U -> no stored password
    needed; the per-user py-3.13 dependencies are on that profile). The export
    writes into a persistent BUILD DB (`analytics.duckdb.build`) and publishes
    atomically onto the served file, so the dashboard's read-only opens are
    never blocked by the export's write lock (T-2026-CU-9050-163, Teil 5).

.NOTES
    ELEVATION REQUIRED - run from an elevated PowerShell (Right-click ->
    "Run as Administrator"). Register-ScheduledTask with a named principal
    needs admin rights.

    REGISTRATION-ONLY - this script ONLY registers the two task definitions.
    It does NOT stop any process, does NOT start either task, and does NOT
    touch the running fleet. Cutover (stopping a manual dashboard instance and
    starting the scheduled one) is a SEPARATE, deliberate operator step, kept
    out of this artifact on purpose (CLAUDE.md hard rule 1: no live
    intervention / fleet restart from a committed dev artifact). After
    registration: a reboot picks up the dashboard via its AtStartup trigger,
    and the export fires at its next 30-min tick.

    OPERATOR ACTION - registering these tasks is itself a deliberate operator
    step on the live VPS. It is intentionally NOT part of any build/dev session.

    Idempotent: `-Force` overwrites an existing task of the same name.
#>

$ErrorActionPreference = "Stop"

# --- Config ------------------------------------------------------------------
$Repo = "C:\Users\Michael\Documents\Kythera"   # repo root = task WorkingDirectory
$Py   = "C:\Windows\py.exe"                     # the py launcher (selects -3.13)
$User = "srv02\michael"                         # S4U principal (no password)
$Port = 8098                                    # dashboard listen port (127.0.0.1)

if (-not (Test-Path $Py)) { throw "py launcher not found at $Py" }
if (-not (Test-Path $Repo)) { throw "repo root not found at $Repo" }

# --- (A) Dashboard autostart -------------------------------------------------
# waitress @127.0.0.1:8098, restarted on boot; on crash restart x3 @1 min;
# no execution time limit (long-lived service); MultipleInstances IgnoreNew so
# a boot trigger never spawns a second listener onto the same port.
$aDash = New-ScheduledTaskAction -Execute $Py `
    -Argument "-3.13 -m tools.dashboard.app --host 127.0.0.1 --port $Port" `
    -WorkingDirectory $Repo
$tDash = New-ScheduledTaskTrigger -AtStartup
$pDash = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
$sDash = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
Register-ScheduledTask -TaskName "Kythera Z1 Dashboard" `
    -Action $aDash -Trigger $tDash -Principal $pDash -Settings $sDash `
    -Description "Z1 read-only analytics dashboard (waitress, 127.0.0.1:$Port). Reboot-safe, restart x3/1min. T-2026-CU-9050-163." `
    -Force | Out-Null
Write-Host "[OK] registered 'Kythera Z1 Dashboard' (AtStartup, S4U, restart x3/1min)"

# --- (B) Analytics export every 30 min ---------------------------------------
# MultipleInstances IgnoreNew = no overlapping export (a run holds the build-DB
# write lock); ExecutionTimeLimit 2h kills a hung run so it cannot pin the lock.
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
Write-Host "     If it does not listen afterwards (possible S4U env problem), re-register"
Write-Host "     with -LogonType Password (interactive, michael's credentials)."
Write-Host "     Verify: Get-ScheduledTaskInfo 'Kythera Z1 Dashboard','Kythera Analytics Export'"
