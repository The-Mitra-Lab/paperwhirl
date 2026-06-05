import { useState, useCallback, useEffect, useRef } from "react";
import { Settings as SettingsIcon, MessageSquare } from "lucide-react";
import Header from "./components/Header";
import InputArea from "./components/InputArea";
import PacketView from "./components/PacketView";
import Settings, { type ConfigState } from "./components/Settings";
import DiscussPanel from "./components/DiscussPanel";
import FirstRunModal from "./components/FirstRunModal";
import ReadingListRail from "./components/ReadingListRail";
import PrintView from "./components/PrintView";
import type { ReviewPacket } from "./types";
import { confirmAction } from "./lib/dialogs";
import { apiFetch, apiBaseSync } from "./lib/api";

// True when the app is running inside the Tauri shell (Phase B+), false in
// the browser (Phase A). The shell hides the native title bar and overlays
// the macOS traffic-light buttons on the SPA — which means the user has
// nothing to grab to drag the window. We compensate with a fixed-position
// `data-tauri-drag-region` strip across the top. The Reading List rail and
// the Settings gear are marked as no-drag so they stay clickable underneath.
const isTauriEnv = (): boolean =>
  typeof window !== "undefined" &&
  ("__TAURI_INTERNALS__" in window || "__TAURI__" in window);

export default function App() {
  const [packet, setPacket] = useState<ReviewPacket | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  // Stage 6 E5: backend signals when <data>/papers/<slug>/ has a
  // local manuscript.pdf or uploaded.pdf. Lets the Download-manuscript
  // button show for no-identifier saved papers (sample_et_al-style).
  const [hasLocalManuscript, setHasLocalManuscript] = useState(false);
  // Stage 6 E7: which lists currently contain this paper. Rendered as
  // a discrete line under the closing discussion. Updated whenever
  // the paper packet is fetched (open / save).
  const [listMemberships, setListMemberships] = useState<
    Array<{ slug: string; name: string }>
  >([]);
  const [slug, setSlug] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // E8 paste-opens toast: "Already in your library" etc. — friendly
  // info, not an error. Auto-cleared after a few seconds so it
  // doesn't linger on the open packet.
  const [info, setInfo] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [isTauri] = useState<boolean>(isTauriEnv);
  const [pendingSubmit, setPendingSubmit] = useState<{
    file: File | null;
    identifier: string;
  } | null>(null);
  // Stage 5 E2: identifier to prefill into the InputArea when a
  // Discuss-panel pill is clicked. Lets the user see WHICH paper
  // is being extracted (the InputArea stays visible during loading
  // and shows the identifier as if it had been pasted).
  const [inputPrefill, setInputPrefill] = useState<string>("");
  const [discussOpen, setDiscussOpen] = useState(false);
  // Stage 5 E1 (post-tirekick): two surfaces, two threads. The mode
  // tracks which one is currently showing — independent of `slug` so
  // a user reading a paper can pop open the general (discover) thread
  // without losing the paper view. Routes both the rendered messages
  // bucket and the slug sent on /api/discuss.
  const [discussMode, setDiscussMode] = useState<"paper" | "general">("general");
  const [discussContext, setDiscussContext] = useState<string | null>(null);
  const [paperDiscussMessages, setPaperDiscussMessages] = useState<
    { role: "user" | "assistant"; content: string }[]
  >([]);
  // Stage 5 E1: the general (no-paper) Discuss thread. Kept as
  // parallel state so opening a paper doesn't evaporate it and
  // closing a paper doesn't evaporate the per-paper thread. The
  // panel reads whichever bucket matches the current slug.
  const [generalDiscussMessages, setGeneralDiscussMessages] = useState<
    { role: "user" | "assistant"; content: string }[]
  >([]);
  // Discuss panel width lives here (not in DiscussPanel) because the
  // main-content right margin has to track it. Persisted across reloads.
  const [discussWidth, setDiscussWidth] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem("pw_discuss_width") || "", 10);
    return Number.isFinite(saved) ? saved : 384;
  });
  useEffect(() => {
    localStorage.setItem("pw_discuss_width", String(discussWidth));
  }, [discussWidth]);

  // generating === true while the SSE stream is still in flight.
  // PacketView/FigureCard use this to decide "empty section -> still
  // thinking..." (during) vs just empty (after).
  const [generating, setGenerating] = useState(false);

  // E7: first-run + reading-list state.
  const [configFolder, setConfigFolder] = useState<string | null>(null);
  const [configChecked, setConfigChecked] = useState(false);
  const [railVisible, setRailVisible] = useState(true);
  const [railRefreshKey, setRailRefreshKey] = useState(0);

  // Detect config on mount. null folder => first-run modal.
  useEffect(() => {
    apiFetch("/api/config")
      .then((r) => r.json())
      .then((d) => setConfigFolder(d.folder))
      .catch(() => setConfigFolder(null))
      .finally(() => setConfigChecked(true));
  }, []);

  const handleConfigSubmit = useCallback(async (folder: string, apiKey: string) => {
    // POST sets the data folder (and runs the v1→v2 slug migration) — the
    // documented first-run path. The key is optional here; if the user
    // entered one, persist it via the same merge-update PUT the Settings
    // panel uses. Leaving it blank is fine — the generate flow still
    // 401-falls-back to opening Settings.
    const r = await apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: "Failed" }));
      throw new Error(body.detail || "Failed to set folder");
    }
    const data = await r.json();
    if (apiKey) {
      const kr = await apiFetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!kr.ok) {
        const body = await kr.json().catch(() => ({ detail: "Failed" }));
        throw new Error(body.detail || "Failed to save API key");
      }
    }
    setConfigFolder(data.folder);
  }, []);

  // Mutually-exclusive sidebar logic — opening any Discuss surface
  // hides the rail; closing it brings the rail back.
  //
  // Two-button design: title-bubble → per-paper thread; header icon
  // → general/discover thread. Clicking the active button closes the
  // panel; clicking the inactive button switches to its mode without
  // closing. This lets the user pop between threads mid-reading
  // without losing either's history.
  const openPaperDiscuss = useCallback(() => {
    setDiscussContext(null);
    if (discussOpen && discussMode === "paper") {
      setDiscussOpen(false);
      setRailVisible(true);
    } else {
      setDiscussMode("paper");
      setDiscussOpen(true);
      setRailVisible(false);
    }
  }, [discussOpen, discussMode]);

  const openGeneralDiscuss = useCallback(() => {
    setDiscussContext(null);
    if (discussOpen && discussMode === "general") {
      setDiscussOpen(false);
      setRailVisible(true);
    } else {
      setDiscussMode("general");
      setDiscussOpen(true);
      setRailVisible(false);
    }
  }, [discussOpen, discussMode]);

  // Track which slugs are persisted under <data>/papers/<slug>/.
  // After save (or rail load), the slug is marked "saved" and any
  // subsequent Discuss messages auto-PUT to the backend.
  const [savedSlugs, setSavedSlugs] = useState<Set<string>>(new Set());
  // Abort handle for the in-flight /api/generate/stream fetch — lets
  // openSavedPacket actually cancel a running generation instead of
  // just flipping a flag (otherwise the stream's setPacket calls keep
  // firing in the background, racing with the loaded saved packet).
  const generateAbortRef = useRef<AbortController | null>(null);

  // Load a saved packet from the rail. If a generation is in flight,
  // confirm before discarding — generation can take 2-3 minutes and
  // silently throwing it away is a footgun. The confirm routes through
  // the cross-shell dialog helper (Tauri native sheet in the shell,
  // window.confirm in the browser).
  const openSavedPacket = useCallback(async (paperSlug: string) => {
    if (generating) {
      const ok = await confirmAction(
        "Generation is still in progress — discard it and switch to this saved paper?",
        { title: "Discard in-progress generation?", kind: "warning" },
      );
      if (!ok) return;
      // User confirmed — abort the SSE stream so its event handlers
      // stop clobbering setPacket as the saved paper loads.
      generateAbortRef.current?.abort();
      generateAbortRef.current = null;
      setGenerating(false);
    }
    setError(null);
    try {
      const r = await apiFetch(`/api/papers/${paperSlug}`);
      if (!r.ok) throw new Error("Failed to load saved packet");
      const data = await r.json();
      setPacket(data.packet);
      setSlug(paperSlug);
      setBaseUrl(`${apiBaseSync()}${data.base_url || `/api/papers/${paperSlug}/assets`}`);
      setHasLocalManuscript(Boolean(data.has_local_manuscript));
      setListMemberships(Array.isArray(data.list_memberships) ? data.list_memberships : []);
      // Pull persisted discussion thread, if any.
      const dr = await apiFetch(`/api/papers/${paperSlug}/discussion`).catch(() => null);
      if (dr && dr.ok) {
        const dd = await dr.json();
        setPaperDiscussMessages(dd.messages || []);
      } else {
        setPaperDiscussMessages([]);
      }
      setSavedSlugs((s) => new Set(s).add(paperSlug));
      setDiscussOpen(false);
      setRailVisible(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  }, [generating]);

  // Auto-persist Discuss messages whenever they change. E8: persists
  // for any slug with a backing extraction — backend's
  // `_discussion_path` decides cache vs library. Removed the prior
  // `savedSlugs.has(slug)` guard which silently dropped Discuss
  // messages on unsaved (browse-mode) papers. Empty messages count
  // too — that's the "clear discussion" state.
  useEffect(() => {
    if (!slug) return;
    const ctrl = new AbortController();
    apiFetch(`/api/papers/${slug}/discussion`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: paperDiscussMessages }),
      signal: ctrl.signal,
    }).catch(() => {});
    return () => ctrl.abort();
  }, [paperDiscussMessages, slug]);

  // Stage 5 E1: the general (search) thread is intentionally
  // in-memory only — persists across paper switches within a session
  // but not across app restarts.
  // No hydrate, no auto-PUT, no on-disk file.

  const handleSaved = useCallback(async () => {
    setRailRefreshKey((k) => k + 1);
    if (slug) {
      setSavedSlugs((s) => new Set(s).add(slug));
      // Stage 6 E5: refresh server-derived flags now that the paper
      // exists at <data>/papers/<slug>/. Specifically picks up
      // has_local_manuscript so the Download-manuscript button can
      // show for no-identifier saved papers (uploaded.pdf persisted
      // by save_packet).
      try {
        const r = await apiFetch(`/api/papers/${slug}`);
        if (r.ok) {
          const data = await r.json();
          setHasLocalManuscript(Boolean(data.has_local_manuscript));
          setListMemberships(Array.isArray(data.list_memberships) ? data.list_memberships : []);
        }
      } catch {
        /* non-fatal — button state will refresh on next packet open */
      }
      // Push the current Discuss thread to the just-saved paper.
      if (paperDiscussMessages.length > 0) {
        try {
          await apiFetch(`/api/papers/${slug}/discussion`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ messages: paperDiscussMessages }),
          });
        } catch {
          /* non-fatal */
        }
      }
    }
  }, [slug, paperDiscussMessages]);

  const generate = useCallback(
    async (
      file: File | null,
      identifier: string,
      opts?: { forceSlug?: string; autoSaveOnDone?: boolean },
    ) => {
      // Abort any prior stream still running, then open a new one.
      generateAbortRef.current?.abort();
      const ctrl = new AbortController();
      generateAbortRef.current = ctrl;

      setLoading(true);
      setError(null);
      setGenerating(true);
      setPacket(null);
      setBaseUrl("");
      setHasLocalManuscript(false);
      setListMemberships([]);
      setSlug("");
      // Stage 5 E2 tire-kick fix (2026-05-18): clear the per-paper
      // Discuss buffer when a new generation starts. Without this,
      // pill-clicking from paper A to paper B leaves paper A's
      // conversation in `paperDiscussMessages` — the user sees A's
      // thread when they open Discuss on B, AND the auto-PUT effect
      // writes A's messages into B's discussion.yaml as soon as slug
      // changes. Safe to clear here because slug="" makes the
      // auto-PUT effect early-return until skeleton arrives.
      setPaperDiscussMessages([]);

      const forceSlug = opts?.forceSlug || "";
      // Stage 5 E7 S9 (2026-05-19): Re-generate is now reachable from
      // unsaved cache papers, not just saved ones. The done-handler
      // auto-save (overwrite=true) is correct only when the paper
      // was *already* in the library at the start of Re-generate —
      // otherwise it would silently move a cache-only paper into
      // the library without a list assignment, violating the
      // save⇔list invariant from Stage 4 E8. Caller decides via
      // `autoSaveOnDone`; defaults off.
      const autoSaveOnDone = opts?.autoSaveOnDone === true;

      try {
        const formData = new FormData();
        if (file) {
          formData.append("pdf", file);
        } else {
          formData.append("identifier", identifier);
        }
        if (forceSlug) {
          // E8 Re-generate path. Skips the paste-opens pre-check on
          // the server AND pins the cache subdir to forceSlug so a
          // post-`done` /save?overwrite=true cleanly replaces the
          // existing saved entry without slug drift.
          formData.append("force_slug", forceSlug);
        }

        const resp = await apiFetch("/api/generate/stream", {
          method: "POST",
          body: formData,
          signal: ctrl.signal,
        });

        if (resp.status === 401) {
          // No API key configured — open Settings, remember the
          // submit, and retry once a key is saved.
          setPendingSubmit({ file, identifier });
          setSettingsOpen(true);
          setLoading(false);
          setGenerating(false);
          return;
        }

        if (!resp.ok) {
          const body = await resp.json().catch(() => ({ detail: resp.statusText }));
          throw new Error(body.detail || "Generation failed");
        }

        const reader = resp.body?.getReader();
        const decoder = new TextDecoder();
        if (!reader) throw new Error("No response stream");

        let buffer = "";
        // Local mutable packet built incrementally as events arrive.
        let next: ReviewPacket | null = null;
        let resolvedSlug = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          // The user may have clicked a saved paper while this read
          // was suspended — bail before any setPacket calls fire and
          // race with the loaded packet.
          if (ctrl.signal.aborted) break;
          buffer += decoder.decode(value, { stream: true });
          let nlIdx;
          while ((nlIdx = buffer.indexOf("\n\n")) !== -1) {
            const chunk = buffer.slice(0, nlIdx).trim();
            buffer = buffer.slice(nlIdx + 2);
            if (!chunk.startsWith("data:")) continue;
            const payload = JSON.parse(chunk.slice(5).trim());

            if (payload.type === "already_saved") {
              // E8 paste-opens: the paper is already in the library
              // (by slug match OR by DOI match against a different
              // slug). Stream ends here — backend skipped extraction
              // entirely. Open the saved packet and surface a toast.
              setLoading(false);
              setInfo("Already in your library — opening saved version.");
              setTimeout(() => setInfo(null), 4000);
              await openSavedPacket(payload.slug);
              return;
            } else if (payload.type === "skeleton") {
              resolvedSlug = payload.slug;
              setSlug(payload.slug);
              setBaseUrl(`${apiBaseSync()}/api/assets/${payload.slug}`);
              // Stage 5 E2 tire-kick fix: hydrate per-paper Discuss
              // from disk if this slug has a prior discussion.yaml
              // (e.g., a cached re-extraction the user discussed
              // before). For brand-new papers the endpoint returns
              // `{messages: []}` and we keep the empty state cleared
              // in generate(). Fire-and-forget — the auto-PUT will
              // re-write whatever this loads.
              apiFetch(`/api/papers/${payload.slug}/discussion`)
                .then((r) => (r.ok ? r.json() : { messages: [] }))
                .then((d) => {
                  if (Array.isArray(d.messages) && d.messages.length > 0) {
                    setPaperDiscussMessages(d.messages);
                  }
                })
                .catch(() => {});
              // Build a scaffold packet so PacketView can render immediately.
              next = {
                schema_version: "paperwhirl.review_session.v2",
                session: { id: payload.slug, title: payload.paper?.title || "" },
                paper: payload.paper || {
                  id: payload.slug,
                  title: "",
                  authors: [],
                  first_author: "",
                  journal: "",
                  doi: null,
                },
                overview: { background: "", gap: "", claims: [] },
                figures: ((payload.figures || []) as Array<{
                  id: string;
                  order: number;
                  source_order: number;
                  label: string;
                  view: {
                    source_page: number | null;
                    asset: string | null;
                    asset_role: string;
                    original_caption: string;
                  };
                }>).map((f, idx) => ({
                  id: f.id,
                  order: f.order ?? idx + 1,
                  source_order: f.source_order ?? idx + 1,
                  label: f.label || `Figure ${idx + 1}`,
                  source_figure_id: f.id,
                  view: {
                    source_page: f.view?.source_page ?? null,
                    asset: f.view?.asset ?? null,
                    asset_role: f.view?.asset_role || "",
                    crop: null,
                    original_caption: f.view?.original_caption || "",
                    display_legend: "",
                  },
                  analysis: {
                    motivation: "",
                    question: "",
                    approach: "",
                    evidence: "",
                    interpretation: "",
                    linked_claims: [],
                  },
                })),
                tables: ((payload.tables || []) as Array<{
                  id: string;
                  order: number;
                  source_order: number;
                  label: string;
                  view: {
                    caption_text: string;
                    html: string;
                    asset?: string | null;
                    asset_role: string;
                  };
                }>).map((t, idx) => ({
                  id: t.id,
                  order: t.order ?? idx + 1,
                  source_order: t.source_order ?? 9999 + idx,
                  label: t.label || `Table ${idx + 1}`,
                  source_table_id: t.id,
                  view: {
                    caption_text: t.view?.caption_text || "",
                    html: t.view?.html || "",
                    asset: t.view?.asset ?? null,
                    asset_role: t.view?.asset_role || "",
                  },
                  analysis: {
                    interpretation: "",
                    linked_claims: [],
                  },
                })),
                discussion: { synthesis: "", takeaways: [], caveats: [], next_steps: [] },
              };
              setPacket(next);
              setLoading(false); // packet scaffold is up; no more spinner needed
            } else if (payload.type === "overview" && next) {
              const cur: ReviewPacket = next;
              next = { ...cur, overview: payload.content.overview };
              setPacket(next);
            } else if (payload.type === "figure" && next) {
              const cur: ReviewPacket = next;
              const i = payload.index ?? 0;
              const figs = cur.figures.slice();
              if (figs[i]) {
                figs[i] = {
                  ...figs[i],
                  view: { ...figs[i].view, ...payload.content.view },
                  analysis: { ...figs[i].analysis, ...payload.content.analysis },
                };
              }
              next = { ...cur, figures: figs };
              setPacket(next);
            } else if (payload.type === "figure_error" && next) {
              setError(`Figure ${payload.id} failed: ${payload.error}`);
            } else if (payload.type === "table" && next) {
              const cur: ReviewPacket = next;
              const i = payload.index ?? 0;
              const tbls = (cur.tables || []).slice();
              if (tbls[i]) {
                tbls[i] = {
                  ...tbls[i],
                  analysis: { ...tbls[i].analysis, ...payload.content.analysis },
                };
              }
              next = { ...cur, tables: tbls };
              setPacket(next);
            } else if (payload.type === "table_error" && next) {
              setError(`Table ${payload.id} failed: ${payload.error}`);
            } else if (payload.type === "discussion" && next) {
              const cur: ReviewPacket = next;
              next = { ...cur, discussion: payload.content };
              setPacket(next);
            } else if (payload.type === "done") {
              // E8: backend no longer auto-saves. The cache holds the
              // extraction; the paper enters the library only when
              // the user clicks Save-to-list. EXCEPT for Re-generate:
              // forceSlug means the user already confirmed they want
              // to overwrite the saved entry, so we POST /save with
              // overwrite=true and reopen the saved packet.
              if (payload.slug) resolvedSlug = payload.slug;
              if (forceSlug && payload.slug && autoSaveOnDone) {
                try {
                  const r = await apiFetch(
                    `/api/papers/${payload.slug}/save?overwrite=true`,
                    { method: "POST" },
                  );
                  if (!r.ok) {
                    const body = await r.json().catch(() => ({ detail: "save failed" }));
                    throw new Error(body.detail || "save failed");
                  }
                  setSavedSlugs((s) => new Set(s).add(payload.slug));
                  setRailRefreshKey((k) => k + 1);
                  await openSavedPacket(payload.slug);
                } catch (e) {
                  setError(e instanceof Error ? e.message : "Re-generate save failed");
                }
              }
            } else if (payload.type === "warning") {
              // Stage 5 E3 (2026-05-18): extraction-level warning
              // (e.g. "couldn't fetch figures from publisher"). Attach
              // to the packet's warnings array so PacketView can
              // render a persistent banner. Persistent (not a toast)
              // because the user may want to re-read it after the
              // packet finishes streaming.
              if (next) {
                const cur: ReviewPacket = next;
                next = {
                  ...cur,
                  warnings: [...(cur.warnings || []), payload.message],
                };
                setPacket(next);
              }
            } else if (payload.type === "error") {
              throw new Error(payload.error);
            }
          }
        }

        if (resolvedSlug) setSlug(resolvedSlug);
      } catch (err) {
        // Intentional abort (user switched to a saved paper) — silent.
        if (
          err instanceof DOMException && err.name === "AbortError" ||
          (err as Error | undefined)?.name === "AbortError"
        ) {
          // no-op
        } else {
          setError(err instanceof Error ? err.message : "Unknown error");
        }
      } finally {
        // Only clear flags if this is still the active controller.
        // If a newer generate() has already swapped the ref to its
        // own controller, our finally must NOT touch shared loading
        // state — that would clobber the newer call's setLoading(true)
        // / setGenerating(true) and the user would see no activity
        // (Stage 5 E2 tire-kick bug: clicking a second identifier
        // pill mid-extraction silently dropped the second click).
        if (generateAbortRef.current === ctrl) {
          generateAbortRef.current = null;
          setLoading(false);
          setGenerating(false);
        }
      }
    },
    []
  );

  const handleSubmit = useCallback(
    (file: File | null, identifier: string) => {
      generate(file, identifier);
    },
    [generate]
  );

  const handleSettingsSaved = useCallback(
    (cfg: ConfigState) => {
      // Stage 6 E7 follow-up (2026-05-27): if the data folder
      // actually changed, the in-memory library state (savedSlugs,
      // current packet, rail, list memberships, …) all derive from
      // the OLD folder. Resetting each piece by hand is error-prone
      // — easier to reload, which re-hydrates everything cleanly.
      // Same effect as the user's quit+restart workaround but
      // in-place. No unsaved state at this point (config was just
      // persisted; no in-flight generation can be running here).
      if (cfg.folder !== configFolder) {
        window.location.reload();
        return;
      }
      setConfigFolder(cfg.folder);
      // If a submit was blocked on a missing key, retry it now.
      if (pendingSubmit && cfg.has_api_key) {
        const p = pendingSubmit;
        setPendingSubmit(null);
        setSettingsOpen(false);
        generate(p.file, p.identifier);
      }
    },
    [generate, pendingSubmit, configFolder]
  );

  // E8 Re-generate: re-run extraction + generation against a saved
  // paper's identifier, pinning the cache slug to the existing
  // saved slug via the streaming endpoint's force_slug param. After
  // generation completes the SSE done handler auto-POSTs
  // /save?overwrite=true, which clears the discussion thread but
  // preserves display name + list memberships. Identifier preference
  // mirrors the resolution pipeline's priority: DOI > PMCID > arXiv
  // ID > bioRxiv DOI > PMID.
  const regeneratePaper = useCallback(
    async (paperSlug: string, paper: ReviewPacket["paper"]) => {
      const identifier =
        paper.doi ||
        paper.pmcid ||
        paper.arxiv_id ||
        paper.biorxiv_doi ||
        paper.pmid ||
        "";
      // Stage 5 E7 S9: capture save state at the start, before the
      // user confirms. The confirm copy + the post-done auto-save
      // both depend on whether this paper is already in the library.
      const wasSaved = savedSlugs.has(paperSlug);
      // Stage 6 E5: no-identifier saved papers can re-generate via
      // the local uploaded.pdf the backend persisted on save. Only
      // bail when there's no identifier AND no saved source either.
      if (!identifier && !wasSaved) {
        setError(
          "Cannot re-generate: this paper has no DOI, PMCID, or arXiv ID. " +
          "Re-generation needs an identifier to re-fetch the source.",
        );
        return;
      }
      const ok = await confirmAction(
        wasSaved
          ? "Re-generate this paper? The existing review packet and " +
            "discussion thread will be replaced. Display name and reading-list " +
            "memberships are preserved."
          : "Re-generate this paper? The current cache packet and " +
            "discussion thread will be replaced. The paper stays unsaved.",
        { title: "Re-generate paper?", kind: "warning" },
      );
      if (!ok) return;
      // Clear the discussion thread BEFORE starting regeneration so
      // the Discuss panel is empty during the stream — letting the user
      // start a fresh thread against the regenerating packet. Explicit
      // PUT (not the auto-PUT useEffect) because generate() clears the
      // slug, which would short-circuit the auto-PUT. Only meaningful
      // for saved papers (cache discussion is recreated on the fly).
      setPaperDiscussMessages([]);
      if (wasSaved) {
        try {
          await apiFetch(`/api/papers/${paperSlug}/discussion`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ messages: [] }),
          });
        } catch {
          /* non-fatal; the auto-PUT will retry as messages accumulate */
        }
      }
      await generate(null, identifier, {
        forceSlug: paperSlug,
        autoSaveOnDone: wasSaved,
      });
    },
    [generate, savedSlugs],
  );

  // Print-mode short-circuit: Playwright's page.pdf() flow loads
  // /?slug=<x>&print=<mode> and renders just the print document.
  const printParams = (() => {
    const p = new URLSearchParams(window.location.search);
    const slug = p.get("slug");
    const print = p.get("print");
    return slug && print ? { slug, print } : null;
  })();
  if (printParams) {
    return <PrintView slug={printParams.slug} mode={printParams.print} />;
  }

  // First-run modal blocks everything until a folder is configured.
  if (configChecked && !configFolder) {
    return (
      <FirstRunModal
        defaultPath="~/PaperWhirl"
        onSubmit={handleConfigSubmit}
      />
    );
  }

  return (
    <div className="min-h-screen bg-white flex">
      {isTauri && (
        <div
          data-tauri-drag-region
          className="fixed top-0 left-0 right-0 h-8 z-30"
          aria-hidden="true"
        />
      )}

      {settingsOpen && (
        <Settings
          onClose={() => setSettingsOpen(false)}
          onSaved={handleSettingsSaved}
        />
      )}

      <button
        onClick={() => setSettingsOpen(true)}
        className="fixed top-4 right-4 z-40 p-2 rounded-lg text-warm-400 hover:text-teal-650 hover:bg-warm-100 transition-colors cursor-pointer"
        title="Settings"
        aria-label="Settings"
        data-tauri-drag-region="false"
      >
        <SettingsIcon size={18} />
      </button>

      {/* Stage 6 E15: per-paper Discuss trigger, relocated from the
        * packet title into the fixed top-right chrome so it stays
        * pinned while the user scrolls the packet (it used to scroll
        * away with the title). Same look as the Search button below —
        * MessageSquare + a letter badge — differing only in "D" vs
        * "S". Behavior is unchanged (toggles the per-paper thread via
        * openPaperDiscuss). Only shown when a paper is open, since the
        * per-paper thread needs a loaded packet. */}
      {packet && (
        <button
          onClick={openPaperDiscuss}
          className="fixed top-4 right-24 z-40 p-2 rounded-lg text-warm-400 hover:text-teal-650 hover:bg-warm-100 transition-colors cursor-pointer"
          title="Discuss this paper"
          aria-label="Discuss this paper"
          data-tauri-drag-region="false"
        >
          <span className="relative inline-flex items-center justify-center">
            <MessageSquare size={18} />
            <span className="absolute text-[9px] font-bold leading-none pointer-events-none"
                  style={{ transform: "translateY(-1px)" }}>
              D
            </span>
          </span>
        </button>
      )}

      {/* Stage 5 E1: always-visible general/discover Discuss trigger.
        * Opens the general (no-paper) thread regardless of whether a
        * paper is loaded — so a reader can pop into discovery mid-
        * reading without losing the per-paper thread. The per-paper
        * thread has its own trigger on the packet title (bubble icon). */}
      <button
        onClick={openGeneralDiscuss}
        className="fixed top-4 right-14 z-40 p-2 rounded-lg text-warm-400 hover:text-teal-650 hover:bg-warm-100 transition-colors cursor-pointer"
        title="Search (general chat / paper discovery)"
        aria-label="Search"
        data-tauri-drag-region="false"
      >
        {/* MessageSquare with a small "S" inside it — the sibling of
          * the per-paper Discuss ("D") button above. Both share this
          * fixed-chrome look; the letter is the only difference:
          * S = Search/discover, D = Discuss this paper. */}
        <span className="relative inline-flex items-center justify-center">
          <MessageSquare size={18} />
          <span className="absolute text-[9px] font-bold leading-none pointer-events-none"
                style={{ transform: "translateY(-1px)" }}>
            S
          </span>
        </span>
      </button>


      {railVisible && (
        <ReadingListRail
          onOpenPacket={openSavedPacket}
          refreshKey={railRefreshKey}
          isTauri={isTauri}
          onPaperDeleted={(deletedSlug) => {
            // E8: rail just deleted a paper from the library. Drop it
            // from savedSlugs; if the deleted paper is the one
            // currently open, return to the landing view.
            setSavedSlugs((s) => {
              const next = new Set(s);
              next.delete(deletedSlug);
              return next;
            });
            if (slug === deletedSlug) {
              setPacket(null);
              setSlug("");
              setBaseUrl("");
              setHasLocalManuscript(false);
              setListMemberships([]);
              setDiscussOpen(false);
              setPaperDiscussMessages([]);
            }
          }}
        />
      )}

      <div
        className={`flex-1 mx-auto px-6 transition-all duration-300 relative ${
          !packet ? "max-w-3xl flex flex-col" : "max-w-3xl"
        }`}
        style={{
          marginRight: packet && discussOpen ? discussWidth : undefined,
        }}
      >
        <Header />

        {/* E8 paste-opens toast. Absolutely positioned inside the main
          * content column so it horizontally centers over the Header
          * title (which is also centered within this column), not the
          * viewport — viewport-center would sit to the left because
          * the rail occupies space on the left. */}
        {info && (
          <div
            data-tauri-drag-region="false"
            className="absolute top-12 left-1/2 -translate-x-1/2 z-50 pointer-events-none"
          >
            <p className="text-sm text-blue-700 bg-blue-50 border border-blue-100 rounded-lg px-4 py-2 shadow-sm whitespace-nowrap">
              {info}
            </p>
          </div>
        )}

        {/* InputArea stays visible whenever no packet is rendered,
          * including during extraction (loading=true). Its
          * internal "Generating..." indicator below the input
          * provides the in-progress cue, and the identifier text
          * in the input box tells the user *which* paper is being
          * extracted — important when extraction is triggered by
          * a Discuss-panel pill click rather than a manual paste. */}
        {!packet && (
          <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-full max-w-xl px-6">
            <InputArea
              onSubmit={handleSubmit}
              loading={loading}
              prefill={inputPrefill}
            />
            {/* Stage 5 E2 tire-kick fix (2026-05-18): when there is
              * no packet, the InputArea is viewport-centered — the
              * error block here keeps the error adjacent to where
              * the user is actually looking. Without this it ended
              * up at the top of the content column under the Header,
              * easy to miss. */}
            {error && (
              <p className="mt-3 text-sm text-red-600 bg-red-50 rounded-lg px-4 py-2">
                {error}
              </p>
            )}
          </div>
        )}

        {/* Packet-loaded layout: error sits at the top of the
          * content column, which is right under the rendered packet
          * title — natural reading position. */}
        {packet && error && (
          <div className="mt-4 max-w-2xl mx-auto">
            <p className="text-sm text-red-600 bg-red-50 rounded-lg px-4 py-2">
              {error}
            </p>
          </div>
        )}


        {packet && (() => {
          const overviewReady = Boolean(
            packet.overview?.background ||
            packet.overview?.gap ||
            packet.overview?.claims?.length
          );
          const resetReview = async () => {
            // Same discard-confirmation as openSavedPacket: if a
            // generation is mid-stream, ask before throwing it away.
            // Without this, the user could lose 2-3 min of generation
            // by accidentally clicking "New review".
            if (generating) {
              const ok = await confirmAction(
                "Generation is still in progress — discard it and start a new review?",
                { title: "Discard in-progress generation?", kind: "warning" },
              );
              if (!ok) return;
            }
            // Abort the in-flight SSE stream so its event handlers
            // stop firing setPacket/setSlug on the cleared state, and
            // reset the loading flag so the input area becomes
            // submittable immediately (otherwise it stays disabled
            // until the first skeleton chunk arrives, blocking the
            // user from entering a new paper).
            generateAbortRef.current?.abort();
            generateAbortRef.current = null;
            setLoading(false);
            setGenerating(false);
            setPacket(null);
            setBaseUrl("");
            setHasLocalManuscript(false);
            setListMemberships([]);
            setSlug("");
            setError(null);
            setDiscussOpen(false);
            setDiscussContext(null);
            setPaperDiscussMessages([]);
            // The rail is implicitly hidden whenever Discuss is open
            // (see openDiscuss). resetReview closes Discuss directly
            // without going through the Discuss onClose handler, so
            // it has to restore the rail itself — otherwise the rail
            // stays hidden with no UI affordance to bring it back.
            setRailVisible(true);
          };
          return (
            <>
              <div className="mt-4 mb-2">
                <button
                  onClick={resetReview}
                  className="text-xs text-warm-400 hover:text-teal-650 transition-colors cursor-pointer"
                >
                  &larr; New review
                </button>
              </div>

              {!overviewReady ? (
                <div className="mt-20 flex flex-col items-center text-warm-400">
                  <p className="text-sm italic">Thinking…</p>
                  <p className="text-xs mt-2 text-warm-300">
                    extracting paper and writing the overview
                  </p>
                </div>
              ) : (
                <PacketView
                  packet={packet}
                  baseUrl={baseUrl}
                  generating={generating}
                  paperSlug={slug}
                  onSaved={handleSaved}
                  isSaved={Boolean(slug && savedSlugs.has(slug))}
                  hasDiscussion={paperDiscussMessages.length > 0}
                  hasLocalManuscript={hasLocalManuscript}
                  listMemberships={listMemberships}
                  onRegenerate={regeneratePaper}
                />
              )}
            </>
          );
        })()}
      </div>

      {/* Stage 5 E1: panel renders whenever discussOpen, not just
        * when a paper is loaded. Slug-empty → general-chat thread
        * (interests-aware, no paper context); slug-truthy → per-paper
        * thread (unchanged from Stage 4). Two parallel message buckets
        * so switching between contexts doesn't evaporate either thread. */}
      <DiscussPanel
        slug={discussMode === "paper" ? slug : ""}
        contextSection={discussMode === "paper" ? discussContext : null}
        visible={discussOpen}
        messages={discussMode === "paper" ? paperDiscussMessages : generalDiscussMessages}
        onMessagesChange={discussMode === "paper" ? setPaperDiscussMessages : setGeneralDiscussMessages}
        onIdentifierClick={(id) => {
          // Stage 5 E2: user clicked an identifier rendered by
          // search_papers in the search thread. Keep the panel
          // open so the user can stay in the search context (read
          // suggestions, click other papers, ask follow-ups) while
          // the new paper extracts in the main view. Prefill the
          // InputArea so the user can SEE which paper is being
          // extracted — without this, the centered "Extracting…"
          // is contextless. generate() already aborts any prior
          // in-flight extraction via generateAbortRef, so a second
          // click cleanly interrupts and switches to the new
          // identifier (and the prefill updates to match).
          setInputPrefill(id);
          generate(null, id);
        }}
        width={discussWidth}
        onWidthChange={setDiscussWidth}
        paperTitle={discussMode === "paper" ? packet?.paper?.title : undefined}
        paperFirstAuthor={
          discussMode === "paper" ? packet?.paper?.first_author : undefined
        }
        paperYear={discussMode === "paper" ? packet?.paper?.year : undefined}
        paperDoi={discussMode === "paper" ? packet?.paper?.doi : undefined}
        onSwitchThread={discussMode === "paper" ? openGeneralDiscuss : openPaperDiscuss}
        paperAvailable={Boolean(packet)}
        onClose={() => {
          // Close Discuss restores the rail (mutually-exclusive pair).
          setDiscussOpen(false);
          setRailVisible(true);
        }}
      />
    </div>
  );
}
