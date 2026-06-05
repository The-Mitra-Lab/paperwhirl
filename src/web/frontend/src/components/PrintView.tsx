import { useEffect, useState } from "react";
import type { ReviewPacket, Figure, Table } from "../types";
import { apiFetch, apiBaseSync } from "../lib/api";

interface PrintViewProps {
  slug: string;
  /** "full" or "full_with_discussion" */
  mode: string;
}

interface ChatMessage {
  role: string;
  content: string;
}

// Mirrors PacketView's byline format so the PDF matches the on-screen view.
function formatByline(paper: ReviewPacket["paper"]): string {
  const authors = paper.authors || [];
  const lastName = (n: string) => n.trim().split(/\s+/).pop() || n.trim();
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

function label(item: { label?: string; id: string }): string {
  return item.label?.replace(/\.$/, "") || item.id.replace(/_/g, " ");
}

// Print-only stylesheet. Scoped under .pw-print so it never touches the
// interactive app. The figures-at-end layout is what avoids the
// whitespace gaps the inline layout produced at page boundaries.
const PRINT_CSS = `
.pw-print {
  font-family: Georgia, "Times New Roman", serif;
  color: #1a1a1a;
  font-size: 11pt;
  line-height: 1.5;
  max-width: 7in;
  margin: 0 auto;
  padding: 0;
}
.pw-print h1.pw-title { font-size: 16pt; margin: 0 0 4px; }
.pw-print .pw-byline { font-style: italic; color: #444; margin: 0 0 16px; }
.pw-print h2 {
  font-size: 12.5pt;
  margin: 18px 0 6px;
  border-bottom: 1px solid #ccc;
  padding-bottom: 2px;
}
.pw-print p { margin: 0 0 8px; }
.pw-print ol, .pw-print ul { margin: 0 0 8px; padding-left: 22px; }
.pw-print li { margin-bottom: 4px; }
.pw-print .pw-caption { font-size: 10pt; color: #444; }
.pw-print .pw-table { overflow-x: hidden; margin: 0 0 8px; }
.pw-print .pw-table table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
.pw-print .pw-table td, .pw-print .pw-table th {
  border: 1px solid #bbb;
  padding: 3px 5px;
  text-align: left;
}
.pw-print .pw-figpage {
  break-before: page;
  break-inside: avoid;
  text-align: center;
  padding-top: 4px;
}
.pw-print .pw-figpage h2 { text-align: left; border: none; margin-top: 0; }
.pw-print .pw-figpage img {
  max-width: 100%;
  max-height: 8in;
  object-fit: contain;
}
.pw-print .pw-figpage .pw-caption { text-align: left; margin-top: 8px; }
.pw-print .pw-thread {
  break-before: page;
  padding-top: 4px;
}
.pw-print .pw-thread h2 {
  font-size: 24pt;
  font-weight: 700;
  text-align: left;
  border: none;
  margin-top: 0;
  margin-bottom: 24px;
}
.pw-print .pw-thread p { margin: 8px 0; }
`;

export default function PrintView({ slug, mode }: PrintViewProps) {
  const [packet, setPacket] = useState<ReviewPacket | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [thread, setThread] = useState<ChatMessage[]>([]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await apiFetch(`/api/papers/${slug}`);
        if (!r.ok) {
          setReady(true);
          return;
        }
        const data = await r.json();
        setPacket(data.packet);
        setBaseUrl(`${apiBaseSync()}${data.base_url || `/api/papers/${slug}/assets`}`);
        if (mode === "full_with_discussion") {
          const dr = await apiFetch(`/api/papers/${slug}/discussion`);
          if (dr.ok) {
            const dd = await dr.json();
            setThread(dd.messages || []);
          }
        }
        // Let images paint, then signal Playwright via data-print-ready.
        setTimeout(() => setReady(true), 800);
      } catch {
        setReady(true);
      }
    })();
  }, [slug, mode]);

  if (!packet) {
    return <div data-print-ready={ready ? "1" : "0"} />;
  }

  const { paper, overview, figures, discussion, session } = packet;
  // Packets extracted before E5 have no `tables` key — the type says
  // it is required, but the data predates it.
  const tables = packet.tables || [];
  const byline = formatByline(paper);

  // Body merges figures + tables by source_order so they read in the
  // paper's natural order. Figures contribute analysis prose only —
  // their images go on dedicated pages at the end.
  type Item = { kind: "figure"; data: Figure } | { kind: "table"; data: Table };
  const items: Item[] = [
    ...figures.map((f) => ({ kind: "figure" as const, data: f })),
    ...tables.map((t) => ({ kind: "table" as const, data: t })),
  ].sort(
    (a, b) => (a.data.source_order ?? 0) - (b.data.source_order ?? 0)
  );

  const figurePages = figures.filter((f) => f.view.asset);

  const hasClosing =
    discussion.synthesis ||
    discussion.takeaways?.length ||
    discussion.caveats?.length ||
    discussion.next_steps?.length;

  return (
    <div className="pw-print" data-print-ready={ready ? "1" : "0"}>
      <style>{PRINT_CSS}</style>

      <h1 className="pw-title">{paper.title || session.title}</h1>
      {byline && <p className="pw-byline">{byline}</p>}

      <h2>Overview</h2>
      {overview.background && (
        <p>
          <strong>Background.</strong> {overview.background}
        </p>
      )}
      {overview.gap && (
        <p>
          <strong>Gap.</strong> {overview.gap}
        </p>
      )}
      {overview.claims?.length > 0 && (
        <>
          <p>
            <strong>Main claims.</strong>
          </p>
          <ol>
            {overview.claims.map((c, i) => (
              <li key={c.id || i}>{c.statement}</li>
            ))}
          </ol>
        </>
      )}

      {items.map((item, idx) => {
        if (item.kind === "figure") {
          const a = item.data.analysis;
          return (
            <div key={`fig-body-${idx}`}>
              <h2>{label(item.data)}</h2>
              {a.motivation && (
                <p>
                  <strong>Motivation.</strong> {a.motivation}
                </p>
              )}
              {a.question && (
                <p>
                  <strong>Question.</strong> {a.question}
                </p>
              )}
              {a.approach && (
                <p>
                  <strong>Approach.</strong> {a.approach}
                </p>
              )}
              {a.evidence && (
                <p>
                  <strong>Evidence.</strong> {a.evidence}
                </p>
              )}
              {a.interpretation && (
                <p>
                  <strong>Interpretation.</strong> {a.interpretation}
                </p>
              )}
            </div>
          );
        }
        const t = item.data;
        return (
          <div key={`tbl-body-${idx}`}>
            <h2>{label(t)}</h2>
            {t.view.html && (
              <div
                className="pw-table"
                dangerouslySetInnerHTML={{ __html: t.view.html }}
              />
            )}
            {/* Stage 6 E9 (2026-05-26): caption below the table, matching
                TableCard. */}
            {t.view.caption_text && (
              <p className="pw-caption">{t.view.caption_text}</p>
            )}
            {t.analysis.interpretation && (
              <p>
                <strong>Interpretation.</strong> {t.analysis.interpretation}
              </p>
            )}
          </div>
        );
      })}

      {hasClosing && (
        <div>
          <h2>Closing Discussion</h2>
          {discussion.synthesis && <p>{discussion.synthesis}</p>}
          {discussion.takeaways?.length > 0 && (
            <>
              <p>
                <strong>Takeaways.</strong>
              </p>
              <ul>
                {discussion.takeaways.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            </>
          )}
          {discussion.caveats?.length > 0 && (
            <>
              <p>
                <strong>Caveats.</strong>
              </p>
              <ul>
                {discussion.caveats.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            </>
          )}
          {discussion.next_steps?.length > 0 && (
            <>
              <p>
                <strong>Next steps.</strong>
              </p>
              <ul>
                {discussion.next_steps.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {figurePages.map((f, i) => (
        <div key={`figpage-${i}`} className="pw-figpage">
          <h2>{label(f)}</h2>
          <img src={`${baseUrl}/${f.view.asset}`} alt={f.label} />
          {f.view.original_caption && (
            <p className="pw-caption">{f.view.original_caption}</p>
          )}
        </div>
      ))}

      {thread.length > 0 && (
        <div className="pw-thread">
          <h2>Discussion</h2>
          {thread.map((m, i) => (
            <p key={i}>
              <strong>{m.role === "user" ? "You" : "Assistant"}:</strong>{" "}
              {m.content}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
