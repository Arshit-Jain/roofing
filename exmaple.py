
import os
import json
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

API_KEY = os.environ.get("llm-api-key")

if not API_KEY:
    raise ValueError(
        "Missing API key. Add llm-api-key=YOUR_API_KEY to your .env file."
    )

client = genai.Client(api_key=API_KEY)

MODEL = "gemini-3.8-flash"
MAX_RETRIES = 5


# =========================================================
# EXTRACTION PROMPT
# =========================================================

PROMPT = """
You are a roofing measurement data extraction system.

Analyze the entire attached roofing measurement PDF.

Extract the information into a structured JSON object that can be
used by a roofing estimation application.

Return ONLY valid JSON. Do not use Markdown or code fences.

Use this structure:

{
  "property": {
    "name": null
  },
  "project_totals": {
    "total_roof_area_sf": null,
    "predominant_pitch": null,
    "predominant_pitch_degrees": null,
    "gross_area_sf": null,
    "waste_percentage": null,
    "ice_water_barrier_sf": null,
    "roof_planes": null,
    "structures": null
  },
  "edge_lengths": {
    "eave_lf": null,
    "rake_lf": null,
    "total_perimeter_lf": null,
    "ridge_lf": null,
    "hip_lf": null,
    "valley_lf": null,
    "overhang_offset_lf": null
  },
  "roof_planes": [],
  "areas_by_pitch": [],
  "roof_sections": [],
  "measurements": {
    "total_roof_area_sf": null,
    "footprint_projected_area_sf": null,
    "gross_area_with_waste_sf": null,
    "roofing_squares_net": null,
    "roofing_squares_gross": null,
    "slope_multiplier": null,
    "predominant_pitch": null,
    "roof_planes": null,
    "ice_water_eave_perimeter_lf": null
  },
  "materials": [],
  "gutters": {
    "total_gutter_lf": null,
    "gutter_type": null,
    "downspouts": null
  },
  "extraction_notes": []
}

RULES:

1. Extract values from the PDF only. Do not invent missing values.
2. Use JSON numbers for numerical measurements.
3. Use square feet for area and linear feet for length.
4. Preserve pitch notation such as "8:12".
5. Store pitch degrees as a number.
6. Store waste percentage as a number, such as 8.
7. Use null for missing or unreadable values, not zero.
8. Preserve net quantities and waste-adjusted quantities separately.
9. For each roof plane, include:
   - name
   - area_sf
   - area_with_waste_sf
   - pitch
   - percentage_of_total
10. For each roof section, include:
   - name
   - area_sf
11. For each material, include:
   - material
   - quantity
   - unit
   - quantity_with_waste
   - unit_with_waste, if applicable
   - based_on
12. For areas by pitch, include:
   - pitch
   - area_sf
   - squares
   - faces
   - percentage_of_roof
13. For gutters, preserve the reported gutter type and length.
14. Include important caveats or ambiguous values in extraction_notes.
15. Do not infer roof polygons, 3D coordinates, or connections from dimensions
    alone. The output is measurement data, not a reconstructed 3D model.
16. Do not combine measurements from different tables unless their meanings
    are clearly equivalent.
17. If two parts of the PDF report different values, preserve the appropriate
    values in their respective fields and explain the discrepancy in notes.
18. Include units in field names where specified by the schema.
"""


# =========================================================
# JSON CLEANING
# =========================================================

def parse_json_response(text):
    """Parse JSON, allowing for accidental Markdown code fences."""

    if not text:
        raise ValueError("Gemini returned an empty response.")

    cleaned = text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json"):].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned[len("```"):].strip()

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    return json.loads(cleaned)


# =========================================================
# PDF EXTRACTION
# =========================================================

def extract_roof_data(pdf_path):
    """Upload a PDF, extract roofing measurements, and return JSON data."""

    if not os.path.isfile(pdf_path):
        raise FileNotFoundError("Selected report PDF was not found.")

    print("Uploading selected PDF for transient Gemini processing.")

    uploaded_file = None
    last_error = None
    try:
        uploaded_file = client.files.upload(file=pdf_path)
        print("PDF uploaded to Gemini for transient processing.")
        for attempt in range(MAX_RETRIES):
            try:
                print(
                    f"Requesting extraction from {MODEL} "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})..."
                )

                response = client.models.generate_content(
                    model=MODEL,
                    contents=[uploaded_file, PROMPT],
                    config={
                        "response_mime_type": "application/json",
                        "max_output_tokens": 8192,
                    },
                )

                output = response.text
                if not output:
                    raise ValueError("Gemini returned no text output.")

                roof_data = parse_json_response(output)
                if not isinstance(roof_data, dict):
                    raise ValueError("Expected a JSON object from Gemini.")

                print("Extraction completed successfully.")
                return roof_data
            except errors.ServerError as exc:
                last_error = exc
            except errors.APIError as exc:
                last_error = exc
                status = getattr(exc, "code", None)
                if status not in (429, 500, 502, 503, 504):
                    raise

            if attempt < MAX_RETRIES - 1:
                wait_seconds = min(2**attempt, 30)
                print(f"Temporary Gemini error; retrying in {wait_seconds} seconds.")
                time.sleep(wait_seconds)

        raise RuntimeError(
            f"Extraction failed after {MAX_RETRIES} attempts ({type(last_error).__name__})."
        ) from None
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                # File API storage is transient only; never print response bodies or credentials.
                print("Warning: Gemini could not confirm deletion of its transient uploaded file.")


# =========================================================
# SAVE JSON
# =========================================================

def save_json(data, output_path):
    """Save extracted data to a UTF-8 JSON file."""

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Saved extracted data to: {output_path}")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    pdf_path = "RoofManager-Example-Report.pdf"
    output_path = "roof_data.json"

    try:
        roof_data = extract_roof_data(pdf_path)

        print("\n" + "=" * 60)
        print("EXTRACTED ROOF DATA")
        print("=" * 60)

        print(json.dumps(roof_data, indent=2, ensure_ascii=False))

        save_json(roof_data, output_path)

    except Exception as exc:
        print(f"\nERROR: Roof extraction failed ({type(exc).__name__}).")
        raise SystemExit(1) from None
