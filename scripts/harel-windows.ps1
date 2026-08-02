<#
.SYNOPSIS
  Run the Harel Terminal on Windows: collection loop + web terminal.

.DESCRIPTION
  The repo ships systemd units, which are Linux-only. This is the Windows
  equivalent. It loads .env (the app reads os.environ directly and does NOT
  parse .env itself), then starts both processes and keeps them running.

  Two ways to use it:

    # Foreground, current session - Ctrl-C stops both.
    .\scripts\harel-windows.ps1

    # Survives logoff/reboot via Task Scheduler (run once, as admin):
    .\scripts\harel-windows.ps1 -Install

.PARAMETER Install
  Register a Scheduled Task that starts this script at logon.

.PARAMETER Interval
  Seconds between collection passes. A full pass measures ~250s, so an interval
  below that means the loop never sleeps and just hammers the sources.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [int]$Interval = 300,
    [double]$Hours = 12,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Harel = Join-Path $Root ".venv\Scripts\harel.exe"

if (-not (Test-Path $Harel)) {
    throw "harel.exe not found at $Harel - create the venv first: py -3 -m venv .venv; .\.venv\Scripts\python -m pip install -e `".[serve,mcp]`""
}

# --- load .env ---------------------------------------------------------------
# The app reads os.environ directly, so .env has to be applied to the process
# environment here. Without SEC_CONTACT_EMAIL the SEC returns 403 and EDGAR -
# the highest-trust source - goes silent.
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $k, $v = $line.Split("=", 2)
            Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim()
        }
    }
}
if (-not $env:SEC_CONTACT_EMAIL) {
    Write-Warning "SEC_CONTACT_EMAIL is not set - the SEC will return 403 and EDGAR will collect nothing."
}

if ($Install) {
    $ps = (Get-Command powershell.exe).Source
    $action = New-ScheduledTaskAction -Execute $ps `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Interval $Interval -Hours $Hours -BindHost $BindHost -Port $Port" `
        -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName "HarelTerminal" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Harel Terminal collection loop + web UI" -Force | Out-Null
    Write-Host "Registered scheduled task 'HarelTerminal' (starts at logon)."
    Write-Host "Start it now with:  Start-ScheduledTask -TaskName HarelTerminal"
    Write-Host "Remove it with:     Unregister-ScheduledTask -TaskName HarelTerminal"
    return
}

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$collectLog = Join-Path $LogDir "collect.log"
$serveLog   = Join-Path $LogDir "serve.log"

Write-Host "Harel Terminal"
Write-Host "  terminal : http://$BindHost`:$Port"
Write-Host "  collect  : every ${Interval}s, ${Hours}h window -> $collectLog"
Write-Host "  Ctrl-C stops both."

$serve = Start-Process -FilePath $Harel -PassThru -NoNewWindow `
    -ArgumentList "--no-color","serve","--host",$BindHost,"--port",$Port `
    -RedirectStandardOutput $serveLog -RedirectStandardError "$serveLog.err"

$watch = Start-Process -FilePath $Harel -PassThru -NoNewWindow `
    -ArgumentList "--no-color","watch","--interval",$Interval,"--hours",$Hours `
    -RedirectStandardOutput $collectLog -RedirectStandardError "$collectLog.err"

try {
    while ($true) {
        Start-Sleep -Seconds 15
        foreach ($p in @(@{P=$serve;N="serve"}, @{P=$watch;N="watch"})) {
            if ($p.P.HasExited) {
                Write-Warning "$($p.N) exited with code $($p.P.ExitCode) - stopping."
                throw "$($p.N) died"
            }
        }
    }
}
finally {
    foreach ($p in @($serve, $watch)) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    Write-Host "stopped."
}
