from decimal import Decimal

from app.estimation.engine import calculate_estimate, roofr_adjusted_area
from app.schemas import EstimateCreate


def test_waste_is_applied_once_and_package_rounds_up() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Shingles",
                    "quantity": "7923",
                    "quantityUnit": "sq ft",
                    "unit": "bundle",
                    "unitPrice": "35",
                    "coveragePerUnit": "33.3",
                    "packageSize": "1",
                    "wastePercentage": "8",
                }
            ]
        )
    )
    line = result["lines"][0]
    assert line["wasteAdjustedQuantity"] == Decimal("8556.84")
    assert line["quantity"] == Decimal("257")
    assert result["materialSubtotal"] == Decimal("8995.00")


def test_area_to_bundle_coverage_conversion_and_exact_money() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Coverage",
                    "quantity": "100",
                    "quantityUnit": "sq ft",
                    "unit": "bundle",
                    "unitPrice": "12.345",
                    "coveragePerUnit": "33.3",
                    "packageSize": "1",
                }
            ]
        )
    )
    assert result["lines"][0]["quantity"] == Decimal("4")
    assert result["materialSubtotal"] == Decimal("49.40")


def test_square_feet_convert_to_roofing_squares_before_purchase_rounding() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Roofing squares",
                    "quantity": "350",
                    "quantityUnit": "sq ft",
                    "unit": "sq",
                    "unitPrice": "120",
                    "packageSize": "1",
                }
            ]
        )
    )
    assert result["lines"][0]["quantity"] == Decimal("4")
    assert result["materialSubtotal"] == Decimal("480.00")


def test_markup_tax_labour_disposal_and_fixed_charges() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Materials",
                    "quantity": "2",
                    "unit": "unit",
                    "unitPrice": "100",
                    "packageSize": "1",
                },
                {
                    "description": "Labour",
                    "quantity": "10",
                    "unit": "sq ft",
                    "unitPrice": "5",
                    "kind": "labour",
                    "fixedCharge": True,
                },
                {
                    "description": "Disposal",
                    "quantity": "1",
                    "unit": "load",
                    "unitPrice": "40",
                    "kind": "disposal",
                    "fixedCharge": True,
                },
                {
                    "description": "Permit",
                    "quantity": "1",
                    "unit": "charge",
                    "unitPrice": "20",
                    "kind": "fixed",
                    "fixedCharge": True,
                },
            ],
            markupPercentage="10",
            taxRate="0.13",
        )
    )
    assert result["materialSubtotal"] == Decimal("200.00")
    assert result["labourTotal"] == Decimal("50.00")
    assert result["disposalTotal"] == Decimal("40.00")
    assert result["additionalChargesTotal"] == Decimal("20.00")
    assert result["markupTotal"] == Decimal("31.00")
    assert result["taxTotal"] == Decimal("44.33")
    assert result["grandTotal"] == Decimal("385.33")


def test_override_and_missing_configuration_are_explicit() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Manual shingle quantity",
                    "quantity": "10",
                    "quantityUnit": "sq ft",
                    "unit": "bundle",
                    "unitPrice": "1.00",
                    "overrideQuantity": "12",
                    "quantityOrigin": "calculated",
                }
            ]
        )
    )
    row = result["lines"][0]
    assert row["quantityOrigin"] == "manual_override"
    assert row["sourceQuantity"] == Decimal("10")
    assert row["calculatedQuantity"] is None
    assert row["finalQuantity"] == Decimal("12")
    assert row["manualOverride"] == Decimal("12")
    assert row["requiresConfiguration"] is False
    assert row["quantity"] == Decimal("12")
    assert row["lineTotal"] == Decimal("12.00")
    assert result["completeForPricing"] is True
    assert any("bypasses incomplete coverage" in warning for warning in row["warnings"])


def test_source_report_difference_is_warning_with_integer_rounding_tolerance() -> None:
    within_tolerance = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Underlayment",
                    "quantity": "7923",
                    "quantityUnit": "sq ft",
                    "unit": "sq ft",
                    "unitPrice": "1",
                    "wastePercentage": "8",
                    "sourceReportedQuantity": "8557",
                    "sourceReportedUnit": "sq ft",
                }
            ]
        )
    )
    assert not any(
        "differs from the source report" in warning for warning in within_tolerance["warnings"]
    )

    beyond_tolerance = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Underlayment",
                    "quantity": "7923",
                    "quantityUnit": "sq ft",
                    "unit": "sq ft",
                    "unitPrice": "1",
                    "wastePercentage": "8",
                    "sourceReportedQuantity": "8500",
                    "sourceReportedUnit": "sq ft",
                }
            ]
        )
    )
    assert any(
        "differs from the source report" in warning for warning in beyond_tolerance["warnings"]
    )


def test_plural_source_purchase_units_compare_without_conversion_guessing() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Field shingles",
                    "quantity": "7923",
                    "quantityUnit": "sq ft",
                    "unit": "bundle",
                    "unitPrice": "0",
                    "coveragePerUnit": "33.3",
                    "packageSize": "1",
                    "wastePercentage": "8",
                    "sourceReportedQuantity": "257",
                    "sourceReportedUnit": "bundles",
                }
            ]
        )
    )
    warnings = result["lines"][0]["warnings"]
    assert not any("source report" in warning for warning in warnings)


def test_report_fixture_known_measurements_and_materials() -> None:
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parents[3] / "roof_data.json").read_text())
    measurements = fixture["measurements"]
    assert measurements["total_roof_area_sf"] == 7923
    assert measurements["footprint_projected_area_sf"] == 6592
    assert measurements["gross_area_with_waste_sf"] == 8557
    assert measurements["slope_multiplier"] == 1.2019
    assert fixture["project_totals"]["roof_planes"] == 31
    assert fixture["project_totals"]["ice_water_barrier_sf"] == 2859
    assert fixture["edge_lengths"]["eave_lf"] + fixture["edge_lengths"]["rake_lf"] == 449
    assert fixture["edge_lengths"]["ridge_lf"] + fixture["edge_lengths"]["hip_lf"] == 531
    materials = {item["material"]: item for item in fixture["materials"]}
    assert materials["Field Shingles"]["quantity"] == 238
    assert materials["Field Shingles"]["quantity_with_waste"] == 257
    assert materials["Drip Edge — Eaves"]["quantity_with_waste"] == 414
    assert materials["Rake Edge (gable drip edge)"]["quantity_with_waste"] == 71
    assert materials["Ridge Cap"]["quantity_with_waste"] == 573


def test_roofr_pitched_and_flat_waste_rounds_up_once_to_source_area_values() -> None:
    pitched_source = {10: 708, 12: 721, 15: 740, 17: 753, 20: 772, 22: 785, 25: 804}
    flat_source = {10: 567, 12: 577, 15: 592, 17: 603, 20: 618, 22: 628}
    for waste, source in pitched_source.items():
        calculated = roofr_adjusted_area(Decimal("643"), Decimal(waste))
        assert abs(calculated - Decimal(source)) <= Decimal("1")
    for waste, source in flat_source.items():
        calculated = roofr_adjusted_area(Decimal("515"), Decimal(waste))
        assert abs(calculated - Decimal(source)) <= Decimal("1")


def test_roofr_estimate_keeps_source_calculated_and_manual_final_quantities_separate() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Pitched asphalt shingle field",
                    "quantity": "643",
                    "quantityUnit": "sq ft",
                    "unit": "sq ft",
                    "unitPrice": "4.25",
                    "wastePercentage": "10",
                    "sourceReportedQuantity": "708",
                    "sourceReportedUnit": "sq ft",
                    "roofType": "pitched",
                    "roofingSystem": "asphalt_shingle",
                }
            ]
        )
    )
    line = result["lines"][0]
    assert line["sourceQuantity"] == Decimal("643")
    assert line["wasteAdjustedQuantity"] == Decimal("707.30")
    assert line["calculatedQuantity"] == Decimal("708")
    assert line["finalQuantity"] == Decimal("708")
    assert line["manualOverride"] is None
    assert line["quantityOrigin"] == "calculated"
    assert line["lineTotal"] == Decimal("3009.00")
    assert not any("source report reference" in item.lower() for item in line["warnings"])

    overridden = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Flat membrane system",
                    "quantity": "515",
                    "quantityUnit": "sq ft",
                    "unit": "roll",
                    "unitPrice": "125",
                    "wastePercentage": "10",
                    "overrideQuantity": "2",
                    "roofType": "flat",
                    "roofingSystem": "tpo",
                }
            ]
        )
    )["lines"][0]
    assert overridden["sourceQuantity"] == Decimal("515")
    assert overridden["calculatedQuantity"] is None
    assert overridden["finalQuantity"] == Decimal("2")
    assert overridden["manualOverride"] == Decimal("2")
    assert overridden["quantityOrigin"] == "manual_override"
    assert overridden["lineTotal"] == Decimal("250.00")


def test_flat_system_requires_explicit_membrane_selection_and_config() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "Flat roof membrane",
                    "quantity": "515",
                    "quantityUnit": "sq ft",
                    "unit": "roll",
                    "unitPrice": "100",
                    "wastePercentage": "0",
                    "roofType": "flat",
                }
            ]
        )
    )
    line = result["lines"][0]
    assert line["requiresConfiguration"] is True
    assert line["lineTotal"] is None
    assert any("select a flat roofing system" in warning for warning in line["warnings"])

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Flat roof areas cannot use"):
        EstimateCreate(
            lines=[
                {
                    "description": "Wrong flat material",
                    "quantity": "515",
                    "unit": "sq ft",
                    "unitPrice": "0",
                    "roofType": "flat",
                    "roofingSystem": "asphalt_shingle",
                }
            ]
        )


def test_flat_roll_dimensions_apply_user_configured_laps_before_package_rounding() -> None:
    result = calculate_estimate(
        EstimateCreate(
            lines=[
                {
                    "description": "TPO roll",
                    "quantity": "515",
                    "quantityUnit": "sq ft",
                    "unit": "roll",
                    "unitPrice": "310",
                    "wastePercentage": "10",
                    "packageSize": "1",
                    "roofType": "flat",
                    "roofingSystem": "tpo",
                    "rollWidthFeet": "10",
                    "rollLengthFeet": "100",
                    "lapWidthInches": "6",
                    "lapLengthInches": "6",
                }
            ]
        )
    )
    line = result["lines"][0]
    assert line["calculationInputs"]["coveragePerUnit"] == Decimal("945.25")
    assert line["calculatedQuantity"] == Decimal("1")
    assert line["lineTotal"] == Decimal("310.00")


def test_roofr_source_references_match_the_selected_system_and_waste() -> None:
    from app.main import _trusted_report_quantity
    from app.schemas import EstimateLineInput

    recommendations = [
        {
            "roofType": "pitched",
            "wastePercentage": "10",
            "sourceAdjustedAreaSqFt": "708",
        },
        {"roofType": "flat", "wastePercentage": "15", "sourceAdjustedAreaSqFt": "592"},
    ]
    references = [
        {
            "material": "IKO - Cambridge",
            "unit": "bundle",
            "roofType": "pitched",
            "quantityByWastePercentage": {"10": "22", "15": "23"},
        }
    ]
    shingle_line = EstimateLineInput(
        description="IKO - Cambridge shingles",
        quantity="643",
        quantityUnit="sq ft",
        unit="bundle",
        unitPrice="0",
        wastePercentage="10",
        roofType="pitched",
        roofingSystem="asphalt_shingle",
    )
    assert _trusted_report_quantity(shingle_line, references, recommendations) == {
        "sourceReportedQuantity": Decimal("22"),
        "sourceReportedUnit": "bundle",
    }
    flat_line = EstimateLineInput(
        description="Flat roofing membrane",
        quantity="515",
        quantityUnit="sq ft",
        unit="roll",
        unitPrice="0",
        wastePercentage="15",
        roofType="flat",
        roofingSystem="tpo",
    )
    assert _trusted_report_quantity(flat_line, references, recommendations) == {
        "sourceReportedQuantity": Decimal("592"),
        "sourceReportedUnit": "sq ft",
    }
