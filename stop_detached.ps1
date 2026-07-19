$ErrorActionPreference = "Stop"

$serverProcesses = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*tablet-remote-desktop*app*server.py*" }

foreach ($proc in $serverProcesses) {
  Stop-Process -Id $proc.ProcessId -Force
}

Get-Process -Name cloudflared -ErrorAction SilentlyContinue |
  Stop-Process -Force

Write-Host "Tablet remote desktop processes stopped."
