# One-time setup: registers the Windows Scheduled Tasks that run
# run_backtest_biweekly.bat on the 1st and 15th of each month at 07:30.
# Two separate MONTHLY tasks, not one -- schtasks' /D option for schedule
# type MONTHLY only accepts a single day (SCHTASKS /Create /SC MONTHLY
# /D 1,15 fails with "ERROR: Invalid value for /D option", confirmed
# 2026-09-21), unlike WEEKLY where /D takes a comma list of weekdays.
# Re-run this after moving the repo (it bakes in an absolute path) or to
# change the schedule; it's safe to re-run, /F overwrites existing tasks.
#
# Verify afterwards with:
#   schtasks /Query /TN "tw_stock_tracker_run_backtest_d1" /V /FO LIST
#   schtasks /Query /TN "tw_stock_tracker_run_backtest_d15" /V /FO LIST
# Remove with:
#   schtasks /Delete /TN "tw_stock_tracker_run_backtest_d1" /F
#   schtasks /Delete /TN "tw_stock_tracker_run_backtest_d15" /F

$repoRoot = Split-Path -Parent $PSScriptRoot
$batPath = Join-Path $repoRoot "scripts\run_backtest_biweekly.bat"

schtasks /Create /SC MONTHLY /D 1 /ST 07:30 `
  /TN "tw_stock_tracker_run_backtest_d1" `
  /TR "`"$batPath`"" `
  /F

schtasks /Create /SC MONTHLY /D 15 /ST 07:30 `
  /TN "tw_stock_tracker_run_backtest_d15" `
  /TR "`"$batPath`"" `
  /F
