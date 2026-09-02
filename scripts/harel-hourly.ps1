<#
.SYNOPSIS
  One collection pass, meant to be fired by Task Scheduler once an hour.

.DESCRIPTION
  `harel watch` is a long-lived loop: it sleeps between passes inside one
  process, so anything that kills the process kills the schedule with it. That
  is exactly what happened here - a pass hit "database is locked", the loop
  survived that pass but the process later went away, and collection was silent
  for a day while the terminal kept serving stale items.

  This script is the opposite shape: a single pass that exits. Task Scheduler
  owns the cadence, so a failed pass costs one hour, not the schedule. It also
  means no second long-lived database connection sitting next to `harel serve`.

  It loads .env itself, because the app reads os.environ directly and does not
  parse .env. Without SEC_CONTACT_EMAIL the SEC returns 403 and EDGAR - the
  highest-trust source - collects nothing.

    # Run one pass now, see the output.
    .\scripts\harel-hourly.ps1

    # Register the hourly task (run once).
    .\scripts\harel-hourly.ps1 -Install

.PARAMETER Install
  Register the Scheduled Task 'HarelCollectHourly' and exit without collecting.

.PARAMETER Hours
  Lookback window per pass. Wider than the interval on purpose: sources publish
  late and backdate, and the deduper drops what we already have.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [double]$Hours = 12
)

$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot
$Harel = Join-Path $Root ".venv\Scripts\harel.exe"
$Task  = "HarelCollectHourly"
# Registering into the root task folder needs elevation on Windows 11; a
# subfolder does not. Same schedule, no admin prompt, and it keeps the task out
# of a list that is otherwise all Microsoft's.
$Folder = "\Harel\"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $Task -TaskPath $Folder -Confirm:$false
    Write-Host "Removed scheduled task '$Folder$Task'."
    return
}

if ($Install) {
    # The task name is fixed and the registration passes -Force, so a second
    # checkout installing over the first is silent by construction - one checkout
    # quietly taking over the other's schedule, and after a move it is why the
    # old path went on firing. Name it rather than let it happen invisibly.
    $existing = $null
    try { $existing = Get-ScheduledTask -TaskName $Task -TaskPath $Folder -ErrorAction Stop } catch {}
    if ($existing) {
        $prior = ($existing.Actions | Select-Object -First 1).Arguments
        if ($prior -and $prior -notlike "*$PSCommandPath*") {
            Write-Warning "'$Folder$Task' is already registered against a DIFFERENT checkout:"
            Write-Warning "    $prior"
            Write-Warning "Re-pointing it at this one: $PSCommandPath"
        }
    }

    $ps = (Get-Command powershell.exe).Source
    $action = New-ScheduledTaskAction -Execute $ps `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Hours $Hours" `
        -WorkingDirectory $Root
    # Two triggers. The -Once one anchors at midnight and repeats hourly forever
    # (leaving RepetitionDuration unset is what "forever" means here; passing
    # TimeSpan::MaxValue overflows the task XML). The AtLogOn one restarts the
    # cycle after a reboot without waiting for the next whole hour.
    $hourly  = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Hours 1)
    # Scoped to this account on purpose: an -AtLogOn trigger with no -User means
    # "any user", which is an administrator-level registration and fails here.
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # StartWhenAvailable is what covers a machine that was asleep at the top of
    # the hour: the pass fires on wake instead of being skipped silently.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $Task -TaskPath $Folder -Action $action `
        -Trigger @($hourly, $atLogon) -Settings $settings `
        -Description "Harel Terminal - hourly collection pass" `
        -Force -ErrorAction Stop | Out-Null

    Write-Host "Registered '$Folder$Task' - one pass every hour, ${Hours}h lookback."
    Write-Host "  bound to    : $Root"
    Write-Host "  run now     : Start-ScheduledTask -TaskName $Task -TaskPath $Folder"
    Write-Host "  last result : Get-ScheduledTaskInfo -TaskName $Task -TaskPath $Folder"
    Write-Host "  remove      : .\scripts\harel-hourly.ps1 -Uninstall"
    return
}

# The log is opened before the first thing that can fail, for the same reason
# this script exists at all. The registered task carries an absolute -File path
# resolved when -Install ran; move or re-clone the repo and it still fires, at a
# $Root that may have no venv. That used to `throw` here, above the log, so the
# only trace was a non-zero LastTaskResult - a silent failure that still reports
# "task ran", which is exactly the shape this file was written to stop.
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Log = Join-Path $LogDir "hourly.log"

if (-not (Test-Path $Harel)) {
    $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  FATAL harel.exe not found at $Harel " +
           "- this task points at a checkout that has moved or has no venv. " +
           "Re-run `"$PSCommandPath`" -Install from the checkout you want, or create the venv: " +
           "py -3 -m venv .venv; .\.venv\Scripts\python -m pip install -e `".[serve,mcp]`""
    $msg | Out-File -FilePath $Log -Append -Encoding utf8
    Write-Error $msg
    exit 1
}

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

# Keep the log readable across months of hourly runs: one header line per pass,
# and hand the tail back to the operator rather than growing without bound.
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Add-Content -Path $Log -Encoding utf8

# PowerShell 5.1 wraps a native program's stderr in ErrorRecords, and under
# ErrorActionPreference=Stop the first one aborts this script. harel writes its
# per-source warnings to stderr, so with Stop in force the very first warning
# killed the pass before a single line reached the log - a silent no-op that
# still reported "task ran". The collector decides what is fatal, not us.
$ErrorActionPreference = "Continue"
# Tee-Object has no -Encoding and defaults to UTF-16 in PowerShell 5.1, which
# turned the log into interleaved gibberish next to the UTF-8 header. Stream to
# the console and write UTF-8 in one pass instead - the feed is Hebrew, so an
# unreadable log is not a cosmetic problem.
& $Harel --no-color collect --hours $Hours 2>&1 |
    ForEach-Object { $line = $_.ToString(); Write-Host $line; $line } |
    Out-File -FilePath $Log -Append -Encoding utf8
$code = $LASTEXITCODE

if ((Get-Item $Log).Length -gt 5MB) {
    $keep = Get-Content $Log -Tail 2000
    Set-Content -Path $Log -Value $keep -Encoding utf8
}

exit $code
