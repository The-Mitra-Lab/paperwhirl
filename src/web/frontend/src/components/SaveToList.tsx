import { useEffect, useRef, useState } from "react";
import { Bookmark, Plus } from "lucide-react";
import type { ReadingListIndexEntry } from "../types";
import { apiFetch } from "../lib/api";

interface SaveToListProps {
  paperSlug: string;
  /** Called after a successful save so the rail can refresh. */
  onSaved?: () => void;
}

export default function SaveToList({ paperSlug, onSaved }: SaveToListProps) {
  const [lists, setLists] = useState<ReadingListIndexEntry[]>([]);
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [creatingNew, setCreatingNew] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  // Stage 6 E7 (2026-05-26): filter for the list selector. With 18+
  // lists the alphabetical menu was still a lot to scan.
  const [query, setQuery] = useState("");
  // Outer wrapper ref so the outside-click handler can tell when a
  // click landed outside both the trigger button and the dropdown.
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    apiFetch("/api/lists")
      .then((r) => r.json())
      .then((d) => setLists(d.lists || []))
      .catch(() => {});
    // Reset the search on each open so a stale filter doesn't hide
    // the lists the user was looking for.
    setQuery("");
  }, [open]);

  // Stage 6 E7: alphabetical (case-insensitive) + substring filter on
  // name. localeCompare with sensitivity:base treats AaÀ as equivalent.
  const visibleLists = lists
    .filter((l) =>
      query.trim() === ""
        ? true
        : l.name.toLowerCase().includes(query.trim().toLowerCase()),
    )
    .slice()
    .sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    );

  // Dropdown dismissibility: Escape and click-outside both close the
  // panel and clear any sticky status message. Without these the
  // dropdown can feel frozen — especially when the panel is showing
  // an error from a failed save (the wider /save 404 bug from
  // ISSUES.md, fixed properly in E7 Step 4).
  useEffect(() => {
    if (!open) return;
    const close = () => {
      setOpen(false);
      setStatus(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    const onClick = (e: MouseEvent) => {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(e.target as Node)) close();
    };
    window.addEventListener("keydown", onKey, true);
    // mousedown fires before click; closes the panel before any
    // accidental interaction with content underneath.
    document.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const ensureSaved = async () => {
    // Copy the active extraction into <data>/papers/<slug>/ if not already.
    const r = await apiFetch(`/api/papers/${paperSlug}/save`, { method: "POST" });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: "save failed" }));
      throw new Error(body.detail || "save failed");
    }
  };

  const addToList = async (listSlug: string) => {
    try {
      await ensureSaved();
      const r = await apiFetch(`/api/lists/${listSlug}/papers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paper_slug: paperSlug }),
      });
      if (!r.ok) throw new Error("add failed");
      const list = lists.find((l) => l.slug === listSlug);
      setStatus(`Saved to ${list?.name ?? listSlug}`);
      setOpen(false);
      onSaved?.();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Save failed");
    }
  };

  const createAndAdd = async () => {
    if (!newName.trim()) return;
    try {
      const r = await apiFetch("/api/lists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName.trim() }),
      });
      if (!r.ok) throw new Error("create failed");
      const created = await r.json();
      await addToList(created.slug);
      setNewName("");
      setCreatingNew(false);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Save failed");
    }
  };

  return (
    // Stage 6 E7 (2026-05-26): no top margin — PacketView lays this
    // out in a shared action-row flex container with DownloadButtons
    // and Re-generate, and supplies the spacing once.
    <div ref={wrapperRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-2 px-3 py-1.5 text-sm text-warm-700 hover:text-teal-650 hover:bg-warm-100 rounded-lg cursor-pointer transition-colors"
      >
        <Bookmark size={14} />
        Save to list
      </button>

      {status && (
        <p className="mt-1 text-xs text-warm-500 italic">{status}</p>
      )}

      {open && (
        // Open upward — this button sits at the bottom of the paper view,
        // so a default downward dropdown would extend past the viewport
        // and force the user to scroll.
        <div className="absolute z-20 bottom-full mb-2 w-64 bg-white border border-warm-200 rounded-lg shadow-lg p-2">
          <p className="text-xs text-warm-500 px-2 py-1">Add to…</p>
          {lists.length > 0 && (
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  setOpen(false);
                  setStatus(null);
                }
              }}
              autoFocus
              placeholder="Filter…"
              className="w-full mb-1 px-2 py-1 text-sm border border-warm-200 rounded focus:outline-none focus:border-teal-650"
            />
          )}
          <div className="space-y-0.5 max-h-60 overflow-y-auto">
            {visibleLists.map((list) => (
              <button
                key={list.slug}
                onClick={() => addToList(list.slug)}
                className="w-full text-left px-2 py-1.5 text-sm text-warm-800 hover:bg-warm-100 rounded cursor-pointer transition-colors"
              >
                {list.name}
              </button>
            ))}
            {lists.length === 0 && (
              <p className="px-2 py-1 text-xs italic text-warm-400">
                No reading lists yet
              </p>
            )}
            {lists.length > 0 && visibleLists.length === 0 && (
              <p className="px-2 py-1 text-xs italic text-warm-400">
                No lists match "{query}"
              </p>
            )}
          </div>
          <div className="mt-1 pt-1 border-t border-warm-100">
            {!creatingNew ? (
              <button
                onClick={() => setCreatingNew(true)}
                className="flex w-full items-center gap-2 px-2 py-1.5 text-sm text-warm-600 hover:bg-warm-100 rounded cursor-pointer transition-colors"
              >
                <Plus size={12} />
                New list…
              </button>
            ) : (
              <div className="flex gap-1 px-2 py-1">
                <input
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") createAndAdd();
                    if (e.key === "Escape") {
                      setCreatingNew(false);
                      setNewName("");
                    }
                  }}
                  onBlur={() => {
                    if (!newName.trim()) {
                      setCreatingNew(false);
                      setNewName("");
                    }
                  }}
                  autoFocus
                  placeholder="Name"
                  className="flex-1 px-2 py-1 text-sm border border-warm-200 rounded focus:outline-none focus:border-teal-650"
                />
                <button
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={createAndAdd}
                  className="px-2 py-1 text-xs bg-teal-650 text-white rounded hover:bg-teal-550"
                >
                  Add
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
