# start.ps1 - run aistudio-api in this console (Ctrl+C stops it; PowerShell asks no Y/N).
# Run:  .\start.ps1
# Override with env vars:  $env:PORT='8081'; .\start.ps1
#                          $env:HOST='127.0.0.1'; .\start.ps1
#                          $env:AISTUDIO_BROWSER_HEADLESS='false'; .\start.ps1
Set-Location -LiteralPath $PSScriptRoot

if (-not $env:PORT) { $env:PORT = '8080' }
if (-not $env:HOST) { $env:HOST = '0.0.0.0' }
$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
$env:PYTHONIOENCODING = 'utf-8'

$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    $py = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = 'python' }
}

& $py -c "import fastapi" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[start] installing dependencies from requirements.txt ..."
    & $py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[start] dependency install failed"
        exit 1
    }
}

Write-Host "[start] aistudio-api on $($env:HOST):$($env:PORT)   (Ctrl+C to stop)"
Write-Host "[start] panel: http://127.0.0.1:$($env:PORT)/   api: http://$($env:HOST):$($env:PORT)/v1beta/models"
Write-Host ""

& $py main.py server --host $env:HOST --port $env:PORT
