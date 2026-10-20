import type { GeometryFacetEvidence } from "@/lib/domain";

export type PagePoint = [number, number];

export type FacetSnap = {
  point: PagePoint;
  facetId: string;
  segmentIndex: number;
};

function toScreen(svg: SVGSVGElement, point: PagePoint): DOMPoint | null {
  const matrix = svg.getScreenCTM();
  if (!matrix) return null;
  const svgPoint = svg.createSVGPoint();
  svgPoint.x = point[0];
  svgPoint.y = point[1];
  return svgPoint.matrixTransform(matrix);
}

function screenDistance(svg: SVGSVGElement, first: PagePoint, second: PagePoint): number {
  const firstScreen = toScreen(svg, first);
  const secondScreen = toScreen(svg, second);
  if (!firstScreen || !secondScreen) return Number.POSITIVE_INFINITY;
  return Math.hypot(firstScreen.x - secondScreen.x, firstScreen.y - secondScreen.y);
}

export function pagePointScreenDistance(svg: SVGSVGElement, first: PagePoint, second: PagePoint): number {
  return screenDistance(svg, first, second);
}

function projectToSegment(point: PagePoint, start: PagePoint, end: PagePoint): PagePoint {
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared <= 1e-12) return [...start];
  const fraction = Math.max(
    0,
    Math.min(1, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / lengthSquared),
  );
  return [start[0] + fraction * dx, start[1] + fraction * dy];
}

export function snapPointToFacets(
  point: PagePoint,
  facets: readonly GeometryFacetEvidence[],
  svg: SVGSVGElement,
  excludedFacetId?: string,
  thresholdPixels = 10,
): FacetSnap | null {
  let best: FacetSnap | null = null;
  let bestDistance = thresholdPixels;
  for (const facet of facets) {
    if (facet.id === excludedFacetId || facet.polygonPagePoints.length < 2) continue;
    for (let index = 0; index < facet.polygonPagePoints.length; index += 1) {
      const start = facet.polygonPagePoints[index];
      const end = facet.polygonPagePoints[(index + 1) % facet.polygonPagePoints.length];
      const candidate = projectToSegment(point, start, end);
      const distance = screenDistance(svg, point, candidate);
      if (distance <= bestDistance) {
        bestDistance = distance;
        best = { point: candidate, facetId: facet.id, segmentIndex: index };
      }
    }
  }
  return best;
}

function polygonArea(points: readonly PagePoint[]): number {
  let twiceArea = 0;
  for (let index = 0; index < points.length; index += 1) {
    const current = points[index];
    const next = points[(index + 1) % points.length];
    twiceArea += current[0] * next[1] - next[0] * current[1];
  }
  return Math.abs(twiceArea) / 2;
}

function pointOnSegment(point: PagePoint, start: PagePoint, end: PagePoint, tolerance = 1e-6): boolean {
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const lengthSquared = dx * dx + dy * dy;
  const cross = (point[0] - start[0]) * dy - (point[1] - start[1]) * dx;
  if (Math.abs(cross) > tolerance * Math.max(1, Math.sqrt(lengthSquared))) return false;
  const dot = (point[0] - start[0]) * dx + (point[1] - start[1]) * dy;
  return dot >= -tolerance && dot <= lengthSquared + tolerance;
}

function pointInPolygon(point: PagePoint, polygon: readonly PagePoint[]): boolean {
  let inside = false;
  for (let index = 0; index < polygon.length; index += 1) {
    const start = polygon[index];
    const end = polygon[(index + 1) % polygon.length];
    if (pointOnSegment(point, start, end)) return true;
    if ((start[1] > point[1]) !== (end[1] > point[1])) {
      const crossingX = start[0] +
        ((point[1] - start[1]) * (end[0] - start[0])) / (end[1] - start[1]);
      if (point[0] < crossingX) inside = !inside;
    }
  }
  return inside;
}

function sharedBoundaryLength(first: readonly PagePoint[], second: readonly PagePoint[]): number {
  let sharedLength = 0;
  for (let firstIndex = 0; firstIndex < first.length; firstIndex += 1) {
    const a = first[firstIndex];
    const b = first[(firstIndex + 1) % first.length];
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const length = Math.hypot(dx, dy);
    if (length <= 1e-8) continue;
    for (let secondIndex = 0; secondIndex < second.length; secondIndex += 1) {
      const c = second[secondIndex];
      const d = second[(secondIndex + 1) % second.length];
      const secondDx = d[0] - c[0];
      const secondDy = d[1] - c[1];
      const secondLength = Math.hypot(secondDx, secondDy);
      if (secondLength <= 1e-8) continue;
      const parallelError = Math.abs(dx * secondDy - dy * secondDx) / (length * secondLength);
      if (parallelError > 0.015) continue;
      const lineDistanceC = Math.abs(dx * (c[1] - a[1]) - dy * (c[0] - a[0])) / length;
      const lineDistanceD = Math.abs(dx * (d[1] - a[1]) - dy * (d[0] - a[0])) / length;
      if (Math.max(lineDistanceC, lineDistanceD) > 1.5) continue;
      const cProjection = ((c[0] - a[0]) * dx + (c[1] - a[1]) * dy) / length;
      const dProjection = ((d[0] - a[0]) * dx + (d[1] - a[1]) * dy) / length;
      const overlap = Math.min(length, Math.max(cProjection, dProjection)) -
        Math.max(0, Math.min(cProjection, dProjection));
      if (overlap > 1) sharedLength += overlap;
    }
  }
  return sharedLength;
}

export function matchingFacetIds(
  polygon: readonly PagePoint[],
  facets: readonly GeometryFacetEvidence[],
): string[] {
  if (polygon.length < 3) return [];
  const center: PagePoint = [
    polygon.reduce((sum, point) => sum + point[0], 0) / polygon.length,
    polygon.reduce((sum, point) => sum + point[1], 0) / polygon.length,
  ];
  return facets.filter((facet) => {
    const outline = facet.polygonPagePoints;
    if (outline.length < 3) return false;
    const containsCenter = pointInPolygon(center, outline);
    const containsVertex = polygon.some((point) => pointInPolygon(point, outline)) ||
      outline.some((point) => pointInPolygon(point, polygon));
    return containsCenter || containsVertex || sharedBoundaryLength(polygon, outline) > 1;
  }).map((facet) => facet.id);
}

function orientation(a: PagePoint, b: PagePoint, c: PagePoint): number {
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

function segmentsIntersect(a: PagePoint, b: PagePoint, c: PagePoint, d: PagePoint): boolean {
  const epsilon = 1e-8;
  const o1 = orientation(a, b, c);
  const o2 = orientation(a, b, d);
  const o3 = orientation(c, d, a);
  const o4 = orientation(c, d, b);
  if (((o1 > epsilon && o2 < -epsilon) || (o1 < -epsilon && o2 > epsilon)) &&
      ((o3 > epsilon && o4 < -epsilon) || (o3 < -epsilon && o4 > epsilon))) return true;
  return (Math.abs(o1) <= epsilon && pointOnSegment(c, a, b)) ||
    (Math.abs(o2) <= epsilon && pointOnSegment(d, a, b)) ||
    (Math.abs(o3) <= epsilon && pointOnSegment(a, c, d)) ||
    (Math.abs(o4) <= epsilon && pointOnSegment(b, c, d));
}

export function validAreaPolygon(points: readonly PagePoint[]): boolean {
  if (points.some((point) => !Number.isFinite(point[0]) || !Number.isFinite(point[1]))) return false;
  const distinct: PagePoint[] = [];
  for (const point of points) {
    if (!distinct.some((other) => Math.hypot(point[0] - other[0], point[1] - other[1]) <= 1e-4)) {
      distinct.push(point);
    }
  }
  if (distinct.length < 3 || polygonArea(distinct) <= 1e-3) return false;
  for (let firstIndex = 0; firstIndex < distinct.length; firstIndex += 1) {
    const firstNext = (firstIndex + 1) % distinct.length;
    for (let secondIndex = firstIndex + 1; secondIndex < distinct.length; secondIndex += 1) {
      const secondNext = (secondIndex + 1) % distinct.length;
      if (firstIndex === secondIndex || firstNext === secondIndex || secondNext === firstIndex) continue;
      if (segmentsIntersect(distinct[firstIndex], distinct[firstNext], distinct[secondIndex], distinct[secondNext])) {
        return false;
      }
    }
  }
  return true;
}
