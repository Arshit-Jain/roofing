# Roofline — Roofing Estimator

Roofline is a browser application for contractors to organize roofing jobs, upload Roof Manager and Roofr PDFs, review extracted quantities, and build an editable estimate. Model-backed report extraction and candidate facet-point extraction use the NVIDIA NIM chat completions API. PDF pages are rendered to PNG data URLs for vision input; Roofr scalar tables are also parsed locally. PyMuPDF analysis supplies independent page-coordinate, scale, and topology checks for geometry review. The browser uses the Firebase App and Auth packages only. FastAPI verifies Firebase ID tokens and is the only application component that reads or writes Firestore. Reports and immutable artifacts are stored as verified Firestore chunks; local files are temporary scratch space.

The application supports project/report workflows, Firestore chunk storage, Roofr measurements, versioned geometry extraction and review, editable estimation, and a client-only Three.js viewer. PDF geometry is never auto-approved. The eight-page 83 Rue Falardeau report produces a partial six-plane candidate from eight facets; the seven-page 17 Deacon Place report produces a 16-plane candidate with unresolved stacking relationships and an edge-length discrepancy. The 11-page 7704 154A Street report yields 12 outlines with calibrated scale and pitch/direction evidence, while overlapping small facets leave shared-edge heights unresolved. Roof Manager produces 21 outline candidates for a reported 31 planes with scale and edge discrepancies. All remain `needs_review`. Users can trace and edit geometry in the browser, and only a validated, explicitly approved version replaces the separately labelled demo in the project 3D tab. Quote PDF generation and Firebase deployment automation are not implemented. See [PDF analysis](docs/PDF_ANALYSIS.md) for measured results.

## Repository layout

```text
apps/web/                 Next.js App Router, Firebase Auth only, estimator UI, R3F roof viewer
apps/web/components/roof/ R3F surfaces, walls, camera presets, and demo viewer
apps/web/lib/roof/        Pure coordinate mapping, triangulation, and geometry validation
fixtures/geometry/        Static, versioned DEMO geometry; never customer-stored
models/                   Source-derived 17 Deacon Place review candidate and local preview
services/api/app/         FastAPI, configuration, auth, Firestore repositories, jobs, extraction, estimation
services/api/app/geometry/ PyMuPDF adapters, robust scale calibration, topology, height solving, validation, review workflow
services/api/tests/       Pytest unit and API tests with a Firestore-shaped fake
firebase/                 Deny-all Firestore rules, index overrides, Auth/Firestore emulator config
scripts/                  Firestore-only policy check and stale-upload cleanup command
docs/                     API, data model, PDF inspection, and decisions
RoofManager-Example-Report.pdf  Sample Roof Manager report
83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf  Eight-page mixed flat / pitched Roofr report
17 Deacon Place, St_ Albert, AB T8N 6S5.pdf  Seven-page Roofr report with 16 pitched facets
roof_data.json            Existing extracted JSON used as the main deterministic fixture
exmaple.py                Legacy Gemini extraction utility (separate from the API pipeline)
```

## Prerequisites

- Python 3.11 through 3.14. The pinned PyMuPDF release publishes supported wheels through Python 3.14; Python 3.15 is not listed as supported.
- Node.js 20.19 or newer and npm. This satisfies Next.js 16 and ESLint 10.
- Java 17 or newer and Firebase CLI only if you will run the local emulators.
- Firebase Web app settings for browser sign-in. For local work, use the Auth and Firestore emulators.
- NVIDIA API key for model-backed extraction and diagram-point requests. Automated checks do not call the provider.
- Firebase Admin credentials through ADC or a backend-only credentials file path for a real Firebase project. Local Firestore emulator mode uses anonymous credentials when `FIRESTORE_EMULATOR_HOST` is configured. Never expose Admin credentials in frontend configuration.

No Python virtual environment is required by the project. Use a supported Python 3.11–3.14 installation consistently for both installation and API commands. This workspace's `python3` is 3.15, which is outside the declared range; use an installed `python3.14` command or another supported interpreter. This Homebrew Python installation is externally managed, so dependency installation here required `python3.14 -m pip install --user --break-system-packages -e 'services/api[dev]'`. On other systems use the normal install command below or a Python distribution that supports user installs.

## Install

From the repository root:

```sh
python3.14 -m pip install -e 'services/api[dev]'
npm ci --prefix apps/web
```

Application dependencies are pinned in `services/api/pyproject.toml`, `apps/web/package.json`, and `apps/web/package-lock.json`. The browser installs only `@firebase/app` and `@firebase/auth`; Firestore access remains server-side. Do not put credentials in either package manifest.

## Configure local development

1. Copy `.env.example` to `.env` at the repository root for the API. Copy `apps/web/.env.example` to `apps/web/.env.local` for Next.js. These contain placeholders and local emulator defaults only.
2. Start the Auth and Firestore emulators in a second terminal:

   ```sh
   firebase emulators:start --only auth,firestore --project demo-roofing-local --config firebase/firebase.json
   ```

   When `FIRESTORE_EMULATOR_HOST` is set, the API uses anonymous credentials for the Firestore emulator and does not require ADC. For a real Firebase project, configure ADC or `GOOGLE_APPLICATION_CREDENTIALS`. Emulator host variables route traffic locally and must never point at production.
3. Start the API:

   ```sh
   cd services/api && python3.14 -m app.run
   ```

   Health checks are `GET /health` and `GET /ready`.
4. Start the browser app:

   ```sh
   npm --prefix apps/web run dev
   ```

   Open `http://localhost:3000`, create an email/password account, create a project, and upload a report. The report upload displays XHR progress and writes chunks to the Firestore emulator. Roof Manager model extraction requires `NVIDIA_API_KEY`; Roofr scalar measurements are parsed locally, while diagram-point extraction uses NVIDIA when needed. Without a key, those model-backed steps report a sanitized configuration message. `roof_data.json` can be normalized without a provider call using the command below.

The Firebase Auth emulator UI and Firestore emulator UI are configured at ports 9099, 8080, and 4000 respectively.

## Run checks

Run from the repository root unless noted:

```sh
python3.14 scripts/check_firestore_only.py
(cd services/api && python3.14 -m pytest tests)
(cd services/api && FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 python3.14 -m pytest tests/test_emulator.py)
(cd services/api && python3.14 -m ruff check app tests)
(cd services/api && python3.14 -m mypy app)
(cd services/api && python3.14 -m app.extraction.fixture)
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run build
```

The pytest suite mocks NVIDIA HTTP responses and uses a Firestore-shaped fake by default. It includes generated vector-PDF topology/height cases, golden analysis of the two bundled measurement PDFs, geometry API ownership/version/approval cases, and extraction/estimation tests. The emulator-only round trip is marked and skips unless `FIRESTORE_EMULATOR_HOST` points at a listening loopback Firestore emulator; the explicit command above runs it separately. Auth token verification is mocked in unit tests. Install Java and `firebase-tools` to exercise browser signup and Firestore against the local emulators. The frontend `npm test` script uses the repository's Node test runner for pure TypeScript geometry, version selection, and approval-gating helpers; Vitest is not configured. The fixture command reads `roof_data.json` and prints normalized tagged measurements without contacting NVIDIA or persisting data.

The current test/build outcomes are recorded in the final implementation handoff. Emulator tests are skipped when the local emulator prerequisites are absent; no cloud services are contacted by the test suite.

## Firebase setup

See [FIREBASE_SETUP.md](FIREBASE_SETUP.md). Only Firebase Authentication and Cloud Firestore are used. Authentication must have email/password enabled. Firestore rules deny every direct client read/write because only the backend uses Admin access. Rules and indexes are configured but have not been deployed. The application does not require Firebase Storage or a Cloud Storage bucket.

## Environment reference

Copy the examples, then change values for the environment. `NEXT_PUBLIC_*` values are embedded in the browser bundle and are public; never put secrets there. The backend API key and Admin credential path are backend-only. Safe defaults below are for local development, not production.

| Variable | Purpose | Required? | Default | Visibility |
|---|---|---:|---|---|
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Firebase Web SDK project key for Auth | Yes for browser sign-in | None | Frontend public |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Firebase Auth domain | Yes for browser sign-in | None | Frontend public |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Firebase project ID used by the Web SDK | Yes for browser sign-in | None | Frontend public |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Firebase Web app ID | Yes for browser sign-in | None | Frontend public |
| `NEXT_PUBLIC_API_BASE_URL` | FastAPI origin | Optional | `http://127.0.0.1:8000` | Frontend public |
| `NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST` | Auth emulator `host:port` | Optional | `127.0.0.1:9099` in example | Frontend public |
| `APP_ENV` | Runtime environment selector | Optional | `development` | Backend |
| `LOG_LEVEL` | Structured application log threshold | Optional | `INFO` | Backend |
| `API_HOST` | Uvicorn listen host | Optional | `127.0.0.1` | Backend |
| `API_PORT` | Uvicorn listen port | Optional | `8000` | Backend |
| `REQUEST_TIMEOUT_SECONDS` | Per-request timeout bound | Optional | `60` | Backend |
| `CORS_ALLOWED_ORIGINS` | Comma-separated exact HTTP(S) origins | Optional | `http://localhost:3000` | Backend |
| `FIREBASE_PROJECT_ID` | Firebase Admin project ID | Optional locally; required explicitly in production | `demo-roofing-local` | Backend |
| `FIRESTORE_DATABASE_ID` | Firestore database ID | Optional | `(default)` | Backend |
| `GOOGLE_APPLICATION_CREDENTIALS` | Backend-only ADC service-account file path | Optional if ADC is otherwise available | ADC lookup | Backend secret path |
| `FIRESTORE_EMULATOR_HOST` | Firestore emulator `host:port` | Optional | unset | Backend |
| `FIREBASE_AUTH_EMULATOR_HOST` | Auth emulator `host:port` | Optional | unset | Backend |
| `NVIDIA_API_KEY` | NVIDIA NIM API credential | Optional until model extraction is requested | unset | Backend secret |
| `NVIDIA_MODEL` | NVIDIA NIM model name | Optional | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | Backend |
| `NVIDIA_API_URL` | NVIDIA chat completions endpoint | Optional | `https://integrate.api.nvidia.com/v1/chat/completions` | Backend |
| `NVIDIA_TIMEOUT_SECONDS` | NVIDIA request timeout | Optional | `120` | Backend |
| `NVIDIA_MAX_RETRIES` | Retries for temporary provider failures | Optional | `2` | Backend |
| `NVIDIA_REASONING_BUDGET` | Model reasoning token budget | Optional | `16384` | Backend |
| `NVIDIA_MAX_OUTPUT_TOKENS` | Maximum model output size | Optional | `65536` | Backend |
| `MALFORMED_OUTPUT_RETRIES` | Extra model attempts for invalid JSON/schema | Optional | `1` | Backend |
| `EXTRACTION_SCHEMA_VERSION` | Normalized extraction schema version | Optional | `1` | Backend |
| `EXTRACTION_PROMPT_VERSION` | Prompt version in extraction idempotency key | Optional | `1` | Backend |
| `MAX_UPLOAD_BYTES` | Maximum PDF size | Optional | `20 MiB` | Backend |
| `MAX_PDF_PAGES` | Maximum accepted pages | Optional | `100` | Backend |
| `MAX_ESTIMATE_LINE_ITEMS` | Maximum line items accepted per estimate | Optional | `100` | Backend |
| `MAX_MATERIAL_SELECTIONS` | Maximum appearance choices saved on one project | Optional | `30` | Backend |
| `FILE_CHUNK_SIZE_BYTES` | Firestore chunk payload size; never above 700 KiB | Optional | `524288` | Backend |
| `FIRESTORE_CHUNK_BATCH_SIZE` | Chunks per Firestore commit | Optional | `8` | Backend |
| `FIRESTORE_DELETE_BATCH_SIZE` | Chunk deletes per Firestore commit (maximum 450) | Optional | `400` | Backend |
| `UPLOAD_STALE_AFTER_SECONDS` | Age before an incomplete upload can be reclaimed | Optional | `900` | Backend |
| `JOB_STALE_AFTER_SECONDS` | Age before a running job is considered interrupted | Optional | `1800` | Backend |
| `JOB_POLL_INTERVAL_SECONDS` | Browser job/report polling interval | Optional | `2` | Backend |
| `JOB_POLL_TIMEOUT_SECONDS` | Maximum time the workspace waits while polling a job | Optional | `180` | Backend |
| `MAX_ARTIFACT_BYTES` | Maximum immutable artifact size | Optional | `20 MiB` | Backend |
| `GEOMETRY_INLINE_LIMIT_BYTES` | Inline geometry threshold; larger geometry uses chunks | Optional | `409600` | Backend |
| `UPLOAD_READ_SIZE_BYTES` | Bounded upload copy buffer | Optional | `65536` | Backend |
| `FIREBASE_RETRY_ATTEMPTS` | Bounded retry attempts for transient Firestore errors | Optional | `3` | Backend |
| `JOB_MAX_CONCURRENCY` | Concurrent in-process extraction tasks | Optional | `2` | Backend |
| `GEOMETRY_SNAP_TOLERANCE_POINTS` | PDF endpoint snap tolerance during topology extraction | Optional | `0.8` | Backend |
| `GEOMETRY_SCALE_TOLERANCE_PERCENT` | Scale-fit and page-registration tolerance | Optional | `3` | Backend |
| `GEOMETRY_FACET_AREA_TOLERANCE_PERCENT` | Per-facet and aggregate area validation tolerance | Optional | `5` | Backend |
| `GEOMETRY_EDGE_CLASS_TOLERANCE_PERCENT` | Reported versus recovered edge-class total tolerance | Optional | `3` | Backend |
| `GEOMETRY_PITCH_TOLERANCE_DEGREES` | Solved mesh pitch-gradient tolerance | Optional | `1` | Backend |
| `GEOMETRY_HEIGHT_TOLERANCE_FEET` | Shared-edge height consistency tolerance | Optional | `0.05` | Backend |
| `GEOMETRY_WALL_DEPTH_FEET` | Estimated wall depth below the roof boundary | Optional | `8` | Backend |
| `GEOMETRY_PAGE_RENDER_DPI` | Resolution for transient PDF page PNG rendering | Optional | `144` | Backend |
| `DEFAULT_CURRENCY` | Estimate default currency | Optional | `CAD` | Backend |
| `DEFAULT_TAX_RATE` | Editable tax-rate default (fraction, not legal advice) | Optional | `0` | Backend |
| `DEFAULT_WASTE_PERCENTAGE` | Editable waste default | Optional | `8` | Backend |
| `DEFAULT_MARKUP_PERCENTAGE` | Editable markup default | Optional | `0` | Backend |

Every backend setting in `app/config.py` has a matching entry in `.env.example`. Project-specific prices, material assumptions, and appearance selections belong to user data in Firestore rather than process environment variables.

## Storage and cost implications

Firestore is the only persistent application store. The 20 MiB PDF limit becomes about 40 documents at the default 512 KiB chunk size. Each chunk costs a document write and consumes document storage; a verified upload also performs a chunk metadata query. Each full reassembly (extraction or download) uses an index-only chunk query plus 40 point reads, then checks the complete-file hash. Deletes cost one delete per chunk and read chunk metadata to find the children. Under the current default, a full 20 MiB file therefore involves roughly 40 chunk writes, 40 reads to verify an upload, 80 reads per extraction/download reassembly, and 40 reads plus 40 deletes on deletion, in addition to metadata operations. Costs vary by region, index configuration, retries, and actual traffic; review current [Firestore pricing](https://firebase.google.com/docs/firestore/pricing) before using real customer files.

Firestore documents are limited to 1 MiB and API requests to 10 MiB; the implementation uses chunk documents and caps each write batch to 9 MiB of raw chunk payload, with an eight-chunk default. These are deliberately conservative relative to the [Firestore quotas](https://firebase.google.com/docs/firestore/quotas). The `chunks.data` field is excluded from single-field indexing. This design has higher read/write cost and latency than object storage, and all downloads pass through the API. A production reverse proxy also needs a matching request-body limit and timeout.

## Known limitations

- **Geometry:** PyMuPDF page-local vector adapters, legend mapping, global label/arrow matching, robust plan/sloped scale hypotheses, polygonization, server-side height solving, validation, edit/version/approval APIs, PDF page rendering, and review UI are implemented. The eight-facet Rue Falardeau Roofr report yields six candidate planes; two directions and shared-height constraints remain unresolved. The 16-facet Deacon Place Roofr report yields 16 candidate planes; area checks pass, but a rake-length check warns, flashing checks are unavailable, and two stacking relationships are unsupported by shared edges. Both remain `needs_review`. Roof Manager yields 21 outline candidates against 31 reported planes, with scale and edge discrepancies; no 3D candidate is generated. The review UI supports tracing, vertex edits, plane ordering, manual scale confirmation, and revalidation. The project 3D tab uses customer geometry only after approval; otherwise the separate demo remains. Absolute heights and wall depth are estimated. The static demo uses six illustrative surfaces, omits the 18/12 SF overlap strips, and is never stored as customer geometry. See [PDF analysis](docs/PDF_ANALYSIS.md) for page and check values.
- **Mixed roof estimating:** Roofr pitched (643 SF) and flat (515 SF) quantities are independent lines. A flat-roof system must be selected and configured with coverage or roll dimensions, lap measurements, package size, and price. No membrane type, lap, coverage, or price is inferred. Roofr waste tables remain source references, separated by system.
- **Extraction:** live NVIDIA extraction needs a configured backend key and makes provider API calls when invoked. Automated checks mock the HTTP response. The final JSON report is saved as an immutable Firestore artifact; model reasoning text is excluded. Facet coordinates are low-confidence candidates marked for review; they never approve a geometry version. NVIDIA accepts images for this vision flow, so the API renders PDF pages into temporary PNG data URLs. The separate legacy root script still uses Gemini Files API. After a failure, the report card shows a sanitized reason and failing stage; Retry extraction polls the job and refreshes the saved measurement version when it finishes.
- **SDK dependency caveat:** the required Python Firebase Admin distribution currently installs `google-cloud-storage` transitively. The application has no direct Storage dependency/import/configuration, makes no Storage calls, and stores no data there. The frontend installs only Firebase App/Auth packages. The repository policy scan checks authored code, config, lockfile, and direct dependency declarations.
- **Jobs:** jobs are persistent Firestore documents but run in FastAPI's in-process background task executor. They are not distributed or guaranteed to resume automatically after a process restart. Stale running jobs are marked failed on startup and can be retried from the report.
- **Estimates and quotes:** the API calculates and stores initial estimate snapshots. The UI has separate pitched and flat lines, editable waste/system/price/coverage/roll/lap/package inputs, edge/flashing/labour/disposal lines, and source/calculated/final quantities. Estimate revisions and quote PDF generation/download are not implemented; the Quote tab states this clearly. No supplier prices are seeded; prices start at editable zero placeholders.
- **Operations:** `npm audit` currently reports five high-severity findings through the development-only `braces` dependency under `eslint-config-next` → `fast-glob` → `micromatch`. The suggested forced audit fix downgrades the Next ESLint config to 14.x, so it was not applied to this Next 16 app. Rate limiting, multi-instance queueing, production secret management, TLS/reverse-proxy configuration, monitoring/alerts, backup/restore validation, deployment automation, and security review remain outstanding. Do not describe this repository as production-ready.

## Deployment notes

Do not deploy with demo project IDs, emulator hosts, placeholder Web config, or a development CORS origin. Configure production Admin credentials and NVIDIA secrets outside source control, exact CORS origins, TLS termination and body limits, request/job limits, monitoring, and backups. Review the deny-all Firestore rules and indexes, then deploy them manually if appropriate. This repository has not deployed any cloud resource or Firebase configuration.

See also [flow.md](flow.md), [architecture.md](architecture.md), [docs/API.md](docs/API.md), [docs/DATA_MODEL.md](docs/DATA_MODEL.md), [docs/DECISIONS.md](docs/DECISIONS.md), and [docs/PDF_ANALYSIS.md](docs/PDF_ANALYSIS.md).
