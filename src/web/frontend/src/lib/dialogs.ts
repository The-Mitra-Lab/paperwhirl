/**
 * Cross-shell dialog helpers.
 *
 * Tauri 2.x's macOS webview blocks `window.confirm` / `window.alert` /
 * `window.prompt` by default — the calls return falsy with no dialog
 * shown. In Tauri mode we route to `@tauri-apps/plugin-dialog`, which
 * shows a native macOS sheet. In the browser we keep `window.confirm`.
 *
 * Note: the API is async (Promise<boolean>) because the Tauri plugin
 * is async. Browser `window.confirm` is wrapped to fit the same
 * signature so callers don't branch.
 */

import {
  confirm as tauriConfirm,
  open as tauriOpen,
  message as tauriMessage,
} from "@tauri-apps/plugin-dialog";

export const isTauriShell = (): boolean =>
  typeof window !== "undefined" &&
  ("__TAURI_INTERNALS__" in window || "__TAURI__" in window);

/**
 * Show a confirmation dialog and return whether the user accepted.
 * Tauri → native sheet via plugin-dialog. Browser → window.confirm.
 */
export async function confirmAction(
  message: string,
  options: { title?: string; kind?: "info" | "warning" } = {},
): Promise<boolean> {
  if (isTauriShell()) {
    return tauriConfirm(message, {
      title: options.title ?? "PaperWhirl",
      kind: options.kind ?? "warning",
    });
  }
  return window.confirm(message);
}

/**
 * Show a single-button info/error dialog. Tauri → native sheet;
 * browser → window.alert. Used for "the action you triggered
 * failed and there's nothing to retry" surface messages.
 */
export async function notify(
  message: string,
  options: { title?: string; kind?: "info" | "warning" | "error" } = {},
): Promise<void> {
  if (isTauriShell()) {
    await tauriMessage(message, {
      title: options.title ?? "PaperWhirl",
      kind: options.kind ?? "info",
    });
    return;
  }
  window.alert(message);
}

/**
 * Open an external URL in the user's default browser.
 * Stage 6 E7 follow-up (2026-05-27): added for the
 * "open in PubMed Central" fallback when PMC reCAPTCHA blocks
 * an in-app manuscript download. URLs are whitelisted via the
 * Tauri capabilities (currently pmc.ncbi.nlm.nih.gov, ncbi.nlm.nih.gov,
 * europepmc.org). Falls back to window.open in a browser context.
 */
export async function openExternal(url: string): Promise<void> {
  if (isTauriShell()) {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

/**
 * Open the native macOS folder picker when running inside Tauri;
 * returns the picked path as a string, or null if the user cancelled.
 * In the browser, returns null immediately — callers should fall
 * back to a typed-path field.
 */
export async function pickFolder(
  options: { defaultPath?: string; title?: string } = {},
): Promise<string | null> {
  if (!isTauriShell()) return null;
  const picked = await tauriOpen({
    directory: true,
    multiple: false,
    defaultPath: options.defaultPath,
    title: options.title ?? "Choose folder",
  });
  // Tauri's open() returns string | string[] | null. We asked for
  // single-select so string | null is what we get in practice; the
  // array branch is defensive.
  if (picked == null) return null;
  return Array.isArray(picked) ? (picked[0] ?? null) : picked;
}
