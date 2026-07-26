$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    py -m venv .venv
    & $python -m pip install --disable-pip-version-check -r requirements.txt
}
& $python app.py
