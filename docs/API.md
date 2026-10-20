# API reference

Base path: `/api`. All routes except `/health` and `/ready` require `Authorization: Bearer <Firebase ID token>`. The API verifies the token and derives the UID from verified claims. Project routes check project ownership before accessing child resources; cross-user resources return 404. The browser never accesses Firestore directly.

Errors use `{ "error": { "code": "...", "message": "..." } }`. Validation errors do not echo request bodies. Unexpected exceptions return a generic 500; logs contain sanitized failure type and extraction stage/operation, not file contents, credentials, or stack traces. Extraction job failures have a safe user-facing error message and can be retried from the report card.

## Health and identity

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/health` | Process liveness check; no database call |
| `GET` | `/ready` | Firestore readiness check |
| `GET` | `/api/me` | Verified user profile |
| `GET` | `/api/config` | Public estimating defaults and upload/page limits, requires auth |

## Projects and reports

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/projects` | Create project; JSON `name`, optional `address` and `customer`; 201 |
| `GET` | `/api/projects` | List caller-owned projects |
| `GET` | `/api/projects/{projectId}` | Read owned project |
| `PATCH` | `/api/projects/{projectId}` | Update name/address/customer/material appearance selections |
| `DELETE` | `/api/projects/{projectId}` | Delete owned project, child chunks, estimate line items, and jobs; 204 |
| `POST` | `/api/projects/{projectId}/reports` | Multipart `file` and optional `autoExtract` (default true); returns `id`, status, duplicate flag, optional `jobId` |
| `GET` | `/api/projects/{projectId}/reports` | List report metadata |
| `GET` | `/api/projects/{projectId}/reports/{reportId}/file` | Verified private PDF download; no public file URL |
| `DELETE` | `/api/projects/{projectId}/reports/{reportId}` | Mark deleting, remove all report chunks, delete report; 204 |
| `POST` | `/api/projects/{projectId}/reports/{reportId}/extract` | Queue/retry extraction; optional `force=true` creates a new idempotency key; 202 |
| `GET` | `/api/jobs/{jobId}` | Read caller-owned persistent job status |

Upload validates size, PDF magic, openability, encryption, page count, and page inspection; creates the report ID from the SHA-256; streams chunks to Firestore; checks chunk indexes and complete-file integrity; then queues work. Roofr measurements are parsed locally; NVIDIA NIM extracts model-backed measurements and review-only diagram points when needed. PDF pages are rendered to image data URLs because the vision API receives images. Neither extraction nor geometry reconstruction runs in the upload request. Upload progress comes from the browser XHR client.

## Measurements, catalogue, and estimates

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/projects/{projectId}/measurements` | Read the project's current measurement version |
| `PUT` | `/api/projects/{projectId}/measurements` | Create reviewed version and update current pointer; accepts `expectedCurrentVersionId` and returns 409 on conflict |
| `GET` | `/api/materials` | List authenticated user's material catalogue |
| `POST` | `/api/materials` | Create catalogue record; 201 |
| `PATCH` | `/api/materials/{materialId}` | Update caller-owned catalogue record |
| `DELETE` | `/api/materials/{materialId}` | Delete caller-owned catalogue record; 204 |
| `POST` | `/api/projects/{projectId}/estimates` | Calculate with Decimal and save initial snapshot; 201 |
| `GET` | `/api/projects/{projectId}/estimates` | List owned project estimate metadata |
| `GET` | `/api/projects/{projectId}/estimates/{estimateId}` | Read owned estimate and line-item snapshots |

## Geometry reconstruction and review

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/projects/{projectId}/geometry/reconstruct` | JSON `reportId`; creates a Firestore geometry version and `geometry_reconstruction` job, returns 202. The job consumes NVIDIA's review-only facet points when available and checks them against PyMuPDF page frames and scale calibration. |
| `GET` | `/api/projects/{projectId}/geometry` | Read current version; optional `version_id` selects an owned historical version. |
| `GET` | `/api/projects/{projectId}/geometry/versions` | List owned geometry revisions and identify the current one. |
| `GET` | `/api/projects/{projectId}/geometry/page-image?page=N&version_id=...` | Render a page from the selected version's report to PNG on demand. The authenticated owner is checked before chunk reads; response uses private no-store headers. The PNG is not stored. |
| `PUT` | `/api/projects/{projectId}/geometry` | Save `expectedCurrentVersionId`, `expectedVersion`, reviewed page-coordinate facets, pitch/directions, edge classes, optional explicitly confirmed manual scale, and optional stacking-order confirmation. The server ignores client heights and rebuilds topology, height constraints, and checks. Returns 409 on stale version. |
| `POST` | `/api/projects/{projectId}/geometry/approve` | JSON `expectedVersion` and explicit `confirmation`. Requires all checks to pass, every plane pitch/direction, solved heights, and no unresolved overlaps. Approved records are immutable; later corrections create a new revision. |

Reconstruction always ends in `needs_review` or `failed`, never `approved`. Source records include page coordinate evidence, per-page legend associations, matched/unmatched labels, plan/sloped scale fits and residuals, area/edge checks, and unresolved constraints. Roofr currently yields eight source facets and a six-plane review candidate with two missing directions and contradictory shared-edge heights. Roof Manager yields 21 outline candidates versus 31 reported planes and no 3D candidate. See [PDF analysis](PDF_ANALYSIS.md) for the measured output and limits. The project 3D tab uses extracted geometry only after a user approves a fully validated version; the static six-surface demo remains separately labelled.

Roofr measurement versions add separate total/pitched/flat areas, roof facet count, aggregate `areasByPitch`, source-labelled `roofPlanes`, edge lengths, flashings, transitions, separate waste references, and `geometryStatus`. A Roofr plane can have a source display area and roof type while `pitch`, vertices, and edge connectivity remain unknown. The endpoint does not claim that a PDF vector drawing is a reconstructed 3D mesh. `PUT` review accepts the version's tagged `values`, optional predominant pitch notation/source-wide waste, and `expectedCurrentVersionId`; source measurements remain preserved.

Source report comparison quantities are resolved by the API from the project's current measurement version; values supplied in a request are ignored. Roofr line inputs can specify `roofType`, `roofingSystem`, `wastePercentage`, `coveragePerUnit`, `packageSize`, and flat-system `rollWidthFeet`, `rollLengthFeet`, `lapWidthInches`, and `lapLengthInches`. If roll dimensions are entered, effective coverage is calculated after the configured laps. A flat line cannot select `asphalt_shingle`; a flat system, waste, coverage/package assumptions, and prices are explicit contractor inputs. Results retain `sourceQuantity`, `calculatedQuantity`, `finalQuantity`, and `manualOverride`. Estimate revisions, quote generation/download, and automatic material-to-estimate selection are not implemented.

## Upload and download security

- Uploads are bounded by `MAX_UPLOAD_BYTES` and `MAX_PDF_PAGES`; the request content type is advisory.
- Original filenames are sanitized and used only for download display.
- File chunks are verified by index, per-chunk SHA-256, total size, and full SHA-256.
- Downloads are streamed from a private temporary directory with `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`; temp files are removed after the response.
- Every report read, download, retry, and delete begins with project ownership verification.
