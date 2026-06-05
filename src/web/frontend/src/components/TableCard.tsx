import { useState } from "react";
import type { Table } from "../types";
import Collapsible from "./Collapsible";
import MathMarkdown from "./MathMarkdown";

const PROSE = "prose prose-sm max-w-none prose-p:text-warm-700 prose-p:leading-relaxed prose-p:my-0 prose-li:text-warm-700";

interface TableCardProps {
  table: Table;
  /** Stage 6 E7: needed for image-only tables (JCI-style). When
   * `view.asset` is set, the card renders an `<img>` at `${baseUrl}/${asset}`. */
  baseUrl: string;
  generating?: boolean;
}

export default function TableCard({ table, baseUrl, generating = false }: TableCardProps) {
  const { view, analysis } = table;
  const [zoomed, setZoomed] = useState(false);
  const cardEmpty =
    !view.caption_text && !analysis.interpretation && !view.html && !view.asset;
  const title = table.label?.replace(/\.$/, "") || table.id.replace(/_/g, " ");
  const linkedClaims =
    analysis.linked_claims.map((c) => c.replace(/_/g, " ")).join(", ") ||
    "none";
  const imgSrc = view.asset ? `${baseUrl}/${view.asset}` : "";

  return (
    <Collapsible title={title}>
      {cardEmpty && generating && (
        <p className="text-sm text-warm-400 italic">Thinking…</p>
      )}

      {view.html && (
        <div
          className="paperwhirl-table mb-4 overflow-x-auto text-sm text-warm-800"
          // The HTML comes from JATS / publisher pages we already trust to
          // emit valid <table> markup; the browser renders it directly.
          dangerouslySetInnerHTML={{ __html: view.html }}
        />
      )}

      {/* Stage 6 E7: image-only tables (JCI deposits tables as
          <graphic> to PMC). Same click-to-zoom pattern as FigureCard. */}
      {view.asset && !view.html && (
        <>
          <img
            src={imgSrc}
            alt={table.label}
            onClick={() => setZoomed(true)}
            className="w-full max-w-xl rounded mb-4 cursor-zoom-in"
          />
          {zoomed && (
            <div
              onClick={() => setZoomed(false)}
              className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center cursor-zoom-out p-8"
            >
              <img
                src={imgSrc}
                alt={table.label}
                className="max-w-full max-h-full object-contain rounded shadow-lg"
              />
            </div>
          )}
        </>
      )}

      {/* Stage 6 E9 (2026-05-26): caption goes BELOW the table per
          user preference; previous "Caption then table" order felt
          backwards for tables (it placed the description before the
          thing it described). Matches FigureCard's caption-below
          convention. */}
      {view.caption_text && (
        <div className="mb-4">
          <p className="text-sm font-bold text-warm-800 mb-1">Caption</p>
          <MathMarkdown block text={view.caption_text} className={`text-sm ${PROSE}`} />
        </div>
      )}

      {analysis.interpretation ? (
        <div className="mb-3">
          <p className="text-sm font-bold text-warm-800 mb-1">Interpretation</p>
          <MathMarkdown block text={analysis.interpretation} className={`text-sm ${PROSE}`} />
        </div>
      ) : (
        generating &&
        view.caption_text && (
          <p className="text-sm text-warm-400 italic mb-3">
            Thinking…
          </p>
        )
      )}

      {analysis.linked_claims.length > 0 && (
        <div className="mb-3">
          <p className="text-sm font-bold text-warm-800 mb-0.5">
            Linked claims
          </p>
          <p className="text-sm text-warm-700">{linkedClaims}</p>
        </div>
      )}
    </Collapsible>
  );
}
