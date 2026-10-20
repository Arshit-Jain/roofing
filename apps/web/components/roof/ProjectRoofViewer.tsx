"use client";

import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { useEffect, useMemo, useRef, useState } from "react";
import { Eye, RotateCcw, Save } from "lucide-react";
import { PerspectiveCamera, Plane, Vector3 } from "three";
import type { OrbitControls as OrbitControlsImplementation } from "three-stdlib";
import type { User } from "@firebase/auth";
import { apiRequest } from "@/lib/api";
import type { GeometryVersion, Project } from "@/lib/domain";
import { RoofModel, type RoofAppearance, type RoofMaterial } from "@/components/roof/RoofModel";
import { WallsLayer } from "@/components/roof/WallsLayer";
import {
  BASE_SLAB_THICKNESS_FT,
  geometryFor3DPreview,
  migrateRoofGeometry,
  roofWallLoops,
  validateGeometry,
  type GeometryPlane,
  type RoofGeometry,
} from "@/lib/roof/geometry";

const swatches = ["#35414b", "#526b64", "#a05e3a", "#7a7366", "#6c7e8e", "#e5e7df"];
const pitchedMaterials: RoofMaterial[] = ["asphalt_shingle", "standing_seam_metal", "tile"];
const flatMaterials: RoofMaterial[] = ["epdm", "tpo", "pvc", "modified_bitumen"];

type ModelFrame = {
  centerU: number;
  centerV: number;
  centerHeight: number;
  minHeight: number;
  radius: number;
};

function validColor(value: string | undefined): value is string {
  return value !== undefined && /^#[0-9a-f]{6}$/i.test(value);
}

function defaultAppearance(plane: GeometryPlane): RoofAppearance {
  return plane.roofType === "flat"
    ? { material: "tpo", color: "#e5e7df" }
    : { material: "asphalt_shingle", color: "#35414b" };
}

function appearanceFromSelections(
  selections: Record<string, string>,
  plane: GeometryPlane,
): RoofAppearance {
  const base = defaultAppearance(plane);
  const color = selections[`appearance.${plane.id}.color`];
  const material = selections[`appearance.${plane.id}.material`];
  const allowedMaterials = plane.roofType === "flat" ? flatMaterials : pitchedMaterials;
  return {
    color: validColor(color) ? color : base.color,
    material: allowedMaterials.includes(material as RoofMaterial) ? material as RoofMaterial : base.material,
  };
}

function frameGeometry(geometry: RoofGeometry): ModelFrame {
  const us = geometry.vertices.map(({ plan }) => plan[0]);
  const vs = geometry.vertices.map(({ plan }) => plan[1]);
  const heights = geometry.vertices.map(({ plan }) => plan[2]);
  const minU = Math.min(...us);
  const maxU = Math.max(...us);
  const minV = Math.min(...vs);
  const maxV = Math.max(...vs);
  const wallBottomHeights = roofWallLoops(geometry).map(
    (loop) => loop.groundPlaneHeightFt - BASE_SLAB_THICKNESS_FT,
  );
  const minHeight = Math.min(...heights, ...wallBottomHeights);
  const maxHeight = Math.max(...heights);
  const width = Math.max(maxU - minU, maxV - minV, maxHeight - minHeight, 8);
  return {
    centerU: (minU + maxU) / 2,
    centerV: (minV + maxV) / 2,
    centerHeight: (minHeight + maxHeight) / 2,
    minHeight,
    radius: Math.max(width * 1.35, 10),
  };
}

function CameraController({
  preset,
  resetToken,
  frame,
  controlsEnabled,
}: {
  preset: "3d" | "top";
  resetToken: number;
  frame: ModelFrame;
  controlsEnabled: boolean;
}) {
  const camera = useThree((state) => state.camera);
  const controls = useRef<OrbitControlsImplementation | null>(null);

  useEffect(() => {
    const target = new Vector3(0, frame.centerHeight, 0);
    const radius = frame.radius;
    if (preset === "top") {
      camera.up.set(0, 0, -1);
      camera.position.set(0, frame.centerHeight + radius * 2.2, 0.001);
    } else {
      camera.up.set(0, 1, 0);
      camera.position.set(radius * 0.9, frame.centerHeight + radius * 0.9, radius * 1.4);
    }
    camera.lookAt(target);
    if (camera instanceof PerspectiveCamera) camera.updateProjectionMatrix();
    if (controls.current) {
      controls.current.target.copy(target);
      controls.current.update();
    }
  }, [camera, frame.centerHeight, frame.radius, preset, resetToken]);

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enabled={controlsEnabled}
      enableDamping
      dampingFactor={0.08}
      enablePan
      minDistance={frame.radius * 0.12}
      maxDistance={frame.radius * 8}
    />
  );
}

function VertexHandles({
  geometry,
  frame,
  onChange,
  onDraggingChange,
}: {
  geometry: RoofGeometry;
  frame: ModelFrame;
  onChange: (id: string, u: number, v: number) => void;
  onDraggingChange: (dragging: boolean) => void;
}) {
  const draggingId = useRef<string | null>(null);
  return (
    <group>
      {geometry.vertices.map((vertex) => (
        <mesh
          key={vertex.id}
          position={[vertex.plan[0], vertex.plan[2], -vertex.plan[1]]}
          renderOrder={5}
          onPointerDown={(event) => {
            event.stopPropagation();
            draggingId.current = vertex.id;
            (event.target as EventTarget & { setPointerCapture?: (pointerId: number) => void })
              .setPointerCapture?.(event.pointerId);
            onDraggingChange(true);
          }}
          onPointerUp={(event) => {
            if (draggingId.current !== vertex.id) return;
            event.stopPropagation();
            draggingId.current = null;
            (event.target as EventTarget & { releasePointerCapture?: (pointerId: number) => void })
              .releasePointerCapture?.(event.pointerId);
            onDraggingChange(false);
          }}
          onPointerCancel={() => {
            draggingId.current = null;
            onDraggingChange(false);
          }}
          onPointerMove={(event) => {
            if (draggingId.current !== vertex.id) return;
            event.stopPropagation();
            const plane = new Plane().setFromNormalAndCoplanarPoint(
              new Vector3(0, 1, 0),
              new Vector3(vertex.plan[0] - frame.centerU, vertex.plan[2], frame.centerV - vertex.plan[1]),
            );
            const point = event.ray.intersectPlane(plane, new Vector3());
            if (point) onChange(vertex.id, point.x + frame.centerU, frame.centerV - point.z);
          }}
        >
          <sphereGeometry args={[Math.max(frame.radius * 0.018, 0.24), 14, 10]} />
          <meshStandardMaterial color="#e28a31" emissive="#6f2c0a" emissiveIntensity={0.25} depthTest={false} />
        </mesh>
      ))}
    </group>
  );
}

function RoofScene({
  geometry,
  appearances,
  selectedPlaneId,
  onSelectPlane,
  cameraPreset,
  cameraResetToken,
  frame,
  editMode,
  onVertexChange,
  onDraggingChange,
}: {
  geometry: RoofGeometry;
  appearances: Record<string, RoofAppearance>;
  selectedPlaneId: string;
  onSelectPlane: (planeId: string) => void;
  cameraPreset: "3d" | "top";
  cameraResetToken: number;
  frame: ModelFrame;
  editMode: boolean;
  onVertexChange: (id: string, u: number, v: number) => void;
  onDraggingChange: (dragging: boolean) => void;
}) {
  return (
    <>
      <CameraController
        preset={cameraPreset}
        resetToken={cameraResetToken}
        frame={frame}
        controlsEnabled={!editMode}
      />
      <hemisphereLight args={[0xffffff, 0x667788, 1.05]} />
      <directionalLight position={[30, 60, 20]} intensity={1.6} />
      <group position={[-frame.centerU, 0, frame.centerV]}>
        <RoofModel
          geometry={geometry}
          appearances={appearances}
          selectedPlaneId={selectedPlaneId}
          onSelectPlane={onSelectPlane}
        />
        <WallsLayer geometry={geometry} />
        {editMode && (
          <VertexHandles
            geometry={geometry}
            frame={frame}
            onChange={onVertexChange}
            onDraggingChange={onDraggingChange}
          />
        )}
      </group>
      <mesh
        rotation={[-Math.PI / 2, 0, 0]}
        position={[0, frame.minHeight - 1, 0]}
      >
        <planeGeometry args={[frame.radius * 3.4, frame.radius * 3.4]} />
        <meshStandardMaterial color="#e7ece7" roughness={1} />
      </mesh>
    </>
  );
}

function humanizeMaterial(material: RoofMaterial): string {
  const labels: Record<RoofMaterial, string> = {
    asphalt_shingle: "Asphalt shingles",
    standing_seam_metal: "Standing seam metal",
    tile: "Tile",
    epdm: "EPDM membrane",
    tpo: "TPO membrane",
    pvc: "PVC membrane",
    modified_bitumen: "Modified bitumen",
  };
  return labels[material];
}

export function ProjectRoofViewer({
  version,
  user,
  projectId,
  geometryStatus,
  selections,
  onSelectionChange,
  onVersionChange,
  onReload,
}: {
  version: GeometryVersion | null;
  user: User;
  projectId: string;
  geometryStatus: Project["geometryStatus"];
  selections: Record<string, string>;
  onSelectionChange: (key: string, value: string) => void;
  onVersionChange: (version: GeometryVersion) => void;
  onReload: () => Promise<void>;
}) {
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [localSelections, setLocalSelections] = useState(selections);
  const [draft, setDraft] = useState<RoofGeometry | null>(null);
  const [selectedPlaneId, setSelectedPlaneId] = useState("");
  const [cameraPreset, setCameraPreset] = useState<"3d" | "top">("3d");
  const [cameraResetToken, setCameraResetToken] = useState(0);
  const [editMode, setEditMode] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [stackingConfirmed, setStackingConfirmed] = useState(false);
  const rawGeometry = version?.geometry ?? null;
  const geometry = useMemo(() => {
    if (!rawGeometry) return null;
    try {
      return migrateRoofGeometry(rawGeometry);
    } catch {
      return null;
    }
  }, [rawGeometry]);
  useEffect(() => {
    setDraft(geometry);
    setSelectedPlaneId(geometry?.planes[0]?.id ?? "");
    setEditMode(false);
    setStackingConfirmed(false);
  }, [geometry, version?.id]);

  const activeGeometry = draft ?? geometry;
  const hasUnresolvedOverlaps = Boolean(
    version?.analysis.validation?.buildDiagnostics?.unresolvedOverlaps?.length,
  );
  const renderGeometry = useMemo(
    () => activeGeometry && !hasUnresolvedOverlaps
      ? geometryFor3DPreview(activeGeometry)
      : null,
    [activeGeometry, hasUnresolvedOverlaps],
  );
  const validation = useMemo(
    () => activeGeometry
      ? validateGeometry(activeGeometry)
      : null,
    [activeGeometry],
  );
  const frame = useMemo(
    () => renderGeometry && renderGeometry.vertices.length > 0 ? frameGeometry(renderGeometry) : null,
    [renderGeometry],
  );
  const selectedPlane = activeGeometry?.planes.find((plane) => plane.id === selectedPlaneId)
    ?? null;
  const appearances = useMemo(
    () => activeGeometry
      ? Object.fromEntries(activeGeometry.planes.map((plane) => [
        plane.id,
        appearanceFromSelections(localSelections, plane),
      ]))
      : {},
    [activeGeometry?.planes, localSelections],
  );
  const selectedAppearance = selectedPlane ? appearanceFromSelections(localSelections, selectedPlane) : null;
  const allowedMaterials = selectedPlane?.roofType === "flat" ? flatMaterials : pitchedMaterials;

  function selectCamera(preset: "3d" | "top") {
    setCameraPreset(preset);
    setCameraResetToken((current) => current + 1);
  }

  function updateAppearance(key: "color" | "material", value: string) {
    if (!selectedPlane) return;
    const selectionKey = `appearance.${selectedPlane.id}.${key}`;
    setLocalSelections((current) => ({ ...current, [selectionKey]: value }));
    onSelectionChange(selectionKey, value);
  }

  function updateVertex(vertexId: string, u: number, v: number) {
    setDraft((current) => current ? {
      ...current,
      vertices: current.vertices.map((vertex) => vertex.id === vertexId
        ? { ...vertex, plan: [u, v, vertex.plan[2]] }
        : vertex),
    } : current);
  }

  function updatePlane(
    planeId: string,
    update: (plane: GeometryPlane) => GeometryPlane,
  ) {
    setDraft((current) => current ? {
      ...current,
      planes: current.planes.map((plane) => plane.id === planeId ? update(plane) : plane),
    } : current);
  }

  function movePlaneOrder(planeId: string, direction: -1 | 1) {
    setDraft((current) => {
      if (!current) return current;
      const ordered = [...current.planes].sort((left, right) => left.drawOrder - right.drawOrder);
      const index = ordered.findIndex((plane) => plane.id === planeId);
      const nextIndex = index + direction;
      if (index < 0 || nextIndex < 0 || nextIndex >= ordered.length) return current;
      [ordered[index], ordered[nextIndex]] = [ordered[nextIndex], ordered[index]];
      return {
        ...current,
        planes: current.planes.map((plane) => ({
          ...plane,
          drawOrder: ordered.findIndex((item) => item.id === plane.id),
        })),
      };
    });
  }

  async function saveGeometry() {
    if (!draft || !version?.isCurrent) return;
    setBusy(true);
    setSaveError(null);
    try {
      const saved = await apiRequest<GeometryVersion>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/geometry`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expectedCurrentVersionId: version.id,
            expectedVersion: version.version,
            confirmStackingOrder: stackingConfirmed,
            vertices: draft.vertices.map((vertex) => ({ id: vertex.id, plan: [vertex.plan[0], vertex.plan[1]] })),
            planes: draft.planes.map((plane) => ({
              id: plane.id,
              name: plane.name,
              roofType: plane.roofType,
              boundaryVertexIds: plane.boundaryVertexIds,
              pitch: {
                notation: plane.pitch.notation,
                rise: plane.pitch.rise,
                run: plane.pitch.run,
              },
              downslopePlanUnitVector: plane.pitch.downslopePlanUnitVector,
              reportedAreaSqFt: plane.reportedAreaSqFt,
              drawOrder: plane.drawOrder,
            })),
          }),
        },
      );
      onVersionChange(saved);
      setDraft(saved.geometry ? migrateRoofGeometry(saved.geometry) : null);
      setEditMode(false);
      await onReload();
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "Geometry changes could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  if (!version || !geometry) {
    const processing = geometryStatus === "processing" || version?.status === "processing";
    const failed = geometryStatus === "failed" || version?.status === "failed";
    return (
      <div className="card empty">
        <h2>Project roof geometry</h2>
        <p className="muted">
          {processing
            ? "The uploaded PDF is being analyzed for roof outlines, dimensions, and pitch. Open Geometry review to inspect its draft when reconstruction finishes."
            : failed
              ? "The PDF could not be reconstructed into an editable roof draft. Open Geometry review to inspect the report and retry reconstruction."
              : "No project-specific roof geometry is available yet. Upload a supported measurement PDF, then start reconstruction from Geometry review."}
        </p>
        {version?.analysis?.facets?.length ? (
          <p className="muted">The PDF contains {version.analysis.facets.length} candidate facet outline(s), but there is not enough calibrated evidence to render them in 3D yet.</p>
        ) : null}
        {processing && <span className="badge">Building project geometry…</span>}
      </div>
    );
  }

  if (!activeGeometry || !renderGeometry) {
    const reason = hasUnresolvedOverlaps
      ? "Confirm the overlapping plane order in Geometry review before opening this 3D preview."
      : geometry.heightSolve?.status !== "solved"
        ? "Shared-edge heights are incomplete or contradictory. Resolve the geometry review blockers before opening this 3D preview."
        : "The solved heights do not satisfy the report’s roof stacking order. Review the planes before opening this 3D preview.";
    return (
      <div className="card empty" role="status">
        <h2>3D roof preview unavailable</h2>
        <p className="muted">{reason}</p>
      </div>
    );
  }

  if (!frame) {
    return <div className="notice" role="status">The PDF has no renderable roof surfaces yet.</div>;
  }

  return (
    <div className="roof-viewer">
      {version.status === "approved" && <div className="notice green-notice" style={{ marginBottom: 13 }}>
        <b><Eye size={14} style={{ display: "inline", verticalAlign: "-2px", marginRight: 4 }} /> Approved project geometry.</b>{" "}
        This geometry is approved for estimates.
      </div>}
      {version.status !== "approved" && <div className="notice" role="status" style={{ marginBottom: 13 }}>
        <b>Review preview only.</b>{" "}
        {geometry.heightSolve?.status === "solved"
          ? "Review and approve this geometry before using it for estimates."
          : "Relative roof heights are estimated because shared-edge constraints remain unresolved. Review and approve this geometry before using it for estimates."}
      </div>}
      {saveError && <div className="error" role="alert">{saveError}</div>}
      <div className="card" style={{ padding: 12 }}>
        <div className="viewer">
          {validation?.valid ? <Canvas camera={{ fov: 40, near: 0.1, far: frame.radius * 20 }} dpr={[1, 1.7]}>
            <color attach="background" args={["#edf1f2"]} />
            <RoofScene
              geometry={renderGeometry}
              appearances={appearances}
              selectedPlaneId={selectedPlaneId}
              onSelectPlane={setSelectedPlaneId}
              cameraPreset={cameraPreset}
              cameraResetToken={cameraResetToken}
              frame={frame}
              editMode={editMode}
              onVertexChange={updateVertex}
              onDraggingChange={setDragging}
            />
          </Canvas> : <div className="card empty"><p className="muted">Correct the boundary coordinates below to restore the 3D preview.</p></div>}
        </div>
        <div className="viewer-controls" style={{ padding: "14px 6px 3px" }}>
          {selectedPlane && selectedAppearance ? <>
            <div className="roof-viewer-selection">
              <b>{selectedPlane.name}</b>
              <span className="muted" style={{ fontSize: 11 }}>Plane ID: {selectedPlane.id}</span>
              <span className="muted" style={{ fontSize: 12 }}>
                {selectedPlane.roofType === "flat" ? "Flat roof · drain " : "Pitched roof · "}
                {selectedPlane.pitch.notation} · {selectedPlane.reportedAreaSqFt === null ? "area not reported" : `report label ${selectedPlane.reportedAreaSqFt} SF`}
              </span>
              <span className="muted" style={{ fontSize: 11 }}>
                Calculated surface area {selectedPlane.meshSurfaceAreaSqFt.toFixed(1)} SF
              </span>
            </div>
            <label className="muted" htmlFor="roof-plane-material" style={{ fontSize: 12 }}>Material</label>
            <select
              id="roof-plane-material"
              className="input"
              value={selectedAppearance.material}
              onChange={(event) => updateAppearance("material", event.target.value)}
              style={{ width: 190, padding: "7px 9px" }}
            >
              {allowedMaterials.map((material) => (
                <option key={material} value={material}>{humanizeMaterial(material)} preview</option>
              ))}
            </select>
            <span className="muted" style={{ fontSize: 12 }}>Color</span>
            {swatches.map((swatch) => (
              <button
                key={swatch}
                type="button"
                className="swatch"
                style={{ background: swatch, outlineColor: swatch === selectedAppearance.color ? "#0d6a58" : undefined }}
                onClick={() => updateAppearance("color", swatch)}
                aria-label={`Select ${swatch} for ${selectedPlane.name}`}
              />
            ))}
            <label className="button secondary small" style={{ padding: "5px 7px" }}>
              Custom
              <input
                type="color"
                value={selectedAppearance.color}
                onChange={(event) => updateAppearance("color", event.target.value)}
                aria-label={`Custom color for ${selectedPlane.name}`}
                style={{ border: 0, padding: 0, width: 20, height: 20, background: "none" }}
              />
            </label>
          </> : <div className="roof-viewer-selection">
            <b>No roof selected</b>
            <span className="muted" style={{ fontSize: 12 }}>Select a facet to edit its material and color.</span>
          </div>}
          <button className="button secondary small" onClick={() => selectCamera("3d")}>
            <RotateCcw size={14} />Reset camera
          </button>
        </div>
        <div className="roof-viewer-toolbar">
          <button className={`button small ${cameraPreset === "3d" ? "" : "secondary"}`} onClick={() => selectCamera("3d")}>
            3D view
          </button>
          <button className={`button small ${cameraPreset === "top" ? "" : "secondary"}`} onClick={() => selectCamera("top")}>
            Top view · source diagram orientation
          </button>
          <button className={`button small ${editMode ? "" : "secondary"}`} onClick={() => setEditMode((current) => !current)}>
            {editMode ? "Done editing" : "Edit roof geometry"}
          </button>
          {editMode && (
            <button className="button small" onClick={() => void saveGeometry()} disabled={busy || dragging || !version.isCurrent || !validation?.valid}>
              <Save size={14} />{busy ? "Saving…" : "Save review draft"}
            </button>
          )}
        </div>
        {editMode && selectedPlane && (
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12, padding: "10px 4px" }}>
            <div className="card panel">
              <h3 style={{ marginTop: 0 }}>Selected plane</h3>
              <label className="field-label">Name</label>
              <input className="input" value={selectedPlane.name} onChange={(event) => updatePlane(selectedPlane.id, (plane) => ({ ...plane, name: event.target.value }))} />
              <label className="field-label">Roof type</label>
              <select className="input" value={selectedPlane.roofType} onChange={(event) => updatePlane(selectedPlane.id, (plane) => ({ ...plane, roofType: event.target.value as "flat" | "pitched" }))}>
                <option value="pitched">Pitched</option>
                <option value="flat">Flat</option>
              </select>
              <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                <label className="field-label">Pitch rise<input className="input" type="number" min="0" max="36" step="0.25" value={selectedPlane.pitch.rise} onChange={(event) => {
                  const rise = Number(event.target.value);
                  updatePlane(selectedPlane.id, (plane) => ({ ...plane, pitch: { ...plane.pitch, rise, notation: `${rise}:${plane.pitch.run}` } }));
                }} /></label>
                <label className="field-label">Pitch run<input className="input" type="number" min="0.25" max="36" step="0.25" value={selectedPlane.pitch.run} onChange={(event) => {
                  const run = Number(event.target.value);
                  updatePlane(selectedPlane.id, (plane) => ({ ...plane, pitch: { ...plane.pitch, run, notation: `${plane.pitch.rise}:${run}` } }));
                }} /></label>
                <label className="field-label">Downslope U<input className="input" type="number" step="0.05" value={selectedPlane.pitch.downslopePlanUnitVector[0]} onChange={(event) => {
                  const u = Number(event.target.value);
                  updatePlane(selectedPlane.id, (plane) => ({ ...plane, pitch: { ...plane.pitch, downslopePlanUnitVector: [u, plane.pitch.downslopePlanUnitVector[1]] } }));
                }} /></label>
                <label className="field-label">Downslope V<input className="input" type="number" step="0.05" value={selectedPlane.pitch.downslopePlanUnitVector[1]} onChange={(event) => {
                  const v = Number(event.target.value);
                  updatePlane(selectedPlane.id, (plane) => ({ ...plane, pitch: { ...plane.pitch, downslopePlanUnitVector: [plane.pitch.downslopePlanUnitVector[0], v] } }));
                }} /></label>
                <label className="field-label">Reported area (SF)<input className="input" type="number" min="0" step="1" value={selectedPlane.reportedAreaSqFt ?? ""} onChange={(event) => updatePlane(selectedPlane.id, (plane) => ({ ...plane, reportedAreaSqFt: event.target.value === "" ? null : Number(event.target.value) }))} /></label>
                <div className="field-label">Overlay order
                  <div className="row" style={{ gap: 6 }}>
                    <button className="button secondary small" disabled={selectedPlane.drawOrder >= activeGeometry.planes.length - 1} onClick={() => movePlaneOrder(selectedPlane.id, 1)}>Above</button>
                    <button className="button secondary small" disabled={selectedPlane.drawOrder <= 0} onClick={() => movePlaneOrder(selectedPlane.id, -1)}>Below</button>
                    <span>{selectedPlane.drawOrder}</span>
                  </div>
                </div>
              </div>
              <label className="field-label row" style={{ marginTop: 10, fontWeight: 400 }}>
                <input type="checkbox" checked={stackingConfirmed} onChange={(event) => setStackingConfirmed(event.target.checked)} />
                I reviewed which planes overlay the others
              </label>
            </div>
            <div className="card panel">
              <h3 style={{ marginTop: 0 }}>Boundary vertices</h3>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>Drag orange points on the model or enter plan coordinates in feet. Shared vertices update every connected roof plane.</p>
              {selectedPlane.boundaryVertexIds.map((vertexId) => {
                const vertex = activeGeometry.vertices.find((item) => item.id === vertexId);
                if (!vertex) return null;
                return (
                  <div className="row" key={vertex.id} style={{ gap: 7, marginBottom: 8 }}>
                    <span className="muted" style={{ width: 62, fontSize: 11 }}>{vertex.id}</span>
                    <label className="field-label" style={{ margin: 0 }}>U<input className="input" type="number" step="0.1" value={vertex.plan[0]} onChange={(event) => updateVertex(vertex.id, Number(event.target.value), vertex.plan[1])} /></label>
                    <label className="field-label" style={{ margin: 0 }}>V<input className="input" type="number" step="0.1" value={vertex.plan[1]} onChange={(event) => updateVertex(vertex.id, vertex.plan[0], Number(event.target.value))} /></label>
                  </div>
                );
              })}
            </div>
          </div>
        )}
        <div className="roof-viewer-toolbar">
          {activeGeometry.planes.map((plane) => (
            <button
              key={plane.id}
              type="button"
              className={`button small ${plane.id === selectedPlaneId ? "" : "secondary"}`}
              onClick={() => setSelectedPlaneId((current) => current === plane.id ? "" : plane.id)}
            >
              {plane.roofType === "flat" ? "Flat" : "Pitched"} · {plane.pitch.notation} · {plane.reportedAreaSqFt ?? "area n/a"} SF
            </button>
          ))}
          <button
            className="button secondary small"
            type="button"
            disabled={!selectedPlaneId}
            onClick={() => setSelectedPlaneId("")}
          >
            Clear selection
          </button>
        </div>
        <div className="roof-viewer-meta">
          <span>One-story walls use an estimated 8-ft height and the roof eave outline as a footprint proxy.</span>
        </div>
      </div>
    </div>
  );
}
