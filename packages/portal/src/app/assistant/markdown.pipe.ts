import { Pipe, PipeTransform } from "@angular/core";

/**
 * Minimal, dependency-free Markdown → HTML for assistant replies.
 *
 * Covers what the model actually emits in chat: GFM tables, fenced/inline code,
 * bold, and bullet / numbered lists, with paragraphs and soft line breaks.
 * Input is HTML-escaped first, so only the tags this renderer emits can appear;
 * the result is additionally sanitized by Angular when bound via `[innerHTML]`.
 */
@Pipe({ name: "markdown", standalone: true })
export class MarkdownPipe implements PipeTransform {
  transform(src: string | null | undefined): string {
    return render(src ?? "");
  }
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Inline spans, applied to already-escaped text. */
function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>");
}

function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitRow(line);
  return (
    cells.length > 0 &&
    cells.every((c) => /^:?-{1,}:?$/.test(c.replace(/\s/g, "")))
  );
}

function render(raw: string): string {
  const lines = escapeHtml(raw).replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let i = 0;

  const flushParagraph = (buf: string[]) => {
    if (buf.length) out.push(`<p>${buf.map(inline).join("<br>")}</p>`);
    buf.length = 0;
  };

  let para: string[] = [];

  while (i < lines.length) {
    const line = lines[i];

    // fenced code block ```
    if (/^\s*```/.test(line)) {
      flushParagraph(para);
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i]))
        code.push(lines[i++]);
      i++; // closing fence
      // The copy affordance is a span (buttons are stripped by Angular's
      // innerHTML sanitizer); the panel handles it via a delegated click.
      out.push(
        `<pre class="md-pre"><span class="md-copy" title="Copy code">copy</span><code>${code.join("\n")}</code></pre>`,
      );
      continue;
    }

    // GFM table: a row with pipes followed by a separator row
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      flushParagraph(para);
      const header = splitRow(line);
      i += 2; // header + separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i++]));
      }
      const th = header.map((c) => `<th>${inline(c)}</th>`).join("");
      const body = rows
        .map(
          (r) =>
            `<tr>${header
              .map((_, c) => `<td>${inline(r[c] ?? "")}</td>`)
              .join("")}</tr>`,
        )
        .join("");
      out.push(
        `<table class="md-table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`,
      );
      continue;
    }

    // lists (consume a contiguous block)
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      flushParagraph(para);
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "")));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(
        `<${tag}>${items.map((it) => `<li>${it}</li>`).join("")}</${tag}>`,
      );
      continue;
    }

    // blank line → paragraph break
    if (!line.trim()) {
      flushParagraph(para);
      i++;
      continue;
    }

    para.push(line);
    i++;
  }
  flushParagraph(para);
  return out.join("");
}
