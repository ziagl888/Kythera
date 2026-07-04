# tools/backup_db.ps1 — nightly PostgreSQL backup for cryptodata
# Schedule via Task Scheduler (SYSTEM or Michael, daily e.g. 03:30):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Michael\Documents\Kythera\tools\backup_db.ps1
# Retention: keep last 7 daily dumps + every Monday dump for 4 weeks.
# NOTE: C: and D: are partitions of the SAME physical disk — copy the newest
# dump offsite (download / object storage) at least weekly for real safety.

$ErrorActionPreference = 'Stop'

$PgDump    = 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe'
$BackupDir = 'D:\_BACKUP\db'
$DbName    = 'cryptodata'
$User      = 'postgres'
$Stamp     = Get-Date -Format 'yyyy-MM-dd'
$Target    = Join-Path $BackupDir "cryptodata_$Stamp.dump"
$Log       = Join-Path $BackupDir 'backup.log'

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $Log -Value $line
}

if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Force $BackupDir | Out-Null }

# --- Dump (custom format, compressed) ---
Write-Log "START dump -> $Target"
& $PgDump -U $User -h localhost -d $DbName -Fc -f $Target
if ($LASTEXITCODE -ne 0) {
    Write-Log "FEHLER: pg_dump exit code $LASTEXITCODE"
    exit 1
}
$sizeMB = [math]::Round((Get-Item $Target).Length / 1MB, 1)
Write-Log "OK dump fertig ($sizeMB MB)"

# --- Sanity check: dump must be readable and non-trivial ---
if ($sizeMB -lt 100) {
    Write-Log "WARNUNG: Dump verdaechtig klein ($sizeMB MB) — nicht rotiert, bitte pruefen"
    exit 1
}

# --- Retention: last 7 days + Monday dumps of last 4 weeks ---
$all = Get-ChildItem $BackupDir -Filter 'cryptodata_*.dump' | Sort-Object Name -Descending
$keep = New-Object System.Collections.Generic.HashSet[string]
$all | Select-Object -First 7 | ForEach-Object { [void]$keep.Add($_.Name) }
foreach ($f in $all) {
    if ($f.Name -match 'cryptodata_(\d{4}-\d{2}-\d{2})\.dump') {
        $d = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd', $null)
        if ($d.DayOfWeek -eq 'Monday' -and $d -gt (Get-Date).AddDays(-28)) { [void]$keep.Add($f.Name) }
    }
}
foreach ($f in $all) {
    if (-not $keep.Contains($f.Name)) {
        Remove-Item $f.FullName -Force
        Write-Log "Rotiert: $($f.Name) geloescht"
    }
}
Write-Log "ENDE ($($keep.Count) Dumps behalten)"
