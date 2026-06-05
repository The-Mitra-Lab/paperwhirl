import { RefreshCw } from "lucide-react";
import type { ReviewPacket, Figure, Table } from "../types";
import Collapsible from "./Collapsible";
import FigureCard from "./FigureCard";
import TableCard from "./TableCard";
import SaveToList from "./SaveToList";
import DownloadButtons from "./DownloadButtons";
import MathMarkdown from "./MathMarkdown";

const PROSE = "prose prose-sm max-w-none prose-p:text-warm-700 prose-p:leading-relaxed prose-p:my-0 prose-li:text-warm-700";

interface PacketViewProps {
  packet: ReviewPacket;
  baseUrl: string;
  generating?: boolean;
  paperSlug?: string;
  onSaved?: () => void;
  /** When this paper has been persisted under <data>/papers/. */
  isSaved?: boolean;
  /** True when at least one Discuss message exists for this packet. */
  hasDiscussion?: boolean;
  /** Stage 6 E5: the paper has a local PDF on disk (manuscript.pdf
   * or uploaded.pdf). Enables the Download-manuscript button for
   * no-identifier saved papers — the backend can serve the local
   * file even when no DOI/PMCID/arXiv exists. */
  hasLocalManuscript?: boolean;
  /** Stage 6 E7: lists currently containing this paper, rendered as
   * a discrete line under the closing discussion. */
  listMemberships?: Array<{ slug: string; name: string }>;
  /** E8 Re-generate: replace the saved review packet with a fresh
   * generation. Confirms in the parent before firing. */
  onRegenerate?: (paperSlug: string, paper: ReviewPacket["paper"]) => void;
}

function StillThinking() {
  return (
    <p className="text-sm text-warm-400 italic">Thinking…</p>
  );
}

function formatByline(paper: ReviewPacket["paper"]): string {
  const authors = paper.authors || [];
  const lastName = (n: string) =>
    n.trim().split(/\s+/).pop() || n.trim();
  const segs: string[] = [];

  if (authors.length === 1) {
    segs.push(lastName(authors[0]));
  } else if (authors.length >= 2) {
    // No trailing period — the ". " segment join supplies it.
    segs.push(`${lastName(authors[0])} et al`);
    segs.push(`Senior Author: ${authors[authors.length - 1]}`);
  } else if (paper.first_author) {
    segs.push(paper.first_author);
  }

  const venue = [paper.journal, paper.year ? String(paper.year) : null]
    .filter(Boolean)
    .join(" ");
  if (venue) segs.push(venue);

  return segs.join(". ");
}

type Item =
  | { kind: "figure"; idx: number; source_order: number; data: Figure }
  | { kind: "table"; idx: number; source_order: number; data: Table };

export default function PacketView({
  packet, baseUrl, generating = false, paperSlug, onSaved,
  isSaved = false, hasDiscussion = false, hasLocalManuscript = false,
  listMemberships = [], onRegenerate,
}: PacketViewProps) {
  const { paper, overview, figures, discussion, session } = packet;
  const tables = packet.tables || [];
  const byline = formatByline(paper);
  // Merge figures + tables and sort by source_order so cards arrive in
  // the paper's natural reading order (Fig 1 -> Fig 2 -> Table 1 -> ...).
  const items: Item[] = [
    ...figures.map((f, idx) => ({
      kind: "figure" as const,
      idx,
      source_order: f.source_order ?? idx,
      data: f,
    })),
    ...tables.map((t, idx) => ({
      kind: "table" as const,
      idx,
      source_order: t.source_order ?? 9999 + idx,
      data: t,
    })),
  ].sort((a, b) => a.source_order - b.source_order);
  const overviewEmpty =
    !overview.background && !overview.gap && overview.claims.length === 0;
  const discussionEmpty =
    !discussion.synthesis &&
    discussion.takeaways.length === 0 &&
    discussion.caveats.length === 0 &&
    discussion.next_steps.length === 0;

  return (
    <div className="w-full max-w-3xl mx-auto mt-8 pb-16">
      <div className="flex items-start justify-between mb-6">
        <div className="flex-1">
          <h2 className="text-lg font-semibold text-warm-900 leading-snug">
            {session.title}
          </h2>
          {byline && (
            <p className="text-xs text-warm-500 mt-1 italic">
              {byline}
            </p>
          )}
        </div>
      </div>

      {/* Stage 5 E3 (2026-05-18): extraction-level warnings (e.g.
        * "figures couldn't be rendered: your network may not have
        * access — try VPN"). Persistent banner so the user can see
        * it even after the streamed text has filled in. Yellow to
        * distinguish from the red error block in App.tsx (errors
        * mean nothing extracted; warnings mean partial-but-usable). */}
      {packet.warnings && packet.warnings.length > 0 && (
        <div className="mb-4 space-y-2">
          {packet.warnings.map((w, i) => (
            <p
              key={i}
              className="text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2"
            >
              {w}
            </p>
          ))}
        </div>
      )}

      <Collapsible title="Overview">
        {overviewEmpty && generating && <StillThinking />}
        <div className="space-y-3">
          {overview.background && (
            <div>
              <p className="text-sm font-bold text-warm-800 mb-0.5">
                Background
              </p>
              <MathMarkdown block text={overview.background} className={`text-sm ${PROSE}`} />
            </div>
          )}

          {overview.gap && (
            <div>
              <p className="text-sm font-bold text-warm-800 mb-0.5">Gap</p>
              <MathMarkdown block text={overview.gap} className={`text-sm ${PROSE}`} />
            </div>
          )}

          {overview.approach && (
            <div>
              <p className="text-sm font-bold text-warm-800 mb-0.5">
                What the authors did
              </p>
              <MathMarkdown block text={overview.approach} className={`text-sm ${PROSE}`} />
            </div>
          )}

          {overview.claims.length > 0 && (
            <div>
              <p className="text-sm font-bold text-warm-800 mb-1">Claims</p>
              <ol className="space-y-2">
                {overview.claims.map((claim, i) => (
                  <li key={claim.id || i} className="text-sm text-warm-700">
                    <span className="font-medium text-warm-800">
                      Claim {i + 1}:
                    </span>{" "}
                    <MathMarkdown text={claim.statement} />
                    {claim.notes && (
                      <span className="text-warm-800 ml-1">
                        <MathMarkdown text={claim.notes} />
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      </Collapsible>

      {/* Don't show figure / table cards or closing discussion until at
          least the overview has rendered — otherwise the page is just a
          wall of "Thinking..." which is noise. */}
      {!overviewEmpty && items.map((it) =>
        it.kind === "figure" ? (
          <FigureCard
            key={`fig-${it.idx}-${it.data.id}`}
            figure={it.data}
            baseUrl={baseUrl}
            generating={generating}
          />
        ) : (
          <TableCard
            key={`tbl-${it.idx}-${it.data.id}`}
            table={it.data}
            baseUrl={baseUrl}
            generating={generating}
          />
        )
      )}

      {!overviewEmpty && (
      <Collapsible title="Closing Discussion">
        {discussionEmpty && generating && <StillThinking />}
        {discussion.synthesis && (
          <div className="mb-3">
            <MathMarkdown block text={discussion.synthesis} className={`text-sm ${PROSE}`} />
          </div>
        )}

        {discussion.takeaways.length > 0 && (
          <div className="mb-3">
            <p className="text-sm font-bold text-warm-800 mb-1">
              Takeaways
            </p>
            <ul className="list-disc pl-4 text-sm text-warm-700 space-y-1">
              {discussion.takeaways.map((t, i) => (
                <li key={i}><MathMarkdown text={t} /></li>
              ))}
            </ul>
          </div>
        )}

        {discussion.caveats.length > 0 && (
          <div className="mb-3">
            <p className="text-sm font-bold text-warm-800 mb-1">Caveats</p>
            <ul className="list-disc pl-4 text-sm text-warm-700 space-y-1">
              {discussion.caveats.map((c, i) => (
                <li key={i}><MathMarkdown text={c} /></li>
              ))}
            </ul>
          </div>
        )}

        {discussion.next_steps.length > 0 && (
          <div>
            <p className="text-sm font-bold text-warm-800 mb-1">
              Next Steps
            </p>
            <ul className="list-disc pl-4 text-sm text-warm-700 space-y-1">
              {discussion.next_steps.map((s, i) => (
                <li key={i}><MathMarkdown text={s} /></li>
              ))}
            </ul>
          </div>
        )}
      </Collapsible>
      )}

      {/* Stage 6 E7 (2026-05-26): unified action row — Download
          manuscript / Download summary [+ discussion] / Save to list /
          Re-generate all share the same flex-wrap row under the
          closing discussion. Single border, single mt offset.
          E8: dropped the isSaved gate so browse-mode papers (cache
          only) can also download. */}
      {!overviewEmpty && !generating && paperSlug && (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-warm-100 pt-3">
          <DownloadButtons
            paperSlug={paperSlug}
            paper={paper}
            hasDiscussion={hasDiscussion}
            hasLocalManuscript={hasLocalManuscript}
          />
          <SaveToList paperSlug={paperSlug} onSaved={onSaved} />
          {/* Stage 5 E7 S9: show Re-generate for cache-only papers too,
              not just saved ones. The original gate (`isSaved`) reflected
              E8's auto-save-on-done path; now App.tsx gates that auto-
              save on `wasSaved` so unsaved papers can re-extract without
              getting silently added to the library. The button label /
              confirm copy adapts to whether list memberships will be
              preserved. */}
          {onRegenerate && (
            <button
              onClick={() => onRegenerate(paperSlug, paper)}
              className="inline-flex items-center gap-2 px-3 py-1.5 text-sm text-warm-700 hover:text-teal-650 hover:bg-warm-100 rounded-lg cursor-pointer transition-colors"
              title={
                isSaved
                  ? "Re-run extraction and generation against this paper. The current packet and discussion thread will be replaced; display name and list memberships stay."
                  : "Re-run extraction and generation against this paper. The current cache entry will be replaced; the paper stays unsaved."
              }
            >
              <RefreshCw size={14} />
              Re-generate
            </button>
          )}
        </div>
      )}
      {!overviewEmpty && !generating && paperSlug && listMemberships.length > 0 && (
        // Stage 6 E7 (2026-05-26): discrete list-membership line under
        // the action row. Same left edge as the bars and the buttons.
        <p className="mt-3 text-xs text-warm-400">
          <span className="font-medium text-warm-500">In lists: </span>
          {listMemberships.map((l) => l.name).join(" · ")}
        </p>
      )}
    </div>
  );
}
