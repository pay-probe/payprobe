import {
  newRow,
  rowState,
  tableFromRows,
  tableRowsFromValue,
  validateTableRow,
  visibleRows,
} from "./de-table.model";
import { DeTable } from "./de-values.model";

const BASE: DeTable = {
  "2": { name: "PAN", len_type: "llvar", length: 19, type: "n" },
  "4": { name: "Amount", len_type: "fixed", length: 12, type: "n" },
  "48": {
    name: "Additional Data",
    len_type: "lllvar",
    length: 999,
    type: "ans",
  },
};

describe("de-table model", () => {
  it("reads a table stored as an object or a JSON string, sorted by DE", () => {
    const rows = tableRowsFromValue(
      JSON.stringify({ "48": BASE["48"], "2": BASE["2"] }),
    );
    expect(rows!.map((r) => r.de)).toEqual(["2", "48"]);
    expect(tableRowsFromValue({ "4": BASE["4"] })![0].spec).toEqual(BASE["4"]);
    expect(tableRowsFromValue("")).toEqual([]);
    expect(tableRowsFromValue("[]")).toBe(null);
    expect(tableRowsFromValue("{oops")).toBe(null);
  });

  it("writes back in DE order and keeps keys the form does not edit", () => {
    const rows = [
      {
        de: "48",
        spec: {
          len_type: "lllvar",
          length: 999,
          type: "ans",
          encoding: { text: "ebcdic" },
        },
      },
      { de: "4", spec: { name: "", len_type: "fixed", length: 12, type: "n" } },
      { de: " ", spec: { len_type: "fixed", length: 1 } },
    ];
    const out = tableFromRows(rows);
    expect(Object.keys(out)).toEqual(["4", "48"]);
    expect("name" in out["4"]).toBeFalse();
    expect((out["48"] as Record<string, unknown>)["encoding"]).toEqual({
      text: "ebcdic",
    });
  });

  it("compares with the baseline on wire-relevant keys only", () => {
    expect(
      rowState({ de: "4", spec: { ...BASE["4"], name: "Renamed" } }, BASE),
    ).toBe("same");
    expect(
      rowState({ de: "4", spec: { ...BASE["4"], length: 10 } }, BASE),
    ).toBe("changed");
    expect(
      rowState(
        { de: "4", spec: { ...BASE["4"], encoding: { text: "ebcdic" } } },
        BASE,
      ),
    ).toBe("changed");
    expect(
      rowState(
        { de: "63", spec: { len_type: "lllvar", length: 999, type: "ans" } },
        BASE,
      ),
    ).toBe("added");
  });

  it("refuses what the codec would refuse", () => {
    expect(validateTableRow({ de: "4", spec: BASE["4"] })).toBe(null);
    expect(validateTableRow({ de: "1", spec: BASE["4"] })).toBe(
      'DE number must be 2 to 192, got "1"',
    );
    expect(validateTableRow({ de: "x", spec: BASE["4"] })).toBe(
      'DE number must be 2 to 192, got "x"',
    );
    expect(
      validateTableRow({ de: "4", spec: { ...BASE["4"], len_type: "lvar" } }),
    ).toBe(
      "DE4: length type must be one of fixed, llvar, lllvar, llllvar, lllllvar",
    );
    expect(
      validateTableRow({ de: "4", spec: { ...BASE["4"], length: undefined } }),
    ).toBe("DE4: length must be a whole number");
    expect(
      validateTableRow({ de: "4", spec: { ...BASE["4"], length: 0 } }),
    ).toBe("DE4: a fixed field needs a length");
    expect(
      validateTableRow({ de: "2", spec: { ...BASE["2"], length: 120 } }),
    ).toBe("DE2: llvar carries at most 99, got 120");
  });

  it("adds from the baseline when it knows the DE, else a blank fixed field", () => {
    expect(newRow("48", BASE).spec).toEqual(BASE["48"] as never);
    expect(newRow("63", BASE).spec).toEqual({
      name: "",
      len_type: "fixed",
      length: 0,
      type: "ans",
    });
  });

  it("filters to differences and by number or name", () => {
    const rows = [
      { de: "2", spec: { ...BASE["2"] } },
      { de: "4", spec: { ...BASE["4"], length: 10 } },
      { de: "48", spec: { ...BASE["48"] } },
    ];
    expect(visibleRows(rows, BASE, "", true).map((r) => r.de)).toEqual(["4"]);
    expect(visibleRows(rows, BASE, "4", false).map((r) => r.de)).toEqual([
      "4",
      "48",
    ]);
    expect(
      visibleRows(rows, BASE, "additional", false).map((r) => r.de),
    ).toEqual(["48"]);
  });
});
