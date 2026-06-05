import { useState, useRef, useEffect, useCallback, type ComponentPropsWithoutRef } from "react";
import { X, Send, RotateCcw, ClipboardCopy, Check, MessageSquare } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { confirmAction } from "../lib/dialogs";
import { normalizeMathDelimiters } from "../lib/math";
import { apiFetch } from "../lib/api";

interface Message {
  role: "user" | "assistant";
  content: string;
}

// Stage 5 E2: SSE event the backend emits around each tool call.
// Rendered as a transient status line above the in-flight assistant
// message; not stored in the thread.
interface ToolEvent {
  name: string;
  query: string;
  sources: string;
  status: "running" | "done";
  count?: number;
}

interface DiscussPanelProps {
  slug: string;
  contextSection: string | null;
  visible: boolean;
  messages: Message[];
  onMessagesChange: (messages: Message[]) => void;
  /** Panel width in px — owned by App so the main-content margin tracks it. */
  width: number;
  onWidthChange: (w: number) => void;
  onClose: () => void;
  /** Stage 6 E15: switch to the *other* thread in place (Discuss ⇄
   * Search) without closing the panel. The open panel (z-50,
   * full-height right edge) covers the top-right [D]/[S] chrome
   * buttons, so this header control is the only way to swap threads
   * while the panel is open. */
  onSwitchThread?: () => void;
  /** True when a paper is open, so a per-paper Discuss thread exists
   * to switch to. Lets the Search thread decide whether to offer the
   * "D" switch — there's nothing to switch to with no paper open. */
  paperAvailable?: boolean;
  /** Stage 5 E2: invoked when the user clicks an identifier rendered
   * in a streamed assistant message. The id is in whatever form the
   * resolver accepts (bare PMID digits, "PMC1234567", or a DOI). */
  onIdentifierClick?: (identifier: string) => void;
  /** Stage 5 E9: paper title, for the "Copy for ChatGPT" bundle
   * header. Undefined on the search thread. */
  paperTitle?: string;
  /** Stage 5 E9: first author / year / DOI, used to round out the
   * Copy-for-ChatGPT header so the receiving model can identify
   * (and ideally fetch) the paper. All optional; DOI line is
   * omitted entirely when absent. */
  paperFirstAuthor?: string;
  paperYear?: number | null;
  paperDoi?: string | null;
}

// Stage 5 E2: regex that rewrites three identifier patterns in
// completed assistant messages into markdown links pointing at a
// custom paperwhirl:// URL scheme. The IdentifierLink component
// (passed to ReactMarkdown as the `a` override) detects that scheme
// and renders a pill-style button wired to onIdentifierClick.
//
// Only run on COMPLETED assistant messages — partial-PMID renders
// mid-stream would briefly button-ize "123456" and then orphan the
// trailing "78" as plain text after the button. Plain markdown
// while streaming avoids the glitch; the rewrite kicks in once the
// turn ends.
const IDENTIFIER_RE =
  /\b(?:PMID\s*:?\s*(\d{6,9})|PMC(\d{6,9})|(?:doi:)?(10\.\d{4,9}\/[A-Za-z0-9._/():-]+))/gi;

function rewriteIdentifiers(content: string): string {
  return content.replace(
    IDENTIFIER_RE,
    (match, pmid, pmc, doi) => {
      if (pmid) {
        return `[PMID ${pmid}](paperwhirl://pmid/${pmid})`;
      }
      if (pmc) {
        return `[PMC${pmc}](paperwhirl://pmcid/PMC${pmc})`;
      }
      if (doi) {
        // Trailing period is a real DOI character in some cases
        // (`10.1101/2022.12.22.521551`), but more often it's a
        // sentence terminator. Strip the very last period only if
        // there's no digit after it — and put it back outside the
        // link so the prose still reads correctly.
        const trailMatch = doi.match(/\.$/);
        const clean = trailMatch ? doi.slice(0, -1) : doi;
        const trail = trailMatch ? "." : "";
        return `[${clean}](paperwhirl://doi/${clean})${trail}`;
      }
      return match;
    },
  );
}

// Custom <a> renderer passed to ReactMarkdown via the components
// override. When the href starts with paperwhirl://, render a pill
// button that fires onIdentifierClick with the identifier (the part
// after the kind segment in the URL). Everything else renders as a
// normal anchor, preserving the existing behavior of any real links
// the model might emit.
function makeIdentifierLink(
  onClick: ((id: string) => void) | undefined,
) {
  return function IdentifierLink(
    props: ComponentPropsWithoutRef<"a">,
  ) {
    const href = props.href || "";
    if (href.startsWith("paperwhirl://")) {
      const after = href.replace(/^paperwhirl:\/\//, "");
      const slashIdx = after.indexOf("/");
      const id = slashIdx >= 0 ? after.slice(slashIdx + 1) : after;
      return (
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            onClick?.(id);
          }}
          className="inline-block align-baseline mx-0.5 px-1.5 py-0 rounded-md bg-teal-50 text-teal-700 hover:bg-teal-100 text-xs font-medium cursor-pointer no-underline transition-colors"
          title={`Open ${id}`}
        >
          {props.children}
        </button>
      );
    }
    return <a {...props} target="_blank" rel="noopener noreferrer" />;
  };
}

export default function DiscussPanel({
  slug,
  contextSection,
  visible,
  messages,
  onMessagesChange,
  width,
  onWidthChange,
  onClose,
  onSwitchThread,
  paperAvailable = false,
  onIdentifierClick,
  paperTitle,
  paperFirstAuthor,
  paperYear,
  paperDoi,
}: DiscussPanelProps) {
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  // Stage 5 E9: transient "copied" check on the Copy-for-ChatGPT button.
  const [copied, setCopied] = useState(false);
  // Stage 5 E2: tool-call indicators for the most-recent assistant
  // turn. Tri-state status because there's a real wall-clock gap
  // between user-hits-send and first-tool-call-fires (OpenAI is
  // composing the tool call) — we want SOMETHING visible from t=0:
  //   - "thinking" → set on send, before any tool call. Covers the
  //     gap and also covers no-tool questions where the model
  //     answers directly.
  //   - "searching" → set on first `running` SSE event. Replaces
  //     "thinking" once we know a search is happening; stays put
  //     across sequential searches.
  //   - "idle" → set on first text-content delta (model is now
  //     composing the answer) or at stream end as belt-and-
  //     suspenders.
  // React 18 batches state updates, so per-event running indicators
  // get collapsed into the next render — the tri-state sidesteps
  // that entirely with state that changes only on real transitions.
  // `toolEvents` is an appended-only list of DONE events shown
  // below the spinner so the user can see what was searched.
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([]);
  const [pendingStatus, setPendingStatus] = useState<
    "idle" | "thinking" | "searching"
  >("idle");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const contextSent = useRef(false);

  // Stage 5 E9: bundle the thread (+ paper title) onto the clipboard
  // so the user can paste it into ChatGPT and keep going there. No
  // ChatGPT API exists to push a conversation into an account, so
  // clipboard is the robust path (no length cap, unlike a ?q= deep
  // link).
  const copyForChatGPT = useCallback(async () => {
    if (messages.length === 0) return;
    let header = "";
    if (paperTitle) {
      // Citation tail "(First Author, Year)" — drop either piece if
      // missing; drop the whole parenthetical if both are absent.
      const cite = [paperFirstAuthor, paperYear ?? undefined]
        .filter(Boolean)
        .join(", ");
      const titleLine = cite
        ? `Paper: ${paperTitle} (${cite})`
        : `Paper: ${paperTitle}`;
      const lines = [titleLine];
      if (paperDoi) lines.push(`DOI: https://doi.org/${paperDoi}`);
      header = lines.join("\n") + "\n\n";
    }
    const intro =
      "Below is a discussion I had about this paper in a review tool. " +
      "Please pick up where it left off and help me dig deeper.\n\n" +
      "--- Discussion so far ---\n\n";
    const transcript = messages
      .map((m) => `${m.role === "user" ? "Me" : "Assistant"}: ${m.content}`)
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(header + intro + transcript);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard permission denied — no-op */
    }
  }, [messages, paperTitle, paperFirstAuthor, paperYear, paperDoi]);
  const IdentifierLink = makeIdentifierLink(onIdentifierClick);
  // Abort handle for the in-flight /api/discuss fetch — lets Escape
  // tear down the stream cleanly and roll back to before the send.
  const abortRef = useRef<AbortController | null>(null);
  // The user's most-recently-sent prompt — restored to the input box
  // on Escape so the user can edit and resend (e.g. "add or correct
  // context") rather than retype from scratch.
  const lastSentRef = useRef<string>("");
  // Captured at send time so the rollback knows what the pre-send
  // message list looked like (and can revert to it).
  const messagesBeforeSendRef = useRef<Message[]>([]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (visible) inputRef.current?.focus();
  }, [visible]);

  const sendMessage = useCallback(
    async (userMessage: string) => {
      // Capture the pre-send state so Escape can roll back cleanly.
      messagesBeforeSendRef.current = messages;
      lastSentRef.current = userMessage;

      const ctrl = new AbortController();
      abortRef.current = ctrl;

      const userMsg: Message = { role: "user", content: userMessage };
      const newMessages = [...messages, userMsg];
      const assistantMsg: Message = { role: "assistant", content: "" };
      onMessagesChange([...newMessages, assistantMsg]);
      setInput("");
      setStreaming(true);
      // E2: clear the prior turn's tool events on every new send —
      // they're tied to a specific assistant turn, not the thread.
      setToolEvents([]);
      // Show "🤔 thinking…" immediately so the user sees activity
      // during the 1-3s gap between send and the first tool_call
      // SSE event. Transitions to "🔍 searching…" once a tool
      // actually fires, or to idle on first text delta.
      setPendingStatus("thinking");

      try {
        const resp = await apiFetch("/api/discuss", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slug,
            messages: newMessages,
            context_section: !contextSent.current ? contextSection : undefined,
          }),
          signal: ctrl.signal,
        });

        if (!resp.ok) {
          const body = await resp.json().catch(() => ({ detail: "Error" }));
          onMessagesChange([
            ...newMessages,
            { role: "assistant", content: `Error: ${body.detail}` },
          ]);
          setStreaming(false);
          return;
        }

        contextSent.current = true;

        const reader = resp.body?.getReader();
        const decoder = new TextDecoder();
        let accumulated = "";

        if (reader) {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            // The user may have pressed Escape between reads — bail
            // before mutating message state with stale content.
            if (ctrl.signal.aborted) break;

            const text = decoder.decode(value, { stream: true });
            const lines = text.split("\n");

            for (const line of lines) {
              if (line.startsWith("data: ")) {
                const data = line.slice(6);
                if (data === "[DONE]") break;
                try {
                  const parsed = JSON.parse(data);
                  if (parsed.type === "tool_call") {
                    if (parsed.status === "running") {
                      // Promote thinking → searching on first
                      // running event. Stays "searching" across
                      // sequential tool rounds until text arrives.
                      setPendingStatus("searching");
                    } else if (parsed.status === "done") {
                      setToolEvents((prev) => [
                        ...prev,
                        {
                          name: parsed.name,
                          query: parsed.query || "",
                          sources: parsed.sources || "all",
                          status: "done",
                          count: parsed.count,
                        },
                      ]);
                    }
                  } else if (parsed.content) {
                    if (accumulated === "") {
                      // First text delta — model is now composing.
                      // Hide the spinner; the done-events list stays.
                      setPendingStatus("idle");
                    }
                    accumulated += parsed.content;
                    onMessagesChange([
                      ...newMessages,
                      { role: "assistant", content: accumulated },
                    ]);
                  }
                } catch {}
              }
            }
          }
        }
      } catch (err) {
        // Intentional abort from Escape — silent. The keydown handler
        // already did the rollback (input restored, trailing messages
        // dropped); nothing more to do here.
        if (
          (err instanceof DOMException && err.name === "AbortError") ||
          (err as Error | undefined)?.name === "AbortError"
        ) {
          // no-op
        } else {
          onMessagesChange([
            ...newMessages,
            { role: "assistant", content: "Connection error. Please try again." },
          ]);
        }
      } finally {
        if (abortRef.current === ctrl) {
          abortRef.current = null;
        }
        setStreaming(false);
        // E2: belt-and-suspenders — if the stream ended without any
        // content delta (e.g., aborted, error, model only emitted
        // tool calls then crashed), make sure the spinner doesn't
        // get stuck on.
        setPendingStatus("idle");
      }
    },
    [messages, slug, contextSection, onMessagesChange]
  );

  // Escape during streaming: abort the in-flight fetch, drop the
  // trailing user + assistant messages from the thread, restore the
  // user's prompt to the input so they can edit and resend. Standard
  // chat-app pattern (ChatGPT / Claude.ai stop-then-edit). Visible
  // Stop button is intentionally omitted — Escape is the only trigger.
  useEffect(() => {
    if (!streaming) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      abortRef.current?.abort();
      abortRef.current = null;
      // Roll the thread back to the pre-send snapshot. setStreaming
      // gets flipped by the fetch's finally block.
      onMessagesChange(messagesBeforeSendRef.current);
      setInput(lastSentRef.current);
      // Defer focus to after React has re-rendered the textarea.
      requestAnimationFrame(() => inputRef.current?.focus());
    };
    // Capture phase so we beat any other listener that might preventDefault.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [streaming, onMessagesChange]);

  const handleSubmit = () => {
    const trimmed = input.trim();
    if (!trimmed || streaming) return;
    sendMessage(trimmed);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const onMove = (ev: MouseEvent) => {
      // Panel is pinned to the right edge — width grows as the cursor
      // moves left of innerWidth.
      onWidthChange(
        Math.min(Math.max(window.innerWidth - ev.clientX, 280), 640)
      );
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  if (!visible) return null;

  return (
    <div
      className="fixed top-0 right-0 h-full bg-white border-l border-warm-200 flex flex-col z-50 shadow-lg"
      style={{ width }}
    >
      <div
        onMouseDown={startResize}
        title="Drag to resize"
        className="absolute top-0 left-0 w-1.5 h-full cursor-col-resize hover:bg-teal-650/40 transition-colors"
      />
      <div className="flex items-center justify-between px-4 py-3 border-b border-warm-200">
        <h3 className="text-sm font-semibold text-warm-800">
          {slug ? "Discuss" : "Search"}
        </h3>
        <div className="flex items-center gap-3">
          <button
            onClick={copyForChatGPT}
            disabled={messages.length === 0}
            className="text-warm-400 hover:text-warm-700 disabled:text-warm-200 disabled:cursor-not-allowed cursor-pointer"
            title="Copy this discussion for ChatGPT"
          >
            {copied ? <Check size={16} className="text-teal-650" /> : <ClipboardCopy size={16} />}
          </button>
          <button
            onClick={async () => {
              if (messages.length === 0) return;
              // Search thread (slug empty) is in-memory only and
              // cheap to recreate — skip the confirm dialog. Per-
              // paper Discuss thread is durable and ties to a
              // specific paper, so keep the confirm there.
              if (slug) {
                if (!(await confirmAction("Clear this discussion?", { kind: "warning" }))) return;
              }
              onMessagesChange([]);
              contextSent.current = false;
            }}
            disabled={messages.length === 0}
            className="text-warm-400 hover:text-warm-700 disabled:text-warm-200 disabled:cursor-not-allowed cursor-pointer"
            title={slug ? "Clear discussion" : "Clear search"}
          >
            <RotateCcw size={16} />
          </button>
          {(slug || paperAvailable) && onSwitchThread && (
            <button
              onClick={onSwitchThread}
              className="text-warm-400 hover:text-warm-700 cursor-pointer"
              title={slug ? "Switch to Search" : "Switch to paper Discussion"}
            >
              {/* MessageSquare + the OTHER thread's letter, matching the
                * top-chrome D/S buttons. In Discuss → "S" (go to Search);
                * in Search → "D" (go to the paper thread). Lets the user
                * toggle threads without closing, since the open panel
                * hides the chrome buttons. */}
              <span className="relative inline-flex items-center justify-center">
                <MessageSquare size={18} />
                <span className="absolute text-[9px] font-bold leading-none pointer-events-none"
                      style={{ transform: "translateY(-1px)" }}>
                  {slug ? "S" : "D"}
                </span>
              </span>
            </button>
          )}
          <button
            onClick={onClose}
            className="text-warm-400 hover:text-warm-700 cursor-pointer"
            title="Close panel"
          >
            <X size={18} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {messages.length === 0 && (
          <p className="text-sm text-warm-400 text-center mt-8">
            {slug
              ? "Ask anything about the paper."
              : "Ask anything — what to read, what's new in a field, what a paper found."}
          </p>
        )}
        {messages.map((msg, i) => {
          const isLast = i === messages.length - 1;
          const isStreamingLast = streaming && isLast;
          // E2: only rewrite identifiers into clickable buttons on
          // COMPLETED assistant messages. While streaming, render
          // raw markdown so partial PMIDs don't briefly button-ize.
          const rendered = normalizeMathDelimiters(
            msg.role === "assistant" && !isStreamingLast
              ? rewriteIdentifiers(msg.content)
              : msg.content,
          );
          return (
            <div
              key={i}
              className={`text-sm ${
                msg.role === "user"
                  ? "text-warm-800 bg-warm-100 rounded-lg px-3 py-2"
                  : "text-warm-700 leading-relaxed"
              }`}
            >
              {/* E2: tool-call indicator block above the last
                * assistant turn. Cumulative list of completed
                * searches with their hit counts, plus a tri-state
                * spinner at the bottom: thinking (between send
                * and first tool call), searching (during tool
                * calls), idle (text streaming or done). All clear
                * on next user send. */}
              {msg.role === "assistant" &&
                isLast &&
                (pendingStatus !== "idle" || toolEvents.length > 0) && (
                  <div className="mb-2 space-y-1">
                    {toolEvents.map((e, j) => {
                      const scopeNote =
                        e.sources === "preprints"
                          ? " (preprints only)"
                          : e.sources === "published"
                            ? " (peer-reviewed only)"
                            : "";
                      return (
                        <div
                          key={j}
                          className="text-xs text-warm-500 italic"
                        >
                          {`✓ found ${e.count ?? 0} result${
                            e.count === 1 ? "" : "s"
                          }${scopeNote} for "${e.query}"`}
                        </div>
                      );
                    })}
                    {pendingStatus === "thinking" && (
                      <div className="text-xs text-warm-500 italic">
                        <span className="inline-block w-2 h-2 bg-teal-650 rounded-full mr-1.5 animate-pulse" />
                        thinking…
                      </div>
                    )}
                    {pendingStatus === "searching" && (
                      <div className="text-xs text-warm-500 italic">
                        <span className="inline-block w-2 h-2 bg-teal-650 rounded-full mr-1.5 animate-pulse" />
                        searching the literature…
                      </div>
                    )}
                  </div>
                )}
              {msg.role === "user" ? (
                msg.content
              ) : (
                <div className="prose prose-sm max-w-none prose-headings:text-warm-800 prose-p:text-warm-700 prose-li:text-warm-700 prose-strong:text-warm-800">
                  <ReactMarkdown
                    components={{ a: IdentifierLink }}
                    remarkPlugins={[remarkMath]}
                    rehypePlugins={[rehypeKatex]}
                    // react-markdown v10's default urlTransform strips
                    // URLs with unknown schemes. Whitelist our custom
                    // `paperwhirl://` so the rewriteIdentifiers links
                    // make it through to IdentifierLink intact; fall
                    // back to the standard http/https/mailto/tel
                    // allowlist for everything else.
                    urlTransform={(url) => {
                      if (url.startsWith("paperwhirl://")) return url;
                      return /^(?:https?|mailto|tel):/i.test(url) ? url : "";
                    }}
                  >
                    {rendered}
                  </ReactMarkdown>
                </div>
              )}
              {msg.role === "assistant" && isStreamingLast && (
                <span className="inline-block w-1.5 h-4 bg-teal-650 ml-0.5 animate-pulse" />
              )}
            </div>
          );
        })}
        <div ref={messagesEndRef} />
      </div>

      <div className="border-t border-warm-200 px-4 py-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={slug ? "Ask about the paper..." : "Ask anything..."}
            rows={1}
            className="flex-1 resize-none text-sm text-warm-800 placeholder:text-warm-400 bg-warm-100 rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-teal-550"
          />
          <button
            onClick={handleSubmit}
            disabled={!input.trim() || streaming}
            className={`p-2 rounded-lg transition-colors cursor-pointer ${
              input.trim() && !streaming
                ? "bg-teal-650 text-white hover:bg-teal-550"
                : "bg-warm-100 text-warm-400 cursor-not-allowed"
            }`}
          >
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
