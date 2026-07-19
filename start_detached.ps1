$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Logs = Join-Path $Root "logs"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Server = Join-Path $Root "app\server.py"
$Cloudflared = Join-Path $Root "tools\cloudflared.exe"
$ServerOutLog = Join-Path $Logs "server.out.log"
$ServerErrLog = Join-Path $Logs "server.err.log"
$TunnelOutLog = Join-Path $Logs "cloudflared.out.log"
$TunnelErrLog = Join-Path $Logs "cloudflared.err.log"

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

if (-not (Test-Path -LiteralPath $VenvPython)) {
  throw "Virtual environment is missing. Run start.ps1 once first."
}

function Get-ProjectServerProcesses {
  @(Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*$Server*" })
}

function Test-LocalServer {
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:18765" -UseBasicParsing -TimeoutSec 2
    return [int]$response.StatusCode -eq 200
  } catch {
    return $false
  }
}

$serverProcesses = Get-ProjectServerProcesses

if (-not (Test-LocalServer)) {
  foreach ($process in $serverProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Process -FilePath $VenvPython `
    -ArgumentList "`"$Server`"" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $ServerOutLog `
    -RedirectStandardError $ServerErrLog `
    -WindowStyle Hidden
}

$tunnelRunning = Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -ieq "cloudflared.exe" -and
    $_.CommandLine -like "*$Cloudflared*" -and
    $_.CommandLine -like "*tunnel --url http://127.0.0.1:18765*"
  }
if (-not $tunnelRunning) {
  if (-not (Test-Path -LiteralPath $Cloudflared)) {
    throw "cloudflared.exe is missing. Run run_cloudflare_tunnel.ps1 once first."
  }
  Start-Process -FilePath $Cloudflared `
    -ArgumentList "tunnel --url http://127.0.0.1:18765" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $TunnelOutLog `
    -RedirectStandardError $TunnelErrLog `
    -WindowStyle Hidden
}

$healthy = $false
for ($attempt = 0; $attempt -lt 10 -and -not $healthy; $attempt++) {
  Start-Sleep -Milliseconds 500
  $healthy = Test-LocalServer
}
if (-not $healthy) {
  throw "Remote service did not become healthy at http://127.0.0.1:18765."
}

$url = $null
$tunnelLogs = @($TunnelOutLog, $TunnelErrLog) | Where-Object { Test-Path -LiteralPath $_ }
if ($tunnelLogs.Count -gt 0) {
  $url = Select-String -Path $tunnelLogs -Pattern "https://[-a-z0-9]+\.trycloudflare\.com" -AllMatches |
    ForEach-Object { $_.Matches.Value } |
    Select-Object -Last 1
}

if ($url) {
  Write-Host "Remote URL: $url"
} else {
  Write-Host "Remote service started. If tunnel was already running, use the existing trycloudflare URL."
}
