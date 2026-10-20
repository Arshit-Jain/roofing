# PDF inspection and geometry recovery

The model-backed extraction path renders PDF pages to PNG data URLs and sends them to NVIDIA NIM for source measurements and candidate facet points. Candidate points remain in PDF page coordinates and are marked low-confidence. PyMuPDF independently reads page dimensions, vector paths, and dimension labels for scale and topology checks. Geometry is persisted as a `needs_review` version and cannot be auto-approved.

## Roofr — 83 Rue Falardeau

Input: `83 Rue Falardeau, Gatineau, QC J8X 3E1.pdf`, eight A4-sized pages. The run below used the configured defaults: 3% scale tolerance, 5% per-facet area tolerance, 3% edge-class total tolerance, and 0.8 pt snap tolerance. The page report records PyMuPDF drawing counts and flattened vector segments; decorative paths on summary pages are not treated as roof facets.

| Page | Content | Drawings / vector segments | Recovery result |
|---|---|---:|---|
| 1 | Cover summary | 0 / 0; 4 image references | Text gives total area 1,158 SF, 8 facets, and predominant pitch 1:12. No vector geometry. |
| 2 | Diagram | 9 / 50; 4 image references | Outline and internal diagram strokes are available in page coordinates. This page is retained as orientation evidence; no scale bar or named plane vertices are present. |
| 3 | Length measurement report | 52 / 103; 4 image references | Page-local legend associates edge colors with classes. Twelve dimension-label observations were matched to nearby same-class segments; the sloped-length hypothesis won with 10 inliers, R² 0.996575, and 0.2760 ft RMS residual. Fitted scale is 0.11709754 ft/page-point on this page. |
| 4 | Area measurement report | 8 / 46; 4 image references | Eight area labels matched eight closed facet outlines; zero area labels were unmatched. Label values are 93, 406, 18, 12, 176, 212, 190, and 56 SF. |
| 5 | Pitch and direction | 15 / 80; 4 image references | Eight pitch labels keyed to the eight area facets. Six vector arrows resolve directions; the 18 SF and 12 SF facets have no uniquely assigned arrow. The footnote says deleted facets are dashed and have no pitch. This run found no explicit PyMuPDF dash-style objects on this page; the adapter records the footnote and excludes dashed drawings if the PDF exposes them. |
| 6 | Pitch-area summary | 59 / 250; 4 image references | Text/table evidence only for geometry; the rounded 1:12, 3:12, and 6:12 area groups remain source measurements. |
| 7 | Waste table | 64 / 256; 4 image references | Waste values are not geometry constraints. |
| 8 | Material summary | 204 / 816; 4 image references | Material rows are not used to infer roof outlines or elevations. |

### Roofr calibration and validation

- The selected plan-length fit is 0.11720077 ft/page-point (9/12 inliers, R² 0.995707, RMS 0.2881 ft). The selected sloped-length fit is 0.11709754 ft/page-point (10/12, R² 0.996575, RMS 0.2760 ft). Sloped length is selected because it has more inliers and the better R².
- The area-page coordinate scale after registration is 0.10679521 ft/page-point. Page 3 → page 4 registration width/height disagreement is 0.0005%; page 5 → page 4 is 0.0001%. Both pass the 3% registration tolerance. The geometry version stores the scale hypothesis, edges used, residuals, fit statistics, and tolerances.
- All eight slope-adjusted facet area checks pass under the 5% tolerance plus the one-sided 1 SF interval for Roofr's upward-rounded labels. Calculated areas are 91.600, 402.550, 16.989, 11.444, 174.461, 209.578, 187.940, and 55.101 SF against labels 93, 406, 18, 12, 176, 212, 190, and 56 SF respectively. The 18 SF label has a 5.616% absolute difference but passes the documented one-sided rounded-label interval; this does not validate its shape by itself.
- The sloped hypothesis edge checks pass for eaves (98.446 vs 98.750 LF, 0.308%), hips (23.269 vs 22.667, 2.657%), rakes (82.122 vs 84.583, 2.910%), ridges (30.651 vs 30.750, 0.322%), transitions (21.669 vs 21.750, 0.372%), and the report's zero valley and curb-flashing lengths. Unspecified edges warn at 3.360% (14.335 vs 14.833 LF). Step flashing (23.5 LF) and wall flashing (18 LF) are not evaluable from the recovered class lines.
- The geometry candidate has six pitch-gradient checks, all `not_evaluable`, because the shared-edge solver did not return a complete height surface. These checks do not treat a requested pitch as a solved pitch. Overall the analysis records 18 passes, 1 warning, 0 failures, and 8 not-evaluable checks (two flashing classes and six pitch gradients); the non-passing/unavailable checks preserve `needs_review`.
- The partial topology contains six candidate planes, 22 vertices, and 27 edges. The 18 SF and 12 SF facets remain in the evidence list but are not in this candidate because their directions are unresolved. The shared-edge solver did not produce heights: two shared edges disagree by 0.159 ft and 1.529 ft against a 0.050 ft tolerance, with five contradictory shared-vertex height values reported. No compromise heights are written.
- The six candidate outlines are two flat/draining 1:12 facets (93 and 406 SF) and four pitched facets (176, 212, 190, and 56 SF at 3:12/6:12). The 18 SF flat and 12 SF pitched facets are preserved as source evidence, but their direction arrows are unresolved and they are omitted from the 3D candidate. These labels are rounded and do not reconcile exactly to the reported 515 SF flat and 643 SF pitched system totals.
- Overall status is `needs_review`. The UI can show the six-plane candidate and all eight source facets in the 2D review, but the candidate is not an approved customer roof. A user must supply or confirm the two missing downslope directions, resolve shared-edge height conflicts, review the unspecified/unevaluable edge totals, and approve a newly validated version before it is used in the project 3D view.

The Roofr report's exact system totals remain 643 SF pitched and 515 SF flat. The five rounded pitch groups total 1,159 SF, and the eight rounded facet labels total 1,163 SF. These are distinct report groupings and are not reconciled by inventing or reallocating area.

## Roofr — 17 Deacon Place

Input: `17 Deacon Place, St_ Albert, AB T8N 6S5.pdf`, a seven-page report. This layout places waste recommendations in the report summary and material calculations on page 7. The parser recognizes this layout separately from the eight-page Rue Falardeau report. The area and pitch diagrams contain 16 vector facets.

| Page | Content | Recovery result |
|---|---|---|
| 1 | Cover summary | Text gives 1,693 SF, 16 facets, and predominant pitch 5:12. |
| 2 | Diagram | Vector roof diagram and compass provide orientation evidence. |
| 3 | Length measurement report | Edge totals include 210 ft 1 in eaves, 123 ft 5 in hips, 31 ft ridges, 37 ft 9 in rakes, 26 ft 11 in valleys, and flashing classes. |
| 4 | Area measurement report | 16 rounded facet labels total 1,703 SF against the exact 1,693 SF total. The report classifies all roof area as pitched and no area as flat. |
| 5 | Pitch and direction | Sixteen pitch labels and vector arrows: two 4:12 facets and fourteen 5:12 facets. The 5:12 group is predominant. |
| 6 | Report summary | Pitch groups are 49 SF at 4:12 and 1,646 SF at 5:12; the rounded groups total 1,695 SF. Waste recommendations are 10%, 12%, 15%, 16%, 17%, 20%, and 22%. |
| 7 | Material calculations | Material rows are parsed from the four displayed waste columns. |

### Deacon Place calibration and validation

- The selected plan-length calibration is 0.11478308 ft/page-point with 15 inliers out of 21 observations, R² 0.998464, and 0.2336 ft RMS residual.
- All 16 area outlines, labels, pitches, and downslope arrows are assigned. Area checks pass within the configured 5% tolerance. The candidate contains 43 vertices, 16 planes, and 55 edges.
- The height solver returns a relative surface, but page-stacking evidence requires facet 11 above facet 15 and facet 12 above facet 13 without support from shared-edge heights. The report also has no absolute eave elevation; the lowest detected eave is only a relative 0 ft datum.
- Validation records 42 passes, 1 warning, 0 failures, and 2 not-evaluable checks. The warning is the rake total (35.582 vs 37.750 LF, 5.743%). Step flashing (23.333 LF) and wall flashing (37.083 LF) are not evaluable from the recovered facet geometry.
- Overall status is `needs_review`. The model in `models/` contains roof surfaces only; wall and absolute heights are not specified in the report.

The report's exact roof area is 1,693 SF, its rounded facet labels total 1,703 SF, and its rounded pitch groups total 1,695 SF. These source groupings remain distinct.

## Roofr — 7704 154A Street Northwest

Input: `7704 154A Street Northwest, Edmonton, AB T5R 1V2.pdf`, an 11-page report with two six-facet structure summaries and a 12-facet report-wide summary. The adapter uses the final report-wide facet count, polygonizes each diagram's shared stroke network, and applies pitch factors to rake edges during the plan/sloped calibration comparison.

- The area and pitch pages yield 12 outlines, all 12 area labels, and all 12 pitch/direction assignments. Rake-adjusted dimension calibration selects the sloped-length hypothesis; page registrations pass and all per-facet rounded-area checks pass.
- The two 7 SF facets overlap the 762 SF lower facet in the report plan. The height solver reports conflicting shared-edge constraints and unresolved stacking for those small facets. Absolute elevation is unavailable.
- The geometry remains `needs_review`. The calibrated draft is available in Geometry review; it must pass height and overlap checks and receive explicit approval before it appears in the project 3D view.

## Roof Manager — `RoofManager-Example-Report.pdf`

Input: five pages. Page 3 and page 4 are landscape vector pages with no embedded image references. The adapter selects page-3 linework using its local color legend, rather than treating every page path or color as a roof edge.

| Page | Content | Drawings / vector segments | Recovery result |
|---|---|---:|---|
| 1 | Summary | 162 / 968; 1 image reference | Aggregate report measurements are selectable text. |
| 2 | Traced footprint | 6 / 534; 1 image reference | The rendered aerial underlay is blank; no useful roof outline was recovered. |
| 3 | Roof Area Analysis | 357 / 34,074; 0 image references | Legend maps eave, rake, ridge, hip, valley, and Lower Eave 1. The diagram-region filter retains 163 classified segments. Polygonization returns 21 closed outline candidates and 10 open endpoints; no large gap is silently closed. |
| 4 | Areas by Section | 179 / 17,668; 0 image references | Aggregate areas and the lower-eave section are available, but no stable per-plane boundaries or per-plane pitches are keyed from this page. |
| 5 | Measurement and material summary | 109 / 560; 1 image reference | Text confirms 7,923 SF, projected area 6,592 SF, predominant pitch 8:12, 31 roof planes, and aggregate lengths. It does not provide 31 per-plane pitch/direction records. |

### Roof Manager calibration and validation

- The local page-3 color legend was read from its swatches and adjacent text. The light-blue swatch maps to `Lower Eave 1`; the page does not expose a separate dashed-style attribute on the classified vector paths.
- Sixty numeric labels were considered. Thirty were associated with a same-class line; 25 were calibration inliers, 5 associated labels were outliers, and 30 labels had no line association. The plan fit is 0.29526817 ft/page-point (25/60 inliers, R² 0.997734, RMS 0.1955 ft); the sloped fit is 0.29493539 ft/page-point (25/60, R² 0.998028, RMS 0.2026 ft). The sloped hypothesis is selected on its higher R², but the scale check warns because only 41.667% of all observations are inliers. The stored calibration includes the matches, residuals, and chosen hypothesis.
- The explicit common-8:12 area hypothesis gives 6,592 × √(1 + (8/12)²) = 7,922.598 SF versus the report's 7,923 SF, a 0.005% difference, so this aggregate check passes at 5%. It is a hypothesis check only; it does not set every plane to 8:12 or solve heights.
- At the selected scale, calibrated edge totals are: eaves 411.197 vs 383 LF (+7.362%, warn); hips 383.391 vs 371 (+3.340%, warn); rakes 55.209 vs 66 (-16.350%, warn); ridges 159.830 vs 160 (-0.106%, pass); valleys 266.531 vs 269 (-0.918%, pass). The 3% class-total tolerance is not met for three classes.
- The network yields 21 closed candidates against 31 report planes and 10 open endpoints. No per-plane pitches or downslope arrows are supplied. There is no 3D candidate, and no plane heights are fabricated.
- Overall validation records 3 passes and 5 warnings: aggregate uniform-pitch area, ridge total, and valley total pass; dimension scale, eaves, hips, rakes, and facet count warn. The reports' aggregate area match does not clear the linework, count, or missing-pitch/direction constraints.
- Final status is `needs_review`; this PDF is **partially outlined but left for manual tracing and review**, not reconstructed as a 31-plane roof. The user must review/remove false-positive candidates, trace missing outlines until the count and connectivity are supported, enter pitch and downslope direction for every plane, and supply lower-eave constraints before a height solution can be evaluated.

## Implementation boundary

`diagram_extract.py` reads word bounding boxes and vector paths. `calibrate.py` fits plan and sloped length hypotheses using robust label-to-segment matching, including one-sided handling for Roofr labels rounded upward. `topology.py` snaps and polygonizes line networks while reporting open endpoints; `heights.py` uses pitch gradients, eaves, shared edges, and explicit stacking relations. `validate.py` records numeric results and tolerances. NVIDIA diagram points are retained as `assisted` candidates only when their page frame matches the PDF calibration; they remain `needs_review` until a contractor verifies them.

The review endpoint accepts page-coordinate edits only; the client never submits heights. The API rebuilds topology, solves heights, and recomputes validation for each new revision. Approval is blocked by any non-passing check, unresolved plane pitch/direction, incomplete/contradictory height constraints, or unresolved overlaps. Page PNGs are rendered on demand from the stored PDF into temporary scratch space and are not stored.

## Known limits

- Roofr's six-plane candidate is useful review evidence, but the two unresolved arrows and contradictory shared-edge constraints prevent a mesh with solved heights. The report's overall measurements remain usable by the estimator independently.
- Roof Manager's aggregate area is consistent with a common 8:12 multiplier, but its per-plane pitch/direction data and 10 plane outlines are missing. The edge-class mismatch also prevents trusting its fitted scale for customer geometry.
- Absolute eave height and ground/wall depth are absent in both reports. Any such values stay marked estimated; a relative lowest-eave datum is not an elevation above grade.
- The static six-surface fixture from `roof3d.html` is still a `demo` preview, omits 18 SF and 12 SF overlap strips by design, and is never persisted as customer geometry. It is independent from extracted geometry and remains available for viewer testing.
- Derived PDF geometry by source: the eight-facet Rue Falardeau Roofr report provides six mixed flat/pitched plan candidates but leaves two facets and shared-height constraints unresolved; the 16-facet Deacon Place Roofr report provides a 16-plane relative-height candidate with unresolved stacking and edge checks. Both remain `needs_review`. Roof Manager provides 21 unlabeled outline candidates, but no plane-type, per-plane-pitch, or direction assignment; it has no 3D candidate and remains `needs_review` for manual tracing.
