export interface Claim {
  id: string;
  statement: string;
  notes?: string;
}

export interface Overview {
  background: string;
  gap: string;
  /** Stage 5 E7 S24: "What the authors did" — the approach/methods
   * narrative, distinct from claims (conclusions). Optional: skeletons
   * extracted before generation, or pre-S24 packets, won't have it. */
  approach?: string;
  claims: Claim[];
}

export interface FigureAnalysis {
  motivation: string;
  question: string;
  approach: string;
  /** Stage 5 E7 S25: ≤3 sentences answering the figure's question.
   * Optional so pre-S25 packets still type-check. */
  results?: string;
  evidence: string;
  interpretation: string;
  linked_claims: string[];
}

export interface FigureView {
  source_page: number | null;
  asset: string | null;
  asset_role: string;
  crop: null;
  original_caption: string;
  display_legend: string;
}

export interface Figure {
  id: string;
  order: number;
  /** Added in Stage 3 E5; older packets lack it — consumers fall back. */
  source_order?: number;
  label: string;
  source_figure_id: string;
  view: FigureView;
  analysis: FigureAnalysis;
}

export interface TableView {
  caption_text: string;
  html: string;
  /** Stage 6 E7: image-only tables (e.g., JCI deposits tables as
   * graphics to PMC). When set, the UI renders the image instead of
   * the html. Either `html` or `asset` is populated, not both. */
  asset?: string | null;
  asset_role: string;
}

export interface TableAnalysis {
  interpretation: string;
  linked_claims: string[];
}

export interface Table {
  id: string;
  order: number;
  /** Added in Stage 3 E5; older packets lack it — consumers fall back. */
  source_order?: number;
  label: string;
  source_table_id: string;
  view: TableView;
  analysis: TableAnalysis;
}

export interface Discussion {
  synthesis: string;
  takeaways: string[];
  caveats: string[];
  next_steps: string[];
}

export interface Paper {
  id: string;
  title: string;
  /** Stage 7 E2: the paper's own abstract, carried from extraction.
   * Shown as the Overview in Scan mode (no LLM). Empty when an
   * extractor / the PDF fallback couldn't capture one. */
  abstract?: string;
  authors: string[];
  first_author: string;
  journal: string | null;
  year: number | null;
  doi: string | null;
  pmcid?: string | null;
  biorxiv_doi?: string | null;
  arxiv_id?: string | null;
  pmid?: string | null;
}

export interface Session {
  id: string;
  title: string;
  /** Stage 7 E2: "scan" (abstract + figures + legends, no LLM) or
   * "full" (the generated Deep Dive summary). Absent on older saved
   * packets → treated as "full". */
  mode?: "scan" | "full" | string;
}

export interface ReviewPacket {
  schema_version: string;
  session: Session;
  paper: Paper;
  overview: Overview;
  figures: Figure[];
  /** Added in Stage 3 E5; older packets lack it — consumers fall back to []. */
  tables?: Table[];
  discussion: Discussion;
  /** Stage 5 E3 (2026-05-18): extraction-level warnings the user
   * should see as a banner (e.g. "figures couldn't be rendered;
   * try VPN"). Populated by the backend's SSE `warning` events.
   * Not persisted with saved packets — each extraction emits its
   * own warnings based on the current network/source state. */
  warnings?: string[];
}

// E7: Reading Lists
export interface ReadingListIndexEntry {
  slug: string;
  name: string;
  description: string;
  updated_at: string;
}

export interface ReadingListIndex {
  schema_version: string;
  lists: ReadingListIndexEntry[];
}

export interface ReadingListPaperRef {
  slug: string;
  added_at: string;
}

export interface ReadingList {
  schema_version: string;
  slug: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  papers: ReadingListPaperRef[];
}
