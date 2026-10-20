import type { GeometryVersion } from "../domain";

export type GeometryVersionSummary = {
  id: string;
  version: number;
  status: GeometryVersion["status"];
  source: GeometryVersion["source"];
  confidence: GeometryVersion["confidence"];
  isCurrent: boolean;
};

export function selectGeometryVersionSummary(
  versions: readonly GeometryVersionSummary[],
  selectedId: string,
): GeometryVersionSummary | null {
  return versions.find((version) => version.id === selectedId) ?? null;
}

export function geometryApprovalBlockers(version: GeometryVersion | null): string[] {
  if (!version) return ["Reconstruct or trace geometry first."];
  const blockers: string[] = [];
  if (version.isCurrent !== false && version.status === "approved") {
    blockers.push("This approved revision is immutable.");
  }
  if (version.isCurrent === false) {
    blockers.push("Only the current revision can be approved.");
  }
  if (version.status === "processing") blockers.push("Geometry processing is still running.");
  if (version.status === "failed") blockers.push("Failed geometry cannot be approved.");

  const facets = version.analysis.facets;
  if (!facets.length) blockers.push("No planes are available.");
  for (const facet of facets) {
    if (!facet.pitch) blockers.push(`${facet.name} has no pitch.`);
    const direction = facet.downslopeDirectionPage;
    if (!direction || direction.length !== 2 || Math.hypot(...direction) <= 1e-8) {
      blockers.push(`${facet.name} has no downslope direction.`);
    }
  }

  if (version.geometry?.heightSolve?.status !== "solved") {
    blockers.push("Shared-edge height constraints are incomplete or contradictory.");
  }
  if (version.analysis.validation?.buildDiagnostics?.unresolvedOverlaps?.length) {
    blockers.push("Overlapping planes need a reviewed stacking order.");
  }
  for (const check of version.analysis.validation?.checks ?? []) {
    if (check.status !== "pass") blockers.push(`${check.name} is ${check.status}.`);
  }
  return [...new Set(blockers)];
}
