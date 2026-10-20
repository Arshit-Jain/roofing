import assert from "node:assert/strict";
import test from "node:test";
import type { GeometryVersion } from "../domain";
import {
  geometryApprovalBlockers,
  selectGeometryVersionSummary,
  type GeometryVersionSummary,
} from "./geometry-review";

const summary = (id: string, version: number, isCurrent: boolean): GeometryVersionSummary => ({
  id,
  version,
  status: isCurrent ? "needs_review" : "approved",
  source: "manual",
  confidence: "medium",
  isCurrent,
});

function reviewVersion(overrides: Partial<GeometryVersion> = {}): GeometryVersion {
  return {
    id: "g-current",
    projectId: "project-1",
    sourceReportId: "report-1",
    status: "needs_review",
    source: "manual",
    confidence: "medium",
    estimatedHeights: true,
    version: 2,
    basedOnVersionId: "g-old",
    isCurrent: true,
    analysis: {
      status: "needs_review",
      sourceFormat: "roofr",
      facets: [
        {
          id: "plane-1",
          name: "Plane 1",
          roofType: "pitched",
          areaLabelSqFt: 100,
          polygonPagePoints: [[0, 0], [10, 0], [10, 10]],
          pitch: { notation: "4:12", rise: 4, run: 12 },
          downslopeDirectionPage: [0, 1],
        },
      ],
      validation: { checks: [{ name: "area", status: "pass", actual: 100, expected: 100 }] },
      warnings: [],
    },
    geometry: {
      schemaVersion: "1.1",
      geometryId: "candidate",
      status: "needs_review",
      source: "manual",
      confidence: "medium",
      estimatedHeights: true,
      units: "feet",
      coordinateSystem: {
        planAxes: "u/v",
        heightAxis: "relative",
        threeJsMapping: "X=u, Y=h, Z=-v",
        orientation: "report",
      },
      compass: { north: "-u", east: "+v", south: "+u", west: "-v", source: "report" },
      sourceReference: { viewer: "", report: "sample.pdf", method: "manual" },
      heightModel: {
        formula: "pitch",
        manualHeightOverrides: [],
        flatDrainPitch: { rise: 1, run: 12, direction: "arrow" },
        wallGroundPlaneHeightFt: -8,
        wallGroundPlaneIsEstimated: true,
      },
      vertices: [],
      planes: [],
      edges: [],
      wallLoops: [],
      overlapOrder: [],
      omittedDiagramAreasSqFt: [],
      areaValidationTolerancePercent: 5,
      validationWarnings: [],
      heightSolve: { status: "solved", failures: [] },
    },
    approvalBlockers: [],
    ...overrides,
  };
}

test("revision selection resolves the chosen version and its current/history state", () => {
  const versions = [summary("g-old", 1, false), summary("g-current", 2, true)];
  assert.equal(selectGeometryVersionSummary(versions, "g-current")?.version, 2);
  assert.equal(selectGeometryVersionSummary(versions, "g-old")?.isCurrent, false);
  assert.equal(selectGeometryVersionSummary(versions, "missing"), null);
});

test("approval stays blocked for missing directions, warnings, and historical revisions", () => {
  const incomplete = reviewVersion({
    isCurrent: false,
    analysis: {
      ...reviewVersion().analysis,
      facets: [{ ...reviewVersion().analysis.facets[0], downslopeDirectionPage: null }],
      validation: { checks: [{ name: "scale", status: "warn", actual: null, expected: null }] },
    },
  });
  const blockers = geometryApprovalBlockers(incomplete);
  assert.ok(blockers.some((item) => item.includes("current revision")));
  assert.ok(blockers.some((item) => item.includes("no downslope direction")));
  assert.ok(blockers.some((item) => item.includes("scale is warn")));
});

test("approval is available only for a solved current candidate with passing checks", () => {
  assert.deepEqual(geometryApprovalBlockers(reviewVersion()), []);
  assert.ok(geometryApprovalBlockers(reviewVersion({ status: "approved" })).length > 0);
  assert.ok(geometryApprovalBlockers(null).length > 0);
});
