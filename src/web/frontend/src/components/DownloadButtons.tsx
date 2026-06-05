import { Download } from "lucide-react";
import type { Paper } from "../types";
import { confirmAction, notify, openExternal } from "../lib/dialogs";
import { apiFetch, apiBaseSync } from "../lib/api";

/**
 * Stage 5 E2 tire-kick fix (2026-05-18): manuscript download via
 * JS fetch instead of `<a download>` so we can catch non-PDF
 * responses (e.g., 404 with JSON body for papers whose publisher
 * PDF can't be fetched) BEFORE the browser saves a `.json` file
 * masquerading as a PDF. The summary-PDF buttons stay as plain
 * `<a download>` — those are produced by our own renderer and
 * essentially always succeed; if they ever 404, fixing the failure
 * mode is a more involved change.
 */
async function downloadVerified(
  url: string,
  fallbackFilename: string,
  pmcid: string | null = null,
): Promise<void> {
  let resp: Response;
  try {
    resp = await apiFetch(url);
  } catch (err) {
    await notify(
      `Couldn't reach the server: ${err instanceof Error ? err.message : "unknown error"}`,
      { kind: "error" },
    );
    return;
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch { /* response wasn't JSON */ }

    // Stage 6 E7 follow-up (2026-05-27): the PMC reCAPTCHA case
    // surfaces as "POW cookie did not appear" in the error
    // detail. Playwright can't solve Google reCAPTCHA, but a
    // real browser session can — offer to open PMC in the
    // user's default browser so they can click through the
    // challenge and download manually. E8 Step 3's always-save
    // uploaded.pdf means dragging the result back into
    // PaperWhirl preserves the manuscript.
    if (pmcid && /POW cookie did not appear/i.test(detail)) {
      const pmcUrl = `https://pmc.ncbi.nlm.nih.gov/articles/${pmcid}/pdf/`;
      const ok = await confirmAction(
        `PMC is gating this paper with a reCAPTCHA challenge that PaperWhirl can't solve automatically. ` +
        `Open ${pmcUrl} in your default browser? You can click through the challenge there, download the PDF, then drag it into PaperWhirl.`,
        { title: "Open PubMed Central?", kind: "info" },
      );
      if (ok) {
        await openExternal(pmcUrl);
      }
      return;
    }

    await notify(`Couldn't download manuscript PDF.\n\n${detail}`, { kind: "error" });
    return;
  }
  const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
  if (!ct.startsWith("application/pdf")) {
    await notify(
      `Expected a PDF but the server returned ${ct || "no content-type"}. ` +
        "Not saving — this would have been a corrupt file.",
      { kind: "error" },
    );
    return;
  }
  // Prefer the filename the server suggested via Content-Disposition.
  let filename = fallbackFilename;
  const cd = resp.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="([^"]+)"/);
  if (m) filename = m[1];
  const blob = await resp.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Give the browser a beat to start the download before revoking.
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
}

interface DownloadButtonsProps {
  paperSlug: string;
  paper: Paper & {
    pmcid?: string | null;
    biorxiv_doi?: string | null;
    arxiv_id?: string | null;
  };
  hasDiscussion: boolean;
  /** Stage 6 E5: backend reports a local manuscript.pdf / uploaded.pdf
   * on disk for this paper. Show the button even when the paper has
   * no identifier (sample_et_al-style PDF drops). */
  hasLocalManuscript?: boolean;
}

/**
 * Whether the backend is likely to be able to serve a manuscript
 * PDF for this paper. Three signals, any of which is sufficient:
 * (a) the paper has an identifier in a publisher family the backend
 * knows how to fetch; (b) the paper has a known preprint identifier
 * (PMCID, bioRxiv DOI, arXiv ID); (c) Stage 6 E5: the backend has
 * a local manuscript.pdf / uploaded.pdf on disk. We can't know for
 * sure without trying (a paywall on the running network can block
 * even when the identifier is present), so this is a "show the
 * button" gate, not a guarantee — the button hits a same-origin
 * URL that 404s with a clean message when the fetch fails, which
 * the browser surfaces in the download dialog.
 */
function hasManuscriptCandidate(
  paper: DownloadButtonsProps["paper"],
  hasLocalManuscript: boolean,
): boolean {
  if (hasLocalManuscript) return true;
  if (paper.pmcid || paper.biorxiv_doi || paper.arxiv_id) return true;
  const doi = paper.doi || "";
  // Mirror the backend's `fetch_publisher_pdf_bytes` dispatch
  // (resolve.py) so the button only shows for DOIs we have a
  // handler for. Stage 6 E9 (2026-05-27) added eLife / PNAS /
  // Frontiers / Oxford; without this list, the Download-
  // manuscript button silently disappeared for those families.
  return (
    doi.startsWith("10.1038/") ||      // Nature/Springer
    doi.startsWith("10.1016/") ||      // Cell/Elsevier
    doi.startsWith("10.1126/") ||      // Science/AAAS
    doi.startsWith("10.7554/") ||      // eLife (E9 Step 1)
    doi.startsWith("10.3389/") ||      // Frontiers (E9 Step 3)
    doi.startsWith("10.1073/pnas.") || // PNAS (E9 Step 2)
    doi.startsWith("10.1093/") ||      // Oxford Academic (E9 Step 4)
    doi.startsWith("10.1371/journal.") || // PLOS (E9 follow-on)
    doi.startsWith("10.1523/JNEUROSCI.") || // JNeurosci (E9 follow-on)
    doi.startsWith("10.1523/jneurosci.") || // case variant
    doi.startsWith("10.1098/")              // Royal Society (E9 follow-on)
  );
}

/**
 * Three download buttons rendered at the bottom of a packet.
 * - Download manuscript: original publisher PDF, fetched + cached
 *   server-side by /api/papers/{slug}/manuscript.pdf (per-family
 *   dispatch: PMC requests, bioRxiv via Playwright, etc). Hidden
 *   when no identifier on the paper is one we know how to fetch.
 *   Same-origin URL so the HTML `download` attribute works.
 * - Download summary: PaperWhirl-generated PDF of the packet.
 * - Download summary + discussion: same packet PDF with chat
 *   inlined. Hidden until at least one Discuss message exists.
 */
export default function DownloadButtons({
  paperSlug, paper, hasDiscussion, hasLocalManuscript = false,
}: DownloadButtonsProps) {
  const base = `/api/papers/${paperSlug}`;
  // Stage 6 E1: <a href download> bypasses fetch / apiFetch, so the
  // anchor needs an absolute URL in production (Tauri-spawned port).
  // apiBaseSync() returns "" in dev (relative + Vite proxy) and the
  // backend origin in production.
  const hrefBase = `${apiBaseSync()}${base}`;
  const btn =
    "inline-flex items-center gap-2 px-3 py-1.5 text-sm text-warm-700 " +
    "hover:text-teal-650 hover:bg-warm-100 rounded-lg cursor-pointer " +
    "transition-colors no-underline";
  // Stage 6 E7 (2026-05-26): drop the wrapping <div> and let PacketView
  // place these buttons in a shared action-row flex container alongside
  // Save-to-list + Re-generate. Single border, single mt offset.
  return (
    <>
      {hasManuscriptCandidate(paper, hasLocalManuscript) && (
        <button
          type="button"
          className={btn}
          onClick={() =>
            downloadVerified(
              `${base}/manuscript.pdf`,
              `${paperSlug}_manuscript.pdf`,
              paper.pmcid ?? null,
            )
          }
        >
          <Download size={14} />
          Download manuscript
        </button>
      )}
      <a className={btn} href={`${hrefBase}/pdf`} download>
        <Download size={14} />
        Download summary
      </a>
      {hasDiscussion && (
        <a className={btn} href={`${hrefBase}/pdf?include_discussion=true`} download>
          <Download size={14} />
          Download summary + discussion
        </a>
      )}
    </>
  );
}
