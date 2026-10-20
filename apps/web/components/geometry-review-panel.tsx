"use client";

import type { User } from "@firebase/auth";
import { LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiRequest, downloadWithAuth } from "@/lib/api";
import type {
  GeometryFacetEvidence,
  GeometryVersion,
  Job,
} from "@/lib/domain";
import {
  geometryApprovalBlockers,
  selectGeometryVersionSummary,
  type GeometryVersionSummary,
} from "@/lib/roof/geometry-review";
import {
  matchingFacetIds,
  pagePointScreenDistance,
  snapPointToFacets,
  validAreaPolygon,
  type PagePoint,
} from "@/lib/roof/geometry-editing";

type Props = {
  user: User;
  projectId: string;
  reportId: string | undefined;
  initialVersion: GeometryVersion | null;
  onVersionChange: (version: GeometryVersion | null) => void;
  onReload: () => Promise<void>;
};

type DragTarget = { facetId: string; vertexIndex: number } | null;
type PageViewBox = { x: number; y: number; width: number; height: number };
type PanTarget = { clientX: number; clientY: number; viewBox: PageViewBox } | null;
type PendingArea = { points: PagePoint[]; candidateFacetIds: string[] };

function clampViewBox(viewBox: PageViewBox, pageWidth: number, pageHeight: number): PageViewBox {
  const width = Math.min(pageWidth, Math.max(pageWidth / 3, viewBox.width));
  const height = Math.min(pageHeight, Math.max(pageHeight / 3, viewBox.height));
  return {
    x: Math.min(pageWidth - width, Math.max(0, viewBox.x)),
    y: Math.min(pageHeight - height, Math.max(0, viewBox.y)),
    width,
    height,
  };
}

function zoomViewBox(
  current: PageViewBox,
  factor: number,
  pageWidth: number,
  pageHeight: number,
  anchor?: [number, number],
): PageViewBox {
  const anchorX = anchor?.[0] ?? current.x + current.width / 2;
  const anchorY = anchor?.[1] ?? current.y + current.height / 2;
  const width = current.width / factor;
  const height = current.height / factor;
  const horizontalPosition = (anchorX - current.x) / current.width;
  const verticalPosition = (anchorY - current.y) / current.height;
  return clampViewBox({
    x: anchorX - horizontalPosition * width,
    y: anchorY - verticalPosition * height,
    width,
    height,
  }, pageWidth, pageHeight);
}

function moveVertex(
  facets: GeometryFacetEvidence[],
  facetId: string,
  vertexIndex: number,
  point: [number, number],
): GeometryFacetEvidence[] {
  return facets.map((facet) => {
    if (facet.id !== facetId) return facet;
    return {
      ...facet,
      polygonPagePoints: facet.polygonPagePoints.map((vertex, index) =>
        index === vertexIndex ? point : vertex,
      ),
    };
  });
}

function createAreaFacet(
  points: PagePoint[],
  source: GeometryFacetEvidence,
  drawOrder: number,
  number: number,
): GeometryFacetEvidence {
  return {
    id: `user-facet-${crypto.randomUUID()}`,
    name: `New roof area ${number}`,
    roofType: source.roofType,
    areaLabelSqFt: null,
    polygonPagePoints: points,
    pitch: source.pitch ? { ...source.pitch } : null,
    downslopeDirectionPage: source.downslopeDirectionPage
      ? [source.downslopeDirectionPage[0], source.downslopeDirectionPage[1]]
      : null,
    edgeTypes: [],
    drawOrder,
    source: "user_traced",
    provenance: {
      outline: "user_edited",
      pitch: "user_edited",
      downslopeDirection: "user_edited",
    },
  };
}

export function GeometryReviewPanel({
  user,
  projectId,
  reportId,
  initialVersion,
  onVersionChange,
  onReload,
}: Props) {
  const [version, setVersion] = useState<GeometryVersion | null>(initialVersion);
  const [versions, setVersions] = useState<GeometryVersionSummary[]>([]);
  const [facets, setFacets] = useState<GeometryFacetEvidence[]>(
    [...(initialVersion?.analysis.facets ?? [])].sort((first, second) => (first.drawOrder ?? 0) - (second.drawOrder ?? 0)),
  );
  const [selectedId, setSelectedId] = useState(initialVersion?.analysis.facets[0]?.id ?? "");
  const [pageImage, setPageImage] = useState<string | null>(null);
  const [confirmStackingOrder, setConfirmStackingOrder] = useState(false);
  const [confirmation, setConfirmation] = useState(false);
  const [dragTarget, setDragTarget] = useState<DragTarget>(null);
  const [panTarget, setPanTarget] = useState<PanTarget>(null);
  const [drawingPoints, setDrawingPoints] = useState<PagePoint[] | null>(null);
  const [cursorPoint, setCursorPoint] = useState<PagePoint | null>(null);
  const [pendingArea, setPendingArea] = useState<PendingArea | null>(null);
  const [pendingSourceId, setPendingSourceId] = useState("");
  const [pageViewBox, setPageViewBox] = useState<PageViewBox>({ x: 0, y: 0, width: 595, height: 842 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const svgRef = useRef<SVGSVGElement | null>(null);
  const spaceHeld = useRef(false);
  const didPan = useRef(false);

  const refreshVersion = useCallback(async (versionId?: string) => {
    try {
      const updated = await apiRequest<GeometryVersion>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry${versionId ? `?version_id=${encodeURIComponent(versionId)}` : ""}`,
      );
      setVersion(updated);
      setFacets([...(updated.analysis.facets ?? [])].sort((first, second) => (first.drawOrder ?? 0) - (second.drawOrder ?? 0)));
      setSelectedId((current) =>
        updated.analysis.facets.some((facet) => facet.id === current)
          ? current
          : updated.analysis.facets[0]?.id ?? "",
      );
      setConfirmStackingOrder(Boolean(updated.analysis.stackingOrderConfirmed));
      setDrawingPoints(null);
      setPendingArea(null);
      setCursorPoint(null);
      onVersionChange(updated);
      const history = await apiRequest<GeometryVersionSummary[]>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry/versions`,
      ).catch(() => []);
      setVersions(history);
      return updated;
    } catch {
      setVersion(null);
      setFacets([]);
      onVersionChange(null);
      return null;
    }
  }, [onVersionChange, projectId, user]);

  useEffect(() => {
    let active = true;
    void apiRequest<GeometryVersionSummary[]>(
      user,
      `/api/projects/${encodeURIComponent(projectId)}/geometry/versions`,
    ).then((history) => {
      if (active) setVersions(history);
    }).catch(() => {
      if (active) setVersions([]);
    });
    return () => { active = false; };
  }, [projectId, user]);

  async function selectVersion(selectedVersionId: string) {
    if (!selectedVersionId) return;
    if (!selectGeometryVersionSummary(versions, selectedVersionId)) {
      setError("The selected geometry revision is no longer available.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const selected = await apiRequest<GeometryVersion>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry?version_id=${encodeURIComponent(selectedVersionId)}`,
      );
      setVersion(selected);
      const orderedFacets = [...(selected.analysis.facets ?? [])].sort(
        (first, second) => (first.drawOrder ?? 0) - (second.drawOrder ?? 0),
      );
      setFacets(orderedFacets);
      setSelectedId(orderedFacets[0]?.id ?? "");
      setConfirmStackingOrder(Boolean(selected.analysis.stackingOrderConfirmed));
      setConfirmation(false);
      setDrawingPoints(null);
      setPendingArea(null);
      setCursorPoint(null);
      onVersionChange(selected);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load the selected geometry version.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (version?.status !== "processing" || !version.jobId) return;
    let active = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const job = await apiRequest<Job>(user, `/api/jobs/${encodeURIComponent(version.jobId ?? "")}`);
        if (!active) return;
        if (job.status === "succeeded" || job.status === "failed") {
          const updated = await refreshVersion();
          if (updated?.status === "needs_review") setMessage("Geometry is ready. Adjust the roof points for the 3D viewer.");
          if (job.status === "failed") setError(job.error?.message ?? "Geometry processing failed.");
          return;
        }
      } catch {
        if (active) setError("Could not poll geometry processing status.");
      }
      if (active) timer = window.setTimeout(() => void poll(), 1500);
    };
    timer = window.setTimeout(() => void poll(), 700);
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refreshVersion, user, version?.jobId, version?.status]);

  const pageNumber = version?.analysis.coordinatePageNumber ??
    (version?.analysis.sourceFormat === "roof_manager" ? 3 : 4);
  const pageSize = version?.analysis.coordinatePageSize ?? { widthPoints: 595, heightPoints: 842 };
  const versionId = version?.id;

  useEffect(() => {
    setPageImage(null);
    setPageViewBox({ x: 0, y: 0, width: pageSize.widthPoints, height: pageSize.heightPoints });
    setPanTarget(null);
  }, [pageSize.heightPoints, pageSize.widthPoints, versionId]);

  useEffect(() => {
    if (!versionId) return;
    let active = true;
    let objectUrl: string | null = null;
    void downloadWithAuth(
      user,
      `/api/projects/${encodeURIComponent(projectId)}/geometry/page-image?page=${pageNumber}&version_id=${encodeURIComponent(versionId)}`,
    )
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setPageImage(objectUrl);
      })
      .catch(() => {
        if (active) setError("Could not render the source report page.");
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [pageNumber, projectId, user, versionId]);

  const blockers = useMemo(() => geometryApprovalBlockers(version), [version]);
  const sourceFacetOptions = useMemo(() => {
    const byId = new Map<string, GeometryFacetEvidence>();
    for (const facet of version?.analysis.facets ?? []) byId.set(facet.id, facet);
    for (const facet of facets) byId.set(facet.id, facet);
    return [...byId.values()];
  }, [facets, version]);
  const pendingSourceOptions = useMemo(() => {
    if (!pendingArea) return [];
    if (!pendingArea.candidateFacetIds.length) return sourceFacetOptions;
    const candidates = new Set(pendingArea.candidateFacetIds);
    return sourceFacetOptions.filter((facet) => candidates.has(facet.id));
  }, [pendingArea, sourceFacetOptions]);
  const hasUnsavedEdits = useMemo(() => {
    if (!version) return false;
    const savedFacets = version.analysis.facets ?? [];
    if (savedFacets.length !== facets.length) return true;
    if (confirmStackingOrder !== Boolean(version.analysis.stackingOrderConfirmed)) return true;
    const savedById = new Map(savedFacets.map((facet) => [facet.id, facet]));
    return facets.some((facet) => {
      const saved = savedById.get(facet.id);
      return !saved ||
        JSON.stringify(facet.polygonPagePoints) !== JSON.stringify(saved.polygonPagePoints) ||
        facet.roofType !== saved.roofType ||
        JSON.stringify(facet.pitch) !== JSON.stringify(saved.pitch) ||
        JSON.stringify(facet.downslopeDirectionPage) !== JSON.stringify(saved.downslopeDirectionPage);
    });
  }, [confirmStackingOrder, facets, version]);
  const shouldSaveBeforeApprove = version?.isCurrent === false || hasUnsavedEdits;
  const invalidFacet = facets.find((facet) => !validAreaPolygon(facet.polygonPagePoints));
  const canSaveDraft = Boolean(
    version &&
    version.status !== "processing" &&
    facets.length > 0 &&
    drawingPoints === null &&
    pendingArea === null &&
    !invalidFacet,
  );

  function svgPagePoint(event: { clientX: number; clientY: number }): [number, number] | null {
    const svg = svgRef.current;
    const matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return null;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(matrix.inverse());
    return [transformed.x, transformed.y];
  }

  function addAreaFromSource(points: PagePoint[], source: GeometryFacetEvidence) {
    const newFacet = createAreaFacet(
      points,
      source,
      facets.length,
      facets.length + 1,
    );
    setFacets((current) => [...current, newFacet]);
    setSelectedId(newFacet.id);
    setDrawingPoints(null);
    setCursorPoint(null);
    setPendingArea(null);
    setPendingSourceId("");
    setError("");
    setMessage(`New area copied roof details from ${source.name}. Save geometry changes to keep it.`);
  }

  function finishAreaTrace(points: PagePoint[]) {
    if (!validAreaPolygon(points)) {
      setError("A roof area needs at least three distinct points and a non-crossing outline.");
      return;
    }
    setDrawingPoints(null);
    setCursorPoint(null);
    const candidates = matchingFacetIds(points, facets);
    if (candidates.length === 1) {
      const source = facets.find((facet) => facet.id === candidates[0]);
      if (source) {
        addAreaFromSource(points, source);
        return;
      }
    }
    const pending = { points, candidateFacetIds: candidates };
    const sourceOptions = candidates.length
      ? sourceFacetOptions.filter((facet) => candidates.includes(facet.id))
      : sourceFacetOptions;
    if (!sourceOptions.length) {
      setError("No roof area is available to copy roof details from.");
      setPendingArea(null);
      return;
    }
    setPendingArea(pending);
    setPendingSourceId(sourceOptions.length === 1 ? sourceOptions[0].id : "");
    setMessage(candidates.length
      ? "This area touches more than one roof section. Choose which section supplies its roof details."
      : "Choose a roof section to copy details from.");
  }

  function startAreaTrace() {
    if (!version || version.status === "processing" || drawingPoints !== null || pendingArea) return;
    setError("");
    setMessage("Click points around the new roof area. Click the first point or press Enter to finish.");
    setDrawingPoints([]);
    setCursorPoint(null);
    requestAnimationFrame(() => svgRef.current?.focus());
  }

  function cancelAreaTrace() {
    setDrawingPoints(null);
    setCursorPoint(null);
    setMessage("Area tracing cancelled.");
  }

  function completePendingArea() {
    if (!pendingArea || !pendingSourceId) return;
    const source = sourceFacetOptions.find((facet) => facet.id === pendingSourceId);
    if (!source) {
      setError("The selected roof section is no longer available.");
      return;
    }
    addAreaFromSource(pendingArea.points, source);
  }

  function deleteSelectedArea() {
    if (!selectedId || pendingArea || drawingPoints !== null) return;
    const selectedIndex = facets.findIndex((facet) => facet.id === selectedId);
    const remaining = facets.filter((facet) => facet.id !== selectedId);
    setFacets(remaining);
    setSelectedId(remaining[Math.min(selectedIndex, remaining.length - 1)]?.id ?? "");
    setMessage("Area removed from this draft. Save geometry changes to keep the deletion.");
  }

  function handleCanvasClick(event: React.MouseEvent<SVGSVGElement>) {
    if (didPan.current) {
      didPan.current = false;
      return;
    }
    if (drawingPoints === null || panTarget || dragTarget) return;
    const point = svgPagePoint(event);
    const svg = svgRef.current;
    if (!point || !svg) return;
    if (event.detail > 1) {
      finishAreaTrace(drawingPoints);
      return;
    }
    const first = drawingPoints[0];
    if (first && drawingPoints.length >= 3 && pagePointScreenDistance(svg, point, first) <= 10) {
      finishAreaTrace(drawingPoints);
      return;
    }
    if (drawingPoints.length >= 500) {
      setError("A roof area can contain at most 500 points.");
      return;
    }
    const snapped = snapPointToFacets(point, facets, svg)?.point ?? point;
    const last = drawingPoints[drawingPoints.length - 1];
    if (last && pagePointScreenDistance(svg, last, snapped) <= 2) return;
    setDrawingPoints((current) => current === null ? current : [...current, snapped]);
    setError("");
  }

  function handleCanvasKeyDown(event: React.KeyboardEvent<SVGSVGElement>) {
    if (drawingPoints === null) return;
    if (event.code === "Space") {
      spaceHeld.current = true;
      event.preventDefault();
    } else if (event.key === "Enter") {
      event.preventDefault();
      finishAreaTrace(drawingPoints);
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelAreaTrace();
    }
  }

  function handleCanvasKeyUp(event: React.KeyboardEvent<SVGSVGElement>) {
    if (event.code === "Space") spaceHeld.current = false;
  }

  function handlePointerMove(event: React.PointerEvent<SVGSVGElement>) {
    if (dragTarget) {
      const point = svgPagePoint(event);
      const svg = svgRef.current;
      if (point && svg) {
        const snapped = snapPointToFacets(point, facets, svg, dragTarget.facetId)?.point ?? point;
        setFacets((current) => moveVertex(current, dragTarget.facetId, dragTarget.vertexIndex, snapped));
      }
      return;
    }
    if (drawingPoints !== null && !panTarget) {
      const point = svgPagePoint(event);
      const svg = svgRef.current;
      if (point && svg) setCursorPoint(snapPointToFacets(point, facets, svg)?.point ?? point);
      return;
    }
    if (!panTarget) return;
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const dx = (event.clientX - panTarget.clientX) * panTarget.viewBox.width / rect.width;
    const dy = (event.clientY - panTarget.clientY) * panTarget.viewBox.height / rect.height;
    if (Math.hypot(event.clientX - panTarget.clientX, event.clientY - panTarget.clientY) > 3) {
      didPan.current = true;
    }
    setPageViewBox(clampViewBox({
      ...panTarget.viewBox,
      x: panTarget.viewBox.x - dx,
      y: panTarget.viewBox.y - dy,
    }, pageSize.widthPoints, pageSize.heightPoints));
  }

  function handleCanvasPointerDown(event: React.PointerEvent<SVGSVGElement>) {
    const zoomed = pageViewBox.width < pageSize.widthPoints;
    const panRequested = spaceHeld.current || event.button === 1;
    if (drawingPoints !== null) {
      if (!zoomed || !panRequested) return;
    } else if (!zoomed || (event.target as Element).tagName.toLowerCase() !== "image") {
      return;
    }
    event.preventDefault();
    didPan.current = false;
    setPanTarget({ clientX: event.clientX, clientY: event.clientY, viewBox: pageViewBox });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handleCanvasPointerUp() {
    setDragTarget(null);
    setPanTarget(null);
    if (didPan.current) window.setTimeout(() => { didPan.current = false; }, 0);
  }

  function handleCanvasPointerCancel() {
    setDragTarget(null);
    setPanTarget(null);
    didPan.current = false;
  }

  function handleCanvasWheel(event: React.WheelEvent<SVGSVGElement>) {
    event.preventDefault();
    const anchor = svgPagePoint(event);
    setPageViewBox((current) => zoomViewBox(
      current,
      event.deltaY < 0 ? 1.2 : 1 / 1.2,
      pageSize.widthPoints,
      pageSize.heightPoints,
      anchor ?? undefined,
    ));
  }

  function changeZoom(factor: number) {
    setPageViewBox((current) => zoomViewBox(current, factor, pageSize.widthPoints, pageSize.heightPoints));
  }

  function resetZoom() {
    setPageViewBox({ x: 0, y: 0, width: pageSize.widthPoints, height: pageSize.heightPoints });
    setPanTarget(null);
  }

  async function startReconstruction() {
    if (!reportId) {
      setError("Upload and complete a measurement report before reconstructing geometry.");
      return;
    }
    setBusy(true);
    setError("");
    setMessage("Starting vector analysis…");
    try {
      const queued = await apiRequest<{ geometryVersionId: string; jobId: string; status: string }>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry/reconstruct`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reportId }),
        },
      );
      const updated = await refreshVersion(queued.geometryVersionId);
      if (!updated) {
        setMessage("The PDF geometry job was accepted. Reloading its status; this can take a few minutes.");
      } else if (updated.status === "processing") {
        setMessage(queued.status === "processing"
          ? "PDF geometry reconstruction is already running. This page will update when analysis finishes."
          : "PDF geometry reconstruction is running. This page will update when analysis finishes.");
      } else if (updated.status === "failed") {
        setError(updated.error?.message ?? "PDF geometry reconstruction failed. Retry or review the report.");
      } else {
        setMessage("Geometry is ready. Adjust the roof points for the 3D viewer.");
      }
      await onReload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start geometry reconstruction.");
      setMessage("");
    } finally {
      setBusy(false);
    }
  }

  async function persistReview(): Promise<GeometryVersion> {
    if (!version) throw new Error("No geometry revision is selected.");
    const current = await apiRequest<GeometryVersion>(
      user,
      `/api/projects/${encodeURIComponent(projectId)}/geometry`,
    );
    return apiRequest<GeometryVersion>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expectedCurrentVersionId: current.id,
            expectedVersion: current.version,
            basedOnVersionId: version.id,
            manualFeetPerPoint: null,
            confirmManualScale: false,
            confirmStackingOrder,
            facets: facets.map((facet, index) => ({
              id: facet.id,
              name: facet.name,
              roofType: facet.roofType,
              polygonPagePoints: facet.polygonPagePoints,
              pitch: facet.pitch ? { notation: facet.pitch.notation, rise: facet.pitch.rise, run: facet.pitch.run } : null,
              downslopeDirectionPage: facet.downslopeDirectionPage,
              edgeTypes: facet.edgeTypes ?? [],
              drawOrder: index,
              provenance: facet.provenance,
              reportedAreaSqFt: facet.areaLabelSqFt,
            })),
          }),
        },
      );
  }

  async function refreshHistory() {
    const history = await apiRequest<GeometryVersionSummary[]>(
      user,
      `/api/projects/${encodeURIComponent(projectId)}/geometry/versions`,
    ).catch(() => []);
    setVersions(history);
  }

  async function saveReview() {
    if (!version || !canSaveDraft) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const saved = await persistReview();
      const updated = { ...saved, isCurrent: true };
      setVersion(updated);
      setFacets(updated.analysis.facets ?? []);
      setSelectedId(updated.analysis.facets.some((facet) => facet.id === selectedId)
        ? selectedId
        : updated.analysis.facets[0]?.id ?? "");
      setConfirmStackingOrder(Boolean(updated.analysis.stackingOrderConfirmed));
      setDrawingPoints(null);
      setPendingArea(null);
      setMessage(`Saved geometry changes as revision ${updated.version}.`);
      setConfirmation(false);
      onVersionChange(updated);
      await Promise.all([refreshHistory(), onReload()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save geometry review.");
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!version) return;
    setBusy(true);
    setError("");
    setMessage("");
    let savedBeforeApproval = false;
    let savedVersionBeforeApproval: GeometryVersion | null = null;
    try {
      let versionToApprove = version;
      if (shouldSaveBeforeApprove) {
        const saved = await persistReview();
        versionToApprove = { ...saved, isCurrent: true };
        savedBeforeApproval = true;
        savedVersionBeforeApproval = versionToApprove;
        setVersion(versionToApprove);
        setFacets(versionToApprove.analysis.facets ?? []);
        setSelectedId(versionToApprove.analysis.facets.some((facet) => facet.id === selectedId)
          ? selectedId
          : versionToApprove.analysis.facets[0]?.id ?? "");
        setConfirmStackingOrder(Boolean(versionToApprove.analysis.stackingOrderConfirmed));
      }
      const approved = await apiRequest<GeometryVersion>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expectedVersion: versionToApprove.version, confirmation }),
        },
      );
      const currentApproved = { ...approved, isCurrent: true };
      setVersion(currentApproved);
      onVersionChange(currentApproved);
      setMessage("Geometry approved. It will be used in the 3D roof tab.");
      await Promise.all([refreshHistory(), onReload()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Geometry could not be approved.");
      if (savedBeforeApproval) {
        if (savedVersionBeforeApproval) onVersionChange(savedVersionBeforeApproval);
        await Promise.all([refreshHistory(), onReload()]);
      }
    } finally {
      setBusy(false);
    }
  }

  const candidatePolygon = (facet: GeometryFacetEvidence) =>
    facet.polygonPagePoints.map((point) => String(point[0]) + "," + String(point[1])).join(" ");
  const candidatePoints = (points: PagePoint[]) =>
    points.map((point) => `${point[0]},${point[1]}`).join(" ");
  const selectedFacet = facets.find((facet) => facet.id === selectedId) ?? null;

  return (
    <section className="geometry-review">
      <div className="panel-head" style={{ alignItems: "flex-start" }}>
        <div>
          <h2>PDF-derived geometry review</h2>
          <div className="subcopy">Select roof areas, trace missing outlines, and adjust points on the report page.</div>
        </div>
        <div style={{ display: "grid", justifyItems: "end", gap: 8 }}>
          {version && <span className={"badge " + (version.status === "approved" ? "green" : "orange")}>
            {version.status.replaceAll("_", " ")} · v{version.version}
          </span>}
          {version && version.status !== "processing" && reportId && <button
            className="button secondary small"
            aria-busy={busy}
            disabled={busy}
            onClick={() => void startReconstruction()}
          >
            {busy ? <><LoaderCircle size={14} className="spin" /> Rebuilding…</> : "Rebuild from PDF"}
          </button>}
          {versions.length > 1 && <div className="field" style={{ maxWidth: 340, margin: 0 }}>
            <select id="geometry-version-history" aria-label="Geometry revision" className="input" value={version?.id ?? ""} onChange={(event) => void selectVersion(event.target.value)}>
              {versions.map((item) => <option key={item.id} value={item.id}>v{item.version} · {item.status.replaceAll("_", " ")}{item.isCurrent ? " · current" : " · history"}</option>)}
            </select>
          </div>}
        </div>
      </div>
      {version?.status === "failed" && <div className="error" role="alert" style={{ marginTop: 12 }}>
        <div>{version.error?.message ?? "The PDF geometry could not be processed."}</div>
        <button className="button secondary small" style={{ marginTop: 10 }} aria-busy={busy} disabled={busy || !reportId} onClick={() => void startReconstruction()}>
          {busy ? <><LoaderCircle size={14} className="spin" /> Retrying…</> : "Retry PDF reconstruction"}
        </button>
      </div>}
      {!version && <div className="card empty" style={{ marginTop: 12 }}>
        <p className="muted">No geometry points are available yet.</p>
        <button className="button" aria-busy={busy} disabled={busy || !reportId} onClick={() => void startReconstruction()}>
          {busy ? <><LoaderCircle size={15} className="spin" /> Starting reconstruction…</> : "Load roof points"}
        </button>
        {busy && <div className="notice" role="status" aria-live="polite" style={{ marginTop: 12 }}>
          <LoaderCircle size={15} className="spin" /> Preparing roof points…
        </div>}
        {error && <div className="error" role="alert" style={{ marginTop: 12 }}>{error}</div>}
        {message && <div className="notice green-notice" role="status" aria-live="polite" style={{ marginTop: 12 }}>{message}</div>}
        {!reportId && <p className="muted">Upload a complete report first.</p>}
      </div>}

      {version && <>
        {version.status === "processing" && <div className="notice" role="status" style={{ marginTop: 12 }}>Preparing roof points…</div>}

        <div className="geometry-review-grid" style={{ marginTop: 16 }}>
          <div className="geometry-review-main card panel">
            <div className="panel-head">
              <div><h3>Adjust points for the 3D viewer</h3></div>
              <span className="muted" style={{ fontSize: 11 }}>{facets.length} roof areas</span>
            </div>
            <div className="row" style={{ justifyContent: "flex-end", gap: 6, marginBottom: 8 }}>
              <button className="button secondary small" type="button" aria-label="Zoom out" disabled={pageViewBox.width >= pageSize.widthPoints} onClick={() => changeZoom(1 / 1.2)}>−</button>
              <span className="muted" aria-live="polite" style={{ minWidth: 42, textAlign: "center", fontSize: 12 }}>{Math.round(pageSize.widthPoints / pageViewBox.width * 100)}%</span>
              <button className="button secondary small" type="button" aria-label="Zoom in" disabled={pageViewBox.width <= pageSize.widthPoints / 3} onClick={() => changeZoom(1.2)}>+</button>
              <button className="button secondary small" type="button" onClick={resetZoom}>Reset</button>
            </div>
            {pageImage ? <svg
              ref={svgRef}
              className={`geometry-page${pageViewBox.width < pageSize.widthPoints ? " zoomed" : ""}${drawingPoints !== null ? " drawing" : ""}${panTarget ? " panning" : ""}`}
              viewBox={`${pageViewBox.x} ${pageViewBox.y} ${pageViewBox.width} ${pageViewBox.height}`}
              role="application"
              aria-label="Roof outlines with editable points from the source report"
              tabIndex={0}
              onPointerMove={handlePointerMove}
              onPointerDown={handleCanvasPointerDown}
              onPointerUp={handleCanvasPointerUp}
              onPointerCancel={handleCanvasPointerCancel}
              onClick={handleCanvasClick}
              onWheel={handleCanvasWheel}
              onKeyDown={handleCanvasKeyDown}
              onKeyUp={handleCanvasKeyUp}
            >
              <image href={pageImage} x="0" y="0" width={pageSize.widthPoints} height={pageSize.heightPoints} />
              {facets.map((facet) => <g key={facet.id}>
                <polygon
                  points={candidatePolygon(facet)}
                  fill={selectedId === facet.id ? "rgba(245, 158, 11, 0.2)" : "rgba(18, 110, 95, 0.12)"}
                  stroke={selectedId === facet.id ? "#db8d29" : "#14796b"}
                  strokeWidth={selectedId === facet.id ? 2.2 : 1.5}
                  vectorEffect="non-scaling-stroke"
                  onClick={() => { if (drawingPoints === null) setSelectedId(facet.id); }}
                />
                {facet.polygonPagePoints.map((point, index) => <circle
                  key={facet.id + "-vertex-" + index}
                  cx={point[0]}
                  cy={point[1]}
                  r={selectedId === facet.id ? 4 : 3.4}
                  className="geometry-vertex"
                  onPointerDown={(event) => {
                    if (drawingPoints !== null) return;
                    event.stopPropagation();
                    setSelectedId(facet.id);
                    setDragTarget({ facetId: facet.id, vertexIndex: index });
                    event.currentTarget.ownerSVGElement?.setPointerCapture(event.pointerId);
                  }}
                />)}
              </g>)}
              {drawingPoints !== null && <g className="geometry-trace" pointerEvents="none">
                {drawingPoints.length > 0 && <polyline
                  points={candidatePoints(cursorPoint ? [...drawingPoints, cursorPoint] : drawingPoints)}
                  fill="none"
                  stroke="#bd6d16"
                  strokeWidth={2}
                  strokeDasharray="5 3"
                  vectorEffect="non-scaling-stroke"
                />}
                {drawingPoints.map((point, index) => <circle key={`trace-${index}`} cx={point[0]} cy={point[1]} r={4} />)}
                {cursorPoint && <circle className="geometry-snap-point" cx={cursorPoint[0]} cy={cursorPoint[1]} r={5} />}
              </g>}
            </svg> : <div className="empty muted">Loading source report page…</div>}
            <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
              Scroll or use +/− to zoom up to 300%. Drag points to adjust an outline; hold Space and drag to pan while tracing.
            </div>
          </div>

          <aside className="geometry-review-side card panel" aria-label="Roof area controls">
            <div className="panel-head">
              <div><h3>Roof areas</h3></div>
              <span className="muted" style={{ fontSize: 11 }}>{facets.length} total</span>
            </div>
            <div className="geometry-plane-list" role="listbox" aria-label="Select roof area">
              {facets.map((facet) => <button
                key={facet.id}
                type="button"
                role="option"
                aria-selected={selectedId === facet.id}
                className={`geometry-plane-choice${selectedId === facet.id ? " selected" : ""}`}
                onClick={() => setSelectedId(facet.id)}
              >
                <strong>{facet.name}</strong>
                <small>{facet.roofType === "flat" ? "Flat" : "Pitched"} · {facet.pitch?.notation ?? "Pitch not assigned"}</small>
              </button>)}
              {!facets.length && <div className="empty muted">No roof areas in this draft. Add an area to continue.</div>}
            </div>
            <div className="row geometry-area-actions">
              <button className="button" type="button" disabled={busy || version.status === "processing" || facets.length >= 100 || drawingPoints !== null || pendingArea !== null || !pageImage} onClick={startAreaTrace}>
                Add area
              </button>
              <button className="button secondary" type="button" disabled={busy || !selectedId || drawingPoints !== null || pendingArea !== null} onClick={deleteSelectedArea}>
                Delete selected
              </button>
            </div>
            {drawingPoints !== null && <div className="notice geometry-area-prompt" role="status">
              Click around the roof area. Snap points to nearby edges, then close the outline. Press Enter to finish or Escape to cancel.
              <button className="button secondary small" type="button" onClick={cancelAreaTrace}>Cancel tracing</button>
            </div>}
            {pendingArea && <div className="card geometry-area-prompt">
              <label className="field">
                <span>{pendingArea.candidateFacetIds.length > 1 ? "Choose the touching section to copy details from" : "Choose a roof section to copy details from"}</span>
                <select className="input" value={pendingSourceId} onChange={(event) => setPendingSourceId(event.target.value)}>
                  <option value="">Select roof section</option>
                  {pendingSourceOptions.map((facet) => <option key={facet.id} value={facet.id}>
                    {facet.name} · {facet.roofType} · {facet.pitch?.notation ?? "pitch missing"}{facets.some((current) => current.id === facet.id) ? "" : " · removed from draft"}
                  </option>)}
                </select>
              </label>
              <div className="row">
                <button className="button small" type="button" disabled={!pendingSourceId} onClick={completePendingArea}>Use roof details</button>
                <button className="button secondary small" type="button" onClick={() => { setPendingArea(null); setPendingSourceId(""); setMessage(""); }}>Cancel</button>
              </div>
            </div>}
            {selectedFacet && <div className="muted geometry-selected-details">
              Selected: {selectedFacet.name}<br />
              {selectedFacet.roofType === "flat" ? "Flat" : "Pitched"} · {selectedFacet.pitch?.notation ?? "Pitch not assigned"}
            </div>}
          </aside>
        </div>
        {invalidFacet && <div className="error" role="alert">{invalidFacet.name} has an invalid outline. Adjust its points before saving.</div>}

        {error && <div className="error" role="alert" style={{ marginTop: 14 }}>{error}</div>}
        {message && <div className="notice green-notice" role="status" style={{ marginTop: 14 }}>{message}</div>}
        <div className="row" style={{ justifyContent: "space-between", marginTop: 16, flexWrap: "wrap" }}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <button className="button" onClick={() => void saveReview()} disabled={busy || !canSaveDraft}>
              {busy ? "Saving…" : "Save geometry changes"}
            </button>
            {(version.analysis.validation?.buildDiagnostics?.unresolvedOverlaps?.length || (hasUnsavedEdits && facets.length > 1)) ? <label className="row muted" style={{ fontSize: 12 }}>
              <input type="checkbox" checked={confirmStackingOrder} onChange={(event) => setConfirmStackingOrder(event.target.checked)} />
              I reviewed the order of overlapping roof planes.
            </label> : null}
            <label className="row muted" style={{ fontSize: 12 }}>
              <input type="checkbox" checked={confirmation} onChange={(event) => setConfirmation(event.target.checked)} />
              I reviewed these points for the 3D viewer.
            </label>
            <button
              className="button secondary"
              onClick={() => void approve()}
              disabled={busy || !confirmation || !canSaveDraft || (blockers.length > 0 && !shouldSaveBeforeApprove) || (version.status === "approved" && version.isCurrent !== false && !shouldSaveBeforeApprove)}
            >
              Approve for 3D viewer
            </button>
          </div>
          {version.status === "approved" && version.isCurrent !== false
            ? <span className="badge green">Active in 3D viewer</span>
            : <span className="muted" style={{ fontSize: 11 }}>{blockers.length > 0 && !shouldSaveBeforeApprove ? "More geometry review is needed before 3D approval." : "Ready for 3D approval."}</span>}
        </div>
      </>}
    </section>
  );
}
