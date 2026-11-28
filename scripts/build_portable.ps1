# Build LiveExtract Portable (Windows x64)
# Compiles native C/C++ bridges (if gcc/g++ is present) and packages LiveExtract.exe into dist/LiveExtract.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$RootDir = Split-Path -Parent $PSScriptRoot
Push-Location $RootDir

try {
    Write-Host "[1/3] Compiling native bridges..." -ForegroundColor Cyan
    if (Get-Command g++ -ErrorAction SilentlyContinue) {
        g++ -O3 -shared -static -o liveextract_7z_bridge.dll native/liveextract_7z_bridge.cpp -loleaut32 -lole32
        Write-Host "      Built liveextract_7z_bridge.dll"
    } else {
        Write-Host "      Warning: g++ not found; skipping liveextract_7z_bridge.dll" -ForegroundColor Yellow
    }

    if (Get-Command gcc -ErrorAction SilentlyContinue) {
        $libz = "C:\msys64\ucrt64\lib\libz.a"
        if (Test-Path $libz) {
            gcc -O3 -shared -static-libgcc -o liveextract_core.dll native/liveextract_stream.c $libz
            Write-Host "      Built liveextract_core.dll"
        }
    }

    Write-Host "[2/3] Packaging LiveExtract.exe via PyInstaller..." -ForegroundColor Cyan
    $addDataArgs = @()
    if (Test-Path "liveextract_core.dll") { $addDataArgs += @("--add-data", "liveextract_core.dll;.") }
    if (Test-Path "liveextract_7z_bridge.dll") { $addDataArgs += @("--add-data", "liveextract_7z_bridge.dll;.") }
    if (Test-Path "assets\icon.ico") { $addDataArgs += @("--add-data", "assets\icon.ico;assets") }

    $iconArgs = @()
    if (Test-Path "assets\icon.ico") { $iconArgs += @("--icon", "assets\icon.ico") }

    $pyArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name", "LiveExtract"
    ) + $iconArgs + @(
        "--hidden-import", "compression.zstd",
        "--hidden-import", "_zstd",
        "--hidden-import", "bz2",
        "--hidden-import", "lzma"
    ) + $addDataArgs + @("launcher.py")

    & python @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    Write-Host "[3/3] Creating portable ZIP archive..." -ForegroundColor Cyan
    if (Test-Path "LICENSE") { Copy-Item "LICENSE" (Join-Path $RootDir "dist\LiveExtract\LICENSE") -Force }
    if (Test-Path "NOTICE")  { Copy-Item "NOTICE"  (Join-Path $RootDir "dist\LiveExtract\NOTICE")  -Force }
    $zipOut = Join-Path $RootDir "dist\LiveExtract-Portable-Windows-x64.zip"
    if (Test-Path $zipOut) { Remove-Item -Force $zipOut }
    Compress-Archive -Path (Join-Path $RootDir "dist\LiveExtract\*") -DestinationPath $zipOut

    Write-Host "`nBuild complete!" -ForegroundColor Green
    Write-Host "  Executable: dist\LiveExtract\LiveExtract.exe"
    Write-Host "  Portable:   dist\LiveExtract-Portable-Windows-x64.zip"
}
finally {
    Pop-Location
}
