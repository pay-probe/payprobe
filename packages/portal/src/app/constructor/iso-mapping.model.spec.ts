import {
  buildPresence,
  copyRule,
  markOf,
  mtiLabel,
  nextMark,
  parseMtis,
  parsePresence,
  setMark,
  sortDes,
} from "./iso-mapping.model";

describe("iso-mapping model", () => {
  const stored = {
    "0100": { mandatory: [2, "3", "11"] },
    "0400": { mandatory: ["7", "11"], conditional: ["90"], note: "kept" },
    "0800": {},
    default: { mandatory: ["11"] },
  };

  it("parses presence as string DEs, numerically sorted, default last", () => {
    const rules = parsePresence(stored);
    expect(rules.map((r) => r.key)).toEqual(["0100", "0400", "default"]);
    expect(rules[0].mandatory).toEqual(["2", "3", "11"]);
  });

  it("drops an empty {} rule, which both validators treat as default", () => {
    expect(parsePresence(stored).some((r) => r.key === "0800")).toBeFalse();
  });

  it("round-trips a rule's keys it does not own", () => {
    const back = buildPresence(parsePresence(stored))!;
    expect(back["0400"]).toEqual({
      note: "kept",
      mandatory: ["7", "11"],
      conditional: ["90"],
    });
  });

  it("writes no presence key when there are no rules", () => {
    expect(buildPresence([])).toBeUndefined();
  });

  it("writes an own rule with nothing marked as an explicit empty mandatory list", () => {
    expect(buildPresence([copyRule("0200")])!["0200"]).toEqual({
      mandatory: [],
    });
  });

  it("copies default into a new MTI rule and cycles a cell none, M, C, none", () => {
    const def = parsePresence(stored).find((r) => r.key === "default")!;
    const r = copyRule("0220", def);
    expect(markOf(r, "11")).toBe("M");
    setMark(r, "11", nextMark(markOf(r, "11")));
    expect(markOf(r, "11")).toBe("C");
    setMark(r, "11", nextMark(markOf(r, "11")));
    expect(markOf(r, "11")).toBe("");
    expect(def.mandatory).toEqual(["11"]);
  });

  it("decodes MTIs for the labels", () => {
    expect(mtiLabel("0100")).toBe("Authorization request");
    expect(mtiLabel("0421")).toBe(
      "Reversal / chargeback advice (acquirer repeat)",
    );
    expect(mtiLabel("02x0")).toBe("");
  });

  it("cleans the MTI list and keeps its order", () => {
    expect(parseMtis(["0200", "0100", "0200", ""])).toEqual(["0200", "0100"]);
    expect(parseMtis("0200")).toEqual([]);
  });

  it("sorts DE numbers numerically", () => {
    expect(sortDes(["128", "2", "11", 2])).toEqual(["2", "11", "128"]);
  });
});
