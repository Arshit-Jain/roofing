import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import {
  mapPlanToThree,
  migrateRoofGeometry,
  geometryFor3DPreview,
  oneStoryWallMeshes,
  planeToMesh,
  validateGeometry,
  wallBaseToMesh,
  type RoofGeometry,
} from "./geometry";

const fixturePath = process.env.ROOF_DEMO_FIXTURE_PATH;
assert.ok(fixturePath, "ROOF_DEMO_FIXTURE_PATH must point to the bundled geometry fixture");
const originalFixture = JSON.parse(readFileSync(resolve(fixturePath), "utf8")) as unknown;
const geometry = migrateRoofGeometry(originalFixture);

function findPlane(id: string) {
  const plane = geometry.planes.find((candidate) => candidate.id === id);
  assert.ok(plane, `Expected fixture plane ${id}`);
  return plane;
}

function heightAt(planeId: string, point: readonly [number, number]): number {
  const plane = findPlane(planeId);
  const mesh = planeToMesh(geometry, plane);
  for (let index = 0; index < mesh.indices.length; index += 3) {
    const [a, b, c] = mesh.indices.slice(index, index + 3).map((vertexIndex) => {
      const vertexId = plane.boundaryVertexIds[vertexIndex];
      const vertex = geometry.vertices.find((candidate) => candidate.id === vertexId);
      assert.ok(vertex);
      return vertex.plan;
    });
    const denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]);
    if (Math.abs(denominator) < 1e-9) continue;
    const wa = ((b[1] - c[1]) * (point[0] - c[0]) + (c[0] - b[0]) * (point[1] - c[1])) / denominator;
    const wb = ((c[1] - a[1]) * (point[0] - c[0]) + (a[0] - c[0]) * (point[1] - c[1])) / denominator;
    const wc = 1 - wa - wb;
    if (wa >= -1e-7 && wb >= -1e-7 && wc >= -1e-7) return wa * a[2] + wb * b[2] + wc * c[2];
  }
  throw new Error(`${point.join(", ")} is outside ${planeId}`);
}

test("plan coordinates map to three.js without rotating the report north-up", () => {
  assert.equal(geometry.schemaVersion, "1.2", "legacy geometry migrates forward with provenance");
  assert.deepEqual(mapPlanToThree([4, 7, 2]), [4, 2, -7]);
  assert.deepEqual(geometry.compass, {
    north: "-u",
    east: "+v",
    south: "+u",
    west: "-v",
    source: "Compass printed on Roofr Diagram page 2",
  });
});

test("shared roof edges reuse vertex IDs and therefore one height", () => {
  const vertexById = new Map(geometry.vertices.map((vertex) => [vertex.id, vertex]));
  const sharedEdges = geometry.edges.filter((edge) => edge.adjacentPlaneIds.length > 1);
  assert.equal(sharedEdges.length, 5);
  for (const edge of sharedEdges) {
    for (const vertexId of edge.vertexIds) {
      const vertex = vertexById.get(vertexId);
      assert.ok(vertex);
      const [u, v, h] = vertex.plan;
      assert.equal(vertexById.get(vertexId)?.plan[2], h, `${vertexId} at (${u}, ${v}) must have one height`);
    }
  }
});

test("nominal pitch area checks use a configurable tolerance and remain warnings", () => {
  const validation = validateGeometry(geometry, { areaTolerancePercent: 5 });
  assert.equal(validation.valid, true, validation.errors.join("\n"));
  assert.equal(validation.areaChecks.length, 6);
  for (const plane of geometry.planes.filter((candidate) => candidate.roofType === "pitched")) {
    const check = validation.areaChecks.find((candidate) => candidate.planeId === plane.id);
    assert.ok(check);
    assert.ok(check.differencePercent <= 5, `${plane.id}: ${check.differencePercent}%`);
  }
  assert.ok(validation.warnings.some((warning) => warning.includes("flat-small-1-12")));
  assert.ok(validateGeometry(geometry, { areaTolerancePercent: 2 }).warnings.length > validation.warnings.length);
});

test("stored plan areas, mesh areas, and computed edge lengths follow the fixture vertices", () => {
  for (const plane of geometry.planes) {
    const mesh = planeToMesh(geometry, plane);
    assert.ok(Math.abs(mesh.planAreaSqFt - plane.planAreaSqFt) < 0.01, plane.id);
    assert.ok(Math.abs(mesh.surfaceAreaSqFt - plane.meshSurfaceAreaSqFt) < 0.01, plane.id);
    assert.equal(mesh.indices.length, (plane.boundaryVertexIds.length - 2) * 3);
    assert.equal(mesh.outlinePositions.length, plane.boundaryVertexIds.length * 6);
  }
  const validation = validateGeometry(geometry);
  assert.equal(validation.valid, true, validation.errors.join("\n"));
  assert.ok(!validation.warnings.some((warning) => warning.includes("stored length differs")));
});

test("top-down overlaps follow the reference stacking order", () => {
  assert.equal(geometry.overlapOrder.length, 3);
  for (const overlap of geometry.overlapOrder) {
    assert.ok(
      heightAt(overlap.abovePlaneId, overlap.witnessPlan) > heightAt(overlap.belowPlaneId, overlap.witnessPlan),
      overlap.reason,
    );
  }
});

test("3D previews require complete solved heights and use those heights", () => {
  assert.equal(geometryFor3DPreview(geometry), null, "unsolved fixtures must not use guessed heights");
  const shiftedHeights = Object.fromEntries(
    geometry.vertices.map((vertex) => [vertex.id, vertex.plan[2] + 2]),
  );
  const solvedGeometry: RoofGeometry = {
    ...geometry,
    heightSolve: { status: "solved", vertexHeights: shiftedHeights },
  };
  const preview = geometryFor3DPreview(solvedGeometry);
  assert.ok(preview, "complete solved heights should render");
  for (const vertex of preview.vertices) {
    assert.equal(vertex.plan[2], shiftedHeights[vertex.id]);
  }

  const incomplete: RoofGeometry = {
    ...solvedGeometry,
    heightSolve: {
      status: "solved",
      vertexHeights: Object.fromEntries(Object.entries(shiftedHeights).slice(1)),
    },
  };
  assert.equal(geometryFor3DPreview(incomplete), null, "partial solver output must not render");

  const conflicting: RoofGeometry = {
    ...solvedGeometry,
    heightSolve: {
      status: "solved",
      vertexHeights: Object.fromEntries(geometry.vertices.map((vertex) => [vertex.id, 0])),
    },
  };
  assert.equal(
    geometryFor3DPreview(conflicting),
    null,
    "solved status cannot override an unsatisfied report stacking order",
  );
});

test("one-story walls stop at the 8-ft plate and rake edges add gable infill", () => {
  const wallGeometry: RoofGeometry = {
    ...geometry,
    vertices: [
      { id: "a", plan: [0, 0, 0] },
      { id: "b", plan: [10, 0, 0] },
      { id: "c", plan: [10, 5, 5] },
      { id: "d", plan: [0, 5, 5] },
    ],
    edges: [
      { id: "eave", vertexIds: ["a", "b"], type: "eave", adjacentPlaneIds: ["plane"], planLengthFt: 10, lengthFt: 10 },
      { id: "rake-right", vertexIds: ["b", "c"], type: "rake", adjacentPlaneIds: ["plane"], planLengthFt: 5, lengthFt: 5 },
      { id: "ridge", vertexIds: ["c", "d"], type: "ridge", adjacentPlaneIds: ["plane", "other-plane"], planLengthFt: 10, lengthFt: 10 },
      { id: "rake-left", vertexIds: ["d", "a"], type: "rake", adjacentPlaneIds: ["plane"], planLengthFt: 5, lengthFt: 5 },
    ],
    wallLoops: [{
      id: "one-floor",
      label: "Estimated walls",
      boundaryVertexIds: ["a", "b", "c", "d"],
      groundPlaneHeightFt: -8,
      estimated: true,
    }],
  };
  const meshes = oneStoryWallMeshes(wallGeometry);
  assert.equal(meshes.floorHeightFt, -8);
  assert.equal(meshes.wallPlateHeightFt, 0);
  assert.equal(meshes.walls.length, 18, "the eave makes two rectangular wall triangles");
  assert.equal(meshes.gables.length, 18, "the two rake edges make two gable triangles");
  const wallHeights = meshes.walls.filter((_, index) => index % 3 === 1);
  assert.ok(wallHeights.every((height) => height === -8 || height === 0));
  const gableHeights = meshes.gables.filter((_, index) => index % 3 === 1);
  assert.ok(gableHeights.every((height) => height >= 0));
  assert.ok(Math.max(...gableHeights) === 5);
});

test("fixed base slabs extend below the estimated one-story floor plane", () => {
  assert.equal(geometry.estimatedHeights, true);
  assert.equal(geometry.wallLoops.length, 3);
  for (const loop of geometry.wallLoops) {
    assert.equal(loop.estimated, true);
    assert.equal(loop.groundPlaneHeightFt, -9);
    const base = wallBaseToMesh(geometry, loop);
    assert.ok(base.length > 0);
    const heights = base.filter((_, index) => index % 3 === 1);
    assert.ok(heights.every((height) => height === -9 || height === -9.5));
  }
});
