$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Run start.ps1 once before resetting the password." }
Push-Location $Root
try { & $Python -m app.reset_password } finally { Pop-Location }