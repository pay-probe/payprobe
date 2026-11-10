import {
  StepFramePreview,
  frameBlocks,
  printable,
  sentRows,
  showSpaces,
  spacedHex,
} from "./frame-preview.model";

const PREVIEW: StepFramePreview = {
  hex: "303031364344623430323030",
  total_bytes: 12,
  mode: "length_prefix",
  framing: "prefix=4 ascii · header=43446234/4in (counted)",
  prefix_hex: "30303136",
  declared_length: 16,
  tpdu_hex: "",
  header_hex: "43446234",
  body_hex: "3032303020534831",
  body_bytes: 8,
  trailer_hex: "",
  request: { mti: "0200", values: { "42": "SH1  ", "4": "000000000200" } },
};

describe("frame-preview model", () => {
  it("lists the frame parts in wire order and skips empty ones", () => {
    expect(frameBlocks(PREVIEW).map((b) => [b.kind, b.bytes])).toEqual([
      ["prefix", 4],
      ["header", 4],
      ["body", 8],
    ]);
    const delimited = frameBlocks({
      ...PREVIEW,
      mode: "delimited",
      start_hex: "02",
      end_hex: "03",
      checksum_hex: "7f",
    });
    expect(delimited.map((b) => b.kind)).toEqual([
      "start",
      "body",
      "end",
      "checksum",
    ]);
  });

  it("shows spaces and non-printable bytes visibly", () => {
    expect(printable("3032303020534831")).toBe("0200·SH1");
    expect(printable("8000")).toBe("..");
    expect(showSpaces("SH1  ")).toBe("SH1··");
    expect(spacedHex("3032ab")).toBe("30 32 AB");
  });

  it("orders the sent fields by DE number", () => {
    expect(sentRows(PREVIEW).map((r) => r.de)).toEqual(["4", "42"]);
  });
});
