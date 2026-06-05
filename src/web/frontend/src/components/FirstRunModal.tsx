import { useState } from "react";
import { isTauriShell, pickFolder } from "../lib/dialogs";

interface FirstRunModalProps {
  defaultPath: string;
  onSubmit: (folder: string, apiKey: string) => Promise<void>;
}

/**
 * First-run modal: appears when GET /api/config returns folder=null.
 * Asks the user where to keep reading lists; suggests a sensible default
 * (~/PaperWhirl) and accepts a typed path. In the Tauri shell, a
 * "Choose…" button opens the native macOS folder picker; in the
 * browser, the typed field stays primary.
 *
 * Also collects the OpenAI API key here so first-run is the one place a
 * new user is asked for it. The key field is optional — leaving it blank
 * is fine; it can be added later in Settings, and the generate flow
 * still 401-falls-back to opening Settings if no key is set by then.
 */
export default function FirstRunModal({ defaultPath, onSubmit }: FirstRunModalProps) {
  const [path, setPath] = useState(defaultPath);
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tauri = isTauriShell();

  const handlePick = async () => {
    const picked = await pickFolder({
      defaultPath: path || undefined,
      title: "Choose your PaperWhirl folder",
    });
    if (picked) setPath(picked);
  };

  const handleSubmit = async () => {
    if (!path.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(path.trim(), apiKey.trim());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to set folder");
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-white rounded-xl shadow-xl max-w-lg w-full p-6">
        <h2 className="text-lg font-semibold text-warm-900 mb-2">
          Choose a folder for your reading lists
        </h2>
        <p className="text-sm text-warm-600 mb-4 leading-relaxed">
          PaperWhirl saves reading lists and review packets under a folder
          you choose. Point it at a Dropbox / iCloud / Drive folder if you
          want the same lists on multiple machines.
        </p>
        <div className="flex gap-2 mb-2">
          <input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            className="flex-1 px-3 py-2 text-sm border border-warm-200 rounded-lg focus:outline-none focus:border-teal-650"
            placeholder="/Users/you/Dropbox/PaperWhirl"
            autoFocus
          />
          {tauri && (
            <button
              type="button"
              onClick={handlePick}
              className="px-3 py-2 text-sm border border-warm-200 rounded-lg text-warm-700 hover:bg-warm-100 transition-colors cursor-pointer"
            >
              Choose…
            </button>
          )}
        </div>
        <p className="text-xs text-warm-400 mb-4">
          The folder will be created if it doesn't exist.
        </p>

        <h2 className="text-lg font-semibold text-warm-900 mb-2">
          OpenAI API key{" "}
          <span className="text-sm font-normal text-warm-400">(optional)</span>
        </h2>
        <p className="text-sm text-warm-600 mb-2 leading-relaxed">
          PaperWhirl uses your own OpenAI key to write summaries. It is stored
          locally on this Mac and never leaves it. You can also add or change it
          later in Settings.
        </p>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
          className="w-full px-3 py-2 text-sm border border-warm-200 rounded-lg focus:outline-none focus:border-teal-650 mb-4"
          placeholder="sk-…"
          autoComplete="off"
        />

        {error && (
          <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2 mb-3">
            {error}
          </p>
        )}
        <button
          onClick={handleSubmit}
          disabled={submitting || !path.trim()}
          className="w-full px-4 py-2 bg-teal-650 text-white rounded-lg text-sm font-medium hover:bg-teal-550 disabled:bg-warm-200 disabled:cursor-not-allowed transition-colors"
        >
          {submitting ? "Setting up…" : "Continue"}
        </button>
      </div>
    </div>
  );
}
