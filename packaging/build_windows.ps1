$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root "backend\.venv\Scripts\python.exe"
$Release = Join-Path $Root "release"
$Build = Join-Path $Root "packaging\build"
$Spec = Join-Path $Root "packaging\pxyfutures.spec"

if (-not (Test-Path $Python)) {
    throw "Backend virtual environment not found: $Python"
}

Write-Host "[1/4] Building frontend..."
Push-Location (Join-Path $Root "frontend")
try {
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
} finally {
    Pop-Location
}

Write-Host "[2/4] Running backend tests..."
Push-Location (Join-Path $Root "backend")
try {
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed" }
} finally {
    Pop-Location
}

Write-Host "[3/4] Packaging single-file EXE..."
& $Python -m PyInstaller --noconfirm --clean --distpath $Release --workpath $Build $Spec
if ($LASTEXITCODE -ne 0) { throw "EXE packaging failed" }

Copy-Item (Join-Path $Root "packaging\client_readme.txt") (Join-Path $Release "README.txt") -Force

Write-Host "[4/4] Done"
Get-ChildItem $Release -Filter "*.exe" | Select-Object FullName, Length
