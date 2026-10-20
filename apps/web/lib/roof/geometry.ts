import { ShapeUtils, Vector2, Vector3 } from "three";

export type PlanPoint = readonly [u: number, v: number, h: number];
export type ThreePoint = readonly [x: number, y: number, z: number];
export type RoofType = "flat" | "pitched";
export type GeometryEdgeType = "eave" | "rake" | "ridge" | "hip" | "valley" | "wall_flashing" | "step_flashing" | "curb_flashing" | "transition" | "unspecified" | "parapet" | "other";
export type GeometryProvenance = "reported" | "inferred" | "user_edited";
export type PlaneGeometryProvenance = Partial<Record<"outline" | "pitch" | "downslopeDirection" | "area", GeometryProvenance>>;

export type GeometryVertex = { id: string; plan: PlanPoint };

export type GeometryPitch = {
  notation: string;
  rise: number;
  run: number;
  degrees: number;
  downslopePlanUnitVector: readonly [number, number];
};

export type GeometryPlane = {
  id: string;
  name: string;
  roofType: RoofType;
  boundaryVertexIds: string[];
  edgeIds: string[];
  pitch: GeometryPitch;
  reportedAreaSqFt: number | null;
  planAreaSqFt: number;
  slopeAdjustedAreaSqFt: number;
  meshSurfaceAreaSqFt: number;
  textureRepeatFeet: number;
  drawOrder: number;
  provenance?: PlaneGeometryProvenance;
  confidence?: number;
};

export type GeometryEdge = {
  id: string;
  vertexIds: readonly [string, string];
  type: GeometryEdgeType;
  adjacentPlaneIds: string[];
  planLengthFt: number;
  lengthFt: number;
};

export type GeometryWallLoop = {
  id: string;
  label: string;
  boundaryVertexIds: string[];
  groundPlaneHeightFt: number;
  estimated: boolean;
};

export const BASE_SLAB_THICKNESS_FT = 0.5;

export type GeometryOverlap = {
  abovePlaneId: string;
  belowPlaneId: string;
  witnessPlan: readonly [number, number];
  reason: string;
};

export type RoofGeometry = {
  schemaVersion: "1.0" | "1.1" | "1.2";
  geometryId: string;
  status: "demo" | "needs_review" | "approved" | "failed";
  source: "demo" | "extracted" | "assisted" | "manual";
  confidence: "illustrative" | "low" | "medium" | "high";
  estimatedHeights: boolean;
  units: "feet";
  coordinateSystem: {
    planAxes: string;
    heightAxis: string;
    threeJsMapping: string;
    orientation: string;
  };
  compass: Record<"north" | "east" | "south" | "west" | "source", string>;
  sourceReference: { viewer: string; report: string; method: string };
  heightModel: {
    formula: string;
    manualHeightOverrides: Array<{ vertexId: string; heightFt: number; reason: string }>;
    flatDrainPitch: { rise: number; run: number; direction: string };
    wallGroundPlaneHeightFt: number;
    wallGroundPlaneIsEstimated: boolean;
  };
  vertices: GeometryVertex[];
  planes: GeometryPlane[];
  edges: GeometryEdge[];
  wallLoops: GeometryWallLoop[];
  heightSolve?: {
    status: string;
    vertexHeights?: Record<string, number>;
    failures?: string[];
  };
  overlapOrder: GeometryOverlap[];
  omittedDiagramAreasSqFt: number[];
  areaValidationTolerancePercent: number;
  validationWarnings: string[];
};

export function migrateRoofGeometry(value: unknown): RoofGeometry {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Roof geometry must be an object.");
  }
  const geometry = value as RoofGeometry;
  if (geometry.schemaVersion === "1.2") return geometry;
  if (geometry.schemaVersion !== "1.0" && geometry.schemaVersion !== "1.1") {
    throw new Error(`Unsupported roof geometry schema ${String(geometry.schemaVersion)}.`);
  }
  return {
    ...geometry,
    schemaVersion: "1.2",
    planes: geometry.planes.map((plane) => ({
      ...plane,
      reportedAreaSqFt:
        typeof plane.reportedAreaSqFt === "number" && Number.isFinite(plane.reportedAreaSqFt)
          ? plane.reportedAreaSqFt
          : null,
      provenance: plane.provenance ?? {
        outline: "reported",
        pitch: "reported",
        downslopeDirection: "reported",
        area: "reported",
      },
      confidence: typeof plane.confidence === "number" && Number.isFinite(plane.confidence)
        ? Math.max(0, Math.min(1, plane.confidence))
        : geometry.confidence === "high" ? 0.95 : geometry.confidence === "medium" ? 0.75 : 0.5,
    })),
  };
}

export type PlaneMesh = {
  positions: number[];
  uvs: number[];
  indices: number[];
  outlinePositions: number[];
  planAreaSqFt: number;
  slopeAdjustedAreaSqFt: number;
  surfaceAreaSqFt: number;
};

export type GeometryValidation = {
  valid: boolean;
  errors: string[];
  warnings: string[];
  areaChecks: Array<{
    planeId: string;
    reportedAreaSqFt: number;
    slopeAdjustedAreaSqFt: number;
    differencePercent: number;
  }>;
};

export type GeometryValidationOptions = { areaTolerancePercent?: number };

export function mapPlanToThree([u, v, h]: PlanPoint): ThreePoint {
  return [u, h, -v];
}

export function geometryFor3DPreview(geometry: RoofGeometry): RoofGeometry | null {
  const solvedHeights = geometry.heightSolve?.vertexHeights;
  if (geometry.heightSolve?.status !== "solved" || !solvedHeights) return null;
  if (geometry.vertices.length === 0 || geometry.planes.length === 0) return null;
  if (!geometry.vertices.every((vertex) => Number.isFinite(solvedHeights[vertex.id]))) return null;

  const solvedGeometry: RoofGeometry = {
    ...geometry,
    vertices: geometry.vertices.map((vertex) => ({
      ...vertex,
      plan: [vertex.plan[0], vertex.plan[1], solvedHeights[vertex.id]] as const,
    })),
  };
  for (const overlap of solvedGeometry.overlapOrder) {
    const above = solvedGeometry.planes.find((plane) => plane.id === overlap.abovePlaneId);
    const below = solvedGeometry.planes.find((plane) => plane.id === overlap.belowPlaneId);
    if (!above || !below) return null;
    const aboveHeight = pointHeightInPlane(solvedGeometry, above, overlap.witnessPlan);
    const belowHeight = pointHeightInPlane(solvedGeometry, below, overlap.witnessPlan);
    if (aboveHeight === null || belowHeight === null || aboveHeight <= belowHeight) return null;
  }
  return solvedGeometry;
}

function area2D(points: readonly (readonly [number, number])[]): number {
  let doubled = 0;
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index];
    const next = points[(index + 1) % points.length];
    doubled += current[0] * next[1] - next[0] * current[1];
  }
  return Math.abs(doubled) / 2;
}

function triangleArea3D(a: ThreePoint, b: ThreePoint, c: ThreePoint): number {
  const first = new Vector3(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
  const second = new Vector3(c[0] - a[0], c[1] - a[1], c[2] - a[2]);
  return first.cross(second).length() / 2;
}

function vertexMap(geometry: RoofGeometry): Map<string, GeometryVertex> {
  return new Map(geometry.vertices.map((vertex) => [vertex.id, vertex]));
}

export function planeToMesh(geometry: RoofGeometry, plane: GeometryPlane): PlaneMesh {
  const vertices = vertexMap(geometry);
  const sourceVertices = plane.boundaryVertexIds.map((id) => {
    const vertex = vertices.get(id);
    if (!vertex) throw new Error(`Plane ${plane.id} references missing vertex ${id}`);
    return vertex;
  });
  if (sourceVertices.length < 3) throw new Error(`Plane ${plane.id} needs at least three boundary vertices`);

  const contour = sourceVertices.map(({ plan: [u, v] }) => new Vector2(u, v));
  const triangles = ShapeUtils.triangulateShape(contour, []);
  if (triangles.length !== sourceVertices.length - 2) {
    throw new Error(`Plane ${plane.id} could not be triangulated as a simple polygon`);
  }

  const positions: number[] = [];
  const uvs: number[] = [];
  const outlinePositions: number[] = [];
  const repeatFeet = plane.textureRepeatFeet > 0 ? plane.textureRepeatFeet : 1;
  const mapped = sourceVertices.map((vertex) => {
    const point = mapPlanToThree(vertex.plan);
    positions.push(...point);
    uvs.push(vertex.plan[0] / repeatFeet, vertex.plan[1] / repeatFeet);
    return point;
  });
  for (let index = 0; index < mapped.length; index += 1) {
    outlinePositions.push(...mapped[index], ...mapped[(index + 1) % mapped.length]);
  }
  const indices = triangles.flatMap((triangle) => triangle);
  const surfaceAreaSqFt = triangles.reduce(
    (total, [a, b, c]) => total + triangleArea3D(mapped[a], mapped[b], mapped[c]),
    0,
  );
  const planAreaSqFt = area2D(sourceVertices.map(({ plan: [u, v] }) => [u, v] as const));
  const slopeRatio = plane.pitch.rise / plane.pitch.run;

  return {
    positions,
    uvs,
    indices,
    outlinePositions,
    planAreaSqFt,
    slopeAdjustedAreaSqFt: planAreaSqFt * Math.sqrt(1 + slopeRatio * slopeRatio),
    surfaceAreaSqFt,
  };
}

export type OneStoryWallMeshes = {
  walls: number[];
  gables: number[];
  floorHeightFt: number;
  wallPlateHeightFt: number;
};

export const ONE_STORY_WALL_HEIGHT_FT = 8;

function pushTriangle(target: number[], first: PlanPoint, second: PlanPoint, third: PlanPoint) {
  target.push(...mapPlanToThree(first), ...mapPlanToThree(second), ...mapPlanToThree(third));
}

export function oneStoryWallMeshes(geometry: RoofGeometry): OneStoryWallMeshes {
  const floorHeightFt = Number.isFinite(geometry.wallLoops[0]?.groundPlaneHeightFt)
    ? geometry.wallLoops[0].groundPlaneHeightFt
    : Number.isFinite(geometry.heightModel.wallGroundPlaneHeightFt)
      ? geometry.heightModel.wallGroundPlaneHeightFt
      : -ONE_STORY_WALL_HEIGHT_FT;
  const wallPlateHeightFt = floorHeightFt + ONE_STORY_WALL_HEIGHT_FT;
  const vertices = vertexMap(geometry);
  const walls: number[] = [];
  const gables: number[] = [];
  const tolerance = 0.05;

  for (const edge of geometry.edges) {
    if (edge.adjacentPlaneIds.length !== 1) continue;
    const first = vertices.get(edge.vertexIds[0]);
    const second = vertices.get(edge.vertexIds[1]);
    if (!first || !second) continue;
    const [u0, v0, h0] = first.plan;
    const [u1, v1, h1] = second.plan;
    const floorFirst: PlanPoint = [u0, v0, floorHeightFt];
    const floorSecond: PlanPoint = [u1, v1, floorHeightFt];
    const plateFirst: PlanPoint = [u0, v0, wallPlateHeightFt];
    const plateSecond: PlanPoint = [u1, v1, wallPlateHeightFt];

    const isEave = edge.type === "eave"
      || (edge.type === "other"
        && Math.abs(h0 - wallPlateHeightFt) <= tolerance
        && Math.abs(h1 - wallPlateHeightFt) <= tolerance);
    if (isEave) {
      pushTriangle(walls, floorFirst, plateFirst, plateSecond);
      pushTriangle(walls, floorFirst, plateSecond, floorSecond);
      continue;
    }

    if (edge.type !== "rake") continue;
    const roofFirst: PlanPoint = [u0, v0, h0];
    const roofSecond: PlanPoint = [u1, v1, h1];
    const firstAbove = h0 > wallPlateHeightFt + 1e-6;
    const secondAbove = h1 > wallPlateHeightFt + 1e-6;
    if (firstAbove && secondAbove) {
      pushTriangle(gables, roofFirst, roofSecond, plateSecond);
      pushTriangle(gables, roofFirst, plateSecond, plateFirst);
    } else if (firstAbove) {
      const fraction = (wallPlateHeightFt - h0) / (h1 - h0);
      const intersection: PlanPoint = [
        u0 + (u1 - u0) * fraction,
        v0 + (v1 - v0) * fraction,
        wallPlateHeightFt,
      ];
      pushTriangle(gables, roofFirst, plateFirst, intersection);
    } else if (secondAbove) {
      const fraction = (wallPlateHeightFt - h1) / (h0 - h1);
      const intersection: PlanPoint = [
        u1 + (u0 - u1) * fraction,
        v1 + (v0 - v1) * fraction,
        wallPlateHeightFt,
      ];
      pushTriangle(gables, plateSecond, roofSecond, intersection);
    }
  }

  return { walls, gables, floorHeightFt, wallPlateHeightFt };
}

export function roofWallLoops(geometry: RoofGeometry): GeometryWallLoop[] {
  const vertices = vertexMap(geometry);
  const incident = new Map<string, string[]>();
  const seenEdges = new Set<string>();
  for (const edge of geometry.edges) {
    if (edge.adjacentPlaneIds.length !== 1) continue;
    const [first, second] = edge.vertexIds;
    if (!vertices.has(first) || !vertices.has(second)) continue;
    const edgeKey = first < second ? `${first}\u0000${second}` : `${second}\u0000${first}`;
    if (seenEdges.has(edgeKey)) continue;
    seenEdges.add(edgeKey);
    incident.set(first, [...(incident.get(first) ?? []), second]);
    incident.set(second, [...(incident.get(second) ?? []), first]);
  }
  if (incident.size === 0) return geometry.wallLoops;

  const neighborsByAngle = new Map<string, string[]>();
  for (const [vertexId, neighbors] of incident) {
    const point = vertices.get(vertexId)!.plan;
    neighborsByAngle.set(
      vertexId,
      [...neighbors].sort((left, right) => {
        const leftPoint = vertices.get(left)!.plan;
        const rightPoint = vertices.get(right)!.plan;
        return Math.atan2(leftPoint[1] - point[1], leftPoint[0] - point[0])
          - Math.atan2(rightPoint[1] - point[1], rightPoint[0] - point[0]);
      }),
    );
  }

  const directedKey = (first: string, second: string) => `${first}\u0000${second}`;
  const visited = new Set<string>();
  const loops: GeometryWallLoop[] = [];
  for (const [start, neighbors] of incident) {
    for (const neighbor of neighbors) {
      const firstKey = directedKey(start, neighbor);
      if (visited.has(firstKey)) continue;
      const ordered: string[] = [];
      let current = start;
      let next = neighbor;
      let closed = false;
      for (let step = 0; step <= seenEdges.size * 2; step += 1) {
        const key = directedKey(current, next);
        if (visited.has(key)) break;
        visited.add(key);
        ordered.push(current);
        const angularNeighbors = neighborsByAngle.get(next) ?? [];
        const reverseIndex = angularNeighbors.indexOf(current);
        if (reverseIndex < 0 || angularNeighbors.length < 2) break;
        const following = angularNeighbors[
          (reverseIndex - 1 + angularNeighbors.length) % angularNeighbors.length
        ];
        if (directedKey(next, following) === firstKey) {
          closed = true;
          break;
        }
        current = next;
        next = following;
      }
      if (!closed || ordered.length < 3) continue;
      let signedArea = 0;
      for (let index = 0; index < ordered.length; index += 1) {
        const first = vertices.get(ordered[index])!.plan;
        const second = vertices.get(ordered[(index + 1) % ordered.length])!.plan;
        signedArea += first[0] * second[1] - second[0] * first[1];
      }
      if (signedArea <= 1e-6) continue;
      const groundPlaneHeightFt = Number.isFinite(
        geometry.wallLoops[0]?.groundPlaneHeightFt
          ?? geometry.heightModel.wallGroundPlaneHeightFt,
      )
        ? geometry.wallLoops[0]?.groundPlaneHeightFt
          ?? geometry.heightModel.wallGroundPlaneHeightFt
        : Math.min(...geometry.vertices.map(({ plan }) => plan[2])) - 8;
      loops.push({
        id: `estimated-one-floor-walls-${loops.length + 1}`,
        label: "Estimated one-floor exterior walls",
        boundaryVertexIds: ordered,
        groundPlaneHeightFt,
        estimated: true,
      });
    }
  }
  return loops.length > 0 ? loops : geometry.wallLoops;
}

export function wallBaseToMesh(
  geometry: RoofGeometry,
  wallLoop: GeometryWallLoop,
  thicknessFt = BASE_SLAB_THICKNESS_FT,
): number[] {
  const vertices = vertexMap(geometry);
  const boundary = wallLoop.boundaryVertexIds.map((id) => {
    const vertex = vertices.get(id);
    if (!vertex) throw new Error(`Wall loop ${wallLoop.id} references missing vertex ${id}`);
    return vertex;
  });
  if (boundary.length < 3) throw new Error(`Wall loop ${wallLoop.id} needs at least three vertices`);

  const contour = boundary.map(({ plan: [u, v] }) => new Vector2(u, v));
  const triangles = ShapeUtils.triangulateShape(contour, []);
  if (triangles.length !== boundary.length - 2) {
    throw new Error(`Wall loop ${wallLoop.id} could not be triangulated as a simple polygon`);
  }
  const top = boundary.map(({ plan: [u, v] }) =>
    mapPlanToThree([u, v, wallLoop.groundPlaneHeightFt]),
  );
  const bottom = boundary.map(({ plan: [u, v] }) =>
    mapPlanToThree([u, v, wallLoop.groundPlaneHeightFt - thicknessFt]),
  );
  const positions: number[] = [];
  for (const [a, b, c] of triangles) {
    positions.push(...top[a], ...top[b], ...top[c]);
    positions.push(...bottom[c], ...bottom[b], ...bottom[a]);
  }
  for (let index = 0; index < boundary.length; index += 1) {
    const next = (index + 1) % boundary.length;
    positions.push(
      ...top[index], ...bottom[index], ...bottom[next],
      ...top[index], ...bottom[next], ...top[next],
    );
  }
  return positions;
}

function pointHeightInPlane(
  geometry: RoofGeometry,
  plane: GeometryPlane,
  point: readonly [number, number],
): number | null {
  const mesh = planeToMesh(geometry, plane);
  const vertices = vertexMap(geometry);
  for (let index = 0; index < mesh.indices.length; index += 3) {
    const triangleIndices = mesh.indices.slice(index, index + 3);
    const triangle = triangleIndices.map((vertexIndex) => {
      const vertexId = plane.boundaryVertexIds[vertexIndex];
      return vertices.get(vertexId)!.plan;
    });
    const [[ax, ay, ah], [bx, by, bh], [cx, cy, ch]] = triangle;
    const denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
    if (Math.abs(denominator) < 1e-9) continue;
    const wa = ((by - cy) * (point[0] - cx) + (cx - bx) * (point[1] - cy)) / denominator;
    const wb = ((cy - ay) * (point[0] - cx) + (ax - cx) * (point[1] - cy)) / denominator;
    const wc = 1 - wa - wb;
    if (wa >= -1e-7 && wb >= -1e-7 && wc >= -1e-7) return wa * ah + wb * bh + wc * ch;
  }
  return null;
}

function edgeHasPlaneBoundary(plane: GeometryPlane, edge: GeometryEdge): boolean {
  const [first, second] = edge.vertexIds;
  return plane.boundaryVertexIds.some((id, index) => {
    const next = plane.boundaryVertexIds[(index + 1) % plane.boundaryVertexIds.length];
    return (id === first && next === second) || (id === second && next === first);
  });
}

function distance3D(a: PlanPoint, b: PlanPoint): number {
  const [ax, ay, az] = mapPlanToThree(a);
  const [bx, by, bz] = mapPlanToThree(b);
  return Math.hypot(bx - ax, by - ay, bz - az);
}

export function validateGeometry(
  geometry: RoofGeometry,
  options: GeometryValidationOptions = {},
): GeometryValidation {
  const areaTolerancePercent = options.areaTolerancePercent ?? geometry.areaValidationTolerancePercent;
  const errors: string[] = [];
  const warnings = [...geometry.validationWarnings];
  const areaChecks: GeometryValidation["areaChecks"] = [];
  const vertices = vertexMap(geometry);
  const planes = new Map(geometry.planes.map((plane) => [plane.id, plane]));
  const edges = new Map(geometry.edges.map((edge) => [edge.id, edge]));

  if (vertices.size !== geometry.vertices.length) errors.push("Vertex IDs must be unique.");
  if (planes.size !== geometry.planes.length) errors.push("Plane IDs must be unique.");
  if (edges.size !== geometry.edges.length) errors.push("Edge IDs must be unique.");
  if (!geometry.estimatedHeights) warnings.push("This fixture has no explicit estimated-height flag.");
  if (!Number.isFinite(areaTolerancePercent) || areaTolerancePercent < 0) {
    errors.push("Area validation tolerance must be a non-negative number.");
  }

  for (const plane of geometry.planes) {
    if (plane.boundaryVertexIds.length < 3) errors.push(`Plane ${plane.id} has fewer than three vertices.`);
    if (plane.pitch.run <= 0 || plane.pitch.rise < 0) errors.push(`Plane ${plane.id} has an invalid pitch.`);
    for (const edgeId of plane.edgeIds) {
      const edge = edges.get(edgeId);
      if (!edge) {
        errors.push(`Plane ${plane.id} references missing edge ${edgeId}.`);
      } else if (!edge.adjacentPlaneIds.includes(plane.id) || !edgeHasPlaneBoundary(plane, edge)) {
        errors.push(`Edge ${edgeId} does not consistently reference plane ${plane.id}.`);
      }
    }
    try {
      const mesh = planeToMesh(geometry, plane);
      if (plane.reportedAreaSqFt !== null) {
        const areaDifference = plane.reportedAreaSqFt === 0
          ? 0
          : Math.abs(mesh.slopeAdjustedAreaSqFt - plane.reportedAreaSqFt) / plane.reportedAreaSqFt * 100;
        areaChecks.push({
          planeId: plane.id,
          reportedAreaSqFt: plane.reportedAreaSqFt,
          slopeAdjustedAreaSqFt: mesh.slopeAdjustedAreaSqFt,
          differencePercent: areaDifference,
        });
        if (areaDifference > areaTolerancePercent) {
          warnings.push(
            `Plane ${plane.id} nominal slope-adjusted area differs from its rounded report label by ${areaDifference.toFixed(1)}%.`,
          );
        }
      } else {
        warnings.push(`Plane ${plane.id} has no source-reported facet area.`);
      }
      if (Math.abs(mesh.planAreaSqFt - plane.planAreaSqFt) > 0.01) {
        warnings.push(`Plane ${plane.id} stored plan area differs from its vertices.`);
      }
      if (Math.abs(mesh.surfaceAreaSqFt - plane.meshSurfaceAreaSqFt) > 0.01) {
        warnings.push(`Plane ${plane.id} stored mesh area differs from its triangulated vertices.`);
      }
    } catch (error) {
      errors.push(error instanceof Error ? error.message : `Plane ${plane.id} could not be triangulated.`);
    }
  }

  for (const edge of geometry.edges) {
    const [firstId, secondId] = edge.vertexIds;
    const first = vertices.get(firstId);
    const second = vertices.get(secondId);
    if (!first || !second) {
      errors.push(`Edge ${edge.id} references a missing vertex.`);
      continue;
    }
    for (const planeId of edge.adjacentPlaneIds) {
      const plane = planes.get(planeId);
      if (!plane || !edgeHasPlaneBoundary(plane, edge)) {
        errors.push(`Shared edge ${edge.id} is not present in adjacent plane ${planeId}.`);
      }
    }
    if (Math.abs(distance3D(first.plan, second.plan) - edge.lengthFt) > 0.01) {
      warnings.push(`Edge ${edge.id} stored length differs from its shared vertex coordinates.`);
    }
  }

  for (const loop of geometry.wallLoops) {
    if (loop.boundaryVertexIds.some((id) => !vertices.has(id))) {
      errors.push(`Wall loop ${loop.id} references a missing vertex.`);
    }
  }

  for (const overlap of geometry.overlapOrder) {
    const above = planes.get(overlap.abovePlaneId);
    const below = planes.get(overlap.belowPlaneId);
    if (!above || !below) {
      errors.push(`Overlap order references a missing plane (${overlap.abovePlaneId}, ${overlap.belowPlaneId}).`);
      continue;
    }
    const upperHeight = pointHeightInPlane(geometry, above, overlap.witnessPlan);
    const lowerHeight = pointHeightInPlane(geometry, below, overlap.witnessPlan);
    if (upperHeight === null || lowerHeight === null) {
      errors.push(`Overlap witness is outside one of its listed planes (${overlap.abovePlaneId}, ${overlap.belowPlaneId}).`);
    } else if (upperHeight <= lowerHeight) {
      errors.push(`Top-down overlap order is inconsistent at ${overlap.witnessPlan.join(", ")}.`);
    }
  }

  return { valid: errors.length === 0, errors, warnings, areaChecks };
}
