"""Typed API and normalized extraction schemas."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

QuantityState = Literal["measured", "zero", "not_surveyed", "unknown", "not_reported"]
QuantityOrigin = Literal["measured", "calculated", "manual_override"]


class QuantityValue(BaseModel):
    state: QuantityState
    value: Decimal | None = None
    unit: str
    source: str | None = None

    @field_validator("value")
    @classmethod
    def state_matches_value(cls, value: Decimal | None, info: Any) -> Decimal | None:
        state = info.data.get("state")
        if state in {"measured", "zero"} and value is None:
            raise ValueError("measured and zero values require a numeric value")
        if state == "zero" and value != 0:
            raise ValueError("zero state must contain numeric zero")
        if state not in {"measured", "zero"} and value is not None:
            raise ValueError("unknown and unreported values cannot contain a number")
        return value


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    customer: Customer | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    customer: Customer | None = None
    materialSelections: dict[str, str] | None = None


class MeasurementEdit(BaseModel):
    values: dict[str, QuantityValue]
    sourceWastePercentage: Decimal | None = Field(default=None, ge=0, le=100)
    predominantPitchNotation: str | None = Field(default=None, max_length=30)
    expectedCurrentVersionId: str | None = None

    @model_validator(mode="after")
    def validate_facet_count(self) -> MeasurementEdit:
        quantity = self.values.get("roofPlaneCount")
        if (
            quantity is not None
            and quantity.value is not None
            and quantity.value != quantity.value.to_integral_value()
        ):
            raise ValueError("Roof facet count must be a whole number")
        return self


class GeometryReconstructRequest(BaseModel):
    reportId: str | None = Field(default=None, min_length=1, max_length=160)


class GeometryPitchEdit(BaseModel):
    notation: str = Field(min_length=1, max_length=20)
    rise: Decimal = Field(ge=0, le=36)
    run: Decimal = Field(gt=0, le=36)


class GeometryFacetEdit(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    roofType: Literal["flat", "pitched"]
    polygonPagePoints: list[tuple[FiniteFloat, FiniteFloat]] = Field(
        min_length=3, max_length=500
    )
    pitch: GeometryPitchEdit | None = None
    downslopeDirectionPage: tuple[FiniteFloat, FiniteFloat] | None = None
    edgeTypes: list[
        Literal[
            "eave",
            "rake",
            "ridge",
            "hip",
            "valley",
            "wall_flashing",
            "step_flashing",
            "curb_flashing",
            "transition",
            "unspecified",
            "parapet",
            "other",
        ]
    ] = Field(default_factory=list, max_length=500)
    reportedAreaSqFt: Decimal | None = Field(default=None, ge=0, le=10_000_000)
    drawOrder: int | None = Field(default=None, ge=0, le=100)
    confidence: FiniteFloat | None = Field(default=None, ge=0, le=1)
    provenance: dict[
        Literal["outline", "pitch", "downslopeDirection", "area"],
        Literal["reported", "inferred", "user_edited"],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def direction_must_be_nonzero(self) -> GeometryFacetEdit:
        if self.downslopeDirectionPage is not None and self.downslopeDirectionPage == (0, 0):
            raise ValueError("Downslope direction must be nonzero")
        if self.edgeTypes and len(self.edgeTypes) != len(self.polygonPagePoints):
            raise ValueError("Each plane boundary segment needs one edge type")
        return self


class GeometryEditRequest(BaseModel):
    expectedCurrentVersionId: str = Field(min_length=1, max_length=160)
    expectedVersion: int = Field(ge=1)
    basedOnVersionId: str | None = Field(default=None, min_length=1, max_length=160)
    facets: list[GeometryFacetEdit] = Field(min_length=1, max_length=100)
    manualFeetPerPoint: Decimal | None = Field(default=None, gt=0, le=100)
    confirmManualScale: bool = False
    confirmStackingOrder: bool = False


class Geometry3DVertexEdit(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    plan: tuple[FiniteFloat, FiniteFloat]


class Geometry3DPlaneEdit(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    roofType: Literal["flat", "pitched"]
    boundaryVertexIds: list[str] = Field(min_length=3, max_length=500)
    pitch: GeometryPitchEdit
    downslopePlanUnitVector: tuple[FiniteFloat, FiniteFloat]
    reportedAreaSqFt: Decimal | None = Field(default=None, ge=0, le=10_000_000)
    drawOrder: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def direction_must_be_nonzero(self) -> Geometry3DPlaneEdit:
        if math.hypot(*self.downslopePlanUnitVector) <= 1e-8:
            raise ValueError("Downslope direction must be nonzero")
        if len(set(self.boundaryVertexIds)) < 3:
            raise ValueError("A plane boundary must reference at least three distinct vertices")
        return self


class Geometry3DEditRequest(BaseModel):
    expectedCurrentVersionId: str = Field(min_length=1, max_length=160)
    expectedVersion: int = Field(ge=1)
    vertices: list[Geometry3DVertexEdit] = Field(min_length=3, max_length=5000)
    planes: list[Geometry3DPlaneEdit] = Field(min_length=1, max_length=100)
    confirmStackingOrder: bool = False

    @model_validator(mode="after")
    def geometry_references_must_be_valid(self) -> Geometry3DEditRequest:
        vertex_ids = [vertex.id for vertex in self.vertices]
        plane_ids = [plane.id for plane in self.planes]
        if len(set(vertex_ids)) != len(vertex_ids):
            raise ValueError("Vertex IDs must be unique")
        if len(set(plane_ids)) != len(plane_ids):
            raise ValueError("Plane IDs must be unique")
        known_vertices = set(vertex_ids)
        for plane in self.planes:
            if not set(plane.boundaryVertexIds).issubset(known_vertices):
                raise ValueError(f"Plane {plane.id} references an unknown vertex")
        return self


class GeometryApproveRequest(BaseModel):
    expectedVersion: int = Field(ge=1)
    confirmation: bool


class PitchArea(BaseModel):
    pitch: str
    areaSqFt: Decimal = Field(ge=0)
    squares: Decimal | None = Field(default=None, ge=0)
    source: str


class RoofPlane(BaseModel):
    id: str
    name: str
    roofType: Literal["flat", "pitched"]
    areaSqFt: Decimal | None = Field(default=None, ge=0)
    pitch: str | None = None
    pitchDegrees: Decimal | None = Field(default=None, ge=0, le=90)
    vertices: list[dict[str, Decimal | str]] = Field(default_factory=list)
    edges: list[str] = Field(default_factory=list)
    materialId: str | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    source: str


class WasteRecommendation(BaseModel):
    roofType: Literal["flat", "pitched"]
    wastePercentage: Decimal = Field(ge=0, le=100)
    sourceAreaSqFt: Decimal = Field(ge=0)
    sourceAdjustedAreaSqFt: Decimal = Field(ge=0)
    source: str


class RoofSystemMeasurement(BaseModel):
    roofType: Literal["flat", "pitched"]
    areaSqFt: Decimal = Field(ge=0)
    selectedSystem: str | None = None
    configurationRequired: bool = True
    source: str


class MaterialInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=3000)
    expectedLifespan: str | None = Field(default=None, max_length=80)
    availabilityNote: str | None = Field(default=None, max_length=500)
    specialOrder: bool = False
    unit: str = Field(min_length=1, max_length=30)
    coveragePerUnit: Decimal | None = Field(default=None, gt=0)
    coverageUnit: str | None = None
    packageSize: Decimal | None = Field(default=None, gt=0)
    packageUnit: str | None = None
    pricePerUnit: Decimal = Field(ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    defaultWastePercentage: Decimal | None = Field(default=None, ge=0, le=100)
    supplier: str | None = Field(default=None, max_length=120)
    active: bool = True
    placeholderPrice: bool = True


class EstimateLineInput(BaseModel):
    description: str = Field(min_length=1, max_length=160)
    quantity: Decimal = Field(ge=0)
    unit: str = Field(min_length=1, max_length=30)
    unitPrice: Decimal = Field(ge=0)
    quantityUnit: str | None = None
    wastePercentage: Decimal | None = Field(default=None, ge=0, le=100)
    coveragePerUnit: Decimal | None = Field(default=None, gt=0)
    coverageUnit: str | None = None
    packageSize: Decimal | None = Field(default=None, gt=0)
    sourceReportedQuantity: Decimal | None = Field(default=None, ge=0)
    sourceReportedUnit: str | None = Field(default=None, min_length=1, max_length=30)
    quantityOrigin: QuantityOrigin = "measured"
    overrideQuantity: Decimal | None = Field(default=None, ge=0)
    fixedCharge: bool = False
    kind: Literal["material", "labour", "disposal", "fixed"] = "material"
    roofType: Literal["flat", "pitched"] | None = None
    roofingSystem: (
        Literal["asphalt_shingle", "epdm", "tpo", "pvc", "modified_bitumen", "other"] | None
    ) = None
    rollWidthFeet: Decimal | None = Field(default=None, gt=0)
    rollLengthFeet: Decimal | None = Field(default=None, gt=0)
    lapWidthInches: Decimal | None = Field(default=None, ge=0)
    lapLengthInches: Decimal | None = Field(default=None, ge=0)
    requireWasteSelection: bool = False

    @field_validator("lapWidthInches", "lapLengthInches")
    @classmethod
    def lap_cannot_exceed_reasonable_dimension(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value > Decimal("120"):
            raise ValueError("Lap dimensions must be 120 inches or less")
        return value

    @model_validator(mode="after")
    def flat_system_cannot_use_asphalt_shingles(self) -> EstimateLineInput:
        if self.roofType == "flat" and self.roofingSystem == "asphalt_shingle":
            raise ValueError("Flat roof areas cannot use the asphalt shingle system")
        return self


class EstimateCreate(BaseModel):
    lines: list[EstimateLineInput] = Field(min_length=1, max_length=499)
    markupPercentage: Decimal | None = Field(default=None, ge=0, le=1000)
    taxRate: Decimal | None = Field(default=None, ge=0, le=1)
    disposalTotal: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


MeasurementScalar = Decimal | str | dict[str, object] | None


class RawRoofReport(BaseModel):
    """Validated report sections; absent measurements remain explicitly unreported."""

    model_config = ConfigDict(extra="allow")
    project_totals: dict[str, MeasurementScalar] = Field(default_factory=dict)
    measurements: dict[str, MeasurementScalar] = Field(default_factory=dict)
    edge_lengths: dict[str, MeasurementScalar] = Field(default_factory=dict)
    materials: list[dict[str, object]] = Field(default_factory=list)
    gutters: dict[str, MeasurementScalar] = Field(default_factory=dict)
    extraction_notes: list[str] = Field(default_factory=list)
    source_format: str | None = None
    property: dict[str, MeasurementScalar] = Field(default_factory=dict)
    roof_systems: list[dict[str, Any]] = Field(default_factory=list)
    areas_by_pitch: list[dict[str, Any]] = Field(default_factory=list)
    roof_planes: list[dict[str, Any]] = Field(default_factory=list)
    flashings: dict[str, MeasurementScalar] = Field(default_factory=dict)
    transitions: dict[str, MeasurementScalar] = Field(default_factory=dict)
    waste_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    geometry_review: dict[str, Any] = Field(default_factory=dict)


class MeasurementVersion(BaseModel):
    sourceReportId: str
    schemaVersion: str
    origin: Literal["extracted", "user_reviewed"]
    basedOnVersionId: str | None = None
    sourceWastePercentage: Decimal | None = Field(default=None, ge=0, le=100)
    predominantPitchNotation: str | None = None
    values: dict[str, QuantityValue]
    materialQuantityReferences: list[dict[str, Any]] = Field(default_factory=list)
    fieldProvenance: dict[str, dict[str, Any]]
    warnings: list[str]
    sourceFormat: str = "roof_manager"
    totalRoofAreaSqFt: Decimal | None = Field(default=None, ge=0)
    pitchedRoofAreaSqFt: Decimal | None = Field(default=None, ge=0)
    flatRoofAreaSqFt: Decimal | None = Field(default=None, ge=0)
    roofFacets: int | None = Field(default=None, ge=0)
    areasByPitch: list[PitchArea] = Field(default_factory=list)
    roofPlanes: list[RoofPlane] = Field(default_factory=list)
    roofSystems: list[RoofSystemMeasurement] = Field(default_factory=list)
    edgeLengths: dict[str, QuantityValue] = Field(default_factory=dict)
    flashings: dict[str, QuantityValue] = Field(default_factory=dict)
    transitions: dict[str, QuantityValue] = Field(default_factory=dict)
    wasteRecommendations: list[WasteRecommendation] = Field(default_factory=list)
    sourceMeasurements: dict[str, QuantityValue] = Field(default_factory=dict)
    correctedMeasurements: dict[str, QuantityValue] = Field(default_factory=dict)
    geometryStatus: Literal["not_started", "needs_review", "approved", "failed"] = "not_started"
    geometryEvidence: dict[str, Any] = Field(default_factory=dict)
