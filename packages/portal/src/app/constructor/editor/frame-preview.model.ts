/** The reply of `POST /scenarios/step-frame-preview` (the TCP adapter's
 *  `preview_frame` plus the connection it resolved) and pure helpers to show
 *  it. No Angular here. */

export interface StepFramePreview {
  hex: string;
  total_bytes: number;
  mode: "length_prefix" | "delimited";
  framing: string;
  body_hex: string;
  body_bytes: number;
  prefix_hex?: string;
  declared_length?: number;
  tpdu_hex?: string;
  header_hex?: string;
  trailer_hex?: string;
  start_hex?: string;
  end_hex?: string;
  checksum_hex?: string;
  request?: { mti?: string; values?: Record<string, string> };
  connection?: string;
  environment?: string;
}

export interface FrameBlock {
  kind:
    | "prefix"
    | "tpdu"
    | "header"
    | "start"
    | "body"
    | "end"
    | "checksum"
    | "trailer";
  label: string;
  hex: string;
  bytes: number;
}

/** The frame's parts in wire order, empty parts left out. */
export function frameBlocks(p: StepFramePreview): FrameBlock[] {
  const raw: [FrameBlock["kind"], string, string | undefined][] =
    p.mode === "delimited"
      ? [
          ["start", "Start", p.start_hex],
          ["body", "Message", p.body_hex],
          ["end", "End", p.end_hex],
          ["checksum", "Checksum", p.checksum_hex],
        ]
      : [
          ["prefix", "Length", p.prefix_hex],
          ["tpdu", "TPDU", p.tpdu_hex],
          ["header", "Header", p.header_hex],
          ["body", "Message", p.body_hex],
          ["trailer", "Trailer", p.trailer_hex],
        ];
  return raw
    .filter(([, , hex]) => !!hex)
    .map(([kind, label, hex]) => ({
      kind,
      label,
      hex: hex as string,
      bytes: (hex as string).length / 2,
    }));
}

/** Hex as byte pairs separated by spaces ("30 32 30 30"). */
export function spacedHex(hex: string): string {
  return (hex.match(/.{1,2}/g) ?? []).join(" ").toUpperCase();
}

/** Bytes as printable text: a space shows as "·", a byte outside printable
 *  ASCII as "." (a binary bitmap, a BCD field), so padding and binary parts
 *  are visible at a glance. */
export function printable(hex: string): string {
  let out = "";
  for (let i = 0; i + 1 < hex.length; i += 2) {
    const b = parseInt(hex.slice(i, i + 2), 16);
    if (b === 0x20) out += "·";
    else if (b > 0x20 && b < 0x7f) out += String.fromCharCode(b);
    else out += ".";
  }
  return out;
}

/** A value with its spaces made visible ("SHOP1··········"). */
export function showSpaces(v: string): string {
  return v.replace(/ /g, "·");
}

/** The sent values in DE order. */
export function sentRows(p: StepFramePreview): { de: string; value: string }[] {
  return Object.entries(p.request?.values ?? {})
    .map(([de, value]) => ({ de, value: String(value) }))
    .sort((a, b) => Number(a.de) - Number(b.de));
}
