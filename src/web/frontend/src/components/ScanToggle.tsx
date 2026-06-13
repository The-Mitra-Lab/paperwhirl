// Stage 7 E2: the prominent Scan / Deep Dive switch. Scan renders the
// abstract + figures + original legends with no LLM (instant, free);
// Deep Dive is the full generated summary. Default is Scan; the choice
// persists (see App.tsx scanMode). A plain segmented control — no new
// deps.
interface ScanToggleProps {
  scanMode: boolean;
  onChange: (next: boolean) => void;
  className?: string;
}

export default function ScanToggle({ scanMode, onChange, className = "" }: ScanToggleProps) {
  return (
    <div
      className={`inline-flex rounded-full border border-warm-300 bg-warm-50 p-0.5 text-xs font-medium ${className}`}
    >
      <button
        type="button"
        onClick={() => onChange(true)}
        aria-pressed={scanMode}
        title="Scan: abstract + figures + legends only — no AI summary, instant and free"
        className={`px-3 py-1 rounded-full transition-colors cursor-pointer ${
          scanMode ? "bg-teal-650 text-white" : "text-warm-600 hover:text-teal-650"
        }`}
      >
        Scan
      </button>
      <button
        type="button"
        onClick={() => onChange(false)}
        aria-pressed={!scanMode}
        title="Deep Dive: full AI-generated, claim-driven summary"
        className={`px-3 py-1 rounded-full transition-colors cursor-pointer ${
          !scanMode ? "bg-teal-650 text-white" : "text-warm-600 hover:text-teal-650"
        }`}
      >
        Deep Dive
      </button>
    </div>
  );
}
