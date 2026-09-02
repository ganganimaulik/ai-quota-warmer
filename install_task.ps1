param (
    [string]$Mode = "all",      # "all" (logon + every 5 hrs), "interval", "daily", "logon"
    [int]$IntervalHours = 5,
    [string]$DailyTime = "08:00"
)

$TaskName = "AIQuotaWarmer_5HourLimit"

# Prefer pythonw.exe so a scheduled run never flashes a console window.
$PythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $PythonCmd) { $PythonCmd = Get-Command py.exe -ErrorAction SilentlyContinue }
if (-not $PythonCmd) {
    Write-Host "[-] Python was not found on PATH. Install it from https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$PythonExe = $PythonCmd.Source
$PythonwExe = Join-Path (Split-Path $PythonExe -Parent) "pythonw.exe"
if (Test-Path $PythonwExe) { $PythonExe = $PythonwExe }

$ScriptPath = Join-Path $PSScriptRoot "quota_warmer.py"
if (-not (Test-Path $ScriptPath)) {
    Write-Host "[-] quota_warmer.py not found next to this script ($PSScriptRoot)." -ForegroundColor Red
    exit 1
}
$ActionArg = "`"$ScriptPath`" --now"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $ActionArg
$Triggers = @()

if ($Mode -eq "all") {
    # 1) At LogOn (triggers immediately after PC boot & login)
    $Triggers += New-ScheduledTaskTrigger -AtLogOn
    # 2) Recurring every 5 hours
    $Triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
} elseif ($Mode -eq "logon") {
    $Triggers += New-ScheduledTaskTrigger -AtLogOn
} elseif ($Mode -eq "daily") {
    $Parts = $DailyTime.Split(':')
    if ($Parts.Count -ne 2) {
        Write-Host "[-] -DailyTime must be HH:MM (e.g. 08:00). Got '$DailyTime'." -ForegroundColor Red
        exit 1
    }
    $Hour = [int]$Parts[0]
    $Min = [int]$Parts[1]
    if ($Hour -lt 0 -or $Hour -gt 23 -or $Min -lt 0 -or $Min -gt 59) {
        Write-Host "[-] -DailyTime out of range: '$DailyTime'." -ForegroundColor Red
        exit 1
    }
    $Triggers += New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $Hour -Minute $Min -Second 0)
} else {
    $Triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
}

# Settings: Start as soon as possible if missed, run on battery too
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# Register task
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Settings $Settings -Force -ErrorAction Stop | Out-Null
    Write-Host "[+] Successfully registered Windows Scheduled Task '$TaskName'!" -ForegroundColor Green
    Write-Host "    Mode: $Mode"
    Write-Host "    Triggers: $($Triggers.Count) active trigger(s)"
    Write-Host "    * Auto-runs on system restart/login: $(if ($Mode -in @('all', 'logon')) { 'YES' } else { 'NO' })"
    Write-Host "    * Interval: Every $IntervalHours hour(s)"
    Write-Host "    * Start When Available (Catch-up if PC was off): YES"
} catch {
    Write-Host "[-] Failed to register task: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "    Common causes:" -ForegroundColor Yellow
    Write-Host "      * 'Access is denied' - a task of this name already exists under another"
    Write-Host "        account, or the -AtLogOn trigger needs elevation. Re-run this script"
    Write-Host "        from an Administrator PowerShell, or remove the old task first:"
    Write-Host "          schtasks /delete /tn AIQuotaWarmer_5HourLimit /f"
    Write-Host ""
    Write-Host "    You do not need this script at all - these need no admin rights:" -ForegroundColor Yellow
    Write-Host "      python quota_warmer.py --install-task     (recurring task via schtasks)"
    Write-Host "      python quota_warmer.py --install-startup  (silent watcher on every login)"
    exit 1
}
