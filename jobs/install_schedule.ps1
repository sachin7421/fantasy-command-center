<#
.SYNOPSIS
    Registers the Fantasy Command Center scheduled jobs with Windows Task Scheduler.

.DESCRIPTION
    Creates one task per job from config.yaml (spec 6). Each task runs the CLI
    headlessly with the project virtualenv and logs to logs\<job>.log.

    Re-running this script replaces the existing tasks, so it is safe to run
    after changing schedule times in config.yaml.

.PARAMETER Remove
    Unregister all tasks instead of installing them.

.PARAMETER WhatIf
    Show what would be registered without changing anything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File jobs\install_schedule.ps1
    powershell -ExecutionPolicy Bypass -File jobs\install_schedule.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$PythonConsole = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Cli = Join-Path $ProjectRoot "fcc.py"
$LogDir = Join-Path $ProjectRoot "logs"
$Prefix = "FantasyCommandCenter"

if (-not (Test-Path $PythonConsole)) {
    throw "Virtualenv not found at $PythonConsole. Create it first: py -3.13 -m venv .venv"
}
# pythonw avoids a console window popping up on every scheduled run.
if (-not (Test-Path $Python)) { $Python = $PythonConsole }

# job name -> (schedule spec, CLI arguments)
$Jobs = @(
    @{ Name = "Waivers";  Day = "TUE";   Time = "07:00"; Args = "waivers" }
    @{ Name = "Injuries"; Day = "DAILY"; Time = "08:00"; Args = "injuries" }
    @{ Name = "LineupThu"; Day = "THU";  Time = "10:00"; Args = "lineup" }
    @{ Name = "LineupSun"; Day = "SUN";  Time = "09:00"; Args = "lineup" }
    @{ Name = "Byes";     Day = "WED";   Time = "07:00"; Args = "byes" }
    @{ Name = "Recap";    Day = "MON";   Time = "08:00"; Args = "recap" }
    @{ Name = "Trades";   Day = "MON";   Time = "08:15"; Args = "trades" }
)

if ($Remove) {
    Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue | ForEach-Object {
        if ($PSCmdlet.ShouldProcess($_.TaskName, "Unregister")) {
            Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
            Write-Host "Removed $($_.TaskName)"
        }
    }
    return
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

foreach ($job in $Jobs) {
    $taskName = "$Prefix-$($job.Name)"
    $logFile = Join-Path $LogDir "$($job.Args).log"

    # cmd wrapper so stdout/stderr land in a log the user can read after the fact.
    $command = "`"$Python`" `"$Cli`" $($job.Args) >> `"$logFile`" 2>&1"
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c $command" -WorkingDirectory $ProjectRoot

    if ($job.Day -eq "DAILY") {
        $trigger = New-ScheduledTaskTrigger -Daily -At $job.Time
    } else {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $job.Day -At $job.Time
    }

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -MultipleInstances IgnoreNew

    if ($PSCmdlet.ShouldProcess($taskName, "Register at $($job.Day) $($job.Time)")) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Description "Fantasy Command Center: $($job.Args)" | Out-Null
        Write-Host ("Installed {0,-32} {1,-6} {2}" -f $taskName, $job.Day, $job.Time)
    }
}

Write-Host ""
Write-Host "StartWhenAvailable is set, so a job missed while the machine was off"
Write-Host "runs at the next opportunity. Every job is idempotent."
Write-Host ""
Write-Host "Verify with : Get-ScheduledTask -TaskName '$Prefix*'"
Write-Host "Run one now : Start-ScheduledTask -TaskName '$Prefix-Injuries'"
Write-Host "Remove all  : powershell -File jobs\install_schedule.ps1 -Remove"
