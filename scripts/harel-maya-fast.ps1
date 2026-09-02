<#
.SYNOPSIS
  One MAYA-only collection pass, meant to be fired by Task Scheduler every
  couple of minutes.

.DESCRIPTION
  `sources.yaml` has said `poll_sec: 120` against maya_tase since the beginning
  and nothing ever honoured it - the field is parsed in config.py and read
  nowhere else. Every source has in practice been polled at the cadence of
  HarelCollectHourly, so an Israeli immediate report (דיווח מיידי) reached the
  feed a median of 69 minutes after MAYA published it. For the one channel that
  is this basket's structural edge - 22 of 22 names carry a tase_issuer_id, and
  Israeli reports land before the US pre-market - an hour is the whole advantage
  given away.

  This task is the hourly script's shape, narrowed to one source. A MAYA-only
  pass is 22 HTTP POSTs and takes ~11s, against ~90s for the full pass, because
  the full pass is paced by google_news's politeness budget and MAYA is not.
  So the fast lane costs about 9% duty cycle at the default two minutes.

  It does NOT replace the hourly pass. That one still runs every source
  including maya_tase at a 12h lookback, and remains the safety net for
  anything this one misses while the machine is asleep.

    # Run one pass now, see the output.
    .\scripts\harel-maya-fast.ps1

    # Register the task (run once).
    .\scripts\harel-maya-fast.ps1 -Install

    # Poll less often, or widen the active window.
    .\scripts\harel-maya-fast.ps1 -Install -IntervalMinutes 5 -FromHour 6 -ToHour 23

.PARAMETER IntervalMinutes
  Minutes between passes. Default 2, matching the poll_sec already declared for
  maya_tase. Each pass is 22 requests to maya.tase.co.il, so 2 minutes across
  the default window is ~11k requests/day - polite by volume, but this is an
  undocumented same-origin endpoint on a host whose sibling already sits behind
  bot protection. Raise this before you lose the channel; 5 minutes still beats
  the status quo by an order of magnitude.

.PARAMETER FromHour / ToHour
  Active window in ISRAEL time, inclusive of FromHour, exclusive of ToHour.
  Outside it the pass exits immediately without a request. MAYA publishes
  essentially nothing between midnight and dawn Israel time, and the hourly
  pass covers the gap anyway.

.PARAMETER Hours
  Lookback per pass. Deliberately much wider than the interval: the MAYA fetch
  window is a fixed 7 days server-side regardless of this, so a wide lookback
  costs nothing extra in requests, and the deduper drops what we already have.
  It buys tolerance for a backdated report and for a machine that was asleep.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [int]$IntervalMinutes = 2,
    [int]$FromHour = 6,
    [int]$ToHour = 23,
    [double]$Hours = 12
)

$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot
$Harel = Join-Path $Root ".venv\Scripts\harel.exe"
$Task  = "HarelMayaFast"
# Same subfolder as the hourly task: registering into the root folder needs
# elevation on Windows 11, a subfolder does not.
$Folder = "\Harel\"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $Task -TaskPath $Folder -Confirm:$false
    Write-Host "Removed scheduled task '$Folder$Task'."
    return
}

if ($Install) {
    $ps = (Get-Command powershell.exe).Source
    $action = New-ScheduledTaskAction -Execute $ps `
        -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
                   "-Hours $Hours -FromHour $FromHour -ToHour $ToHour") `
        -WorkingDirectory $Root
    # Anchored at midnight and repeating forever - leaving RepetitionDuration
    # unset is what "forever" means here; TimeSpan::MaxValue overflows the XML.
    $repeat  = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    # Scoped to this account: -AtLogOn with no -User means "any user", which is
    # an administrator-level registration and fails here.
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # A pass is ~11s. Five minutes is a generous ceiling that still guarantees a
    # wedged run cannot sit on the write lock into the next one; IgnoreNew then
    # skips rather than stacking passes on top of each other.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $Task -TaskPath $Folder -Action $action `
        -Trigger @($repeat, $atLogon) -Settings $settings `
        -Description "Harel Terminal - fast MAYA/TASE immediate-report poll" `
        -Force -ErrorAction Stop | Out-Null

    Write-Host "Registered '$Folder$Task' - maya_tase every $IntervalMinutes min, ${FromHour}:00-${ToHour}:00 Israel time, ${Hours}h lookback."
    Write-Host "  run now     : Start-ScheduledTask -TaskName $Task -TaskPath $Folder"
    Write-Host "  last result : Get-ScheduledTaskInfo -TaskName $Task -TaskPath $Folder"
    Write-Host "  log         : logs\maya-fast.log"
    Write-Host "  remove      : .\scripts\harel-maya-fast.ps1 -Uninstall"
    return
}

if (-not (Test-Path $Harel)) {
    throw "harel.exe not found at $Harel - create the venv first: py -3 -m venv .venv; .\.venv\Scripts\python -m pip install -e `".[serve,mcp]`""
}

# The window is Israel's, not the machine's - this box happens to run Israel
# time but the whole point of the source is an Israeli publication clock, and a
# laptop that travels must not silently stop polling.
try {
    $tz  = [TimeZoneInfo]::FindSystemTimeZoneById("Israel Standard Time")
    $now = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $tz)
} catch {
    $now = [DateTimeOffset]::Now
}
if ($now.Hour -lt $FromHour -or $now.Hour -ge $ToHour) { exit 0 }

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

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Log = Join-Path $LogDir "maya-fast.log"

# PowerShell 5.1 wraps a native program's stderr in ErrorRecords, and under
# ErrorActionPreference=Stop the first one aborts the script - harel writes its
# per-source warnings to stderr, so Stop would kill the pass before anything
# reached the log. The collector decides what is fatal, not us.
$ErrorActionPreference = "Continue"
$out = & $Harel --no-color collect --sources maya_tase --hours $Hours 2>&1 |
    ForEach-Object { $_.ToString() }
$code = $LASTEXITCODE

# At this cadence the log is the problem, not the signal: 500 passes a day of
# full output would bury the one pass that mattered. So a quiet pass leaves a
# single heartbeat line, and only a pass that stored something new or failed
# gets written out in full.
#
# Deliberately NOT keyed on the ALERTS block. `report.alerts` is every alert in
# the lookback window, not the ones this pass found, so one Rule 425 at 08:00
# would make all 500 remaining passes that day "notable" and reproduce exactly
# the unreadable log this is meant to avoid. A pass that finds an alert worth
# printing has stored something new, and `$new` already catches it.
$stamp   = $now.ToString("yyyy-MM-dd HH:mm:ss")
$summary = ($out | Where-Object { $_ -match '^collected \d+' } | Select-Object -First 1)
if (-not $summary) { $summary = "no summary line - see exit code" }
$new     = 0
if ($summary -match '\((\d+) new\)') { $new = [int]$Matches[1] }
$notable = ($new -gt 0) -or ($code -ne 0)

if ($notable) {
    @("=== $stamp ===") + $out | Out-File -FilePath $Log -Append -Encoding utf8
} else {
    "$stamp  $summary" | Out-File -FilePath $Log -Append -Encoding utf8
}

if ((Get-Item $Log).Length -gt 5MB) {
    $keep = Get-Content $Log -Tail 2000
    Set-Content -Path $Log -Value $keep -Encoding utf8
}

exit $code
