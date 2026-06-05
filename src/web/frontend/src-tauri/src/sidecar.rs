// Stage 6 E1: production-only sidecar that owns the Python backend.
// Picks a free local port, spawns `paperwhirl` with PAPERWHIRL_PORT
// set, pipes stdout+stderr to ~/Library/Logs/PaperWhirl/backend.log
// (rotated 3-deep), and polls /api/health until it answers. The
// shutdown path runs on app quit. Dev builds compile this file but
// never call into it — see lib.rs `cfg!(not(debug_assertions))`.

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

pub fn pick_free_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    Ok(port)
}

fn log_dir() -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    let dir = home.join("Library").join("Logs").join("PaperWhirl");
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

fn rotate(dir: &PathBuf) {
    // Keep the 3 most recent launches.
    // backend.log -> backend.1.log -> backend.2.log -> (dropped)
    let cur = dir.join("backend.log");
    let l1 = dir.join("backend.1.log");
    let l2 = dir.join("backend.2.log");
    let _ = std::fs::remove_file(&l2);
    let _ = std::fs::rename(&l1, &l2);
    let _ = std::fs::rename(&cur, &l1);
}

pub fn spawn_backend(port: u16, resource_dir: &PathBuf) -> std::io::Result<Child> {
    let dir = log_dir().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::Other,
            "could not resolve ~/Library/Logs/PaperWhirl",
        )
    })?;
    rotate(&dir);

    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("backend.log"))?;
    let log_err = log.try_clone()?;

    // Stage 6 E2: spawn the bundled `paperwhirl` shell wrapper.
    // resource_dir is `<app>/Contents/Resources/`, populated by
    // bundle_python.sh + the tauri.conf.json bundle.resources map.
    // The wrapper resolves its sibling python3.12 at runtime via
    // $0, so the .app works wherever the user drags it.
    let bin = resource_dir.join("python").join("bin").join("paperwhirl");
    let browsers = resource_dir.join("browsers");

    // Stage 6 E4: prepend the bundled Poppler bin/ to PATH so the
    // Python code's `subprocess.run(["pdftotext", ...])` calls
    // resolve to the bundled binary instead of failing with
    // "command not found".
    let poppler_bin = resource_dir.join("poppler").join("bin");
    let path = match std::env::var("PATH") {
        Ok(existing) => format!("{}:{}", poppler_bin.display(), existing),
        Err(_) => poppler_bin.display().to_string(),
    };

    Command::new(&bin)
        .env("PAPERWHIRL_HOST", "127.0.0.1")
        .env("PAPERWHIRL_PORT", port.to_string())
        // Stage 6 E3: point Playwright at the bundled Chromium so
        // extraction works without `playwright install` ever being
        // run on the user's machine. SKIP_BROWSER_DOWNLOAD prevents
        // Playwright from trying to "fix" a missing install at
        // runtime by downloading.
        .env("PLAYWRIGHT_BROWSERS_PATH", &browsers)
        .env("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
        .env("PATH", &path)
        // Stage 6 E9 follow-on: tell the Python sidecar it's
        // owned by Tauri. The Python side gates a parent-death
        // watchdog on this — when the shell exits / is force-
        // quit (Activity Monitor, multi-launch, etc.) and the
        // sidecar gets reparented to launchd, the watchdog
        // exits cleanly instead of leaving an orphan process
        // listening on its port. WindowEvent::Destroyed in
        // lib.rs catches the normal-quit case but not crashes
        // or force-quits; this is the belt-and-suspenders.
        .env("PAPERWHIRL_MANAGED_BY_TAURI", "1")
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err))
        .spawn()
}

pub async fn wait_ready(port: u16) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(1))
        .build()
        .map_err(|e| e.to_string())?;
    let url = format!("http://127.0.0.1:{port}/api/health");
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while std::time::Instant::now() < deadline {
        if let Ok(resp) = client.get(&url).send().await {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err(format!(
        "backend did not respond on http://127.0.0.1:{port}/api/health within 30s"
    ))
}

pub fn shutdown(child: &mut Child) {
    // The Python backend has no in-flight state to flush (config
    // writes are synchronous), so SIGKILL via std::process::Child
    // is enough. If a future feature adds work that needs a
    // graceful shutdown, swap this for SIGTERM + 2s wait + SIGKILL.
    let _ = child.kill();
    let _ = child.wait();
}
