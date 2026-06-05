// Stage 5 E7 S17/S18 (2026-05-19): LLMs emit math with mixed
// delimiters — some use TeX-markdown `$…$` / `$$…$$` (what remark-math
// expects), others use LaTeX `\(…\)` / `\[…\]`. Normalize the LaTeX
// forms to the `$` forms so KaTeX renders both. Done as a string
// pre-pass because remark-math doesn't recognize `\(`/`\[` natively.
export function normalizeMathDelimiters(md: string): string {
  return md
    .replace(/\\\[([\s\S]+?)\\\]/g, (_m, body) => `$$${body}$$`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_m, body) => `$${body}$`);
}
