# First-logon setup for the Ambient Windows test VM. Runs once, elevated, from the unattend CD.
# Log: C:\ambient-setup\firstlogon.log    Done marker: C:\ambient-setup\firstlogon.done
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force C:\ambient-setup | Out-Null
Start-Transcript -Path C:\ambient-setup\firstlogon.log -Append
$cd = Split-Path -Parent $MyInvocation.MyCommand.Path

# Keep the desktop live: no sleep, no screen-off, no lock, no surprise update reboots.
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
# Let script shims such as npm.ps1 run in PowerShell.
Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force

# OpenSSH server: use the Windows capability, fall back to Microsoft's MSI from GitHub.
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  try { Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop } catch { Write-Host "capability install failed: $_" }
}
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  try {
    [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
    $rel = Invoke-RestMethod 'https://api.github.com/repos/PowerShell/Win32-OpenSSH/releases/latest' -UseBasicParsing
    $msi = $rel.assets | Where-Object { $_.name -like 'OpenSSH-Win64-*.msi' } | Select-Object -First 1
    Invoke-WebRequest $msi.browser_download_url -OutFile C:\ambient-setup\openssh.msi -UseBasicParsing
    Start-Process msiexec.exe -ArgumentList '/i C:\ambient-setup\openssh.msi /qn /norestart' -Wait
  } catch { Write-Host "msi install failed: $_" }
}
New-Item -ItemType Directory -Force C:\ProgramData\ssh | Out-Null
Copy-Item (Join-Path $cd 'authorized_keys') C:\ProgramData\ssh\administrators_authorized_keys -Force
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F'
New-Item -Path HKLM:\SOFTWARE\OpenSSH -Force | Out-Null
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force | Out-Null
if (-not (Get-NetFirewallRule -Name 'ambient-sshd' -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name 'ambient-sshd' -DisplayName 'OpenSSH Server (Ambient test VM)' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -Profile Any
}
Set-Service sshd -StartupType Automatic
Start-Service sshd
Restart-Service sshd
Get-Service sshd | Format-List Name, Status, StartType

Set-Content C:\ambient-setup\firstlogon.done (Get-Date -Format o)
Stop-Transcript
