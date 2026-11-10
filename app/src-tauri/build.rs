fn main() {
    // Declare the shell's own commands so a capability can grant them to the engine's
    // pages (remote http://127.0.0.1:* URLs get no command access unless granted).
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["open_main", "toggle_pause"]),
    ))
    .expect("failed to run tauri-build");
}
