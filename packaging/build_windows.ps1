$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$Release = Join-Path $Root "release"
$Build = Join-Path $Root "packaging\build"
$Spec = Join-Path $Root "packaging\pxyfutures.spec"
$Readme = Join-Path $Release "README.txt"
$Zip = Join-Path $Release "PXYFUTURES-client.zip"
$PackageOutput = Join-Path $Build "validated-package"
$Receipt = Join-Path $PackageOutput "版本信息.json"
$SourceCommit = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to identify source commit" }
$SourceStatus = @(& git -C $Root status --porcelain)
if ($LASTEXITCODE -ne 0 -or $SourceStatus.Count -ne 0) {
    throw "Commit and push tested source changes before creating a customer release"
}

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
    & $env:ComSpec /d /s /c "npm.cmd test"
    if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed" }
    & $env:ComSpec /d /s /c "npm.cmd run build -- --mode=desktop --base=/ --outDir=../packaging/build/frontend-desktop"
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
} finally {
    Pop-Location
}

$FrontendDist = Join-Path $Build "frontend-desktop"
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
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Backend checks failed" }
} finally {
    Pop-Location
}

Write-Host "[4/5] Packaging single-file EXE..."
& $Python -m PyInstaller --noconfirm --clean --distpath $PackageOutput --workpath $Build $Spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller packaging failed" }

$Exe = Get-ChildItem $PackageOutput -Filter "*.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $Exe) { throw "Packaged EXE was not found in: $Release" }
$Version = (& $Python -c "from app.version import APP_VERSION; print(APP_VERSION)").Trim()
& $Python (Join-Path $PSScriptRoot "verify_customer_package.py") --exe $Exe.FullName --expected-version $Version --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "Customer package verification failed" }
$PackageInfo = Get-Content -LiteralPath $Receipt -Raw -Encoding UTF8 | ConvertFrom-Json
$FinalCommit = (& git -C $Root rev-parse HEAD).Trim()
$FinalStatus = @(& git -C $Root status --porcelain)
if ($FinalCommit -ne $SourceCommit -or $FinalStatus.Count -ne 0) {
    throw "Source changed during packaging; run checks again before release"
}
$PackageInfo | Add-Member -NotePropertyName source_commit -NotePropertyValue $SourceCommit
$PackageInfo | Add-Member -NotePropertyName built_at -NotePropertyValue ([DateTime]::UtcNow.ToString("o"))
$PackageInfo | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding UTF8

# 验收成功后才替换交付包，上一个版本按固定文件名保留。
$Previous = Join-Path $Release "previous"
New-Item -ItemType Directory -Force -Path $Previous | Out-Null
foreach ($Name in @($Exe.Name, "PXYFUTURES-client.zip", "版本信息.json", "README.txt")) {
    $CurrentFile = Join-Path $Release $Name
    if (Test-Path -LiteralPath $CurrentFile -PathType Leaf) {
        Copy-Item -LiteralPath $CurrentFile -Destination (Join-Path $Previous $Name) -Force
    }
}
Copy-Item -LiteralPath $Exe.FullName -Destination $Release -Force
Copy-Item -LiteralPath $Receipt -Destination $Release -Force
Copy-Item (Join-Path $Root "packaging\client_readme.txt") $Readme -Force
$Exe = Get-Item -LiteralPath (Join-Path $Release $Exe.Name)
$ReleaseReceipt = Join-Path $Release "版本信息.json"
$ZipCreated = $false
for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
    try {
        Compress-Archive -LiteralPath @($Exe.FullName, $Readme, $ReleaseReceipt) -DestinationPath $Zip -CompressionLevel Optimal -Force
        $ZipCreated = $true
        break
    } catch {
        if ($Attempt -eq 10) { throw }
        Start-Sleep -Seconds 1
    }
}
if (-not $ZipCreated) { throw "Client ZIP was not created: $Zip" }
Remove-Item -LiteralPath (Join-Path $PackageOutput $Exe.Name), $Receipt -Force

Write-Host "[5/5] Build completed."
Get-ChildItem $Release -File | Select-Object Name, Length, LastWriteTime
