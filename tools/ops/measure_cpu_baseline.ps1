<#
.SYNOPSIS
    Read-only CPU baseline sampler for the Z0/C3 programme
    (T-2026-KYT-9050-009, audit item Z0 "VPS-CPU dauerhaft auf 100%").

.DESCRIPTION
    Samples per-process CPU over a window and reports where the base load
    sits: total utilisation, share per executable, and the top consumers by
    PID. Read-only - it never touches the fleet, the DB, or any file outside
    the report path.

    Why WMI and not Get-Process: Get-Process .CPU is CUMULATIVE seconds since
    process start, which makes a long-running bot look expensive forever
    (the measurement trap from the 2026-07-20 root-cause session). The
    perf-counter class reports the load DURING the sample interval, which is
    what "base load" means.

    PID -> bot attribution is deliberately NOT attempted here. Win32_Process
    .CommandLine returns $null for the fleet when this runs unelevated (the
    watchdog runs with RunLevel Highest), and the watchdog log records a PID
    only for the dashboard. The script therefore reports PID, parent PID and
    start time and leaves the mapping to an elevated run or to the per-bot
    recipe from T-2026-CU-9050-166. Guessing an attribution would be worse
    than reporting none.

    THE OBSERVER IS PART OF THE MEASUREMENT. Two effects, both measured on
    2026-08-02 and both large enough to invert a verdict:
      - The sampler itself is not free. One WMI query over ~1000 processes
        every 15s put WmiPrvSE at 4.4% of a 10-core box (~0.44 cores) during
        the reference run. Raise -IntervalSec if that matters more than
        resolution.
      - An interactive agent/Claude session on the box costs far more than the
        sampler: 16.4% of the box for claude alone, ~34% once bash, powershell,
        conhost, WmiPrvSE and Taskmgr are added. A run taken from inside such a
        session therefore CANNOT produce a clean Z0 baseline - it measures the
        observer. For the real base load, schedule this at a quiet hour with no
        session attached and read the "Share per executable" table.
    The per-executable table exists precisely so the observer's share can be
    subtracted rather than silently believed.

    "Procs" in that table counts DISTINCT PIDs seen during the window, not
    concurrent ones - a high count is process churn (pool respawns, per-query
    postgres backends), which is itself a Z0 signal.

.PARAMETER Minutes
    Length of the sampling window. Default 10 (the Z0 ledger prescribes a
    10-minute sample).

.PARAMETER IntervalSec
    Seconds between samples. Default 15.

.PARAMETER OutFile
    Optional path for a CSV of the per-PID means.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\ops\measure_cpu_baseline.ps1 -Minutes 10
#>
[CmdletBinding()]
param(
    [int]$Minutes = 10,
    [int]$IntervalSec = 15,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'

$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$deadline = (Get-Date).AddMinutes($Minutes)
$samples = 0
$totals = @{}      # IDProcess -> [double] summed percent
$names = @{}       # IDProcess -> executable name
$machineTotal = 0.0

# Snapshot the process table once: start time and parent survive here even
# though CommandLine does not.
$procMeta = @{}
foreach ($p in Get-CimInstance Win32_Process) {
    $procMeta[[int]$p.ProcessId] = [pscustomobject]@{
        Parent  = [int]$p.ParentProcessId
        Started = $p.CreationDate
    }
}

Write-Host ("CPU baseline sample - {0} min at {1}s interval, {2} logical cores, start {3}" -f
    $Minutes, $IntervalSec, $cores, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))

while ((Get-Date) -lt $deadline) {
    $snap = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process
    foreach ($row in $snap) {
        if ($row.Name -eq '_Total') { $machineTotal += [double]$row.PercentProcessorTime; continue }
        if ($row.Name -eq 'Idle') { continue }
        $procId = [int]$row.IDProcess
        if ($procId -eq 0) { continue }
        $totals[$procId] = [double]($totals[$procId]) + [double]$row.PercentProcessorTime
        # Perf instance names disambiguate same-named processes as "python#3";
        # strip the suffix so the per-executable grouping does not shatter the
        # 60+ fleet pythons into 60 one-member groups.
        $names[$procId] = ($row.Name -replace '#\d+$', '')
    }
    $samples++
    Start-Sleep -Seconds $IntervalSec
}

if ($samples -eq 0) { Write-Host 'No samples taken.'; exit 1 }

$rows = foreach ($procId in $totals.Keys) {
    $meanPerCore = $totals[$procId] / $samples          # 0..100 per core
    [pscustomobject]@{
        Pid           = $procId
        Name          = $names[$procId]
        MeanPctOfCore = [math]::Round($meanPerCore, 2)
        MeanPctOfBox  = [math]::Round($meanPerCore / $cores, 2)
        Parent        = if ($procMeta.ContainsKey($procId)) { $procMeta[$procId].Parent } else { $null }
        Started       = if ($procMeta.ContainsKey($procId)) { $procMeta[$procId].Started } else { $null }
    }
}

$boxMean = [math]::Round(($machineTotal / $samples) / $cores, 1)
Write-Host ''
Write-Host ("=== Box utilisation over {0} samples: {1}% of {2} cores ===" -f $samples, $boxMean, $cores)

Write-Host ''
Write-Host '=== Share per executable (% of the whole box) ==='
$rows | Group-Object Name | ForEach-Object {
    [pscustomobject]@{
        Executable = $_.Name
        Procs      = $_.Count
        PctOfBox   = [math]::Round((($_.Group | Measure-Object MeanPctOfBox -Sum).Sum), 1)
    }
} | Sort-Object PctOfBox -Descending | Select-Object -First 15 | Format-Table -AutoSize

Write-Host '=== Top 20 single processes (% of the whole box) ==='
$rows | Sort-Object MeanPctOfBox -Descending | Select-Object -First 20 |
    Format-Table Pid, Name, MeanPctOfCore, MeanPctOfBox, Parent, Started -AutoSize

if ($OutFile) {
    $rows | Sort-Object MeanPctOfBox -Descending | Export-Csv -Path $OutFile -NoTypeInformation -Encoding UTF8
    Write-Host ("Per-PID means written to {0}" -f $OutFile)
}
