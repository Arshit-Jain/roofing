"use client";

import { useEffect, useMemo } from "react";
import { BufferGeometry, DoubleSide, Float32BufferAttribute } from "three";
import {
  oneStoryWallMeshes,
  roofWallLoops,
  wallBaseToMesh,
  type GeometryWallLoop,
  type RoofGeometry,
} from "@/lib/roof/geometry";

function EstimatedBase({ geometry, loop }: {
  geometry: RoofGeometry;
  loop: GeometryWallLoop;
}) {
  const base = useMemo(() => {
    const geometryValue = new BufferGeometry();
    geometryValue.setAttribute(
      "position",
      new Float32BufferAttribute(wallBaseToMesh(geometry, loop), 3),
    );
    geometryValue.computeVertexNormals();
    return geometryValue;
  }, [geometry, loop]);

  useEffect(() => () => base.dispose(), [base]);

  return (
    <mesh geometry={base}>
      <meshStandardMaterial color="#929b98" roughness={0.95} side={DoubleSide} flatShading />
    </mesh>
  );
}

function EstimatedSurface({ positions, color }: {
  positions: number[];
  color: string;
}) {
  const surface = useMemo(() => {
    const geometryValue = new BufferGeometry();
    geometryValue.setAttribute(
      "position",
      new Float32BufferAttribute(positions, 3),
    );
    geometryValue.computeVertexNormals();
    return geometryValue;
  }, [positions]);

  useEffect(() => () => surface.dispose(), [surface]);

  if (positions.length === 0) return null;

  return (
    <mesh geometry={surface}>
      <meshStandardMaterial color={color} roughness={0.92} side={DoubleSide} flatShading />
    </mesh>
  );
}

export function WallsLayer({ geometry }: { geometry: RoofGeometry }) {
  const wallLoops = useMemo(() => roofWallLoops(geometry), [geometry]);
  const wallMeshes = useMemo(() => oneStoryWallMeshes(geometry), [geometry]);
  return (
    <group>
      {wallLoops.map((loop) => (
        <group key={loop.id}>
          <EstimatedBase geometry={geometry} loop={loop} />
        </group>
      ))}
      <EstimatedSurface positions={wallMeshes.walls} color="#d9d2c3" />
      <EstimatedSurface positions={wallMeshes.gables} color="#c7bfaf" />
    </group>
  );
}
