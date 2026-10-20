# Working rules for this repository

## Commands

- Install API dependencies into a supported Python 3.11–3.14 installation: `python3.14 -m pip install -e 'services/api[dev]'`.
- Install browser dependencies from the pinned lockfile: `npm ci --prefix apps/web`.
- Start emulators: `firebase emulators:start --only auth,firestore --project demo-roofing-local --config firebase/firebase.json`.
- Start API: from `services/api`, run `python3.14 -m app.run` (uses validated `API_HOST`/`API_PORT`).
- Start web: `npm --prefix apps/web run dev`.
- Backend tests/lint/types: from `services/api`, run `python3.14 -m pytest tests`, `python3.14 -m ruff check app tests`, and `python3.14 -m mypy app`.
- Frontend: `npm --prefix apps/web test`, `npm --prefix apps/web run typecheck`, `npm --prefix apps/web run lint`, and `npm --prefix apps/web run build`.
- Policy check: `python3.14 scripts/check_firestore_only.py`.
- Sample normalization without a model call: from `services/api`, `python3.14 -m app.extraction.fixture`.

## Repository structure

- `apps/web`: Next.js App Router, React/TypeScript UI, Firebase Authentication, and the demo React Three Fiber canvas.
- `services/api/app`: FastAPI, validated configuration, Firebase Admin authentication, Firestore access, chunked files, extraction, and Decimal estimation.
- `services/api/app/geometry`: PDF vector adapters, robust scale calibration, topology, height solving, validation, review workflow, and schema migration.
- `services/api/tests`: unit and API tests with local fakes; NVIDIA calls must remain mocked.
- `apps/web/components/geometry-review-panel.tsx`: PDF overlay editor for geometry candidates; approved geometry alone can replace the labelled 3D demo.
- `firebase`: deny-all Firestore rules, chunk index override, and Auth/Firestore emulator config only.
- `scripts`: repository policy check and stale upload cleanup.
- Root `roof_data.json` is the existing machine-readable sample; `RoofManager-Example-Report.pdf` is its source report.
- Root `83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf` is the supplied Roofr mixed-roof report. Its selectable measurement tables are parsed directly; its geometry stays `needs_review`.
- Root `17 Deacon Place, St_ Albert, AB T8N 6S5.pdf` is a seven-page Roofr report with 16 pitched facets. The parser supports this layout and the eight-page Rue Falardeau layout; its 16-plane candidate remains `needs_review`. A source-derived review candidate and preview are in `models/`.
- Root `7704 154A Street Northwest, Edmonton, AB T5R 1V2.pdf` is an 11-page Roofr report with 12 facets across two structures. Its full facet outlines and pitch/direction evidence are recoverable, but two small overlapping facets leave shared-edge heights unresolved; keep it `needs_review`.
- Root `RoofManager-Example-Report.pdf` is the Roof Manager geometry golden fixture. Current adapters find 21 outline candidates versus 31 reported planes; it stays `needs_review` for manual tracing, pitches/directions, and lower-eave constraints. See `docs/PDF_ANALYSIS.md` for measured values.
- `fixtures/geometry/demo_roofr_83_rue_falardeau.json` is a static six-surface DEMO port of `roof3d.html`, not extracted customer geometry. It is bundled into the client and must never be persisted as a project geometry version.
- Keep the Milestone 1 geometry schema at version 1.1 unless a change requires a schema bump and migration. Existing demo fixture must continue to load.

## Hard rules

1. **Firestore is the only persistent application storage.** Do not add Cloud Storage, another database, or public file URLs. The browser must not import Firestore or any storage SDK; Admin Firestore access stays on the server.
2. **Do not put secrets in frontend code.** `NEXT_PUBLIC_*` values are public. Keep NVIDIA keys and Admin credentials backend-only and out of tracked files.
3. **Every resource endpoint checks ownership.** Verify the Firebase ID token, then verify the authenticated UID owns the project/resource before reading, writing, uploading, downloading, retrying, or deleting. Return 404 for another user's resources.
4. **Money uses `Decimal` in calculation and integer cents or decimal strings in Firestore.** Never use floats for money.
5. **Do not invent customer roof geometry.** The bundled Roofr reference geometry is explicitly `DEMO`, has estimated heights/wall depth, and is not a model of the uploaded customer's roof. Aggregate measurements, pitch, or a demo model do not prove the customer's roof shape. Require review when source constraints are insufficient.
6. **Geometry approval stays server-controlled.** Read coordinates from page-local PyMuPDF evidence; calibrate from labeled dimensions; preserve calibration, validation, source, confidence, compass, and estimated-height metadata. Never accept client heights. Each save creates a version and reruns topology, heights, overlaps, area, edge, and pitch checks. Only explicit approval of a fully validated version permits the customer geometry to appear in the project 3D tab. Keep the demo as a separate labelled fallback.
7. Keep versions and source provenance. Save corrected measurements as a new version. Estimate line items snapshot quantities, assumptions, and prices.
8. No cloud resources or Firebase rules are deployed automatically. No live provider call is part of a test. NVIDIA facet points are review candidates only; deterministic geometry checks and human approval remain required.
9. Operational settings belong in validated configuration and `.env.example`; user-editable material/project settings belong in Firestore.
10. Keep docs synchronized with actual routes and behavior. Quote/PDF generation has no current API. Do not call Roofr or Roof Manager geometry accurate while checks fail or inputs remain unresolved.
11. Preserve Roofr's flat and pitched systems independently. Do not apply shingle quantities or a predominant pitch to flat facets; keep source values, corrections, and rounded pitch-group references distinct.

