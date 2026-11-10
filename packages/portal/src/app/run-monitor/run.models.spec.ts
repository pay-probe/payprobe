import { phaseOneProblems } from "./run.models";

describe("phaseOneProblems", () => {
  it("names the unhealthy components, failed fixtures and retry recoveries", () => {
    expect(
      phaseOneProblems({
        status: "failed",
        components: 3,
        failed: ["hsm_primary"],
        fixture_failed: ["seed-accounts"],
        recovered: ["pos_host"],
      }),
    ).toEqual({
      unhealthy: ["hsm_primary"],
      fixtures: ["seed-accounts"],
      recovered: ["pos_host"],
    });
  });

  it("ignores the numeric counts of phases 2 and 3 and missing data", () => {
    expect(phaseOneProblems({ status: "failed", total: 4, failed: 1 })).toEqual(
      {
        unhealthy: [],
        fixtures: [],
        recovered: [],
      },
    );
    expect(phaseOneProblems(null).unhealthy).toEqual([]);
  });
});
