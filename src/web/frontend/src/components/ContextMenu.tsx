import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface ContextMenuItem {
  label: string;
  onClick: () => void;
  danger?: boolean;
}

interface ContextMenuProps {
  x: number;
  y: number;
  items: ContextMenuItem[];
  onClose: () => void;
}

export default function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  // Stage 5 E7 S12 (2026-05-19): the menu used to anchor its top-left
  // at the click point and always open downward/rightward. For papers
  // near the bottom of a long reading-list rail the menu spilled below
  // the window. Measure after layout and flip the menu up / left when
  // it would overflow the viewport. Start at the click point so the
  // first paint isn't wildly off, then correct.
  const [pos, setPos] = useState({ left: x, top: y });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const margin = 8;
    let top = y;
    let left = x;
    if (y + rect.height > window.innerHeight - margin) {
      // Flip up: anchor the menu's bottom at the click point.
      top = Math.max(margin, y - rect.height);
    }
    if (x + rect.width > window.innerWidth - margin) {
      left = Math.max(margin, x - rect.width);
    }
    setPos({ left, top });
  }, [x, y]);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      style={{ left: pos.left, top: pos.top }}
      className="fixed z-50 min-w-[160px] bg-white border border-warm-200 rounded-lg shadow-lg py-1"
    >
      {items.map((it, i) => (
        <button
          key={i}
          onClick={() => {
            it.onClick();
            onClose();
          }}
          className={`block w-full text-left px-3 py-1.5 text-sm cursor-pointer transition-colors ${
            it.danger
              ? "text-red-600 hover:bg-red-50"
              : "text-warm-800 hover:bg-warm-100"
          }`}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}
