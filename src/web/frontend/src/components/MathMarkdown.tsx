import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { normalizeMathDelimiters } from "../lib/math";

// Stage 5 E7 S18 (2026-05-19): shared markdown+math renderer for
// packet prose (overview, claims, figure/table analysis, discussion).
// Mirrors the Discuss panel's rendering so equations typeset the same
// way everywhere. Inline-only by default (`<span>` wrapper) so it
// drops into existing prose spots without forcing block layout; pass
// `block` for multi-paragraph fields.
interface MathMarkdownProps {
  text: string;
  className?: string;
  block?: boolean;
}

export default function MathMarkdown({ text, className, block = false }: MathMarkdownProps) {
  const Wrapper = block ? "div" : "span";
  // Inline mode (e.g. a claim after a "Claim 1:" label, or a list
  // item): render markdown paragraphs as fragments so the text flows
  // inline instead of getting wrapped in a block <p>.
  const components = block ? undefined : { p: ({ children }: { children?: ReactNode }) => <>{children}</> };
  return (
    <Wrapper className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {normalizeMathDelimiters(text)}
      </ReactMarkdown>
    </Wrapper>
  );
}
