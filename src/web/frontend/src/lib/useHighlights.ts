// Stage 7 E4: manual select-to-highlight over summary prose.
//
// Anchoring is section-scoped: each highlight stores the
// `data-hl-key` of its Collapsible section plus a char offset+length
// within that section's textContent. Section content is stable while
// the section is open, so this survives collapsing/expanding *other*
// sections (Collapsible unmounts its children, so whole-page offsets
// would not).
//
// Rendering uses the CSS Custom Highlight API (CSS.highlights +
// Highlight(range) + ::highlight(pw-highlight)), which paints ranges
// without mutating the DOM — so it never fights react-markdown and
// re-applies cleanly on re-render. Feature-detected; a no-op where
// unsupported.
import { useCallback, useEffect, useRef } from "react";
import { apiFetch } from "./api";

interface Highlight {
  key: string;
  offset: number;
  len: number;
  quote: string;
}

const HL_NAME = "pw-highlight";

// The CSS Custom Highlight API isn't in the TS DOM lib yet; access it
// through loose casts.
const cssAny = (typeof CSS !== "undefined" ? (CSS as unknown as { highlights?: Map<string, unknown> }) : undefined);
const HighlightCtor = (typeof window !== "undefined"
  ? (window as unknown as { Highlight?: new (...ranges: Range[]) => unknown }).Highlight
  : undefined);
const SUPPORTED = Boolean(cssAny?.highlights && HighlightCtor);

export function useHighlights(
  slug: string,
  containerRef: React.RefObject<HTMLElement | null>,
  opts: { enabled: boolean; reloadKey: number },
) {
  const { enabled, reloadKey } = opts;
  const itemsRef = useRef<Highlight[]>([]);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const slugRef = useRef(slug);
  slugRef.current = slug;

  const sectionOf = (node: Node | null): HTMLElement | null => {
    const el = node instanceof HTMLElement ? node : node?.parentElement ?? null;
    return el ? (el.closest("[data-hl-key]") as HTMLElement | null) : null;
  };

  // char offset of (container, off) measured from the start of `section`.
  const offsetIn = (section: HTMLElement, container: Node, off: number): number => {
    const r = document.createRange();
    r.selectNodeContents(section);
    try {
      r.setEnd(container, off);
    } catch {
      return 0;
    }
    return r.toString().length;
  };

  // map [offset, offset+len] within section.textContent -> a DOM Range
  const rangeFromAnchor = (section: HTMLElement, offset: number, len: number): Range | null => {
    const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT);
    let acc = 0;
    let startNode: Node | null = null;
    let startOff = 0;
    let endNode: Node | null = null;
    let endOff = 0;
    let n: Node | null;
    while ((n = walker.nextNode())) {
      const tlen = (n.textContent || "").length;
      const next = acc + tlen;
      if (startNode === null && offset <= next) {
        startNode = n;
        startOff = offset - acc;
      }
      if (startNode !== null && offset + len <= next) {
        endNode = n;
        endOff = offset + len - acc;
        break;
      }
      acc = next;
    }
    if (!startNode || !endNode) return null;
    const r = document.createRange();
    try {
      r.setStart(startNode, startOff);
      r.setEnd(endNode, endOff);
    } catch {
      return null;
    }
    return r;
  };

  const apply = useCallback(() => {
    if (!SUPPORTED || !containerRef.current) return;
    const ranges: Range[] = [];
    for (const h of itemsRef.current) {
      const section = containerRef.current.querySelector(
        `[data-hl-key="${CSS.escape(h.key)}"]`,
      ) as HTMLElement | null;
      if (!section) continue;
      const r = rangeFromAnchor(section, h.offset, h.len);
      if (r) ranges.push(r);
    }
    // Rebuild the registry from scratch every apply. WKWebView does not
    // reliably repaint when the last highlight is removed via an in-place
    // delete / empty-set (the "can't erase the last one" bug) — a full
    // clear() + re-add forces a clean re-evaluation each time.
    cssAny!.highlights!.clear();
    if (ranges.length > 0) {
      cssAny!.highlights!.set(HL_NAME, new HighlightCtor!(...ranges));
    }
  }, [containerRef]);

  const save = useCallback(() => {
    const s = slugRef.current;
    if (!s) return;
    apiFetch(`/api/papers/${s}/highlights`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ highlights: itemsRef.current }),
    }).catch(() => {});
  }, []);

  // Load (and re-load when the paper or generation changes).
  useEffect(() => {
    if (!SUPPORTED) return;
    if (!slug) {
      itemsRef.current = [];
      apply();
      return;
    }
    let cancelled = false;
    apiFetch(`/api/papers/${slug}/highlights`)
      .then((r) => (r.ok ? r.json() : { highlights: [] }))
      .then((d) => {
        if (cancelled) return;
        itemsRef.current = Array.isArray(d.highlights) ? d.highlights : [];
        apply();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [slug, reloadKey, apply]);

  // Capture (mouseup) + re-apply on DOM mutations (collapse/expand).
  useEffect(() => {
    if (!SUPPORTED) return;
    const el = containerRef.current;
    if (!el) return;

    const onMouseUp = () => {
      if (!enabledRef.current) return;
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);
      const section = sectionOf(range.startContainer);
      if (!section || !el.contains(section)) return;
      const key = section.getAttribute("data-hl-key");
      if (!key) return;

      if (sel.isCollapsed) {
        // A click — remove a highlight if the caret sits inside one.
        const at = offsetIn(section, range.startContainer, range.startOffset);
        const idx = itemsRef.current.findIndex(
          (h) => h.key === key && at >= h.offset && at <= h.offset + h.len,
        );
        if (idx >= 0) {
          itemsRef.current = itemsRef.current.filter((_, i) => i !== idx);
          apply();
          save();
        }
        return;
      }

      // Non-empty selection: only highlight within a single section.
      if (sectionOf(range.endContainer) !== section) {
        return;
      }
      const quote = sel.toString();
      const len = quote.length;
      if (len === 0) return;
      const offset = offsetIn(section, range.startContainer, range.startOffset);
      // Skip exact duplicates.
      const dup = itemsRef.current.some(
        (h) => h.key === key && h.offset === offset && h.len === len,
      );
      if (!dup) {
        itemsRef.current = [...itemsRef.current, { key, offset, len, quote }];
        apply();
        save();
      }
      sel.removeAllRanges();
    };

    el.addEventListener("mouseup", onMouseUp);
    const mo = new MutationObserver(() => apply());
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    return () => {
      el.removeEventListener("mouseup", onMouseUp);
      mo.disconnect();
    };
  }, [containerRef, apply, save]);

  // Tear down the registry when the component using the hook unmounts.
  useEffect(() => {
    return () => {
      if (SUPPORTED) cssAny!.highlights!.delete(HL_NAME);
    };
  }, []);
}
