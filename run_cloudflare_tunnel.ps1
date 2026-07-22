$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Local = Join-Path $Root "tools\cloudflared.exe"
$Command = if (Test-Path -LiteralPath $Local) { $Local } else { (Get-Command cloudflared -ErrorAction SilentlyContinue).Source }
if (-not $Command) {
  throw "cloudflared is not installed. Download it free from https://developers.cloudflare.com/tunnel/downloads/ and place it on PATH or in tools\."
}
Write-Host "Starting a free best-effort Quick Tunnel for http://127.0.0.1:18765"
Write-Host "Keep this window open. Quick Tunnel has no uptime guarantee."
& $Command tunnel --url "http://127.0.0.1:18765"