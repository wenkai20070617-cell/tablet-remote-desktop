# Starts the host (and the tunnel when cloudflared is available) in the
# background.  With -Watch it also supervises them, restarting either one if it
# exits or stops answering; install_autostart.ps1 wires that up for a logon.
[CmdletBinding()]
param(
  [switch]$Watch,
  [switch]$NoTunnel
)

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

function Get-SupervisorProcess([string]$PidFile) {
  if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
  $recorded = 0
  if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$recorded)) { return $null }
  $process = Get-Process -Id $recorded -ErrorAction SilentlyContinue
  if (-not $process) { return $null }
  # The command line may be unavailable; a live process at the recorded id is
  # accepted, and a matching command line confirms it when it can be read.
  try {
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.CommandLine -and
        $candidate.CommandLine -notlike "*service_runner.ps1*" -and
        $candidate.CommandLine -notlike "*watch.ps1*") {
      return $null
    }
  } catch {}
  return $process
}

function Test-LocalServer {
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:18765/api/health" -UseBasicParsing -TimeoutSec 3
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

if (-not $NoTunnel) {
  $cloudflared = Join-Path $Root "tools\cloudflared.exe"
  if (-not (Test-Path -LiteralPath $cloudflared)) {
    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command) { $cloudflared = $command.Source } else { $cloudflared = $null }
  }
  $tunnelProcess = Get-TrackedProcess $TunnelPid
  if ($cloudflared -and -not $tunnelProcess) {
    # A named tunnel (logs\tunnel.json) gives a stable address; without it this
    # is a best-effort quick tunnel whose address changes on every start.
    $tunnelArguments = @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:18765")
    $tunnelConfigPath = Join-Path $Logs "tunnel.json"
    if (Test-Path -LiteralPath $tunnelConfigPath) {
      try {
        $tunnelConfig = Get-Content -LiteralPath $tunnelConfigPath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($tunnelConfig.name) {
          $tunnelArguments = @("tunnel", "--no-autoupdate", "run", [string]$tunnelConfig.name)
          if ($tunnelConfig.credentials) { $tunnelArguments += @("--credentials-file", [string]$tunnelConfig.credentials) }
          if ($tunnelConfig.url) { $tunnelArguments += @("--url", [string]$tunnelConfig.url) }
          Write-Host "Using the named tunnel '$($tunnelConfig.name)'."
        }
      } catch {
        Write-Warning "logs\tunnel.json could not be parsed; using a quick tunnel."
      }
    }
    $tunnelProcess = Start-Process -FilePath $cloudflared -ArgumentList $tunnelArguments `
      -WorkingDirectory $Root -RedirectStandardOutput (Join-Path $Logs "cloudflared.out.log") `
      -RedirectStandardError (Join-Path $Logs "cloudflared.err.log") -WindowStyle Hidden -PassThru
    Set-TrackedProcess $TunnelPid $tunnelProcess $cloudflared
  }
}

$healthy = $false
for ($attempt = 0; $attempt -lt 20 -and -not $healthy; $attempt++) {
  Start-Sleep -Milliseconds 500
  $healthy = Test-LocalServer
}
if (-not $healthy) { throw "Remote service did not become healthy at http://127.0.0.1:18765." }
Write-Host "Remote service is running at http://127.0.0.1:18765"
$cloudflaredFound = (Test-Path -LiteralPath (Join-Path $Root "tools\cloudflared.exe")) -or
  [bool](Get-Command cloudflared -ErrorAction SilentlyContinue)
if (-not $NoTunnel -and -not $cloudflaredFound) {
  Write-Host "cloudflared was not found; only local access was started."
}

if ($Watch) {
  # Supervise in this window (blocks until Ctrl+C).
  Write-Host "Supervising the host and tunnel; press Ctrl+C to stop watching."
  $runner = Join-Path $Root "tools\service_runner.ps1"
  if (Test-Path -LiteralPath $runner) {
    & $runner -NoTunnel:$NoTunnel
  } else {
    & (Join-Path $Root "watch.ps1") -NoTunnel:$NoTunnel
  }
} else {
  # Supervise from a detached process, so the host stays up after this script
  # and its parent shell exit.
  $runner = Join-Path $Root "tools\service_runner.ps1"
  $supervisorPid = Join-Path $Logs "supervisor.pid"
  $running = Get-SupervisorProcess $supervisorPid
  if ($running) {
    Write-Host "A background supervisor is already running (pid $($running.Id))."
  } elseif (Test-Path -LiteralPath $runner) {
    $shell = (Get-Command pwsh -ErrorAction SilentlyContinue)
    if (-not $shell) { $shell = Get-Command powershell -ErrorAction Stop }
    $runnerArguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $runner)
    if ($NoTunnel) { $runnerArguments += "-NoTunnel" }
    $supervisor = Start-Process -FilePath $shell.Source -ArgumentList $runnerArguments -WorkingDirectory $Root `
      -RedirectStandardOutput (Join-Path $Logs "supervisor.out.log") `
      -RedirectStandardError (Join-Path $Logs "supervisor.err.log") `
      -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $supervisorPid -Value $supervisor.Id -Encoding UTF8 -NoNewline
    Write-Host "Background supervisor started (pid $($supervisor.Id)); logs\watch.log records its actions."
  } else {
    Write-Host "Run .\watch.ps1 (or .\install_autostart.ps1) to keep them running automatically."
  }
}