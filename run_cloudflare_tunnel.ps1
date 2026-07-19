$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Tools = Join-Path $Root "tools"
$Cloudflared = Join-Path $Tools "cloudflared.exe"

New-Item -ItemType Directory -Force -Path $Tools | Out-Null

if (-not (Test-Path -LiteralPath $Cloudflared)) {
  $Url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
  Write-Host "Downloading cloudflared..."
  Invoke-WebRequest -Uri $Url -OutFile $Cloudflared
}

Write-Host ""
Write-Host "Starting Cloudflare Quick Tunnel for http://127.0.0.1:18765"
Write-Host "Keep this window open while using the tablet."
Write-Host "Use the https://...trycloudflare.com URL printed below."
Write-Host ""

& $Cloudflared tunnel --url "http://127.0.0.1:18765"
