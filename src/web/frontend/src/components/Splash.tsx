import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";

// Stage 6 E1: shown until the backend's /api/health returns 200, or
// until a 30s deadline expires. In dev the backend is already up
// (Rob runs `paperwhirl` in a terminal) so this resolves in one or
// two poll cycles. In production the Tauri shell spawned the backend
// at app launch — splash bridges the cold start.
//
// Visual polish (logo, animation, branded copy) lands in Stage 6 E9
// onboarding. This component is intentionally plain so the
// production path can be tire-kicked without designed assets.

type Props = {
  onReady: () => void;
};

export default function Splash({ onReady }: Props) {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const deadline = Date.now() + 30_000;

    const poll = async () => {
      while (!cancelled && Date.now() < deadline) {
        try {
          const r = await apiFetch("/api/health");
          if (r.ok) {
            if (!cancelled) onReady();
            return;
          }
        } catch {
          // backend not up yet — keep polling
        }
        await new Promise((res) => setTimeout(res, 100));
      }
      if (!cancelled) {
        setError(
          "PaperWhirl's backend didn't start in time. Please quit the app and try again. If this keeps happening, the log at ~/Library/Logs/PaperWhirl/backend.log will help.",
        );
      }
    };
    poll();
    return () => {
      cancelled = true;
    };
  }, [onReady]);

  return (
    <div
      className="flex h-screen w-screen items-center justify-center bg-stone-50"
      data-tauri-drag-region
    >
      <div className="flex flex-col items-center gap-4 text-stone-600">
        <div className="text-2xl font-medium tracking-tight">PaperWhirl</div>
        {error ? (
          <div className="max-w-md text-center text-sm text-red-700">
            {error}
          </div>
        ) : (
          <>
            <div className="text-sm">Starting…</div>
            <div className="h-1 w-32 overflow-hidden rounded-full bg-stone-200">
              <div className="h-full w-1/3 animate-pulse rounded-full bg-stone-400" />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
