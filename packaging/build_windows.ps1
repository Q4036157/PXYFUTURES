$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Release = Join-Path $Root "release"
$Build = Join-Path $Root "packaging\build"
$Spec = Join-Path $Root "packaging\pxyfutures.spec"
$Readme = Join-Path $Release "README.txt"
$Zip = Join-Path $Release "PXYFUTURES-client.zip"

if (-not (Test-Path $Python)) {
    throw "Backend virtual environment not found: $Python"
}

Write-Host "[1/5] Preparing desktop packaging dependencies..."
Push-Location $Backend
try {
    & $Python -m pip install --disable-pip-version-check -e ".[desktop]"
    if ($LASTEXITCODE -ne 0) { throw "Desktop dependency installation failed" }
} finally {
    Pop-Location
}

Write-Host "[2/5] Building frontend..."
Push-Location (Join-Path $Root "frontend")
try {
    & $env:ComSpec /d /s /c "npm.cmd run build -- --mode=desktop --base=/"
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
} finally {
    Pop-Location
}

$FrontendDist = Join-Path $Root "frontend\dist"
$DesktopIndex = Join-Path $FrontendDist "index.html"
$IndexHtml = Get-Content $DesktopIndex -Raw
$AssetMatches = [regex]::Matches($IndexHtml, '(?:src|href)="(/assets/[^\"]+)"')
if ($AssetMatches.Count -eq 0) {
    throw "Desktop frontend does not reference root /assets files"
}
foreach ($AssetMatch in $AssetMatches) {
    $AssetPath = Join-Path $FrontendDist $AssetMatch.Groups[1].Value.TrimStart("/")
    if (-not (Test-Path $AssetPath -PathType Leaf)) {
        throw "Desktop frontend asset was not built: $AssetPath"
    }
}
$HostedApiReference = Get-ChildItem (Join-Path $FrontendDist "assets") -Filter "*.js" |
    Select-String -Pattern "/futures-app/api" -SimpleMatch |
    Select-Object -First 1
if ($null -ne $HostedApiReference) {
    throw "Desktop frontend still references the hosted /futures-app/api path"
}

Write-Host "[3/5] Running backend tests..."
Push-Location $Backend
try {
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed" }
} finally {
    Pop-Location
}

Write-Host "[4/5] Packaging single-file EXE..."
& $Python -m PyInstaller --noconfirm --clean --distpath $Release --workpath $Build $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller packaging failed" }

Copy-Item (Join-Path $Root "packaging\client_readme.txt") $Readme -Force
$Exe = Get-ChildItem $Release -Filter "*.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $Exe) { throw "Packaged EXE was not found in: $Release" }
$ZipCreated = $false
for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
    try {
        Compress-Archive -LiteralPath @($Exe.FullName, $Readme) -DestinationPath $Zip -CompressionLevel Optimal -Force
        $ZipCreated = $true
        break
    } catch {
        if ($Attempt -eq 10) { throw }
        Start-Sleep -Seconds 1
    }
}
if (-not $ZipCreated) { throw "Client ZIP was not created: $Zip" }

Write-Host "[5/5] Build completed."
Get-ChildItem $Release -File | Select-Object Name, Length, LastWriteTime
