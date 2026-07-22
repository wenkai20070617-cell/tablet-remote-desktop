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