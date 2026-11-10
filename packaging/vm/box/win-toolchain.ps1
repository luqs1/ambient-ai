# Install the build toolchain in the Windows test VM. Run elevated as the 'ambient' user (win-toolchain on the Mac does this).
# Log: C:\ambient-setup\toolchain.log    Done marker (with versions): C:\ambient-setup\toolchain.done
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$dl = 'C:\ambient-setup\dl'
New-Item -ItemType Directory -Force $dl | Out-Null
Remove-Item C:\ambient-setup\toolchain.done -ErrorAction SilentlyContinue
Start-Transcript -Path C:\ambient-setup\toolchain.log -Append
$sys = "$env:SystemRoot\System32"
function Fetch($url, $out) { Write-Host ">> download $url"; & "$sys\curl.exe" -sS -L --fail --retry 5 -o $out $url; if ($LASTEXITCODE) { Write-Host "!! download failed: $url" } }
function Step($name) { Write-Host ""; Write-Host "==== $name  $(Get-Date -Format T)" }
# Vendor install scripts run in a child PowerShell so they cannot change this script's PATH or error settings.
function RunRemoteScript($url) { & "$sys\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Invoke-RestMethod $url | Invoke-Expression" }

Step 'git'
if (-not (Test-Path 'C:\Program Files\Git\cmd\git.exe')) {
$rel = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -UseBasicParsing
$asset = $rel.assets | Where-Object { $_.name -match '^Git-[\d.]+-64-bit\.exe$' } | Select-Object -First 1
Fetch $asset.browser_download_url "$dl\git-setup.exe"
Start-Process "$dl\git-setup.exe" -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /SUPPRESSMSGBOXES' -Wait
}

Step 'Node LTS'
if (-not (Test-Path 'C:\Program Files\nodejs\node.exe')) {
$lts = (Invoke-RestMethod 'https://nodejs.org/dist/index.json' -UseBasicParsing) | Where-Object { $_.lts } | Select-Object -First 1
Fetch "https://nodejs.org/dist/$($lts.version)/node-$($lts.version)-x64.msi" "$dl\node.msi"
Start-Process "$sys\msiexec.exe" -ArgumentList "/i $dl\node.msi /qn /norestart" -Wait
}

Step 'uv + Python 3.13'
$uv = "$env:USERPROFILE\.local\bin\uv.exe"
if (-not (Test-Path $uv)) { RunRemoteScript 'https://astral.sh/uv/install.ps1' }
& $uv python install 3.13 --default
if ($LASTEXITCODE) { & $uv python install 3.13 }

Step 'bun'
if (-not (Test-Path "$env:USERPROFILE\.bun\bin\bun.exe")) { RunRemoteScript 'https://bun.sh/install.ps1' }

Step 'Visual Studio Build Tools 2022, C++ workload (the slow one)'
Fetch 'https://aka.ms/vs/17/release/vs_BuildTools.exe' "$dl\vs_BuildTools.exe"
$vs = Start-Process "$dl\vs_BuildTools.exe" -ArgumentList '--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended' -Wait -PassThru
Write-Host "vs_BuildTools exit code: $($vs.ExitCode)  (0 = ok, 3010 = ok, reboot needed)"

Step 'Rust (stable, MSVC)'
Fetch 'https://win.rustup.rs/x86_64' "$dl\rustup-init.exe"
& "$dl\rustup-init.exe" -y --default-toolchain stable --default-host x86_64-pc-windows-msvc

Step 'versions'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$wv = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue).pv
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$v = @(
  "git:      $(git --version 2>&1)"
  "node:     $(node --version 2>&1)"
  "npm:      $(npm --version 2>&1)"
  "bun:      $(bun --version 2>&1)"
  "uv:       $(uv --version 2>&1)"
  "python:   $(python --version 2>&1)"
  "rustc:    $(rustc --version 2>&1)"
  "cargo:    $(cargo --version 2>&1)"
  "rustup:   $(rustup show active-toolchain 2>&1)"
  "msvc:     $(& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property catalog_productDisplayVersion 2>&1)"
  "webview2: $wv"
  "vs exit:  $($vs.ExitCode)"
)
$v | ForEach-Object { Write-Host $_ }
Set-Content C:\ambient-setup\toolchain.done $v
Stop-Transcript
# New PATH entries only reach sshd after a restart; rebooting also finishes the Build Tools install.
& "$sys\shutdown.exe" /r /t 5
