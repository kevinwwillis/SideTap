# Builds tools\wda-sign into %LOCALAPPDATA%\SideTap\bin\wda-sign.exe, where device.wda_sign_path() finds it.
# wda-sign is go-ios's signer without the phone lookup `ios sign app` does first, so a re-sign works
# with the phone on Wi-Fi only. Needs Go (winget install GoLang.Go).
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot '..\tools\wda-sign'
$out = Join-Path $env:LOCALAPPDATA 'SideTap\bin\wda-sign.exe'
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
Push-Location $src
try {
    go build -trimpath -o $out .
    if ($LASTEXITCODE -ne 0) { throw "go build failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}
Write-Host "built $out"
