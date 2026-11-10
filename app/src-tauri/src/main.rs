// Ambient: menu-bar shell around the ambient-engine sidecar.
//
// The engine (frozen Python + mitmproxy) does the work and serves the UI on
// 127.0.0.1:<port>. This shell starts/stops it, owns the tray, and shows the
// engine's UI: a small panel under the menu-bar icon (/panel) and a full window (/).
// It also polls the engine for a "waiting" count (shown next to the icon) and for
// notifications (things a trigger wants to offer you, results of triggers that ran
// on their own), which it posts as native macOS notifications.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, PhysicalPosition, Rect, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_notification::NotificationExt;

const PANEL_W: f64 = 380.0;
const PANEL_H: f64 = 580.0;

#[cfg(not(windows))]
const LOADING_URL: &str = "tauri://localhost/index.html";
// WebView2 has no custom schemes: Tauri serves the bundled page from http://tauri.localhost there.
#[cfg(windows)]
const LOADING_URL: &str = "http://tauri.localhost/index.html";

struct Engine {
    child: Mutex<Option<Child>>,
    args: Vec<String>,
    port: u16,
    // bumped whenever the window should stop waiting (pause/quit/new wait)
    generation: AtomicU64,
    pause_item: Mutex<Option<MenuItem<Wry>>>,
    // an engine was already answering on the port when we started (e.g. run from
    // source): use it, never start a second capture, and never stop it ourselves
    external: AtomicBool,
    paused: AtomicBool,
}

/// AMBIENT_ENGINE_ARGS (space-separated) replaces the engine's default mode;
/// the UI port follows any `ambient_ui=N` in it.
fn engine_config() -> (Vec<String>, u16) {
    let args: Vec<String> = std::env::var("AMBIENT_ENGINE_ARGS")
        .map(|s| s.split_whitespace().map(String::from).collect())
        .unwrap_or_default();
    let port = args
        .iter()
        .find_map(|a| a.strip_prefix("ambient_ui=").and_then(|p| p.parse().ok()))
        .unwrap_or(8787);
    (args, port)
}

fn engine_binary() -> PathBuf {
    // Tauri copies externalBin sidecars next to the main executable
    // (Contents/MacOS on macOS, the install folder on Windows, /usr/bin on Linux).
    let exe = std::env::current_exe().expect("current exe");
    let name = if cfg!(windows) { "ambient-engine.exe" } else { "ambient-engine" };
    exe.parent().unwrap().join(name)
}

fn log_file() -> PathBuf {
    let dir = if cfg!(target_os = "macos") {
        PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".into())).join("Library/Logs/Ambient")
    } else if cfg!(windows) {
        PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into())).join("Ambient").join("logs")
    } else {
        let state = std::env::var("XDG_STATE_HOME").unwrap_or_else(|_| {
            format!("{}/.local/state", std::env::var("HOME").unwrap_or_else(|_| "/tmp".into()))
        });
        PathBuf::from(state).join("ambient")
    };
    let _ = std::fs::create_dir_all(&dir);
    dir.join("engine.log")
}

/// Ask the engine to shut down the way Ctrl-C would, so capture is released cleanly.
#[cfg(unix)]
fn interrupt(child: &mut Child) {
    unsafe {
        libc::kill(child.id() as i32, libc::SIGINT);
    }
}

/// Windows has no SIGINT for a windowless child. The engine watches its stdin instead
/// (AMBIENT_STOP_ON_STDIN_EOF): closing our end makes it shut down the way Ctrl-C would, which
/// ends the elevated capture helper and releases the network. If it doesn't exit in time,
/// stop_engine kills it; the helper still exits because its pipe to the engine closes.
#[cfg(not(unix))]
fn interrupt(child: &mut Child) {
    drop(child.stdin.take());
}

#[cfg(windows)]
#[link(name = "user32")]
extern "system" {
    fn AllowSetForegroundWindow(process_id: u32) -> i32;
}

/// When the panel last lost focus (Windows only, see toggle_panel).
#[cfg(windows)]
static PANEL_BLURRED_AT: Mutex<Option<Instant>> = Mutex::new(None);

fn running(child: &mut Option<Child>) -> bool {
    match child {
        Some(c) => matches!(c.try_wait(), Ok(None)),
        None => false,
    }
}

fn start_engine(engine: &Engine) {
    engine.paused.store(false, Ordering::SeqCst);
    if engine.external.load(Ordering::SeqCst) {
        return;
    }
    let mut guard = engine.child.lock().unwrap();
    if running(&mut guard) {
        return;
    }
    let mut cmd = Command::new(engine_binary());
    cmd.args(&engine.args)
        .env("AMBIENT_LOG_FILE", log_file())
        .env("AMBIENT_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console window behind the tray app
        // We keep the write end of the engine's stdin; closing it is how we ask it to stop (see interrupt).
        cmd.stdin(Stdio::piped()).env("AMBIENT_STOP_ON_STDIN_EOF", "1");
        // Agents the engine starts (Python ones included) should read and write UTF-8, as on macOS.
        cmd.env("PYTHONUTF8", "1");
    }
    let spawned = cmd.spawn();
    // The engine asks Windows for permission to capture (a UAC prompt for its helper). Windows only puts
    // that prompt in front when the asking process may take the foreground; otherwise it just blinks in
    // the taskbar. We are the app the person just clicked, so hand that right to the engine.
    #[cfg(windows)]
    if let Ok(c) = &spawned {
        unsafe {
            AllowSetForegroundWindow(c.id());
        }
    }
    match spawned {
        Ok(c) => *guard = Some(c),
        Err(e) => eprintln!("ambient: failed to start engine: {e}"),
    }
}

/// SIGINT first so mitmproxy shuts the macOS redirector down cleanly.
fn stop_engine(engine: &Engine) {
    engine.paused.store(true, Ordering::SeqCst);
    let mut guard = engine.child.lock().unwrap();
    if let Some(mut c) = guard.take() {
        interrupt(&mut c);
        let deadline = Instant::now() + Duration::from_secs(8);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = c.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = c.kill();
        let _ = c.wait();
    }
}

fn engine_ready(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    if s.write_all(b"GET /api/summary HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").is_err() {
        return false;
    }
    let mut buf = [0u8; 16];
    let n = s.read(&mut buf).unwrap_or(0);
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.0 200") || head.starts_with("HTTP/1.1 200")
}

/// GET a small JSON endpoint from the engine (HTTP/1.0, so the body ends at EOF).
fn engine_get(port: u16, path: &str) -> Option<serde_json::Value> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_millis(400)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
    s.write_all(format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").as_bytes()).ok()?;
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    let (head, body) = text.split_once("\r\n\r\n")?;
    if !head.contains(" 200") {
        return None;
    }
    serde_json::from_str(body).ok()
}

/// The engine we started has ended by itself.
#[cfg(windows)]
fn engine_exited(engine: &Engine) -> bool {
    let mut guard = engine.child.lock().unwrap();
    matches!(guard.as_mut().map(|c| c.try_wait()), Some(Ok(Some(_))))
}

/// Every 2s: put the "waiting" count next to the menu-bar icon and post new notifications.
fn watch_engine(app: AppHandle) {
    std::thread::spawn(move || {
        let port = app.state::<Engine>().port;
        let mut cursor: Option<u64> = None; // first poll only sets the cursor, so a restart doesn't replay old ones
        let mut last_title = String::from("-");
        #[cfg(windows)]
        let mut last_tip = String::new();
        loop {
            std::thread::sleep(Duration::from_secs(2));
            let paused = app.state::<Engine>().paused.load(Ordering::SeqCst);
            // On Windows the engine ends itself when it can't start the capture helper: the person answered
            // No to the Windows permission prompt, or security software blocked it. Show "paused" and say
            // why, instead of "starting" forever. Resume watching asks Windows again.
            #[cfg(windows)]
            if !paused && engine_exited(&app.state::<Engine>()) {
                set_paused(&app, true);
                let _ = app
                    .notification()
                    .builder()
                    .title("Ambient isn't watching")
                    .body("Windows didn't let Ambient see app traffic. Right-click the Ambient icon, choose Resume watching, and answer Yes when Windows asks.")
                    .show();
                continue;
            }
            let status = if paused { None } else { engine_get(port, "/api/status") };
            let (title, tip) = match &status {
                None if paused => (String::new(), "Ambient · paused".to_string()),
                None => (String::new(), "Ambient · starting".to_string()),
                Some(st) => {
                    let waiting = st["waiting"].as_u64().unwrap_or(0);
                    let running = st["running"].as_u64().unwrap_or(0);
                    let armed = st["armed"].as_array().map(|a| a.len()).unwrap_or(0);
                    let title = if waiting > 0 { format!("{waiting}") } else { String::new() };
                    let mut tip = String::from("Ambient · listening");
                    if waiting > 0 { tip += &format!(" · {waiting} waiting for you"); }
                    if running > 0 { tip += &format!(" · {running} agent working"); }
                    if armed > 0 { tip += &format!(" · {armed} trigger(s) run on their own"); }
                    (title, tip)
                }
            };
            if title != last_title {
                if let Some(tray) = app.tray_by_id("ambient") {
                    let _ = tray.set_title(if title.is_empty() { None } else { Some(title.as_str()) });
                    let _ = tray.set_tooltip(Some(tip.as_str()));
                }
                last_title = title;
            }
            // Windows shows no text next to a tray icon, so the tooltip carries the state: keep it current.
            #[cfg(windows)]
            if tip != last_tip {
                if let Some(tray) = app.tray_by_id("ambient") {
                    let _ = tray.set_tooltip(Some(tip.as_str()));
                }
                last_tip = tip.clone();
            }
            if status.is_none() {
                continue;
            }
            let after = cursor.unwrap_or(0);
            let Some(feed) = engine_get(port, &format!("/api/notes?after={after}")) else { continue };
            let seq = feed["seq"].as_u64().unwrap_or(after);
            if seq < after {
                cursor = Some(0); // the engine restarted and is numbering from the start again
                continue;
            }
            if cursor.is_some() {
                for n in feed["notes"].as_array().into_iter().flatten() {
                    let _ = app
                        .notification()
                        .builder()
                        .title(n["title"].as_str().unwrap_or("Ambient"))
                        .body(n["body"].as_str().unwrap_or(""))
                        .show();
                }
            }
            cursor = Some(seq);
        }
    });
}

/// The panel under the menu-bar icon. Built once, then shown/hidden.
fn toggle_panel(app: &AppHandle, icon: Option<Rect>) {
    // On Windows a click on the tray icon first takes focus from the open panel, which hides it; the
    // same click must not then open it again.
    #[cfg(windows)]
    if PANEL_BLURRED_AT.lock().unwrap().map_or(false, |t| t.elapsed() < Duration::from_millis(400)) {
        return;
    }
    let port = app.state::<Engine>().port;
    let panel = match app.get_webview_window("panel") {
        Some(w) => {
            if w.is_visible().unwrap_or(false) {
                let _ = w.hide();
                return;
            }
            w
        }
        None => {
            let url = if engine_ready(port) {
                WebviewUrl::External(Url::parse(&format!("http://127.0.0.1:{port}/panel")).unwrap())
            } else {
                WebviewUrl::App("index.html".into())
            };
            let Ok(w) = WebviewWindowBuilder::new(app, "panel", url)
                .title("Ambient")
                .inner_size(PANEL_W, PANEL_H)
                .resizable(false)
                .decorations(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .visible(false)
                .shadow(true)
                .build()
            else {
                return;
            };
            w
        }
    };
    // If the engine came up after the panel was built, point it at the engine now.
    if let Ok(u) = panel.url() {
        // (On Windows the bundled loading page is http too, at tauri.localhost: go by the host there.)
        let on_engine = if cfg!(windows) { u.host_str() == Some("127.0.0.1") } else { u.scheme() == "http" };
        if !on_engine && engine_ready(port) {
            let _ = panel.navigate(Url::parse(&format!("http://127.0.0.1:{port}/panel")).unwrap());
        }
    }
    // Work in physical pixels of the monitor the tray icon is on (its scale, its bounds): a hidden
    // window doesn't reliably know which monitor or scale it will land on.
    let primary = app.primary_monitor().ok().flatten();
    let pscale = primary.as_ref().map(|m| m.scale_factor()).unwrap_or(1.0);
    let icon_px = icon.map(|r| {
        let pos: PhysicalPosition<f64> = r.position.to_physical(pscale);
        let size: tauri::PhysicalSize<f64> = r.size.to_physical(pscale);
        (pos, size)
    });
    let monitor = icon_px
        .and_then(|(pos, size)| app.monitor_from_point(pos.x + size.width / 2.0, pos.y + size.height / 2.0).ok().flatten())
        .or(primary);
    let scale = monitor.as_ref().map(|m| m.scale_factor()).unwrap_or(pscale);
    let (mx, my, mw, mh) = monitor
        .as_ref()
        .map(|m| (m.position().x as f64, m.position().y as f64, m.size().width as f64, m.size().height as f64))
        .unwrap_or((0.0, 0.0, 1440.0 * scale, 900.0 * scale));
    let (w, h) = (PANEL_W * scale, PANEL_H * scale);
    let gap = 6.0 * scale;
    let (x, y) = match icon_px {
        Some((pos, size)) => {
            let x = pos.x + size.width / 2.0 - w / 2.0;
            // Menu bar at the top (macOS, most Linux desktops): drop down. Taskbar at the bottom (Windows): pop up.
            let y = if pos.y - my > mh / 2.0 { pos.y - h - gap } else { pos.y + size.height + gap };
            (x, y)
        }
        // No icon position (Linux trays only report menu clicks): top-right corner, under the bar.
        None => (mx + mw - w - gap, my + 36.0 * scale),
    };
    let x = x.min(mx + mw - w - gap).max(mx + gap);
    let y = y.min(my + mh - h - gap).max(my + gap);
    if std::env::var("AMBIENT_DEBUG_PANEL").is_ok() {
        eprintln!("panel: icon={icon_px:?} monitor=({mx},{my},{mw},{mh}) scale={scale} -> ({x},{y}) size=({w},{h})");
    }
    let _ = panel.set_position(PhysicalPosition::new(x, y));
    let _ = panel.show();
    let _ = panel.set_focus();
}

#[tauri::command]
fn open_main(app: AppHandle) {
    if let Some(p) = app.get_webview_window("panel") {
        let _ = p.hide();
    }
    show_window(&app);
}

#[tauri::command]
fn toggle_pause(app: AppHandle) -> bool {
    let paused = !app.state::<Engine>().paused.load(Ordering::SeqCst);
    set_paused(&app, paused);
    paused
}

/// Show the loading page, then switch the window to the engine UI once it answers.
fn point_window_at_engine(app: &AppHandle) {
    let engine = app.state::<Engine>();
    let gen = engine.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let port = engine.port;
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.navigate(Url::parse(LOADING_URL).unwrap());
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(90);
        while Instant::now() < deadline {
            if handle.state::<Engine>().generation.load(Ordering::SeqCst) != gen {
                return; // superseded (paused, or a newer wait started)
            }
            if engine_ready(port) {
                if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.navigate(Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap());
                }
                return;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    });
}

fn show_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let built = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Ambient")
        .inner_size(1100.0, 760.0)
        .min_inner_size(720.0, 480.0)
        .build();
    if let Ok(w) = built {
        let _ = w.set_focus();
        let paused = app.state::<Engine>().paused.load(Ordering::SeqCst);
        if paused {
            let _ = w.navigate(Url::parse(&format!("{LOADING_URL}#paused")).unwrap());
        } else {
            point_window_at_engine(app);
        }
    }
}

fn set_paused(app: &AppHandle, paused: bool) {
    let engine = app.state::<Engine>();
    if engine.external.load(Ordering::SeqCst) {
        let _ = app
            .notification()
            .builder()
            .title("Ambient")
            .body("This engine was started outside the app, so pause it where you started it.")
            .show();
        return;
    }
    if paused {
        engine.generation.fetch_add(1, Ordering::SeqCst);
        stop_engine(&engine);
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.navigate(Url::parse(&format!("{LOADING_URL}#paused")).unwrap());
        }
    } else {
        start_engine(&engine);
        if app.get_webview_window("main").is_some() {
            point_window_at_engine(app);
        }
    }
    let item = engine.pause_item.lock().unwrap().clone();
    if let Some(item) = item {
        let _ = item.set_text(if paused { "Resume watching" } else { "Pause watching" });
    }
}

fn main() {
    let (args, port) = engine_config();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show_window(app)))
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![open_main, toggle_pause])
        .manage(Engine {
            child: Mutex::new(None),
            args,
            port,
            generation: AtomicU64::new(0),
            pause_item: Mutex::new(None),
            external: AtomicBool::new(false),
            paused: AtomicBool::new(false),
        })
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let peek = MenuItem::with_id(app, "panel", "Show what's happening", true, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Open Ambient", true, None::<&str>)?;
            let pause = MenuItem::with_id(app, "pause", "Pause watching", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Ambient", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&peek, &open, &pause, &sep, &quit])?;
            *app.state::<Engine>().pause_item.lock().unwrap() = Some(pause);

            // tray.png is a one-colour template glyph for the macOS menu bar; it would vanish on a dark
            // Windows taskbar, which wants the full-colour icon.
            #[cfg(not(any(windows, target_os = "linux")))]
            let tray_icon = Image::from_bytes(include_bytes!("../icons/tray.png"))?;
            #[cfg(windows)]
            let tray_icon = Image::from_bytes(include_bytes!("../icons/32x32.png"))?;
            // Linux panels don't recolour template icons either (it would be black on a dark panel).
            #[cfg(target_os = "linux")]
            let tray_icon = Image::from_bytes(include_bytes!("../icons/64x64.png"))?;
            TrayIconBuilder::with_id("ambient")
                .icon(tray_icon)
                .icon_as_template(true)
                .tooltip("Ambient")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, rect, .. } = event {
                        toggle_panel(tray.app_handle(), Some(rect));
                    }
                })
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "panel" => toggle_panel(app, app.tray_by_id("ambient").and_then(|t| t.rect().ok().flatten())),
                    "open" => show_window(app),
                    "pause" => {
                        let paused = app.state::<Engine>().paused.load(Ordering::SeqCst);
                        set_paused(app, !paused);
                    }
                    "quit" => {
                        let engine = app.state::<Engine>();
                        engine.generation.fetch_add(1, Ordering::SeqCst);
                        stop_engine(&engine);
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            let engine = app.state::<Engine>();
            if engine_ready(engine.port) {
                engine.external.store(true, Ordering::SeqCst);
            }
            start_engine(&engine);
            watch_engine(app.handle().clone());
            show_window(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| match event {
            // Closing the window keeps Ambient running in the menu bar.
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            // The panel behaves like a menu: clicking elsewhere puts it away.
            WindowEvent::Focused(false) if window.label() == "panel" => {
                #[cfg(windows)]
                if window.is_visible().unwrap_or(false) {
                    *PANEL_BLURRED_AT.lock().unwrap() = Some(Instant::now());
                }
                let _ = window.hide();
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("error while building Ambient");

    app.run(|app, event| match event {
        RunEvent::ExitRequested { api, code, .. } if code.is_none() => api.prevent_exit(),
        RunEvent::Exit => stop_engine(&app.state::<Engine>()),
        _ => {}
    });
}
