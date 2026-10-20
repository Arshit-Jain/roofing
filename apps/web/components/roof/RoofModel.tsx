"use client";

import { useEffect, useMemo } from "react";
import {
  BufferGeometry,
  CanvasTexture,
  DoubleSide,
  Float32BufferAttribute,
  RepeatWrapping,
  SRGBColorSpace,
} from "three";
import { planeToMesh, type GeometryPlane, type RoofGeometry } from "@/lib/roof/geometry";

export type RoofMaterial =
  | "asphalt_shingle"
  | "standing_seam_metal"
  | "tile"
  | "epdm"
  | "tpo"
  | "pvc"
  | "modified_bitumen";

export type RoofAppearance = { color: string; material: RoofMaterial };
export type RoofAppearances = Record<string, RoofAppearance>;

const defaults: Record<"flat" | "pitched", RoofAppearance> = {
  pitched: { color: "#35414b", material: "asphalt_shingle" },
  flat: { color: "#e5e7df", material: "tpo" },
};

function createPatternTexture(material: RoofMaterial): CanvasTexture | null {
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.fillStyle = "#e7e8e2";
  context.fillRect(0, 0, 128, 128);
  context.strokeStyle = "#aab4ad";
  context.lineWidth = 2;

  if (material === "standing_seam_metal") {
    for (let x = 8; x < 128; x += 32) {
      context.beginPath();
      context.moveTo(x, 0);
      context.lineTo(x, 128);
      context.stroke();
      context.beginPath();
      context.moveTo(x + 3, 0);
      context.lineTo(x + 3, 128);
      context.stroke();
    }
  } else if (material === "tile") {
    for (let row = 0; row < 4; row += 1) {
      for (let column = 0; column < 4; column += 1) {
        context.beginPath();
        context.roundRect(column * 32 + (row % 2 ? -15 : 1), row * 32 + 1, 30, 30, 8);
        context.stroke();
      }
    }
  } else if (material === "asphalt_shingle") {
    for (let row = 0; row < 8; row += 1) {
      const offset = row % 2 ? -16 : 0;
      for (let column = 0; column < 5; column += 1) {
        const x = column * 32 + offset;
        const y = row * 16;
        context.beginPath();
        context.moveTo(x, y);
        context.lineTo(x, y + 16);
        context.lineTo(x + 32, y + 16);
        context.stroke();
      }
    }
  } else {
    for (const y of [8, 64, 120]) {
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(128, y);
      context.stroke();
    }
    context.lineWidth = 1;
    for (let x = 16; x < 128; x += 32) {
      context.beginPath();
      context.moveTo(x, 8);
      context.lineTo(x, 120);
      context.stroke();
    }
  }

  const texture = new CanvasTexture(canvas);
  texture.wrapS = RepeatWrapping;
  texture.wrapT = RepeatWrapping;
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

function planeGeometry(positions: number[], uvs: number[], indices?: number[]): BufferGeometry {
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new Float32BufferAttribute(positions, 3));
  if (uvs.length > 0) geometry.setAttribute("uv", new Float32BufferAttribute(uvs, 2));
  if (indices) {
    geometry.setIndex(indices);
    geometry.computeVertexNormals();
  }
  return geometry;
}

function RoofPlaneSurface({
  geometry,
  plane,
  appearance,
  selected,
  onSelect,
}: {
  geometry: RoofGeometry;
  plane: GeometryPlane;
  appearance: RoofAppearance;
  selected: boolean;
  onSelect: () => void;
}) {
  const meshData = useMemo(() => planeToMesh(geometry, plane), [geometry, plane]);
  const surface = useMemo(
    () => planeGeometry(meshData.positions, meshData.uvs, meshData.indices),
    [meshData],
  );
  const outline = useMemo(
    () => planeGeometry(meshData.outlinePositions, []),
    [meshData],
  );
  const texture = useMemo(() => createPatternTexture(appearance.material), [appearance.material]);
  useEffect(() => () => {
    surface.dispose();
    outline.dispose();
    texture?.dispose();
  }, [outline, surface, texture]);

  return (
    <group>
      <mesh
        geometry={surface}
        onClick={(event) => {
          event.stopPropagation();
          onSelect();
        }}
      >
        <meshBasicMaterial
          map={texture}
          color={selected ? "#d49c45" : appearance.color}
          side={DoubleSide}
          flatShading
        />
      </mesh>
      <lineSegments geometry={outline} renderOrder={2}>
        <lineBasicMaterial color={selected ? "#e99a42" : "#849197"} depthTest toneMapped={false} />
      </lineSegments>
    </group>
  );
}

export function RoofModel({
  geometry,
  appearances,
  selectedPlaneId,
  onSelectPlane,
}: {
  geometry: RoofGeometry;
  appearances: RoofAppearances;
  selectedPlaneId: string;
  onSelectPlane: (planeId: string) => void;
}) {
  return (
    <group>
      {[...geometry.planes].sort((left, right) => left.drawOrder - right.drawOrder).map((plane) => (
        <RoofPlaneSurface
          key={plane.id}
          geometry={geometry}
          plane={plane}
          appearance={appearances[plane.id] ?? defaults[plane.roofType]}
          selected={selectedPlaneId === plane.id}
          onSelect={() => onSelectPlane(selectedPlaneId === plane.id ? "" : plane.id)}
        />
      ))}
    </group>
  );
}
