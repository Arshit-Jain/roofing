# Firestore data model

Firestore is the only persistent application store. All API paths are derived from IDs after token verification and ownership checks. The Admin SDK bypasses rules, so route-level authorization is mandatory. Money fields use integer minor units or decimal strings; this implementation currently persists money as decimal strings.

## Implemented documents

### `users/{uid}`

Created on the first verified request if missing: `displayName`, `email`, `createdAt`, and `updatedAt` (server timestamps). The current API creates the profile but does not yet update `updatedAt` on each login.

### `users/{uid}/materialCatalog/{materialId}`

`name`, `category`, `unit`, optional `coveragePerUnit`, `coverageUnit`, `packageSize`, and `packageUnit`; `pricePerUnit` as a decimal string; `currency` (from `DEFAULT_CURRENCY`, CAD by default); `defaultWastePercentage` (from `DEFAULT_WASTE_PERCENTAGE` unless provided); optional `supplier`; `active`; `placeholderPrice`; `updatedAt`. Records are scoped by the authenticated user's UID. Prices entered in the UI are explicitly placeholders until the contractor enters a real price.

### `projects/{projectId}`

`ownerUid`, `name`, optional `address`, optional small `customer` map (`name`, `email`, `phone`), `status`, `currentReportId`, `currentMeasurementVersionId`, `currentGeometryVersionId`, `materialSelections`, `createdAt`, and `updatedAt`. Status values used by the app are `uploaded`, `processing`, `needs_review`, `ready`, and `failed`; deletion briefly uses `deleting`.

### `projects/{projectId}/reports/{reportId}`

`ownerUid`, sanitized display-only `originalFilename`, `contentType`, `fileSizeBytes`, whole-file `sha256`, `chunkSizeBytes`, `chunkCount`, `status`, `extractionStatus`, optional sanitized `extractionError`, and server timestamps (`createdAt`, `updatedAt`, `completedAt`). The report ID is derived from the project-scoped PDF SHA-256. Upload statuses: `uploading`, `complete`, `failed`, `deleting`. Extraction statuses: `not_started`, `queued`, `processing`, `succeeded`, `failed`.

### `{report-or-artifact}/chunks/{chunkId}`

Each child has numeric `index`, Firestore bytes `data`, and SHA-256 `checksum`. IDs are zero-padded (`000000`, `000001`, …). The API verifies contiguous indexes, each chunk hash, total size, and whole-file hash. Chunk documents are deleted before their parent. The `data` field has a single-field index exemption.

### `projects/{projectId}/measurementVersions/{versionId}`

`sourceReportId`, `schemaVersion`, `sourceFormat`, `origin` (`extracted` or `user_reviewed`), optional `basedOnVersionId`, optional `sourceWastePercentage`, `predominantPitchNotation`, `values`, `materialQuantityReferences`, `fieldProvenance`, `warnings`, and `createdAt`. `values` is a map of normalized fields to tagged quantity objects: `{state, value?, unit, source?}`, where state is `measured`, `zero`, `not_surveyed`, `unknown`, or `not_reported`. Corrected versions retain source measurements in `sourceMeasurements` and changed values in `correctedMeasurements`; the original version is not overwritten. A transaction updates the project's current-version pointer. A missing source waste value remains missing until a user sets it; Roofr's separate flat/pitched recommendation rows are source references and are not treated as a selected project factor.

Roofr measurement versions additionally store `totalRoofAreaSqFt`, `pitchedRoofAreaSqFt`, `flatRoofAreaSqFt`, `roofFacets`, `areasByPitch[]`, `roofPlanes[]`, `roofSystems[]`, `edgeLengths`, `flashings`, `transitions`, `wasteRecommendations[]`, `geometryStatus`, and `geometryEvidence`. Each `roofPlanes[]` item retains an individually addressable report-local ID, `roofType`, display-rounded `areaSqFt`, source pitch where keyed, source page-coordinate outline when extracted, direction evidence, confidence, and provenance. The area/pitch adapter enumerates each report's stated facet count. The eight-facet Rue Falardeau report has a partial six-plane candidate; the 16-facet Deacon Place report has a 16-plane candidate. Both remain `needs_review` while direction, stacking, or edge checks are unresolved. Reported aggregate pitch groups and pitched/flat areas remain separate because rounded group totals do not reconcile exactly.

### Bundled demo geometry (not a Firestore document)

`fixtures/geometry/demo_roofr_83_rue_falardeau.json` is the canonical static versioned demo fixture. The web `predev`, `prebuild`, and `prestart` scripts copy it to an ignored `public/geometry` asset, where the viewer fetches and validates it. It contains six illustrative surfaces, per-surface vertices/pitches/areas, edge references, overlap order, page compass, estimated wall loops, and validation warnings. It is not uploaded or persisted as customer geometry. The only related Firestore write is user-selected material appearance in the project's `materialSelections` map.

### `projects/{projectId}/artifacts/{artifactId}`

Implemented for raw extraction JSON; also used by the geometry storage helper when given a large payload. Fields include `kind`, `ownerUid`, optional `sourceReportId`, `contentType`, `fileSizeBytes`, `sha256`, `chunkSizeBytes`, `chunkCount`, `status`, and timestamps. The final NVIDIA JSON report, without reasoning text, is immutable after successful creation. Large artifacts use the same `chunks` layout as reports.

### `projects/{projectId}/estimates/{estimateId}`

Initial estimate snapshot fields include `ownerUid`, `status` (`draft`), `revision` (currently 1), `measurementVersionId`, `lineItemCount`, `completeForPricing`, material/labour/disposal/additional-charge totals, markup percentage/amount, tax rate/amount, grand total, currency, and timestamps. Monetary values are stored as decimal strings. If any line lacks the unit or package assumptions needed for pricing, the line total is null, the estimate is marked incomplete, and the visible grand total is labelled provisional.

### `projects/{projectId}/estimates/{estimateId}/lineItems/{lineItemId}`

Snapshot of `description`, `unit`, `sourceQuantity`, `wasteAdjustedQuantity`, `calculatedQuantity`, `finalQuantity`, optional `manualOverride`, unit price, waste percentage, quantity origin (`measured`, `calculated`, `manual_override`), source report quantity reference (resolved server-side from the selected measurement version), calculation inputs, line total, kind, configuration status, and warnings. Roofing lines also retain `roofType`, selected `roofingSystem`, optional roll width/length and lap dimensions. Flat lines cannot select asphalt shingles. An unselected flat system or waste factor, or an unconfigured coverage/package assumption, keeps the line provisional unless the contractor provides a final quantity override. Later catalogue edits cannot mutate this saved snapshot.

### `jobs/{jobId}`

Top-level so protected `GET /api/jobs/{jobId}` can resolve it. Fields: `ownerUid`, `projectId`, optional `reportId`, `type` (`extraction` or `geometry_reconstruction`), `status` (`queued`, `running`, `succeeded`, `failed`), `attempt`, optional sanitized `error`, `resultRefs`, `createdAt`, `startedAt`, `finishedAt`, and `updatedAt`.

## `projects/{projectId}/geometryVersions/{geometryVersionId}`

Implemented by Milestone 2. Each immutable revision stores `ownerUid`, `projectId`, `sourceReportId`, `measurementVersionId`, `status` (`processing`, `needs_review`, `approved`, `failed`), `source` (`demo`, `extracted`, `assisted`, `manual`), `confidence`, `estimatedHeights`, optimistic integer `version`, optional `basedOnVersionId`, `analysis`, `calibration`, `validationResults`, optional `jobId`, and server timestamps. An approved record also has `approvedByUid` and `approvedAt`. `analysis` holds per-page extraction counts, source facet records in PDF page points, label matching results, scale hypotheses, validation numbers/tolerances, warnings, and manual inputs required. The browser does not submit or control height values.

The canonical geometry payload uses schema version `1.1`, units `feet`, a report-preserving coordinate system and compass, vertex IDs with `[u, v, h]` coordinates, plane IDs with boundary vertex and edge references, per-plane pitches and areas, typed/shared edges, wall loops, overlap relations, `estimatedHeights`, and validation warnings. `geometry` is inline in the version document when it is at or below `GEOMETRY_INLINE_LIMIT_BYTES` (default 400 KiB); otherwise `geometryArtifactId` references a chunked `geometry_json` artifact. The storage helper hides this choice from the route.

`PUT /geometry` creates a new `source: manual` revision with `basedOnVersionId`; it never overwrites the source extraction or an approved version. The backend reruns topology, the height solver, and checks before moving `projects/{projectId}.currentGeometryVersionId` transactionally. A conflict returns 409. `POST /geometry/approve` requires explicit confirmation, all checks passing, every plane pitch/direction set, solved height constraints, and no unresolved overlaps. Approved geometry is immutable; later edits create another version. The static demo fixture is never written as a customer geometry version.

## Jobs and geometry page images

`jobs/{jobId}` supports `type: extraction | geometry_reconstruction`; statuses are `queued`, `running`, `succeeded`, and `failed`. Jobs remain top-level for protected polling. `resultRefs` points to project-owned version IDs; each job poll verifies both job and project ownership. Geometry page PNGs are rendered from verified report chunks on demand to temporary scratch files and returned privately; no page-image document or public URL is stored.

## Quote records reserved for a later milestone

Estimate revisions beyond the initial snapshot and quote PDF generation are not implemented in this milestone. Future estimate revisions may set `supersedesEstimateId` and quote artifact IDs; a `quote_pdf` artifact will use the same chunked immutable-file store. Do not describe those records or endpoints as implemented until they exist.

## Deletion and indexing

Firestore does not cascade-delete subcollections. The project/report deletion code marks report/artifact parents deleting, removes chunk children in bounded batches, verifies that no chunk indexes remain, and only then removes parents. Project delete also removes estimate line-item children and project jobs. `firestore.indexes.json` excludes `chunks.data` from single-field indexing. Rules deny every direct client operation.
