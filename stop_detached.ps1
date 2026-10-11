$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Root "logs"

function Get-SafeTrackedProcess([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  $raw = Get-Content -LiteralPath $Path -Raw
  try {
    $metadata = $raw | ConvertFrom-Json -ErrorAction Stop
    $process = Get-Process -Id ([int]$metadata.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $expected = [IO.Path]::GetFullPath([string]$metadata.executable)
    $actual = [IO.Path]::GetFullPath($process.Path)
    if ($process.StartTime.ToUniversalTime().Ticks -ne [int64]$metadata.startedUtcTicks) { return $null }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($expected, $actual)) { return $null }
    return $process
  } catch {
    # Legacy PID files are accepted only when the command line contains this repository path.
    $legacyPid = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$legacyPid)) { return $null }
    $legacy = Get-CimInstance Win32_Process -Filter "ProcessId=$legacyPid" -ErrorAction SilentlyContinue
    if (-not $legacy -or $legacy.CommandLine -notlike "*$Root*") { return $null }
    return Get-Process -Id $legacyPid -ErrorAction SilentlyContinue
  }
}

# A running supervisor would restart what is about to be stopped, so it goes first.
$supervisorPidFile = Join-Path $Logs "supervisor.pid"
$watchdogStopped = $false
if (Test-Path -LiteralPath $supervisorPidFile) {
  $supervisorPid = 0
  if ([int]::TryParse((Get-Content -LiteralPath $supervisorPidFile -Raw).Trim(), [ref]$supervisorPid)) {
    $process = Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue
    if ($process) {
      # Confirm it is ours before terminating, so a recycled process id is safe.
      $isOurs = $true
      try {
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$supervisorPid" -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.CommandLine -and
            $candidate.CommandLine -notlike "*service_runner.ps1*" -and
            $candidate.CommandLine -notlike "*watch.ps1*") {
          $isOurs = $false
        }
      } catch {}
      if ($isOurs) {
        Stop-Process -Id $supervisorPid -Force -ErrorAction SilentlyContinue
        $watchdogStopped = $true
      }
    }
  }
  Remove-Item -LiteralPath $supervisorPidFile -Force -ErrorAction SilentlyContinue
}
$watchScript = Join-Path $Root "watch.ps1"
$runnerScript = Join-Path $Root "service_runner.ps1"
foreach ($shellName in @("pwsh.exe", "powershell.exe")) {
  $candidates = Get-CimInstance Win32_Process -Filter "Name='$shellName'" -ErrorAction SilentlyContinue |
    Where-Object {
      $_.CommandLine -and ($_.CommandLine -like "*$watchScript*" -or $_.CommandLine -like "*$runnerScript*")
    }
  foreach ($candidate in $candidates) {
    Stop-Process -Id $candidate.ProcessId -Force -ErrorAction SilentlyContinue
    $watchdogStopped = $true
  }
}
if ($watchdogStopped) {
  Write-Host "Stopped the supervisor. A scheduled task will start it again at the next logon."
}

foreach ($Name in @("server.pid", "cloudflared.pid")) {
  $PidFile = Join-Path $Logs $Name
  if (-not (Test-Path -LiteralPath $PidFile)) { continue }
  $Process = Get-SafeTrackedProcess $PidFile
  if ($Process) {
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $PidFile -Force
}

Write-Host "Tablet Remote Desktop background processes stopped."