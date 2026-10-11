# Background supervisor: starts the host (and the tunnel) once, then keeps
# checking them, restarting either one if it exits or stops answering.  It is
# designed to be started detached by start_detached.ps1 or by a scheduled task.
#
#   powershell -File tools\service_runner.ps1 [-NoTunnel] [-MaxSeconds N]
#
# Every action is appended to logs\watch.log.
[CmdletBinding()]
param(
  [int]$CheckSeconds = 15,
  [switch]$NoTunnel,
  [int]$MaxSeconds = 0
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Logs = Join-Path $Root "logs"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$HealthUrl = "http://127.0.0.1:18765/api/health"
$ServerPid = Join-Path $Logs "server.pid"
$TunnelPid = Join-Path $Logs "cloudflared.pid"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

$startedAt = Get-Date
$backoff = 1
$tunnelBackoff = 1
$warnedMissing = $false

function Write-Log([string]$Message) {
  $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"), $Message
  Add-Content -LiteralPath (Join-Path $Logs "watch.log") -Value $line -Encoding UTF8
}

function Get-TrackedProcess([string]$PidFile) {
  if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
  try {
    $metadata = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json -ErrorAction Stop
    $process = Get-Process -Id ([int]$metadata.pid) -ErrorAction SilentlyContinue
    if (-not $process) { throw "not running" }
    if ($process.StartTime.ToUniversalTime().Ticks -ne [int64]$metadata.startedUtcTicks) {
      throw "process id was reused"
    }
    return $process
  } catch {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $null
  }
}

function Set-TrackedProcess([string]$PidFile, $Process, [string]$Executable) {
  $Process.Refresh()
  [ordered]@{
    pid             = $Process.Id
    startedUtcTicks = $Process.StartTime.ToUniversalTime().Ticks
    executable      = [IO.Path]::GetFullPath($Executable)
  } | ConvertTo-Json -Compress | Set-Content -LiteralPath $PidFile -Encoding UTF8 -NoNewline
}

function Test-HostHealth {
  try {
    $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 4
    return [int]$response.StatusCode -eq 200
  } catch { return $false }
}

function Find-Cloudflared {
  $local = Join-Path $Root "tools\cloudflared.exe"
  if (Test-Path -LiteralPath $local) { return $local }
  $command = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  return $null
}

function Get-TunnelArguments {
  # A named tunnel with a fixed hostname is the durable option.
  $configPath = Join-Path $Logs "tunnel.json"
  if (Test-Path -LiteralPath $configPath) {
    try {
      $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
      Write-Log "tunnel.json could not be parsed; using a quick tunnel"
      $config = $null
    }
    if ($config -and $config.name) {
      $arguments = @("tunnel", "--no-autoupdate", "run", [string]$config.name)
      if ($config.credentials) { $arguments += @("--credentials-file", [string]$config.credentials) }
      if ($config.url) { $arguments += @("--url", [string]$config.url) }
      return @{ Arguments = $arguments; Named = $true }
    }
  }
  return @{ Arguments = @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:18765"); Named = $false }
}

function Start-Host {
  $process = Start-Process -FilePath $Python -ArgumentList "-m", "app.server" -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $Logs "server.out.log") `
    -RedirectStandardError (Join-Path $Logs "server.err.log") -WindowStyle Hidden -PassThru
  Set-TrackedProcess $ServerPid $process $Python
  return $process
}

function Start-Tunnel([string]$Cloudflared) {
  $plan = Get-TunnelArguments
  $mode = if ($plan.Named) { "named tunnel" } else { "quick tunnel" }
  Write-Log "starting cloudflared ($mode)"
  $process = Start-Process -FilePath $Cloudflared -ArgumentList $plan.Arguments -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $Logs "cloudflared.out.log") `
    -RedirectStandardError (Join-Path $Logs "cloudflared.err.log") -WindowStyle Hidden -PassThru
  Set-TrackedProcess $TunnelPid $process $Cloudflared
  return $process
}

Write-Log "supervisor started (every ${CheckSeconds}s, tunnel=$(-not $NoTunnel))"

while ($true) {
  $server = Get-TrackedProcess $ServerPid
  $healthy = Test-HostHealth
  if (-not $server) {
    if ($healthy) {
      Write-Log "a host started outside this supervisor is answering; adopting it"
      $backoff = 1
    } else {
      Write-Log "host is not running; starting it"
      Start-Host | Out-Null
      $backoff = 1
    }
  } elseif (-not $healthy) {
    Write-Log "host did not answer $HealthUrl; restarting it"
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ServerPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min(60, $backoff * 2)
    continue
  } else {
    $backoff = 1
  }

  if (-not $NoTunnel) {
    $cloudflared = Find-Cloudflared
    $tunnel = Get-TrackedProcess $TunnelPid
    if (-not $cloudflared) {
      if (-not $warnedMissing) {
        Write-Log "cloudflared was not found; only local access is supervised"
        $warnedMissing = $true
      }
    } elseif (-not $tunnel) {
      Start-Tunnel $cloudflared | Out-Null
      $tunnelBackoff = 1
    } elseif ($tunnel.HasExited) {
      Write-Log "cloudflared exited with code $($tunnel.ExitCode); restarting in ${tunnelBackoff}s"
      Remove-Item -LiteralPath $TunnelPid -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds $tunnelBackoff
      $tunnelBackoff = [Math]::Min(120, $tunnelBackoff * 2)
      continue
    }
  }

  if ($MaxSeconds -gt 0 -and ((Get-Date) - $startedAt).TotalSeconds -ge $MaxSeconds) {
    Write-Log "supervisor reached its time limit; stopping"
    break
  }
  Start-Sleep -Seconds $CheckSeconds
}
