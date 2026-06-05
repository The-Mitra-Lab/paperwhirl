// Stage 6 E1: in release builds, this shell spawns the Python
// backend on launch and tears it down on quit. Dev builds skip
// the sidecar entirely — `get_backend_port` returns None and the
// frontend's apiBase() falls back to Vite-proxied relative URLs.

mod sidecar;

use std::sync::Mutex;
use tauri::{Emitter, Manager};

struct BackendPort(u16);
struct ChildHandle(Mutex<Option<std::process::Child>>);

#[tauri::command]
fn get_backend_port(state: tauri::State<BackendPort>) -> Option<u16> {
    if state.0 == 0 {
        None
    } else {
        Some(state.0)
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![get_backend_port])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
                // Dev: no sidecar. `paperwhirl` already runs in a
                // terminal on :8000. Stash a sentinel 0 so
                // get_backend_port returns None.
                app.manage(BackendPort(0));
                app.manage(ChildHandle(Mutex::new(None)));
            } else {
                let resource_dir = app
                    .path()
                    .resource_dir()
                    .expect("Stage 6 E2: failed to resolve resource_dir");
                let port = sidecar::pick_free_port()
                    .expect("Stage 6 E1: pick_free_port failed");
                let child = sidecar::spawn_backend(port, &resource_dir)
                    .expect("Stage 6 E2: failed to spawn bundled paperwhirl backend");
                app.manage(BackendPort(port));
                app.manage(ChildHandle(Mutex::new(Some(child))));

                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    match sidecar::wait_ready(port).await {
                        Ok(()) => {
                            // Stage 6 E3 follow-up (2026-05-25): navigate
                            // the main window from the bundled splash
                            // (tauri://localhost/...) to the backend URL
                            // (http://127.0.0.1:<port>/). The backend
                            // serves the same React build from inside
                            // the paperwhirl package — so the UI and
                            // API end up on the same origin and all the
                            // cross-origin pain (CORS, <a href download>
                            // blocked by WKWebView) goes away.
                            if let Some(main_window) = handle.get_webview_window("main") {
                                let backend_url = format!("http://127.0.0.1:{port}/");
                                if let Ok(parsed) = backend_url.parse() {
                                    let _ = main_window.navigate(parsed);
                                }
                            }
                            let _ = handle.emit("backend-ready", port);
                        }
                        Err(e) => {
                            let _ = handle.emit("backend-error", e);
                        }
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| match event {
            // Stage 6 E7 (2026-05-26): on macOS, closing the window
            // does NOT exit the app by default — it stays alive in the
            // Dock with no UI. That left orphan Python backends running
            // (observed 3 simultaneous backends during E7 tire-kick).
            // Force-quit on the main window's destroy event so the
            // ExitRequested handler below tears down the sidecar.
            tauri::RunEvent::WindowEvent {
                label,
                event: tauri::WindowEvent::Destroyed,
                ..
            } if label == "main" => {
                app_handle.exit(0);
            }
            tauri::RunEvent::ExitRequested { .. } => {
                if let Some(state) = app_handle.try_state::<ChildHandle>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            sidecar::shutdown(&mut child);
                        }
                    }
                }
            }
            _ => {}
        });
}
