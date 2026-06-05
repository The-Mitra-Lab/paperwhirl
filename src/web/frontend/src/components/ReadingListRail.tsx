import { useEffect, useRef, useState } from "react";
import { Plus, ChevronRight, FileText, Search, ArrowDownAZ, Clock } from "lucide-react";
import type { ReadingListIndexEntry, ReadingList } from "../types";
import ContextMenu, { type ContextMenuItem } from "./ContextMenu";
import { confirmAction } from "../lib/dialogs";
import { apiFetch } from "../lib/api";

interface ReadingListRailProps {
  onOpenPacket: (slug: string, display_name: string) => void;
  /** Bump to force a re-fetch of lists + the expanded lists. */
  refreshKey?: number;
  /** True when running inside the Tauri shell — the rail's top content
   *  shifts down to clear the 32 px window drag region. */
  isTauri?: boolean;
  /** E8: called after a paper is deleted from the library so the parent
   *  can clear it from savedSlugs and close it if it's the open packet. */
  onPaperDeleted?: (slug: string) => void;
}

type MenuTarget =
  | { kind: "list"; slug: string; name: string }
  | { kind: "paper"; listSlug: string; paperSlug: string; name: string };

type EditingTarget =
  | { kind: "list"; slug: string }
  | { kind: "paper"; slug: string };

/**
 * OpenAI-style left rail showing Reading Lists.
 * Several lists can be expanded at once; click a packet to load it.
 * The search box filters saved papers (and lists) by name.
 */
export default function ReadingListRail({ onOpenPacket, refreshKey = 0, isTauri = false, onPaperDeleted }: ReadingListRailProps) {
  const [lists, setLists] = useState<ReadingListIndexEntry[]>([]);
  // Multiple lists can be expanded at once.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Per-list data cache, keyed by list slug — feeds both the expanded
  // rows and the search filter.
  const [listData, setListData] = useState<Record<string, ReadingList>>({});
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [search, setSearch] = useState("");
  // Display names render in the rail. searchText is a per-paper
  // haystack (title + authors + overview: background / gap / claims)
  // that widens what the search box matches beyond the short
  // "Author Year" label.
  const [displayNames, setDisplayNames] = useState<Record<string, string>>({});
  const [searchText, setSearchText] = useState<Record<string, string>>({});
  const [menu, setMenu] = useState<{ x: number; y: number; target: MenuTarget } | null>(null);
  const [editing, setEditing] = useState<EditingTarget | null>(null);
  const [editValue, setEditValue] = useState("");
  // Set to true when the user presses Escape during an inline rename so
  // the input's onBlur=commitEdit doesn't fire a stale commit before the
  // unmount completes.
  const cancelEditRef = useRef(false);
  // Local nonce that triggers a re-fetch without bumping parent's refreshKey.
  const [localKey, setLocalKey] = useState(0);
  const bump = () => setLocalKey((k) => k + 1);

  // Resizable rail width, persisted across reloads.
  const [railWidth, setRailWidth] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem("pw_rail_width") || "", 10);
    return Number.isFinite(saved) ? saved : 256;
  });
  useEffect(() => {
    localStorage.setItem("pw_rail_width", String(railWidth));
  }, [railWidth]);

  // Sort order for papers within a list, persisted across reloads.
  // "alpha" = A→Z by the shown display_name; "recent" = most-recently
  // added first (reverse of _index.yaml insertion order).
  const [paperSort, setPaperSort] = useState<"alpha" | "recent">(() =>
    localStorage.getItem("pw_paper_sort") === "recent" ? "recent" : "alpha",
  );
  useEffect(() => {
    localStorage.setItem("pw_paper_sort", paperSort);
  }, [paperSort]);

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const onMove = (ev: MouseEvent) => {
      // The rail's left edge sits at x=0, so the cursor x is the width.
      setRailWidth(Math.min(Math.max(ev.clientX, 180), 480));
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const q = search.trim().toLowerCase();
  const searching = q.length > 0;
  // Stage 6 E7 (2026-05-26): render lists alphabetically (case-insensitive).
  // Server keeps _index.yaml in insertion order; the rail re-sorts.
  const sortedLists = lists
    .slice()
    .sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    );

  // Search matches the display name plus the paper's title, authors,
  // and overview (background, gap, and main-claim statements) — so a
  // word from any of those finds the paper even when its rail label
  // is the short "Author Year" form.
  const paperMatches = (slug: string) =>
    ((displayNames[slug] || slug) + " " + (searchText[slug] || ""))
      .toLowerCase()
      .includes(q);

  // Keep the current expanded set reachable from the refresh effect
  // without making it a dependency (which would re-fetch on every toggle).
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;

  const fetchListData = async (slug: string) => {
    try {
      const r = await apiFetch(`/api/lists/${slug}`);
      if (!r.ok) return;
      const data: ReadingList = await r.json();
      setListData((prev) => ({ ...prev, [slug]: data }));
      const unknown = (data.papers || []).filter(
        (p) => !displayNames[p.slug] || !searchText[p.slug]
      );
      if (unknown.length === 0) return;
      const nameUpdates: Record<string, string> = {};
      const searchUpdates: Record<string, string> = {};
      await Promise.all(
        unknown.map((p) =>
          apiFetch(`/api/papers/${p.slug}`)
            .then((res) => (res.ok ? res.json() : null))
            .then((d) => {
              if (d?.display_name) nameUpdates[p.slug] = d.display_name;
              const paper = d?.packet?.paper || {};
              const overview = d?.packet?.overview || {};
              const claimText = (overview.claims || [])
                .map((c: { statement?: string }) => c.statement || "")
                .filter(Boolean)
                .join("  ");
              const parts = [
                paper.title,
                (paper.authors || []).join(" "),
                overview.background,
                overview.gap,
                claimText,
              ].filter(Boolean);
              if (parts.length) searchUpdates[p.slug] = parts.join("  ");
            })
            .catch(() => {})
        )
      );
      if (Object.keys(nameUpdates).length > 0) {
        setDisplayNames((prev) => ({ ...prev, ...nameUpdates }));
      }
      if (Object.keys(searchUpdates).length > 0) {
        setSearchText((prev) => ({ ...prev, ...searchUpdates }));
      }
    } catch {
      /* non-fatal */
    }
  };

  useEffect(() => {
    apiFetch("/api/lists")
      .then((r) => r.json())
      .then((d) => setLists(d.lists || []))
      .catch(() => {});
  }, [refreshKey, localKey]);

  // Re-fetch every currently-expanded list when something changes.
  useEffect(() => {
    expandedRef.current.forEach((slug) => fetchListData(slug));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, localKey]);

  // While searching, make sure every list's data is loaded so the
  // filter can see all papers.
  useEffect(() => {
    if (!searching) return;
    lists.forEach((l) => {
      if (!listData[l.slug]) fetchListData(l.slug);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searching, lists]);

  const toggleExpand = (slug: string) => {
    const willExpand = !expanded.has(slug);
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
    // Always refetch on expand. The prior `!listData[slug]` guard
    // showed stale data when a paper was added to a *collapsed* list
    // (refreshKey only refreshes currently-expanded lists, so the
    // collapsed-then-expanded list rendered its cached copy without
    // the new addition).
    if (willExpand) fetchListData(slug);
  };

  const renameList = async (slug: string, name: string) => {
    if (!name.trim()) return;
    await apiFetch(`/api/lists/${slug}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    bump();
  };

  const deleteList = async (slug: string) => {
    // Stage 5 E4 (2026-05-18): fetch the orphan preview from the
    // backend before confirming, so the dialog can name how many
    // papers will be cascade-deleted. Local rail state can't be
    // trusted for this — only-expanded lists have their papers
    // loaded; an unexpanded list might contain orphan-candidates
    // we'd miscount as orphans. Backend computes against the
    // ground truth.
    let orphanCount = 0;
    try {
      const r = await apiFetch(`/api/lists/${slug}/orphan_preview`);
      if (r.ok) {
        const data = await r.json();
        orphanCount = data.count ?? 0;
      }
    } catch {
      // Network failure — fall back to a generic warning. Better
      // to confirm than to silently skip the action.
    }
    const message =
      orphanCount === 0
        ? "Delete this reading list?"
        : `Deleting this list will also delete ${orphanCount} paper${
            orphanCount === 1 ? "" : "s"
          } from your library (no other lists contain ${
            orphanCount === 1 ? "it" : "them"
          }). Move ${orphanCount === 1 ? "it" : "them"} to another ` +
          `list first if you want to keep ${
            orphanCount === 1 ? "it" : "them"
          }. Are you sure?`;
    if (!(await confirmAction(message, { kind: "warning" }))) return;
    await apiFetch(`/api/lists/${slug}`, { method: "DELETE" });
    setExpanded((prev) => {
      const next = new Set(prev);
      next.delete(slug);
      return next;
    });
    setListData((prev) => {
      const next = { ...prev };
      delete next[slug];
      return next;
    });
    bump();
  };

  // Count how many reading lists currently contain a given paper. Used
  // by the last-list-removal confirm to enforce the E8 "save ⇔ in ≥1
  // list" invariant: removing the only list a paper appears in would
  // orphan its papers/<slug>/ dir, so we ask whether to delete it
  // outright instead.
  const countListMemberships = async (paperSlug: string): Promise<number> => {
    let count = 0;
    for (const l of lists) {
      let data: ReadingList | undefined = listData[l.slug];
      if (!data) {
        try {
          const r = await apiFetch(`/api/lists/${l.slug}`);
          if (r.ok) data = await r.json();
        } catch {
          continue;
        }
      }
      if (data && (data.papers || []).some((p) => p.slug === paperSlug)) {
        count++;
      }
    }
    return count;
  };

  const removePaperFromList = async (listSlug: string, paperSlug: string) => {
    const memberships = await countListMemberships(paperSlug);
    if (memberships <= 1) {
      // E8 last-list confirm: with "save ⇔ in ≥1 list", removing the
      // only list would orphan the paper. Ask before deleting outright.
      const ok = await confirmAction(
        "This is the only reading list this paper is in. Delete the paper from your library?",
        { title: "Delete paper?", kind: "warning" },
      );
      if (!ok) return;
      await apiFetch(`/api/papers/${paperSlug}`, { method: "DELETE" });
      onPaperDeleted?.(paperSlug);
    } else {
      await apiFetch(`/api/lists/${listSlug}/papers/${paperSlug}`, { method: "DELETE" });
    }
    bump();
  };

  const deletePaperFromLibrary = async (paperSlug: string, name: string) => {
    const ok = await confirmAction(
      `Delete "${name}" from your library? This removes the paper from all reading lists and clears its discussion thread.`,
      { title: "Delete from library?", kind: "warning" },
    );
    if (!ok) return;
    await apiFetch(`/api/papers/${paperSlug}`, { method: "DELETE" });
    onPaperDeleted?.(paperSlug);
    bump();
  };

  const renamePaper = async (paperSlug: string, name: string) => {
    if (!name.trim()) return;
    await apiFetch(`/api/papers/${paperSlug}/display_name`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    setDisplayNames((prev) => ({ ...prev, [paperSlug]: name.trim() }));
  };

  const beginEdit = (target: EditingTarget, initial: string) => {
    setEditing(target);
    setEditValue(initial);
  };

  const commitEdit = async () => {
    if (cancelEditRef.current) {
      cancelEditRef.current = false;
      return;
    }
    if (!editing) return;
    if (editing.kind === "list") await renameList(editing.slug, editValue);
    else await renamePaper(editing.slug, editValue);
    setEditing(null);
    setEditValue("");
  };

  const cancelEdit = () => {
    cancelEditRef.current = true;
    setEditing(null);
    setEditValue("");
  };

  const menuItems = (): ContextMenuItem[] => {
    if (!menu) return [];
    if (menu.target.kind === "list") {
      const t = menu.target;
      return [
        { label: "Rename list", onClick: () => beginEdit({ kind: "list", slug: t.slug }, t.name) },
        { label: "Delete list", onClick: () => deleteList(t.slug), danger: true },
      ];
    } else {
      const t = menu.target;
      return [
        { label: "Rename paper", onClick: () => beginEdit({ kind: "paper", slug: t.paperSlug }, t.name) },
        { label: "Remove from this list", onClick: () => removePaperFromList(t.listSlug, t.paperSlug), danger: true },
        { label: "Delete from library", onClick: () => deletePaperFromLibrary(t.paperSlug, t.name), danger: true },
      ];
    }
  };

  const handleCreate = async () => {
    if (!newName.trim()) return;
    const r = await apiFetch("/api/lists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName.trim() }),
    });
    if (r.ok) {
      const created = await r.json();
      setLists((ls) => [
        ...ls,
        {
          slug: created.slug,
          name: created.name,
          description: created.description || "",
          updated_at: created.updated_at,
        },
      ]);
      setExpanded((prev) => new Set(prev).add(created.slug));
      fetchListData(created.slug);
    }
    setNewName("");
    setCreating(false);
  };

  return (
    <div
      data-tauri-drag-region="false"
      className="shrink-0 h-screen sticky top-0 relative"
      style={{ width: railWidth }}
    >
      <div className="h-full overflow-y-auto border-r border-warm-100 bg-warm-50/40">
      <div className={isTauri ? "p-3 pt-11" : "p-3"}>
        {!creating ? (
          <button
            onClick={() => setCreating(true)}
            className="flex w-full items-center gap-2 px-2 py-1.5 rounded-lg text-sm text-warm-700 hover:bg-warm-100 transition-colors cursor-pointer"
          >
            <Plus size={14} />
            New reading list
          </button>
        ) : (
          <div className="flex gap-1 mb-1">
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleCreate();
                if (e.key === "Escape") { setCreating(false); setNewName(""); }
              }}
              onBlur={() => {
                // If user clicks elsewhere with nothing typed, treat as
                // cancel. Otherwise leave open so the Add click works.
                if (!newName.trim()) {
                  setCreating(false);
                  setNewName("");
                }
              }}
              autoFocus
              placeholder="Name"
              className="flex-1 px-2 py-1 text-sm border border-warm-200 rounded focus:outline-none focus:border-teal-650"
            />
            <button
              onMouseDown={(e) => e.preventDefault()}  // keep input focus so onBlur doesn't cancel before click
              onClick={handleCreate}
              className="px-2 py-1 text-xs bg-teal-650 text-white rounded hover:bg-teal-550"
            >
              Add
            </button>
          </div>
        )}

        <div className="mt-2 relative">
          <Search
            size={13}
            className="absolute left-2 top-1/2 -translate-y-1/2 text-warm-400 pointer-events-none"
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search papers"
            className="w-full pl-7 pr-2 py-1.5 text-sm border border-warm-200 rounded-lg focus:outline-none focus:border-teal-650"
          />
        </div>

        <div className="mt-1.5 flex justify-end">
          <button
            onClick={() =>
              setPaperSort((s) => (s === "alpha" ? "recent" : "alpha"))
            }
            title={
              paperSort === "alpha"
                ? "Sorted A→Z by title — click for most-recent first"
                : "Most-recently added first — click for A→Z by title"
            }
            className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-warm-500 hover:bg-warm-100 hover:text-warm-700 transition-colors cursor-pointer"
          >
            {paperSort === "alpha" ? (
              <>
                <ArrowDownAZ size={13} />
                Title
              </>
            ) : (
              <>
                <Clock size={13} />
                Recent
              </>
            )}
          </button>
        </div>

        <div className="mt-2 space-y-0.5">
          {sortedLists.map((list) => {
            const data = listData[list.slug];

            // Decide which papers to show + whether the list shows at all.
            let papersToShow = data?.papers || [];
            let isOpen = expanded.has(list.slug);
            if (searching) {
              // Not loaded yet — it'll appear once its data arrives.
              if (!data) return null;
              const nameMatch = list.name.toLowerCase().includes(q);
              const matching = papersToShow.filter((p) => paperMatches(p.slug));
              if (!nameMatch && matching.length === 0) return null;
              papersToShow = nameMatch ? papersToShow : matching;
              isOpen = true;
            }

            // Order papers within a list per the rail's sort toggle.
            // "recent" = most-recently added first (reverse of the
            // server's _index.yaml insertion order). "alpha" = A→Z by the
            // name shown in the rail — display_name, which defaults to the
            // title and honors user renames, so you sort by what you
            // actually see. display_name loads asynchronously per paper;
            // names that haven't arrived yet sort to the end (￿ sentinel)
            // and slot into place once they load and React re-renders.
            papersToShow =
              paperSort === "recent"
                ? [...papersToShow].reverse()
                : [...papersToShow].sort((a, b) =>
                    (displayNames[a.slug] ?? "￿").localeCompare(
                      displayNames[b.slug] ?? "￿",
                      undefined,
                      { sensitivity: "base" },
                    ),
                  );

            const isEditingList = editing?.kind === "list" && editing.slug === list.slug;
            return (
              <div key={list.slug}>
                {isEditingList ? (
                  <input
                    type="text"
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitEdit();
                      if (e.key === "Escape") cancelEdit();
                    }}
                    onBlur={commitEdit}
                    autoFocus
                    className="w-full px-2 py-1.5 text-sm border border-warm-300 rounded focus:outline-none focus:border-teal-650"
                  />
                ) : (
                  <button
                    onClick={() => toggleExpand(list.slug)}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setMenu({
                        x: e.clientX,
                        y: e.clientY,
                        target: { kind: "list", slug: list.slug, name: list.name },
                      });
                    }}
                    className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-sm text-warm-800 hover:bg-warm-100 rounded transition-colors cursor-pointer"
                  >
                    <ChevronRight
                      size={14}
                      className={`text-warm-400 shrink-0 transition-transform duration-150 ${
                        isOpen ? "rotate-90" : ""
                      }`}
                    />
                    <span className="truncate">{list.name}</span>
                  </button>
                )}
                {isOpen && data && (
                  <div className="ml-4 mt-0.5 mb-1 space-y-0.5">
                    {papersToShow.length === 0 && (
                      <p className="px-2 py-1 text-xs italic text-warm-400">
                        No papers yet
                      </p>
                    )}
                    {papersToShow.map((p) => {
                      // displayNames[p.slug] arrives asynchronously
                      // (per-paper /api/papers fetch after the list
                      // data lands). Use a placeholder during the
                      // brief gap — flashing the internal slug
                      // (e.g. "pmc8525616") leaks implementation
                      // detail into the rail.
                      const loadedName = displayNames[p.slug];
                      const name = loadedName ?? "…";
                      const isEditingPaper =
                        editing?.kind === "paper" && editing.slug === p.slug;
                      if (isEditingPaper) {
                        return (
                          <input
                            key={p.slug}
                            type="text"
                            value={editValue}
                            onChange={(e) => setEditValue(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") commitEdit();
                              if (e.key === "Escape") {
                                setEditing(null);
                                setEditValue("");
                              }
                            }}
                            onBlur={commitEdit}
                            autoFocus
                            className="w-full px-2 py-1 text-xs border border-warm-300 rounded focus:outline-none focus:border-teal-650"
                          />
                        );
                      }
                      return (
                        <button
                          key={p.slug}
                          data-tauri-drag-region="false"
                          onClick={() => onOpenPacket(p.slug, name)}
                          onContextMenu={(e) => {
                            e.preventDefault();
                            setMenu({
                              x: e.clientX,
                              y: e.clientY,
                              target: {
                                kind: "paper",
                                listSlug: list.slug,
                                paperSlug: p.slug,
                                name,
                              },
                            });
                          }}
                          className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-xs text-warm-700 hover:bg-warm-100 rounded transition-colors cursor-pointer"
                        >
                          <FileText size={11} className="text-warm-400 shrink-0" />
                          <span className="truncate">{name}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
          {lists.length === 0 && !creating && (
            <p className="px-2 py-2 text-xs italic text-warm-400">
              No reading lists yet
            </p>
          )}
          {searching && lists.length > 0 &&
            lists.every((l) => {
              const data = listData[l.slug];
              if (!data) return false;
              const nameMatch = l.name.toLowerCase().includes(q);
              const hasPaper = (data.papers || []).some((p) =>
                paperMatches(p.slug)
              );
              return !nameMatch && !hasPaper;
            }) && (
              <p className="px-2 py-2 text-xs italic text-warm-400">
                No matches
              </p>
            )}
        </div>
      </div>
      </div>
      <div
        onMouseDown={startResize}
        title="Drag to resize"
        className="absolute top-0 right-0 w-1.5 h-full cursor-col-resize hover:bg-teal-650/40 transition-colors"
      />
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems()}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  );
}
