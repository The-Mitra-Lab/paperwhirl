import { useState } from "react";
import type { Figure } from "../types";
import Collapsible from "./Collapsible";
import MathMarkdown from "./MathMarkdown";

// Shared prose styling for math-rendered packet fields: typography
// plugin classes + warm text color, paragraph margins collapsed so the
// rendered <p> sits flush like the plain text it replaces.
const PROSE = "prose prose-sm max-w-none prose-p:text-warm-700 prose-p:my-0 prose-li:text-warm-700";

interface FigureCardProps {
  figure: Figure;
  baseUrl: string;
  generating?: boolean;
}

export default function FigureCard({ figure, baseUrl, generating = false }: FigureCardProps) {
  const { view, analysis } = figure;
  const [zoomed, setZoomed] = useState(false);
  // Stage 5 E16 (2026-05-24): retry-on-error for the figure image.
  // Some freshly-generated cache assets get a 404 on the first browser
  // fetch (filesystem timing between file-write and the browser's GET)
  // and Safari/WKWebView caches that failure aggressively — refusing
  // to retry the same URL even after the file exists. The "?" the
  // user saw was the broken-image icon. Two recoveries:
  //   - automatic: on error, bump the cache-buster `gen` and decrement
  //     the auto-retry budget. Capped at 3 attempts so a truly missing
  //     asset doesn't loop.
  //   - manual: clicking the broken state RESETS the auto-retry
  //     budget so the user gets a fresh batch of 3 attempts — not
  //     just one. Stage 6 E11 follow-up (2026-05-28): the prior
  //     version only granted one more attempt on manual click; if
  //     the file was still missing at the moment of click, the
  //     image error handler immediately fell through to the broken
  //     branch and the user saw nothing change. Resetting the
  //     budget gives the filesystem-write race time to resolve.
  const [gen, setGen] = useState(0);
  const [retriesLeft, setRetriesLeft] = useState(3);
  const [broken, setBroken] = useState(false);
  const imgSrc = view.asset
    ? gen === 0
      ? `${baseUrl}/${view.asset}`
      : `${baseUrl}/${view.asset}?_=${gen}`
    : "";
  const handleImgError = () => {
    if (retriesLeft > 0) {
      setRetriesLeft((n) => n - 1);
      setGen((g) => g + 1);
    } else {
      setBroken(true);
    }
  };
  const handleRetry = () => {
    setBroken(false);
    setRetriesLeft(3);
    setGen((g) => g + 1);
  };
  const linkedClaims =
    analysis.linked_claims.map((c) => c.replace(/_/g, " ")).join(", ") ||
    "none";
  const analysisEmpty =
    !analysis.motivation && !analysis.evidence && !analysis.question &&
    !analysis.interpretation && !analysis.approach && !analysis.results;
  const cardEmpty = !view.original_caption && !view.display_legend && analysisEmpty;
  const title = figure.label?.replace(/\.$/, "") || figure.id.replace(/_/g, " ");

  return (
    <Collapsible title={title}>
      {cardEmpty && generating && (
        <p className="text-sm text-warm-400 italic">Thinking…</p>
      )}
      {view.asset && !broken && (
        <>
          <img
            src={imgSrc}
            alt={figure.label}
            onClick={() => setZoomed(true)}
            onError={handleImgError}
            className="w-full max-w-xl rounded mb-4 cursor-zoom-in"
          />
          {zoomed && (
            <div
              onClick={() => setZoomed(false)}
              className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center cursor-zoom-out p-8"
            >
              <img
                src={imgSrc}
                alt={figure.label}
                className="max-w-full max-h-full object-contain rounded shadow-lg"
              />
            </div>
          )}
        </>
      )}
      {view.asset && broken && (
        <button
          type="button"
          onClick={handleRetry}
          className="w-full max-w-xl rounded mb-4 border border-warm-300 bg-warm-50 hover:bg-warm-100 text-warm-600 text-sm py-8 cursor-pointer"
        >
          Figure didn't load — click to retry
        </button>
      )}

      {view.original_caption && (
        <div className="mb-4">
          <p className="text-sm font-bold text-warm-800 mb-1">
            Legend
          </p>
          <MathMarkdown
            block
            text={view.original_caption}
            className={`text-sm ${PROSE} prose-p:leading-relaxed`}
          />
        </div>
      )}

      {/*
        Two columns of analysis fields. The previous implementation
        used a single 6-cell CSS grid, which sized each row to its
        tallest cell — so when the right-column fields (Evidence,
        Interpretation, Linked claims) were longer than the left
        (Motivation, Question, Approach), the left got trailing
        whitespace to match. The fix is two independent flex columns
        wrapped in the same responsive grid: each column packs tight
        with its own gap; cross-column row alignment goes away.
        Mobile (grid-cols-1) flattens into the natural reading
        order: Motivation → Question → Approach → Results →
        Evidence → Interpretation → Linked claims.
      */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 text-sm">
        <div className="flex flex-col gap-4">
          <AnalysisField label="Motivation" text={analysis.motivation} />
          <AnalysisField label="Question" text={analysis.question} />
          <AnalysisField label="Approach" text={analysis.approach} />
          <AnalysisField label="Results" text={analysis.results || ""} />
        </div>
        <div className="flex flex-col gap-4">
          <AnalysisField label="Evidence" text={analysis.evidence} />
          <AnalysisField label="Interpretation" text={analysis.interpretation} />
          <div>
            <p className="text-sm font-bold text-warm-800 mb-0.5">
              Linked claims
            </p>
            <p className="text-warm-700">{linkedClaims}</p>
          </div>
        </div>
      </div>

    </Collapsible>
  );
}

function AnalysisField({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <div>
      <p className="text-sm font-bold text-warm-800 mb-0.5">
        {label}
      </p>
      <MathMarkdown block text={text} className={PROSE} />
    </div>
  );
}
