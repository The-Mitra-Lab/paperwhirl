// Stage 6 E1: in production builds the Python backend runs as a
// Tauri-spawned sidecar on a random local port. The frontend asks
// Rust for the port once at boot via the `get_backend_port` command,
// caches it, and uses it as the API base. In dev (Vite + external
// `paperwhirl` on :8000) apiBase is the empty string so fetch calls
// stay relative and go through Vite's /api proxy.

import { invoke } from "@tauri-apps/api/core";

let cachedBase: string | null = null;

function inTauri(): boolean {
  return (
    typeof window !== "undefined" &&
    ("__TAURI_INTERNALS__" in window || "__TAURI__" in window)
  );
}

async function resolveApiBase(): Promise<string> {
  if (cachedBase !== null) return cachedBase;
  if (import.meta.env.DEV || !inTauri()) {
    cachedBase = "";
    return cachedBase;
  }
  try {
    const port = await invoke<number | null>("get_backend_port");
    cachedBase = port && port > 0 ? `http://127.0.0.1:${port}` : "";
  } catch {
    cachedBase = "";
  }
  return cachedBase;
}

// Drop-in `fetch` replacement. Every callsite that used to do
// `fetch("/api/...")` becomes `apiFetch("/api/...")`. Same signature
// otherwise.
export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const base = await resolveApiBase();
  return fetch(`${base}${path}`, init);
}

// For callers that need the base string directly — e.g. <img src>
// URLs that don't go through fetch. Returns "" in dev and
// `http://127.0.0.1:<port>` in production.
export async function apiBase(): Promise<string> {
  return resolveApiBase();
}

// Synchronous variant for hot paths that have to build a URL string
// inline (e.g. baseUrl state for <img src>). Safe to call AFTER the
// splash has resolved at least one health check — by that point
// resolveApiBase() has populated the cache. Returns "" if called
// before the cache is warm, which yields relative URLs (correct for
// dev; degrades to a broken request in production, but in practice
// nothing in the main App tree renders before Splash unmounts).
export function apiBaseSync(): string {
  return cachedBase ?? "";
}
