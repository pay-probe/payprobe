import {
  DeTable,
  padFixed,
  paddingNote,
  activeTable,
  formatHint,
  isStaticStan,
  isStaticTime,
  rowsFromValue,
  searchDes,
  unknownDe,
  validateRow,
  valueFromRows,
} from "./de-values.model";

const TABLE: DeTable = {
  "2": {
    name: "Primary Account Number (PAN)",
    len_type: "llvar",
    length: 19,
    type: "n",
  },
  "3": { name: "Processing Code", len_type: "fixed", length: 6, type: "n" },
  "4": {
    name: "Amount, Transaction",
    len_type: "fixed",
    length: 12,
    type: "n",
  },
  "11": { name: "STAN", len_type: "fixed", length: 6, type: "n" },
  "41": {
    name: "Card Acceptor Terminal ID",
    len_type: "fixed",
    length: 8,
    type: "ans",
  },
  "52": { name: "PIN Data", len_type: "fixed", length: 16, type: "b" },
};

describe("de-values model", () => {
  it("reads values stored as an object or as a JSON string, sorted by DE", () => {
    const fromObj = rowsFromValue({
      "11": "000102",
      "2": "${vars.pan}",
      "4": "000000010000",
    });
    const fromStr = rowsFromValue(
      '{"11":"000102","2":"${vars.pan}","4":"000000010000"}',
    );
    expect(fromObj).toEqual([
      { de: "2", value: "${vars.pan}" },
      { de: "4", value: "000000010000" },
      { de: "11", value: "000102" },
    ]);
    expect(fromStr).toEqual(fromObj);
    expect(rowsFromValue(undefined)).toEqual([]);
    expect(rowsFromValue("")).toEqual([]);
    expect(rowsFromValue("{not json")).toBe(null);
    expect(rowsFromValue("[1,2]")).toBe(null);
  });

  it("writes an object in DE order, dropping blank DEs", () => {
    expect(
      valueFromRows([
        { de: "11", value: "${seq.stan(6)}" },
        { de: " ", value: "x" },
        { de: "3", value: "000000" },
      ]),
    ).toEqual({ "3": "000000", "11": "${seq.stan(6)}" });
    expect(
      Object.keys(
        valueFromRows([
          { de: "41", value: "a" },
          { de: "4", value: "b" },
        ]),
      ),
    ).toEqual(["4", "41"]);
  });

  it("validates literals with the codec's wording and skips references", () => {
    expect(validateRow({ de: "4", value: "0000000002A" }, TABLE["4"])).toBe(
      "DE4 expected 12 chars, got 11",
    );
    expect(validateRow({ de: "4", value: "000000010000" }, TABLE["4"])).toBe(
      null,
    );
    expect(validateRow({ de: "3", value: "01000A" }, TABLE["3"])).toBe(
      "DE3 is numeric (n): digits only",
    );
    expect(
      validateRow({ de: "2", value: "41111111111111111111" }, TABLE["2"]),
    ).toBe("DE2 allows at most 19 chars, got 20");
    expect(
      validateRow({ de: "2", value: "4111111111111111" }, TABLE["2"]),
    ).toBe(null);
    expect(
      validateRow({ de: "52", value: "ZZ00000000000000" }, TABLE["52"]),
    ).toBe("DE52 is binary (b): hex digits only");
    expect(
      validateRow({ de: "11", value: "${seq.stan(6)}" }, TABLE["11"]),
    ).toBe(null);
    expect(validateRow({ de: "11", value: "" }, TABLE["11"])).toBe(
      "DE11 is empty",
    );
    expect(validateRow({ de: "99", value: "x" }, undefined)).toBe(null);
  });

  it("flags DEs missing from the table, and literal STAN / time values", () => {
    expect(unknownDe("24", TABLE)).toBeTrue();
    expect(unknownDe("11", TABLE)).toBeFalse();
    expect(unknownDe("24", {})).toBeFalse(); // no table loaded yet: no claim
    expect(isStaticStan({ de: "11", value: "000102" })).toBeTrue();
    expect(isStaticStan({ de: "11", value: "${seq.stan(6)}" })).toBeFalse();
    expect(isStaticTime({ de: "7", value: "1011150001" })).toBeTrue();
    expect(isStaticTime({ de: "12", value: "${now.fmt(%H%M%S)}" })).toBeFalse();
    expect(isStaticTime({ de: "4", value: "000000010000" })).toBeFalse();
  });

  it("describes a DE's shape in a few characters", () => {
    expect(formatHint(TABLE["4"])).toBe("n 12 fixed");
    expect(formatHint(TABLE["2"])).toBe("n ≤ 19 llvar");
    expect(formatHint(undefined)).toBe("");
  });

  it("searches by number prefix or name, excluding present DEs", () => {
    expect(searchDes(TABLE, "4", []).map((m) => m.de)).toEqual(["4", "41"]);
    expect(searchDes(TABLE, "DE 4", ["4"]).map((m) => m.de)).toEqual(["41"]);
    expect(searchDes(TABLE, "terminal", []).map((m) => m.de)).toEqual(["41"]);
    expect(searchDes(TABLE, "", ["2", "3"]).map((m) => m.de)).toEqual([
      "4",
      "11",
      "41",
      "52",
    ]);
  });

  it("picks the step's table, then the dialect's, then the stock one", () => {
    const stock = { "2": { name: "stock" } };
    const dialect = { "2": { name: "dialect" } };
    const step = { "2": { name: "step" } };
    expect(activeTable(step, dialect, stock).source).toBe("step");
    expect(activeTable({}, dialect, stock).source).toBe("dialect");
    expect(activeTable(undefined, undefined, stock).source).toBe("stock");
    expect(activeTable(null, null, null)).toEqual({
      table: {},
      source: "none",
    });
  });

  it("pads short fixed literals like the codec, and says so", () => {
    const ans15 = { len_type: "fixed", length: 15, type: "ans" };
    expect(padFixed("10000", TABLE["4"])).toBe("000000010000");
    expect(padFixed("MERCH01", ans15)).toBe("MERCH01        ");
    expect(padFixed("0000000002A", TABLE["4"])).toBe("0000000002A");
    expect(padFixed("${rand.amount(1,9)}", TABLE["4"])).toBe(
      "${rand.amount(1,9)}",
    );
    expect(padFixed("4111", TABLE["2"])).toBe("4111"); // llvar
    expect(padFixed("ABCD", TABLE["52"])).toBe("ABCD"); // b: no fill
    expect(validateRow({ de: "4", value: "10000" }, TABLE["4"])).toBe(null);
    expect(validateRow({ de: "42", value: "MERCH01" }, ans15)).toBe(null);
    expect(paddingNote({ de: "4", value: "10000" }, TABLE["4"])).toBe(
      "Sent as 000000010000: 7 zeros added on the left.",
    );
    expect(paddingNote({ de: "42", value: "MERCH01" }, ans15)).toBe(
      'Sent as "MERCH01        ": 8 spaces added on the right.',
    );
    expect(paddingNote({ de: "4", value: "000000010000" }, TABLE["4"])).toBe(
      null,
    );
  });
});
