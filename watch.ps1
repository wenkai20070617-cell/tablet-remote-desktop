# Supervises the host and, when configured, the Cloudflare tunnel, restarting
# either one if it exits or stops answering.  This is the foreground wrapper
# around tools\service_runner.ps1, which start_detached.ps1 and the scheduled
# task run detached.
#
#   .\watch.ps1                 # supervise in this window
#   .\watch.ps1 -NoTunnel       # host only
#
# Install it as a logon task so remote access survives a reboot:
#   .\install_autostart.ps1 -StartNow
[CmdletBinding()]
param(
  [int]$CheckSeconds = 15,
  [switch]$NoTunnel,
  [int]$MaxSeconds = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $Root "tools\service_runner.ps1"
if (-not (Test-Path -LiteralPath $Runner)) { throw "tools\service_runner.ps1 was not found." }

Write-Host "Supervising the host$(if ($NoTunnel) { '' } else { ' and tunnel' }); press Ctrl+C to stop watching."
Write-Host "Actions are appended to logs\watch.log."

& $Runner -CheckSeconds $CheckSeconds -NoTunnel:$NoTunnel -MaxSeconds $MaxSeconds
