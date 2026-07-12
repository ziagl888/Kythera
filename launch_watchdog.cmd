@echo off
rem Kythera Watchdog Launcher v5 (T-2026-CU-9050-100) - action of the scheduled
rem task "Kythera Watchdog" (password logon SRV02\Michael, RunLevel Highest).
rem The task references this file by its absolute repo-root path; moving or
rem renaming it requires an elevated task re-registration.
rem
rem v4 -> v5: the debug log gets a UNIQUE timestamped name per start. With the
rem fixed name, bots orphaned by a fleet stop kept the previous log's handle
rem open, and cmd's >> redirect onto a locked file FAILS while leaving
rem errorlevel 0 - the python line was silently skipped and the watchdog never
rem started ("silent exit 0", 2026-07-12; the same mechanism most likely
rem explains the earlier S4U exit-0 mystery from T-068). A unique target can
rem never collide with a handle held by the previous generation.
rem logs\watchdog_launch.log stays append-only as the tiny start/exit ledger
rem and names the debug file of each start; a locked ledger only loses the
rem echo, it cannot block the python line.
cd /d C:\Users\Michael\Documents\Kythera
rem Per-user site-packages: fleet deps are installed for Michael only.
set PYTHONPATH=C:\Users\Michael\AppData\Roaming\Python\Python313\site-packages
rem %DATE% is locale-dependent (this host: "Sat 07/12/2026"); %TIME% pads the
rem hour with a space before 10:00 - replaced with 0 below.
set LOGSTAMP=%DATE:~10,4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%
set LOGSTAMP=%LOGSTAMP: =0%
echo ==== %DATE% %TIME% launcher v5 start, debug log watchdog_debug_%LOGSTAMP%.log ==== >> logs\watchdog_launch.log
"C:\Program Files\Python313_12\python.exe" -u -X faulthandler main_watchdog.py >> logs\watchdog_debug_%LOGSTAMP%.log 2>&1
echo %DATE% %TIME% launcher v5: exit %ERRORLEVEL% >> logs\watchdog_launch.log
