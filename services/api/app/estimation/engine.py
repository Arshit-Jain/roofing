"""Decimal-based estimator. Quantities are measured in base units; package rounding is separate."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from app.schemas import EstimateCreate, EstimateLineInput

CENT = Decimal("0.01")
UNIT_FACTORS: dict[str, tuple[str, Decimal]] = {
    "sf": ("area", Decimal("1")),
    "sqft": ("area", Decimal("1")),
    "sq ft": ("area", Decimal("1")),
    "square foot": ("area", Decimal("1")),
    "squares": ("area", Decimal("100")),
    "square": ("area", Decimal("100")),
    "sq": ("area", Decimal("100")),
    "ft": ("length", Decimal("1")),
    "lf": ("length", Decimal("1")),
    "linear ft": ("length", Decimal("1")),
}
UNIT_ALIASES = {
    "bundles": "bundle",
    "pcs": "piece",
    "pieces": "piece",
    "sticks": "stick",
    "units": "unit",
}


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def convert_quantity(quantity: Decimal, from_unit: str, to_unit: str) -> Decimal | None:
    """Convert only explicit, dimensionally safe roofing units."""
    source_value = from_unit.strip().casefold()
    target_value = to_unit.strip().casefold()
    source = UNIT_ALIASES.get(source_value, source_value)
    target = UNIT_ALIASES.get(target_value, target_value)
    if source == target:
        return quantity
    from_data = UNIT_FACTORS.get(source)
    to_data = UNIT_FACTORS.get(target)
    if not from_data or not to_data or from_data[0] != to_data[0]:
        return None
    return quantity * from_data[1] / to_data[1]


def roofr_adjusted_area(area_sq_ft: Decimal, waste_percentage: Decimal) -> Decimal:
    """Return Roofr's whole-SF, round-up quantity as a source-table comparison value."""
    return (area_sq_ft * (Decimal("1") + waste_percentage / Decimal("100"))).to_integral_value(
        rounding=ROUND_CEILING
    )


def calculate_line(
    line: EstimateLineInput, default_waste_percentage: Decimal = Decimal("0")
) -> dict[str, Any]:
    warnings: list[str] = []
    system_required = line.roofType is not None and line.roofingSystem is None
    waste_required = line.requireWasteSelection and line.wastePercentage is None
    requires_configuration = system_required or waste_required
    if system_required:
        warnings.append(f"requires configuration: select a {line.roofType} roofing system")
    if waste_required:
        warnings.append("requires configuration: select a waste percentage for this roof system")
    source_quantity = line.quantity
    waste_percentage = (
        line.wastePercentage
        if line.wastePercentage is not None
        else Decimal("0")
        if line.requireWasteSelection
        else default_waste_percentage
    )
    waste_quantity = source_quantity * (Decimal("1") + waste_percentage / Decimal("100"))
    purchase_units: Decimal | None = waste_quantity
    conversion_source = line.quantityUnit or line.unit
    coverage_per_unit = line.coveragePerUnit
    roll_configuration_incomplete = False
    if line.rollWidthFeet is not None and line.rollLengthFeet is not None:
        lap_width = line.lapWidthInches
        lap_length = line.lapLengthInches
        effective_width = line.rollWidthFeet - (lap_width or Decimal("0")) / Decimal("12")
        effective_length = line.rollLengthFeet - (lap_length or Decimal("0")) / Decimal("12")
        if effective_width <= 0 or effective_length <= 0:
            warnings.append("requires configuration: laps leave no usable roll coverage")
            purchase_units = None
            requires_configuration = True
        else:
            coverage_per_unit = effective_width * effective_length
            if lap_width is None or lap_length is None:
                warnings.append(
                    "Roll dimensions are set, but both lap dimensions still need configuration"
                )
                roll_configuration_incomplete = True
    if coverage_per_unit is not None and purchase_units is not None:
        coverage_unit = line.coverageUnit or conversion_source
        coverage_quantity = convert_quantity(waste_quantity, conversion_source, coverage_unit)
        if coverage_quantity is None:
            warnings.append(
                "requires configuration: coverage unit cannot be converted from quantity unit"
            )
            requires_configuration = True
            purchase_units = None
        else:
            purchase_units = (coverage_quantity / coverage_per_unit).to_integral_value(
                rounding=ROUND_CEILING
            )
    elif purchase_units is not None:
        converted_quantity = convert_quantity(waste_quantity, conversion_source, line.unit)
        if converted_quantity is None:
            warnings.append(
                "requires configuration: quantity unit cannot be converted to purchase unit"
            )
            requires_configuration = True
            purchase_units = None
        else:
            purchase_units = converted_quantity
            if (
                line.roofType is not None
                and UNIT_FACTORS.get(conversion_source.strip().casefold(), (None, None))[0]
                == "area"
                and UNIT_FACTORS.get(line.unit.strip().casefold(), (None, None))[0] == "area"
            ):
                purchase_units = purchase_units.to_integral_value(rounding=ROUND_CEILING)
    if (
        line.packageSize is None
        and coverage_per_unit is not None
        and line.kind == "material"
        and not line.fixedCharge
    ):
        warnings.append("requires configuration: package size is not set")
        requires_configuration = True
    elif line.packageSize is not None and purchase_units is not None:
        packages = (purchase_units / line.packageSize).to_integral_value(rounding=ROUND_CEILING)
        purchase_units = packages * line.packageSize
    if roll_configuration_incomplete:
        requires_configuration = True
    if line.sourceReportedQuantity is not None:
        source_unit = line.sourceReportedUnit or line.unit
        comparable_calculated = convert_quantity(waste_quantity, conversion_source, source_unit)
        comparable_source_quantity = line.sourceReportedQuantity
        if comparable_calculated is not None:
            differs = abs(comparable_calculated - comparable_source_quantity) > Decimal("1")
        else:
            converted_purchase = (
                convert_quantity(purchase_units, line.unit, source_unit)
                if purchase_units is not None
                else None
            )
            differs = converted_purchase is None or abs(
                converted_purchase - comparable_source_quantity
            ) > Decimal("1")
        if differs and comparable_calculated is None and purchase_units is None:
            warnings.append(
                "requires configuration: source report quantity uses an incompatible unit"
            )
        elif differs:
            warnings.append(
                "App-calculated purchase quantity differs from the source report reference"
            )

    calculated_quantity = purchase_units
    manual_override = line.overrideQuantity
    final_quantity = manual_override if manual_override is not None else calculated_quantity
    if manual_override is not None:
        warnings.append(
            "Final purchase quantity is a manual override; source quantity and waste "
            "calculation are preserved"
        )
        unresolved_configuration = [
            warning
            for warning in warnings
            if warning.startswith("requires configuration:")
            and not warning.startswith("requires configuration: select a ")
        ]
        if unresolved_configuration:
            warnings = [warning for warning in warnings if warning not in unresolved_configuration]
            warnings.append("Manual final quantity bypasses incomplete coverage or package inputs")
        requires_configuration = system_required or waste_required
    if final_quantity is None:
        requires_configuration = True
    rounded_unit_price = money(line.unitPrice)
    cost = (
        None
        if requires_configuration or final_quantity is None
        else money(final_quantity * rounded_unit_price)
    )
    return {
        "description": line.description,
        "unit": line.unit,
        "quantityUnit": line.quantityUnit or line.unit,
        "sourceQuantity": source_quantity,
        "wasteAdjustedQuantity": waste_quantity,
        "calculatedQuantity": calculated_quantity,
        "finalQuantity": final_quantity,
        "manualOverride": manual_override,
        "quantity": final_quantity,
        "unitPrice": rounded_unit_price,
        "quantityOrigin": "manual_override" if manual_override is not None else "calculated",
        "sourceQuantityOrigin": line.quantityOrigin,
        "kind": line.kind,
        "wastePercentage": waste_percentage,
        "calculationInputs": {
            "baseQuantity": source_quantity,
            "wasteMultiplier": Decimal("1") + waste_percentage / Decimal("100"),
            "coveragePerUnit": coverage_per_unit,
            "coverageUnit": line.coverageUnit,
            "packageSize": line.packageSize,
            "rollWidthFeet": line.rollWidthFeet,
            "rollLengthFeet": line.rollLengthFeet,
            "lapWidthInches": line.lapWidthInches,
            "lapLengthInches": line.lapLengthInches,
            "sourceReportedQuantity": line.sourceReportedQuantity,
            "sourceReportedUnit": line.sourceReportedUnit,
            "unitPrice": line.unitPrice,
            "roofType": line.roofType,
            "roofingSystem": line.roofingSystem,
        },
        "lineTotal": cost,
        "requiresConfiguration": requires_configuration,
        "warnings": warnings,
    }


def calculate_estimate(
    request: EstimateCreate,
    *,
    default_waste_percentage: Decimal = Decimal("0"),
    default_markup_percentage: Decimal = Decimal("0"),
    default_tax_rate: Decimal = Decimal("0"),
    default_currency: str = "CAD",
) -> dict[str, Any]:
    lines = [calculate_line(line, default_waste_percentage) for line in request.lines]
    material_subtotal = sum(
        (
            row["lineTotal"]
            for row in lines
            if row["kind"] == "material" and row["lineTotal"] is not None
        ),
        Decimal("0.00"),
    )
    labour_total = sum(
        (
            row["lineTotal"]
            for row in lines
            if row["kind"] == "labour" and row["lineTotal"] is not None
        ),
        Decimal("0.00"),
    )
    disposal_total = money(request.disposalTotal) + sum(
        (
            row["lineTotal"]
            for row in lines
            if row["kind"] == "disposal" and row["lineTotal"] is not None
        ),
        Decimal("0.00"),
    )
    fixed_total = sum(
        (
            row["lineTotal"]
            for row in lines
            if row["kind"] == "fixed" and row["lineTotal"] is not None
        ),
        Decimal("0.00"),
    )
    subtotal = material_subtotal + labour_total + disposal_total + fixed_total
    markup_percentage = (
        request.markupPercentage
        if request.markupPercentage is not None
        else default_markup_percentage
    )
    tax_rate = request.taxRate if request.taxRate is not None else default_tax_rate
    currency = request.currency or default_currency
    markup = money(subtotal * markup_percentage / Decimal("100"))
    taxable = subtotal + markup
    tax = money(taxable * tax_rate)
    grand_total = money(taxable + tax)
    return {
        "lines": lines,
        "completeForPricing": all(not row["requiresConfiguration"] for row in lines),
        "materialSubtotal": money(material_subtotal),
        "labourTotal": money(labour_total),
        "disposalTotal": disposal_total,
        "additionalChargesTotal": money(fixed_total),
        "markupPercentage": markup_percentage,
        "markupTotal": markup,
        "taxRate": tax_rate,
        "taxTotal": tax,
        "grandTotal": grand_total,
        "currency": currency.upper(),
        "warnings": [warning for row in lines for warning in row["warnings"]],
    }
