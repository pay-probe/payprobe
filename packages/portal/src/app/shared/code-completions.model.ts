/**
 * Completions for the code step's snippet contract (worker/engine/code_runner.py):
 * the snippet is the body of `__main(context, inputs)` (Python) or of
 * `async (context, inputs) => {…}` (JS/TS), where
 *   - `inputs`  is the node's resolved input map (the Inputs rows, by name);
 *   - `context` is `{step_id: {request, response}}` of the upstream steps;
 * and it returns an object, exposed downstream as `${node_id.response.*}`.
 *
 * Pure data here; `registerCodeCompletions` adapts it to Monaco once.
 */

export interface CodeHints {
  /** Names of the node's Inputs rows (empty names are skipped). */
  inputs: { name: string; value?: string }[];
  /** Upstream step ids, in execution order. */
  steps: string[];
}

export type CompletionKind = "variable" | "field" | "snippet";

export interface CompletionSpec {
  label: string;
  kind: CompletionKind;
  insertText: string;
  /** True when insertText carries ${1:…} placeholders. */
  snippet?: boolean;
  detail: string;
  documentation?: string;
  /** Text Monaco filters on, when it differs from the label. */
  filterText?: string;
}

type CodeLanguage = "python" | "javascript" | "typescript";

export const CODE_LANGUAGES: readonly CodeLanguage[] = [
  "python",
  "javascript",
  "typescript",
];

/** Everything the editor offers for a snippet with these hints. */
export function completionSpecs(
  language: string,
  hints: CodeHints,
): CompletionSpec[] {
  const py = language === "python";
  const out: CompletionSpec[] = [
    {
      label: "inputs",
      kind: "variable",
      insertText: "inputs",
      detail: "this node's inputs, by name",
      documentation:
        "The Inputs rows above, with ${…} references already resolved.",
    },
    {
      label: "context",
      kind: "variable",
      insertText: "context",
      detail: "upstream results: {step_id: {request, response}}",
    },
  ];

  for (const row of hints.inputs) {
    const name = row.name.trim();
    if (!name) continue;
    out.push({
      label: `inputs["${name}"]`,
      kind: "field",
      insertText: `inputs["${name}"]`,
      detail: "input",
      documentation: row.value ? `= ${row.value}` : undefined,
    });
  }

  for (const id of hints.steps) {
    out.push({
      label: py ? `context["${id}"]["response"]` : `context["${id}"].response`,
      kind: "field",
      insertText: py
        ? `context["${id}"]["response"]`
        : `context["${id}"].response`,
      detail: "upstream step response",
    });
    out.push({
      label: py ? `context["${id}"]["request"]` : `context["${id}"].request`,
      kind: "field",
      insertText: py
        ? `context["${id}"]["request"]`
        : `context["${id}"].request`,
      detail: "upstream step request",
    });
  }

  out.push(...(py ? PY_SNIPPETS : JS_SNIPPETS));
  return out;
}

const PY_SNIPPETS: CompletionSpec[] = [
  {
    label: "return {…}",
    filterText: "return",
    kind: "snippet",
    snippet: true,
    insertText: 'return {\n    "${1:key}": ${2:value},\n}',
    detail: "return the node's response object",
  },
  {
    label: "get response field",
    filterText: "context get",
    kind: "snippet",
    snippet: true,
    insertText: 'context["${1:step_id}"]["response"].get("${2:field}")',
    detail: "read an upstream field, None when absent",
  },
  {
    label: "try / except",
    filterText: "try",
    kind: "snippet",
    snippet: true,
    insertText:
      'try:\n    ${1:pass}\nexcept ${2:Exception} as exc:\n    return {"error": str(exc)}',
    detail: "return the error instead of failing the step",
  },
];

const JS_SNIPPETS: CompletionSpec[] = [
  {
    label: "return {…}",
    filterText: "return",
    kind: "snippet",
    snippet: true,
    insertText: "return {\n  ${1:key}: ${2:value},\n};",
    detail: "return the node's response object",
  },
  {
    label: "get response field",
    filterText: "context get",
    kind: "snippet",
    snippet: true,
    insertText: 'context["${1:step_id}"]?.response?.${2:field}',
    detail: "read an upstream field, undefined when absent",
  },
];

/**
 * The text before the cursor that a completion replaces: an identifier with
 * any bracket/quote/dot tail, so `inputs["am` is replaced by `inputs["amount"]`
 * instead of producing `inputs["inputs["amount"]`.
 */
export function replacePrefix(textBeforeCursor: string): string {
  return /[A-Za-z_][\w[\]"'.-]*$/.exec(textBeforeCursor)?.[0] ?? "";
}

// -- Monaco adapter (registered once per language, hints looked up per model) --

const hintsByModel = new Map<string, CodeHints>();
let registered = false;

/** Attach (or replace) the hints for one editor model; null detaches. */
export function setModelHints(modelUri: string, hints: CodeHints | null): void {
  if (hints) hintsByModel.set(modelUri, hints);
  else hintsByModel.delete(modelUri);
}

/** Monaco providers are global per language, so register exactly once. */
export function registerCodeCompletions(monaco: any): void {
  if (registered) return;
  registered = true;
  const kinds: Record<CompletionKind, number> = {
    variable: monaco.languages.CompletionItemKind.Variable,
    field: monaco.languages.CompletionItemKind.Field,
    snippet: monaco.languages.CompletionItemKind.Snippet,
  };
  for (const language of CODE_LANGUAGES) {
    monaco.languages.registerCompletionItemProvider(language, {
      triggerCharacters: ["[", '"', "."],
      provideCompletionItems: (model: any, position: any) => {
        // Only editors that passed hints (the code step) get these items.
        const hints = hintsByModel.get(model.uri.toString());
        if (!hints) return { suggestions: [] };
        const before = model.getValueInRange({
          startLineNumber: position.lineNumber,
          startColumn: 1,
          endLineNumber: position.lineNumber,
          endColumn: position.column,
        });
        const prefix = replacePrefix(before);
        const range = {
          startLineNumber: position.lineNumber,
          endLineNumber: position.lineNumber,
          startColumn: position.column - prefix.length,
          endColumn: position.column,
        };
        return {
          suggestions: completionSpecs(model.getLanguageId(), hints).map(
            (s) => ({
              label: s.label,
              kind: kinds[s.kind],
              insertText: s.insertText,
              insertTextRules: s.snippet
                ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
                : undefined,
              detail: s.detail,
              documentation: s.documentation,
              filterText: s.filterText,
              range,
            }),
          ),
        };
      },
    });
  }
}
