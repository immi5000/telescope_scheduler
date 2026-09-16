/** Shared display helpers. Formatting lives in one place so a label cannot
 *  mean one thing on the timeline and another on the heatmap. */

const CATALOG_PREFIX = /^(NGC|IC|M|Sh2|Abell|Cr|Mel|Barnard|B|LDN|LBN|vdB)$/i;

/**
 * "NGC 7000 North America" -> "NGC 7000", "M31 Andromeda" -> "M31".
 *
 * Taking only the first token collapses NGC 7000 and NGC 7331 to the same
 * label, which puts two different objects under one heading on the timeline
 * and the heatmap -- a real misread, not a cosmetic one.
 */
export function shortName(name: string): string {
  const parts = name.trim().split(/\s+/);
  const first = parts[0] ?? name;
  if (parts.length > 1 && CATALOG_PREFIX.test(first) && /^\d/.test(parts[1] ?? "")) {
    return `${first} ${parts[1]}`;
  }
  return first;
}

export function hhmm(iso: string): string {
  return iso.slice(11, 16);
}
