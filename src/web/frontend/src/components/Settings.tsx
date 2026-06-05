import { useEffect, useState } from "react";
import { isTauriShell, pickFolder } from "../lib/dialogs";
import { apiFetch } from "../lib/api";

export interface ConfigState {
  folder: string | null;
  has_api_key: boolean;
  api_key_masked: string | null;
  model: string;
  model_options: string[];
  generation_prompt: string;
  default_generation_prompt: string;
  discussion_prompt: string;
  default_discussion_prompt: string;
  interests: string;
}

interface SettingsProps {
  onClose: () => void;
  /** Called after a successful save with the fresh config. */
  onSaved?: (cfg: ConfigState) => void;
}

export default function Settings({ onClose, onSaved }: SettingsProps) {
  const [cfg, setCfg] = useState<ConfigState | null>(null);
  const [folder, setFolder] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState(""); // raw, only when the user edits it
  const [prompt, setPrompt] = useState("");
  const [discussionPrompt, setDiscussionPrompt] = useState("");
  const [interests, setInterests] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    apiFetch("/api/config")
      .then((r) => r.json())
      .then((d: ConfigState) => {
        setCfg(d);
        setFolder(d.folder || "");
        setModel(d.model);
        // Pre-fill with the default so it can be edited in place, not
        // just erased-and-retyped. An empty override means "use default".
        setPrompt(d.generation_prompt || d.default_generation_prompt);
        setDiscussionPrompt(
          d.discussion_prompt || d.default_discussion_prompt
        );
        setInterests(d.interests || "");
      })
      .catch(() => setStatus("Could not load settings"));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = async () => {
    setSaving(true);
    setStatus(null);
    // folder / model / prompt are always safe to re-send. The API key
    // is only included when the user actually typed one — we never
    // receive the raw key, so we must not echo the masked value back.
    const fields: Record<string, string> = {
      folder: folder.trim(),
      model,
      // If a prompt is unchanged from its default, store nothing —
      // keeps the config minimal and "Reset to default" genuinely resets.
      generation_prompt:
        prompt.trim() === (cfg?.default_generation_prompt || "").trim()
          ? ""
          : prompt,
      discussion_prompt:
        discussionPrompt.trim() ===
        (cfg?.default_discussion_prompt || "").trim()
          ? ""
          : discussionPrompt,
      interests: interests,
    };
    if (apiKey.trim()) fields.api_key = apiKey.trim();
    try {
      const r = await apiFetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({ detail: "save failed" }));
        throw new Error(b.detail || "save failed");
      }
      const fresh: ConfigState = await r.json();
      setCfg(fresh);
      setApiKey("");
      setPrompt(fresh.generation_prompt || fresh.default_generation_prompt);
      setDiscussionPrompt(
        fresh.discussion_prompt || fresh.default_discussion_prompt
      );
      setInterests(fresh.interests || "");
      setStatus("Saved");
      onSaved?.(fresh);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  if (!cfg) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
        <div className="bg-white rounded-xl shadow-lg p-6 w-full max-w-md mx-4">
          <p className="text-sm text-warm-400 italic">Loading settings…</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-lg p-6 w-full max-w-md mx-4 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-warm-800">Settings</h2>
          <button
            onClick={onClose}
            className="text-warm-400 hover:text-warm-800 cursor-pointer text-sm"
            aria-label="Close settings"
          >
            ✕
          </button>
        </div>

        <label className="block text-xs font-semibold text-warm-700 mb-1">
          OpenAI API Key
        </label>
        <p className="text-xs text-warm-400 mb-1">
          {cfg.has_api_key
            ? `Key set (${cfg.api_key_masked}).`
            : "No key set."}{" "}
          Stored in ~/.paperwhirl/config.json.
        </p>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={cfg.has_api_key ? "Enter a new key to replace" : "sk-..."}
          className="w-full rounded-lg border border-warm-200 px-3 py-2 text-sm mb-4 focus:border-teal-550 focus:outline-none"
        />

        <label className="block text-xs font-semibold text-warm-700 mb-1">
          Data folder
        </label>
        <p className="text-xs text-warm-400 mb-1">
          Where reading lists and saved papers live.
        </p>
        <div className="flex gap-2 mb-4">
          <input
            type="text"
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="~/PaperWhirl"
            className="flex-1 rounded-lg border border-warm-200 px-3 py-2 text-sm focus:border-teal-550 focus:outline-none"
          />
          {isTauriShell() && (
            <button
              type="button"
              onClick={async () => {
                const picked = await pickFolder({
                  defaultPath: folder || undefined,
                  title: "Choose your PaperWhirl folder",
                });
                if (picked) setFolder(picked);
              }}
              className="px-3 py-2 text-sm border border-warm-200 rounded-lg text-warm-700 hover:bg-warm-100 transition-colors cursor-pointer"
            >
              Choose…
            </button>
          )}
        </div>

        <label className="block text-xs font-semibold text-warm-700 mb-1">
          Model
        </label>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="w-full rounded-lg border border-warm-200 px-3 py-2 text-sm mb-4 focus:border-teal-550 focus:outline-none cursor-pointer"
        >
          {cfg.model_options.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
          {!cfg.model_options.includes(model) && (
            <option value={model}>{model}</option>
          )}
        </select>

        <button
          onClick={() => setAdvancedOpen((o) => !o)}
          className="text-xs font-semibold text-warm-600 hover:text-teal-650 cursor-pointer mb-2"
        >
          {advancedOpen ? "▾" : "▸"} Advanced
        </button>
        {advancedOpen && (
          <div className="mb-4">
            <label className="block text-xs font-semibold text-warm-700 mb-1">
              Research interests
            </label>
            <p className="text-xs text-warm-400 mb-1">
              Free-text description of what you're working on. Injected
              into the general Discuss chat (no paper loaded) so paper
              suggestions can be tailored. Not used when reading a
              specific paper — that context comes from the paper itself.
            </p>
            <textarea
              value={interests}
              onChange={(e) => setInterests(e.target.value)}
              rows={4}
              placeholder="e.g. single-cell multiomics in early-stage tumors; lineage tracing methods"
              className="w-full rounded-lg border border-warm-200 px-3 py-2 text-xs mb-4 focus:border-teal-550 focus:outline-none"
            />

            <label className="block text-xs font-semibold text-warm-700 mb-1">
              Review preamble
            </label>
            <p className="text-xs text-warm-400 mb-1">
              The shared voice and ground rules included in every
              section prompt (overview, figures, tables, discussion).
              Edit it to taste — a poor preamble silently degrades
              output quality. "Reset to default" restores the original.
            </p>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={6}
              className="w-full rounded-lg border border-warm-200 px-3 py-2 text-xs font-mono mb-1 focus:border-teal-550 focus:outline-none"
            />
            <button
              onClick={() => setPrompt(cfg.default_generation_prompt)}
              className="text-xs text-warm-400 hover:text-teal-650 cursor-pointer"
            >
              Reset to default
            </button>

            <label className="block text-xs font-semibold text-warm-700 mt-4 mb-1">
              Discussion prompt
            </label>
            <p className="text-xs text-warm-400 mb-1">
              The system prompt for the Discuss panel — voice,
              default answer length, formatting rules. The paper text
              and review packet are appended automatically. "Reset to
              default" restores the original.
            </p>
            <textarea
              value={discussionPrompt}
              onChange={(e) => setDiscussionPrompt(e.target.value)}
              rows={6}
              className="w-full rounded-lg border border-warm-200 px-3 py-2 text-xs font-mono mb-1 focus:border-teal-550 focus:outline-none"
            />
            <button
              onClick={() => setDiscussionPrompt(cfg.default_discussion_prompt)}
              className="text-xs text-warm-400 hover:text-teal-650 cursor-pointer"
            >
              Reset to default
            </button>
          </div>
        )}

        <div className="flex items-center gap-3 mt-2">
          <button
            onClick={save}
            disabled={saving}
            className="rounded-lg bg-teal-650 text-white px-4 py-2 text-sm font-medium hover:bg-teal-550 disabled:opacity-50 cursor-pointer transition-colors"
          >
            {saving ? "Saving…" : "Save"}
          </button>
          {status && (
            <span className="text-xs text-warm-500 italic">{status}</span>
          )}
        </div>
      </div>
    </div>
  );
}
