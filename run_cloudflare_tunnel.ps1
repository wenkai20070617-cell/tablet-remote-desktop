# Starts a Cloudflare tunnel for http://127.0.0.1:18765.
#
# Two modes:
#
#   Named tunnel (recommended for long-term remote work)
#     A fixed hostname that survives restarts and is not rate limited.
#     Create logs\tunnel.json next to this script:
#       {
#         "name": "my-remote-desktop",
#         "credentials": "C:\\Users\\me\\.cloudflared\\<id>.json",
#         "url": "http://127.0.0.1:18765"
#       }
#     Finish the one-time setup first:
#       cloudflared tunnel login
#       cloudflared tunnel create my-remote-desktop
#       cloudflared tunnel route dns my-remote-desktop remote.example.com
#
#   Quick tunnel (default when no config exists)
#     A random https://<name>.trycloudflare.com address, best effort only.
#
# Environment overrides: TRD_TUNNEL_NAME, TRD_TUNNEL_URL, TRD_TUNNEL_CREDENTIALS
[CmdletBinding()]
param(
  [string]$Name = $env:TRD_TUNNEL_NAME,
  [string]$Url = $env:TRD_TUNNEL_URL,
  [string]$Credentials = $env:TRD_TUNNEL_CREDENTIALS,
  [string]$LocalUrl = "http://127.0.0.1:18765",
  [switch]$Quick
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $Root "logs\tunnel.json"

if ((Test-Path -LiteralPath $configPath) -and -not $Quick) {
  try {
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "logs\tunnel.json is not valid JSON. Fix it or delete it to use a quick tunnel."
  }
  if (-not $Name) { $Name = [string]$config.name }
  if (-not $Url) { $Url = [string]$config.url }
  if (-not $Credentials) { $Credentials = [string]$config.credentials }
}

$Command = if (Test-Path -LiteralPath (Join-Path $Root "tools\cloudflared.exe")) {
  Join-Path $Root "tools\cloudflared.exe"
} else {
  (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
}
if (-not $Command) {
  throw "cloudflared is not installed. Download it free from https://developers.cloudflare.com/tunnel/downloads/ and place it on PATH or in tools\."
}

if ($Name -and -not $Quick) {
  $arguments = @("tunnel", "--no-autoupdate", "run", $Name)
  if ($Credentials) { $arguments += @("--credentials-file", $Credentials) }
  $arguments += @("--url", ($(if ($Url) { $Url } else { $LocalUrl })))
  Write-Host "Starting the named tunnel '$Name' -> $LocalUrl"
  Write-Host "This address is stable across restarts. Use .\install_autostart.ps1 to keep it running."
  & $Command @arguments
  exit $LASTEXITCODE
}

Write-Host "Starting a free best-effort Quick Tunnel for $LocalUrl"
Write-Host "Quick Tunnel has no uptime guarantee and the address changes on every start."
Write-Host "For long-term access, create logs\tunnel.json with a named tunnel (see the header of this script)."
& $Command tunnel --no-autoupdate --url $LocalUrl
