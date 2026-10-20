import type { RoofGeometry } from "@/lib/roof/geometry";

export const projectStatuses = ["uploaded", "processing", "needs_review", "ready", "failed"] as const;
export type ProjectStatus = (typeof projectStatuses)[number];

export const quantityStates = ["measured", "zero", "not_surveyed", "unknown", "not_reported"] as const;
export type QuantityState = (typeof quantityStates)[number];

export const quantityOrigins = ["measured", "calculated", "manual_override"] as const;
export type QuantityOrigin = (typeof quantityOrigins)[number];

export type Project = {
  id: string;
  name: string;
  address: string | null;
  status: ProjectStatus;
  currentReportId?: string | null;
  currentMeasurementVersionId: string | null;
  currentGeometryVersionId?: string | null;
  geometryStatus?: "not_started" | "processing" | "needs_review" | "approved" | "failed";
  materialSelections?: Record<string, string>;
  updatedAt?: string;
};

export type Quantity = {
  state: QuantityState;
  value: string | null;
  unit: string;
  source?: string | null;
};

export type MeasurementVersion = {
  id: string;
  sourceReportId: string;
  origin: "extracted" | "user_reviewed";
  sourceWastePercentage: string | null;
  predominantPitchNotation?: string | null;
  values: Record<string, Quantity>;
  materialQuantityReferences: Array<{
    material?: string;
    quantity?: number | string | null;
    unit?: string | null;
    quantity_with_waste?: number | string | null;
    unit_with_waste?: string | null;
    based_on?: string | null;
    coveragePerUnit?: number | string | null;
    coverageUnit?: string | null;
    coveragePurchaseUnit?: string | null;
  }>;
  warnings: string[];
  sourceFormat?: "roof_manager" | "roofr" | string;
  totalRoofAreaSqFt?: string | null;
  pitchedRoofAreaSqFt?: string | null;
  flatRoofAreaSqFt?: string | null;
  roofFacets?: number | null;
  areasByPitch?: Array<{ pitch: string; areaSqFt: string; squares: string | null; source: string }>;
  roofPlanes?: RoofPlane[];
  roofSystems?: Array<{ roofType: "flat" | "pitched"; areaSqFt: string; selectedSystem: string | null; configurationRequired: boolean; source: string }>;
  edgeLengths?: Record<string, Quantity>;
  flashings?: Record<string, Quantity>;
  transitions?: Record<string, Quantity>;
  wasteRecommendations?: Array<{ roofType: "flat" | "pitched"; wastePercentage: string; sourceAreaSqFt: string; sourceAdjustedAreaSqFt: string; source: string }>;
  sourceMeasurements?: Record<string, Quantity>;
  correctedMeasurements?: Record<string, Quantity>;
  geometryStatus?: "not_started" | "needs_review" | "approved" | "failed";
  geometryEvidence?: Record<string, unknown>;
};

export type RoofPlane = {
  id: string;
  name: string;
  roofType: "flat" | "pitched";
  areaSqFt: string | null;
  pitch: string | null;
  pitchDegrees: string | null;
  vertices: Array<Record<string, string>>;
  edges: string[];
  materialId: string | null;
  confidence: string | null;
  source: string;
};

export type Report = {
  id: string;
  originalFilename: string;
  status: string;
  extractionStatus: string;
  extractionError?: string;
  extractionErrorCode?: string;
  extractionErrorStage?: string;
  extractionErrorOperation?: string;
};

export type Job = {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  error?: { code?: string; message?: string; stage?: string; operation?: string };
  progress?: { stage: string; message: string; at?: string };
  events?: Array<{
    stage: string;
    message: string;
    level?: "info" | "warning" | "error";
    at?: string;
  }>;
};

export type GeometryFacetEvidence = {
  id: string;
  name: string;
  roofType: "flat" | "pitched";
  areaLabelSqFt: number | null;
  polygonPagePoints: Array<[number, number]>;
  pitch: { notation: string; rise: number; run: number; degrees?: number } | null;
  downslopeDirectionPage: [number, number] | null;
  edgeTypes?: Array<"eave" | "rake" | "ridge" | "hip" | "valley" | "wall_flashing" | "step_flashing" | "curb_flashing" | "transition" | "unspecified" | "parapet" | "other">;
  drawOrder?: number;
  source?: string;
  confidence?: number | "low" | "medium" | "high";
  provenance?: Partial<Record<"outline" | "pitch" | "downslopeDirection" | "area", "reported" | "inferred" | "user_edited">>;
};

export type GeometryValidationCheck = {
  name: string;
  status: "pass" | "warn" | "fail" | "not_evaluable";
  actual: number | null;
  expected: number | null;
  tolerancePercent?: number;
  toleranceDegrees?: number;
  differenceDegrees?: number;
  unit?: string;
  reason?: string;
};

export type GeometryAnalysis = {
  status: "needs_review" | "unsupported" | "failed" | string;
  sourceFormat: "roofr" | "roof_manager" | "unknown" | string;
  reason?: string;
  coordinatePageNumber?: number | null;
  coordinatePageSize?: { widthPoints: number; heightPoints: number } | null;
  pageCalibrations?: {
    feetPerPoint?: number | null;
    originPagePoint?: [number, number] | null;
    method?: string;
    hypothesisChosen?: string | null;
    tolerances?: Record<string, number>;
  };
  facets: GeometryFacetEvidence[];
  validation?: {
    checks: GeometryValidationCheck[];
    summary?: { passCount: number; warningCount: number; failureCount: number; notEvaluableCount: number; status: string };
    scaleHypotheses?: Record<string, unknown>;
    buildDiagnostics?: {
      warnings?: string[];
      failures?: string[];
      unresolvedOverlaps?: Array<{ firstPlaneId: string; secondPlaneId: string }>;
    };
  };
  warnings: string[];
  assumptions?: string[];
  stackingOrderConfirmed?: boolean;
  manualInputRequired?: string[];
  reported?: Record<string, unknown>;
};

export type GeometryVersion = {
  id: string;
  projectId: string;
  sourceReportId: string;
  measurementVersionId?: string | null;
  status: "processing" | "needs_review" | "approved" | "failed";
  source: "demo" | "extracted" | "assisted" | "manual";
  confidence: "illustrative" | "low" | "medium" | "high";
  estimatedHeights: boolean;
  version: number;
  basedOnVersionId: string | null;
  isCurrent?: boolean;
  jobId?: string;
  error?: { code: string; message: string };
  analysis: GeometryAnalysis;
  geometry: RoofGeometry | null;
  approvalBlockers?: string[];
};

export type AppConfig = {
  currency: string;
  defaultTaxRate: string;
  defaultWastePercentage: string;
  defaultMarkupPercentage: string;
  maxUploadBytes: number;
  maxPdfPages: number;
  jobPollIntervalSeconds: number;
  jobPollTimeoutSeconds: number;
};

export type Material = {
  id: string;
  name: string;
  category: string;
  description?: string | null;
  expectedLifespan?: string | null;
  availabilityNote?: string | null;
  specialOrder?: boolean;
  unit: string;
  pricePerUnit: string;
  currency: string;
  coveragePerUnit?: string | null;
  coverageUnit?: string | null;
  packageSize?: string | null;
  packageUnit?: string | null;
  defaultWastePercentage: string;
  placeholderPrice?: boolean;
  active: boolean;
};

export type EstimateLine = {
  description: string;
  quantity: string;
  quantityUnit?: string;
  unit: string;
  unitPrice: string;
  coveragePerUnit?: string | null;
  coverageUnit?: string | null;
  packageSize?: string | null;
  wastePercentage: string;
  /** UI grouping for roof-level waste selection; omitted from API requests. */
  wasteGroup?: "main" | "flat" | "pitched";
  quantityOrigin?: QuantityOrigin;
  kind: "material" | "labour" | "disposal" | "fixed";
  fixedCharge?: boolean;
  roofType?: "flat" | "pitched";
  roofingSystem?: "asphalt_shingle" | "epdm" | "tpo" | "pvc" | "modified_bitumen" | "other";
  rollWidthFeet?: string | null;
  rollLengthFeet?: string | null;
  lapWidthInches?: string | null;
  lapLengthInches?: string | null;
  requireWasteSelection?: boolean;
  overrideQuantity?: string | null;
};

export type EstimateLineResult = {
  description: string;
  quantity: string | null;
  sourceQuantity: string;
  calculatedQuantity: string | null;
  finalQuantity: string | null;
  manualOverride: string | null;
  sourceQuantityOrigin: QuantityOrigin;
  unit: string;
  unitPrice: string;
  wastePercentage: string;
  lineTotal: string | null;
  quantityOrigin: QuantityOrigin;
  wasteAdjustedQuantity: string;
  quantityUnit: string;
  calculationInputs: {
    sourceReportedQuantity: string | null;
    sourceReportedUnit: string | null;
    [key: string]: unknown;
  };
  requiresConfiguration: boolean;
  warnings: string[];
};

export type EstimateResult = {
  id: string;
  lines: EstimateLineResult[];
  completeForPricing: boolean;
  materialSubtotal: string;
  labourTotal: string;
  disposalTotal: string;
  additionalChargesTotal: string;
  markupTotal: string;
  taxTotal: string;
  grandTotal: string;
  currency: string;
  warnings: string[];
};
