# Registers the watchdog so the host (and tunnel) come back after a reboot or
# a crash.  Run once from an elevated PowerShell if you want the machine-wide
# start-on-boot behaviour; a normal window is enough for start-at-logon.
#
#   .\install_autostart.ps1                 # start at logon (current user)
#   .\install_autostart.ps1 -StartNow       # register and launch immediately
#   .\install_autostart.ps1 -Boot           # also start at system boot (elevated)
#   .\install_autostart.ps1 -Remove         # unregister
[CmdletBinding()]
param(
  [switch]$StartNow,
  [switch]$Boot,
  [switch]$Remove,
  [switch]$NoTunnel
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchScript = Join-Path $Root "watch.ps1"
$TaskName = "TabletRemoteDesktop"
$Pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue)
if (-not $Pwsh) { $Pwsh = Get-Command powershell -ErrorAction Stop }

if ($Remove) {
  foreach ($name in @($TaskName, "$TaskName-Boot")) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
      Unregister-ScheduledTask -TaskName $name -Confirm:$false
      Write-Host "Removed scheduled task $name"
    }
  }
  exit 0
}

if (-not (Test-Path -LiteralPath $WatchScript)) { throw "watch.ps1 was not found next to this script." }
$logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$WatchScript`"")
if ($NoTunnel) { $arguments += "-NoTunnel" }
$action = New-ScheduledTaskAction -Execute $Pwsh.Source -Argument ($arguments -join " ") -WorkingDirectory $Root
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 0)

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logonTrigger `
  -Settings $settings -Principal $principal -Force `
  -Description "Tablet Remote Desktop host and tunnel watchdog (starts at logon)." | Out-Null
Write-Host "Registered scheduled task $TaskName (starts at logon)."
if ($Boot) {
  $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if (-not $isAdmin) {
    Write-Warning "-Boot needs an elevated PowerShell; skipping the boot trigger."
  } else {
    $bootTrigger = New-ScheduledTaskTrigger -AtStartup
    $systemPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName "$TaskName-Boot" -Action $action -Trigger $bootTrigger `
      -Settings $settings -Principal $systemPrincipal -Force `
      -Description "Tablet Remote Desktop host and tunnel watchdog (starts at boot)." | Out-Null
    Write-Host "Registered scheduled task $TaskName-Boot (starts at boot)."
  }
}

if ($StartNow) {
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Started $TaskName."
  Start-Sleep -Seconds 8
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:18765/api/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "Health check: HTTP $([int]$response.StatusCode)"
  } catch {
    Write-Warning "The host did not answer yet. Check logs\watch.log and logs\server.err.log."
  }
}

Write-Host ""
Write-Host "Status:  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Logs:    $logs\watch.log, $logs\server.err.log"
Write-Host "Remove:  .\install_autostart.ps1 -Remove"
