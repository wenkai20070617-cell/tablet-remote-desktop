$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Root "logs"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ServerPid = Join-Path $Logs "server.pid"
$TunnelPid = Join-Path $Logs "cloudflared.pid"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
if (-not (Test-Path -LiteralPath $Python)) { throw "Virtual environment is missing. Run start.ps1 once first." }

function Get-TrackedProcess([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  try {
    $metadata = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop
    $process = Get-Process -Id ([int]$metadata.pid) -ErrorAction SilentlyContinue
    if (-not $process) { throw "process is not running" }
    $started = [int64]$metadata.startedUtcTicks
    $expected = [IO.Path]::GetFullPath([string]$metadata.executable)
    $actual = [IO.Path]::GetFullPath($process.Path)
    if ($process.StartTime.ToUniversalTime().Ticks -ne $started) { throw "process id was reused" }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($expected, $actual)) { throw "executable changed" }
    return $process
  } catch {
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return $null
  }
}

function Set-TrackedProcess([string]$Path, $Process, [string]$Executable) {
  $Process.Refresh()
  [ordered]@{
    pid = $Process.Id
    startedUtcTicks = $Process.StartTime.ToUniversalTime().Ticks
    executable = [IO.Path]::GetFullPath($Executable)
  } | ConvertTo-Json -Compress | Set-Content -LiteralPath $Path -Encoding UTF8 -NoNewline
}

function Test-LocalServer {
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:18765/api/status" -UseBasicParsing -TimeoutSec 2
    return [int]$response.StatusCode -eq 200
  } catch { return $false }
}

$serverProcess = Get-TrackedProcess $ServerPid
if (-not (Test-LocalServer) -and -not $serverProcess) {
  $serverProcess = Start-Process -FilePath $Python -ArgumentList "-m", "app.server" -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $Logs "server.out.log") `
    -RedirectStandardError (Join-Path $Logs "server.err.log") -WindowStyle Hidden -PassThru
  Set-TrackedProcess $ServerPid $serverProcess $Python
}

$cloudflared = Join-Path $Root "tools\cloudflared.exe"
if (-not (Test-Path -LiteralPath $cloudflared)) {
  $command = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($command) { $cloudflared = $command.Source } else { $cloudflared = $null }
}
$tunnelProcess = Get-TrackedProcess $TunnelPid
if ($cloudflared -and -not $tunnelProcess) {
  $tunnelProcess = Start-Process -FilePath $cloudflared -ArgumentList "tunnel", "--url", "http://127.0.0.1:18765" `
    -WorkingDirectory $Root -RedirectStandardOutput (Join-Path $Logs "cloudflared.out.log") `
    -RedirectStandardError (Join-Path $Logs "cloudflared.err.log") -WindowStyle Hidden -PassThru
  Set-TrackedProcess $TunnelPid $tunnelProcess $cloudflared
}

$healthy = $false
for ($attempt = 0; $attempt -lt 20 -and -not $healthy; $attempt++) {
  Start-Sleep -Milliseconds 500
  $healthy = Test-LocalServer
}
if (-not $healthy) { throw "Remote service did not become healthy at http://127.0.0.1:18765." }
Write-Host "Remote service is running at http://127.0.0.1:18765"
if (-not $cloudflared) { Write-Host "cloudflared was not found; only local access was started." }