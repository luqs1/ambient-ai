; Hooks for the NSIS installer Tauri generates (bundle.windows.nsis.installerHooks in
; app/src-tauri/tauri.windows.conf.json). They make sure nothing of Ambient is still running, and that the
; capture driver is unloaded, before files are replaced (upgrade) or removed (uninstall).

!macro AmbientStopEverything
  ; The engine (started by ambient.exe) and the elevated capture helper it started through UAC.
  nsExec::Exec 'taskkill /F /T /IM ambient-engine.exe'
  Pop $0
  nsExec::Exec 'taskkill /F /IM windows-redirector.exe'
  Pop $0
  ; The WinDivert driver stays loaded after its last user exits and keeps WinDivert64.sys in use.
  ; Stopping its (temporary, self-registered) service unloads it; Ambient loads it again on next start.
  nsExec::Exec 'sc stop WinDivert'
  Pop $0
  Sleep 1500
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro AmbientStopEverything
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro AmbientStopEverything
!macroend
