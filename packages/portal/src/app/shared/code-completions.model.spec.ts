import { completionSpecs, replacePrefix } from "./code-completions.model";

describe("code completions model", () => {
  const hints = {
    inputs: [
      { name: "amount", value: "${step_001.response.amount}" },
      { name: " " },
    ],
    steps: ["step_001"],
  };

  it("offers the named inputs in bracket form and skips empty names", () => {
    const labels = completionSpecs("python", hints).map((s) => s.label);
    expect(labels).toContain('inputs["amount"]');
    expect(labels.filter((l) => l.startsWith("inputs[")).length).toBe(1);
  });

  it("reads upstream steps from context in the language's own form", () => {
    const py = completionSpecs("python", hints).map((s) => s.insertText);
    const js = completionSpecs("typescript", hints).map((s) => s.insertText);
    expect(py).toContain('context["step_001"]["response"]');
    expect(js).toContain('context["step_001"].response');
  });

  it("gives Python an indented return snippet and JS a semicolon one", () => {
    const pyRet = completionSpecs("python", hints).find(
      (s) => s.label === "return {…}",
    );
    const jsRet = completionSpecs("javascript", hints).find(
      (s) => s.label === "return {…}",
    );
    expect(pyRet!.snippet).toBe(true);
    expect(pyRet!.insertText).toContain('\n    "${1:key}"');
    expect(jsRet!.insertText.endsWith("};")).toBe(true);
  });

  it("replaces the whole partially typed reference, not just the last word", () => {
    expect(replacePrefix('x = inputs["am')).toBe('inputs["am');
    expect(replacePrefix("    ret")).toBe("ret");
    expect(replacePrefix('a = context["step_001"].resp')).toBe(
      'context["step_001"].resp',
    );
    expect(replacePrefix("x = (")).toBe("");
  });
});
