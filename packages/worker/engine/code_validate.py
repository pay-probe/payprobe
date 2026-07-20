"""Syntax validation for ``code`` nodes — compile/parse without executing.

Reuses the same runtimes the code runner uses, so the diagnostics match how a
snippet will actually run:
- Python: ``compile()`` the snippet wrapped in the same function body the runner
  uses (so a top-level ``return`` is legal).
- JavaScript / TypeScript: esbuild ``transformSync`` (precise locations); when
  esbuild is unavailable, a best-effort ``new Function`` parse for JS.

Returns ``{"ok": bool, "errors": [{"line", "column", "message"}]}`` with line
numbers in the user's own coordinates (the wrapper line is subtracted out).
"""

from __future__ import annotations

import asyncio
import json
import shutil

# Parse + report errors from a wrapped async arrow, mapping locations back to
# the user's snippet (the wrapper adds exactly one line above the code).
_NODE_VALIDATE = r"""
const fs = require('fs');
(() => {
  let data;
  try { data = JSON.parse(fs.readFileSync(0, 'utf8')); }
  catch (e) { process.stdout.write(JSON.stringify({ ok: false, errors: [{ line: 1, column: 1, message: 'bad input' }] })); return; }
  let esbuild = null;
  try { esbuild = require('esbuild'); } catch (_) {}
  const wrapped = '(async (context, inputs) => {\n' + (data.code || '') + '\n})';
  try {
    if (esbuild) {
      esbuild.transformSync(wrapped, { loader: data.language === 'typescript' ? 'ts' : 'js' });
    } else if (data.language === 'typescript') {
      process.stdout.write(JSON.stringify({ ok: true, errors: [], skipped: 'esbuild unavailable' }));
      return;
    } else {
      new Function('context', 'inputs', data.code || '');
    }
    process.stdout.write(JSON.stringify({ ok: true, errors: [] }));
  } catch (e) {
    let errors = (e.errors || []).map((er) => ({
      line: Math.max((er.location ? er.location.line : 1) - 1, 1),
      column: (er.location ? er.location.column : 0) + 1,
      message: er.text || 'syntax error',
    }));
    if (!errors.length) errors = [{ line: 1, column: 1, message: String(e.message || e) }];
    process.stdout.write(JSON.stringify({ ok: false, errors }));
  }
})();
"""


def _validate_python(code: str) -> dict:
    wrapped = "def __main(context, inputs):\n" + "".join(
        "    " + line + "\n" for line in code.split("\n")
    )
    try:
        compile(wrapped, "<code-node>", "exec")
        return {"ok": True, "errors": []}
    except SyntaxError as exc:
        line = max((exc.lineno or 2) - 1, 1)  # wrapper occupies line 1
        col = max((exc.offset or 5) - 4, 1)  # strip the 4-space indent
        return {
            "ok": False,
            "errors": [{"line": line, "column": col, "message": exc.msg or "syntax error"}],
        }
    except ValueError as exc:  # e.g. null bytes
        return {"ok": False, "errors": [{"line": 1, "column": 1, "message": str(exc)}]}


async def _validate_node(language: str, code: str) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ok": True, "errors": [], "skipped": "node runtime unavailable"}
    payload = json.dumps({"language": language, "code": code}).encode()
    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            "-e",
            _NODE_VALIDATE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(payload), timeout=10)
    except (asyncio.TimeoutError, OSError):
        return {"ok": True, "errors": [], "skipped": "validator timed out"}
    text = out.decode(errors="replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": True, "errors": [], "skipped": "validator produced no result"}


async def validate_syntax(language: str, code: str) -> dict:
    """Check ``code`` for syntax errors in ``language`` without running it."""
    if not (code or "").strip():
        return {"ok": True, "errors": []}
    if language == "python":
        return _validate_python(code)
    if language in ("typescript", "javascript"):
        return await _validate_node(language, code)
    return {"ok": True, "errors": [], "skipped": f"unsupported language '{language}'"}
