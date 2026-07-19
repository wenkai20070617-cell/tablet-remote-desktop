$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) {
  $VenvPython
} else {
  (Get-Command python -ErrorAction Stop).Source
}

& $Python (Join-Path $Root "app\reset_password.py")
