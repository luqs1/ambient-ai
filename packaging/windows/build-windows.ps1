# Build Ambient for Windows: the frozen engine (PyInstaller) and the installers (Tauri: NSIS .exe and .msi).
# Run in PowerShell on Windows 10/11 x64 from anywhere in the repo:
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build-windows.ps1
#   ... -SkipEngine         reuse the engine frozen by the last run
#   ... -Bundles nsis       only the .exe installer (default: nsis,msi)
#
# Needs on PATH: uv, cargo (Rust, MSVC toolchain + Visual Studio Build Tools "Desktop development with C++"),
# bun. Nothing is signed. Output: dist\Ambient_<version>_x64-setup.exe and dist\Ambient_<version>_x64_en-US.msi.
param([switch]$SkipEngine, [string]$Bundles = "nsis,msi")
# Not "Stop": uv, cargo and bun write progress to stderr, which Windows PowerShell 5.1 would turn into a
# terminating error whenever the output is redirected. Every step checks its exit code instead.
$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

$triple = ((rustc -vV) | Select-String '^host: ').ToString().Substring(6).Trim()     # x86_64-pc-windows-msvc
$engineDir = Join-Path $root "build\engine-dist\ambient-engine"

Write-Host "==> 1/3 freezing the engine (PyInstaller, folder build)"
if (-not $SkipEngine -or -not (Test-Path "$engineDir\ambient-engine.exe")) {
    uv sync; Check "uv sync"
    uv run --with pyinstaller pyinstaller --noconfirm --log-level WARN `
        --distpath build\engine-dist --workpath build\engine-work packaging\windows\ambient-engine.spec
    Check "pyinstaller"
}
# Tauri wants the sidecar named with the target triple; the engine's own files (_internal) ride along as resources
# and are installed next to it (see app\src-tauri\tauri.windows.conf.json).
$bin = Join-Path $root "app\src-tauri\binaries"
New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item "$engineDir\ambient-engine.exe" "$bin\ambient-engine-$triple.exe" -Force -ErrorAction Stop
robocopy "$engineDir\_internal" "$bin\_internal" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying the engine's files failed (robocopy $LASTEXITCODE)" }
$global:LASTEXITCODE = 0

Write-Host "==> 2/3 building the app and installers (Tauri: $Bundles)"
Set-Location (Join-Path $root "app")
if (-not (Test-Path node_modules)) { bun install; Check "bun install" }
bunx tauri build --bundles $Bundles; Check "tauri build"
Set-Location $root

Write-Host "==> 3/3 collecting installers in dist\"
New-Item -ItemType Directory -Force (Join-Path $root "dist") | Out-Null
$out = Get-ChildItem "app\src-tauri\target\release\bundle\nsis\*-setup.exe", "app\src-tauri\target\release\bundle\msi\*.msi" -ErrorAction SilentlyContinue
if (-not $out) { throw "no installer was produced" }
$out | ForEach-Object { Copy-Item $_.FullName dist\ -Force -ErrorAction Stop; "{0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB) }
