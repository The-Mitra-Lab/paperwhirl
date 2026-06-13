// Stage 7 E3: full-screen image lightbox with true zoom + pan.
// Replaces FigureCard's fit-to-screen overlay. Open by clicking a
// figure; zoom with wheel / two-finger scroll / trackpad pinch (the
// browser delivers pinch as wheel+ctrlKey); pan by dragging when
// zoomed; double-click toggles 1 <-> 2.5x; close via backdrop, the
// close button, or Esc. The component unmounts when closed, so the
// transform resets fresh on each open.
import { useState, useRef, useEffect, useCallback } from "react";

interface ImageLightboxProps {
  src: string;
  alt?: string;
  onClose: () => void;
}

const MIN_SCALE = 1;
const MAX_SCALE = 8;
const clamp = (s: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));

export default function ImageLightbox({ src, alt, onClose }: ImageLightboxProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(1);
  const [tx, setTx] = useState(0);
  const [ty, setTy] = useState(0);
  const [dragging, setDragging] = useState(false);
  // Drag origin (pointer + translate at mousedown). A ref so the
  // mousemove handler reads the latest without re-binding.
  const dragRef = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);

  // Esc closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Native, non-passive wheel listener: React's synthetic onWheel is
  // passive, so preventDefault there can't reliably stop the gesture
  // from scrolling the page behind the overlay.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      // Pinch (ctrlKey) is coarser than scroll; scale the step by the
      // current zoom so it feels linear.
      const factor = e.ctrlKey ? 0.012 : 0.0018;
      setScale((s) => {
        const next = clamp(s - e.deltaY * factor * s);
        if (next <= MIN_SCALE) {
          setTx(0);
          setTy(0);
        }
        return next;
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (scale <= MIN_SCALE) return;
      e.preventDefault();
      dragRef.current = { x: e.clientX, y: e.clientY, tx, ty };
      setDragging(true);
    },
    [scale, tx, ty],
  );

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const d = dragRef.current;
    if (!d) return;
    setTx(d.tx + (e.clientX - d.x));
    setTy(d.ty + (e.clientY - d.y));
  }, []);

  const endDrag = useCallback(() => {
    dragRef.current = null;
    setDragging(false);
  }, []);

  const onDoubleClick = useCallback(() => {
    setScale((s) => {
      if (s > MIN_SCALE) {
        setTx(0);
        setTy(0);
        return MIN_SCALE;
      }
      return 2.5;
    });
  }, []);

  const zoomed = scale > MIN_SCALE;

  return (
    <div
      ref={containerRef}
      onClick={onClose}
      onMouseMove={onMouseMove}
      onMouseUp={endDrag}
      onMouseLeave={endDrag}
      className="fixed inset-0 z-[60] bg-black/85 flex items-center justify-center overflow-hidden select-none"
      data-tauri-drag-region="false"
    >
      <img
        src={src}
        alt={alt}
        onClick={(e) => e.stopPropagation()}
        onMouseDown={onMouseDown}
        onDoubleClick={onDoubleClick}
        draggable={false}
        style={{
          transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
          cursor: zoomed ? (dragging ? "grabbing" : "grab") : "zoom-in",
          transition: dragging ? "none" : "transform 75ms ease-out",
        }}
        className="max-w-[92vw] max-h-[92vh] object-contain rounded shadow-lg"
      />

      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        title="Close (Esc)"
        aria-label="Close"
        className="fixed top-4 right-4 z-[61] w-9 h-9 flex items-center justify-center rounded-full bg-white/10 text-white/80 hover:bg-white/20 hover:text-white cursor-pointer"
      >
        ✕
      </button>

      <p className="fixed bottom-4 left-1/2 -translate-x-1/2 text-xs text-white/50 pointer-events-none">
        scroll or pinch to zoom · drag to pan · double-click to reset · Esc to close
      </p>
    </div>
  );
}
