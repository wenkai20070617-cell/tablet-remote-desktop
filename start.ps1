$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"
$Stamp = Join-Path $Venv ".requirements.sha256"

function Find-WorkingPython {
  $Candidates = @(Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object Source)
  $Candidates += @(Get-Command py -All -ErrorAction SilentlyContinue | ForEach-Object Source)
  foreach ($Candidate in ($Candidates | Select-Object -Unique)) {
    & $Candidate -c "import encodings,sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,15) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return $Candidate }
  }
  throw "Python 3.10-3.14 was not found or the installation is damaged. Install Python from python.org."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
  $Python = Find-WorkingPython
  & $Python -m venv $Venv
}

$RequiredHash = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
$InstalledHash = if (Test-Path -LiteralPath $Stamp) { (Get-Content -LiteralPath $Stamp -Raw).Trim() } else { "" }
if ($RequiredHash -ne $InstalledHash) {
  & $VenvPython -m pip install -r $Requirements
  if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
  Set-Content -LiteralPath $Stamp -Value $RequiredHash -NoNewline
}

Push-Location $Root
try { & $VenvPython -m app.server } finally { Pop-Location }