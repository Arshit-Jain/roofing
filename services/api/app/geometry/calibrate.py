"""Robust drawing-scale calibration and cross-page similarity registration."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Literal

from app.geometry.topology import Point2, segment_distance


@dataclass(frozen=True)
class DimensionObservation:
    id: str
    value_feet: float
    location: Point2
    page_number: int
    edge_class: str | None = None
    rounding_step_feet: float = 1.0
    rounded_up: bool = True


@dataclass(frozen=True)
class CalibrationSegment:
    id: str
    start: Point2
    end: Point2
    page_number: int
    edge_class: str | None = None
    sloped_factor: float = 1.0

    @property
    def length_points(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(frozen=True)
class ScaleMatch:
    observation_id: str
    segment_id: str | None
    label_feet: float
    predicted_feet: float | None
    residual_feet: float | None
    distance_points: float | None
    inlier: bool


@dataclass(frozen=True)
class ScaleFit:
    hypothesis: Literal["plan", "sloped"]
    status: Literal["calibrated", "needs_review"]
    feet_per_point: float | None
    inlier_count: int
    observation_count: int
    residual_rms_feet: float | None
    r_squared: float | None
    matches: tuple[ScaleMatch, ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "method": "one-sided rounded-dimension RANSAC with trimmed least-squares refinement",
            "hypothesis": self.hypothesis,
            "status": self.status,
            "feetPerPoint": self.feet_per_point,
            "inlierCount": self.inlier_count,
            "observationCount": self.observation_count,
            "residualRmsFeet": self.residual_rms_feet,
            "rSquared": self.r_squared,
            "matches": [asdict(match) for match in self.matches],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PageTransform:
    scale: float
    translate_x: float
    translate_y: float
    width_ratio: float
    height_ratio: float
    disagreement_percent: float
    rotation_degrees: float = 0.0

    def apply(self, point: Point2) -> Point2:
        return (
            self.scale * point[0] + self.translate_x,
            self.scale * point[1] + self.translate_y,
        )

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def fit_scale(
    observations: list[DimensionObservation],
    segments: list[CalibrationSegment],
    *,
    hypothesis: Literal["plan", "sloped"] = "plan",
    max_label_distance_points: float = 22.0,
    max_relative_residual: float = 0.5,
    minimum_inliers: int = 3,
) -> ScaleFit:
    """Fit feet per PDF point without treating rounded-up labels as exact lengths.

    Each printed value N is an interval [N-step, N] when it is displayed rounded up.
    Candidate segment associations are spatially bounded and each label contributes
    at most one match, preventing a cluster of collinear path fragments from inflating
    the inlier count.
    """
    candidates: dict[str, list[tuple[CalibrationSegment, float, float]]] = {}
    scale_seeds: set[float] = set()
    for observation in observations:
        nearby: list[tuple[CalibrationSegment, float, float]] = []
        for segment in segments:
            if segment.page_number != observation.page_number:
                continue
            if observation.edge_class and segment.edge_class != observation.edge_class:
                continue
            distance = segment_distance(observation.location, segment.start, segment.end)
            length = segment.length_points
            if distance > max_label_distance_points or length <= 1e-6:
                continue
            factor = segment.sloped_factor if hypothesis == "sloped" else 1.0
            adjusted_length = length * factor
            nearby.append((segment, distance, adjusted_length))
            midpoint = observation.value_feet - observation.rounding_step_feet / 2
            if midpoint > 0 and adjusted_length > 0:
                scale_seeds.add(midpoint / adjusted_length)
        candidates[observation.id] = nearby

    if not observations or not scale_seeds:
        return ScaleFit(
            hypothesis,
            "needs_review",
            None,
            0,
            len(observations),
            None,
            None,
            (),
            "No dimension labels could be associated with nearby vector segments.",
        )

    best_key: tuple[int, float, float] = (-1, float("inf"), float("inf"))
    best_scale: float | None = None
    best_matches: list[ScaleMatch] = []
    for seed in scale_seeds:
        matches: list[ScaleMatch] = []
        for observation in observations:
            choices: list[tuple[float, float, CalibrationSegment, float]] = []
            for segment, distance, adjusted_length in candidates[observation.id]:
                predicted = seed * adjusted_length
                if observation.rounded_up:
                    lower = observation.value_feet - observation.rounding_step_feet
                    upper = observation.value_feet
                else:
                    half_step = observation.rounding_step_feet / 2
                    lower = observation.value_feet - half_step
                    upper = observation.value_feet + half_step
                interval_error = max(lower - predicted, 0.0, predicted - upper)
                allowed = max(observation.rounding_step_feet * max_relative_residual, 0.05)
                if interval_error <= allowed:
                    choices.append((interval_error, distance, segment, adjusted_length))
            if choices:
                interval_error, distance, segment, adjusted_length = min(
                    choices, key=lambda value: (value[0], value[1], value[2].id)
                )
                predicted = seed * adjusted_length
                midpoint = observation.value_feet - (
                    observation.rounding_step_feet / 2 if observation.rounded_up else 0
                )
                matches.append(
                    ScaleMatch(
                        observation.id,
                        segment.id,
                        observation.value_feet,
                        predicted,
                        predicted - midpoint,
                        distance,
                        interval_error <= 1e-5,
                    )
                )
            else:
                matches.append(
                    ScaleMatch(
                        observation.id, None, observation.value_feet, None, None, None, False
                    )
                )
        inlier_matches = [match for match in matches if match.inlier]
        residuals = [abs(match.residual_feet or 0) for match in inlier_matches]
        distances = [match.distance_points or 0 for match in inlier_matches]
        key = (
            -len(inlier_matches),
            statistics.median(residuals) if residuals else math.inf,
            statistics.median(distances) if distances else math.inf,
        )
        if key < best_key:
            best_key, best_scale, best_matches = key, seed, matches

    inliers = [match for match in best_matches if match.inlier and match.segment_id]
    if len(inliers) >= 2:
        ratios: list[float] = []
        observation_by_id = {observation.id: observation for observation in observations}
        segment_by_id = {segment.id: segment for segment in segments}
        for match in inliers:
            observation = observation_by_id[match.observation_id]
            segment = segment_by_id[match.segment_id or ""]
            length = segment.length_points * (
                segment.sloped_factor if hypothesis == "sloped" else 1.0
            )
            midpoint = observation.value_feet - (
                observation.rounding_step_feet / 2 if observation.rounded_up else 0
            )
            if length > 0 and midpoint > 0:
                ratios.append(midpoint / length)
        if ratios:
            refined = statistics.median(ratios)
            if refined > 0:
                best_scale = refined
                best_matches = _evaluate_scale(
                    observations,
                    candidates,
                    best_scale,
                    hypothesis,
                    max_relative_residual,
                )
                inliers = [match for match in best_matches if match.inlier and match.segment_id]

    residuals = [match.residual_feet or 0.0 for match in inliers]
    rms = (
        math.sqrt(sum(value * value for value in residuals) / len(residuals)) if residuals else None
    )
    r_squared = _r_squared(inliers)
    status: Literal["calibrated", "needs_review"] = (
        "calibrated"
        if best_scale is not None and len(inliers) >= minimum_inliers and (r_squared or 0) >= 0.5
        else "needs_review"
    )
    reason = (
        None
        if status == "calibrated"
        else (
            f"Only {len(inliers)} of {len(observations)} dimension labels fit "
            f"the {hypothesis} hypothesis."
        )
    )
    return ScaleFit(
        hypothesis,
        status,
        best_scale,
        len(inliers),
        len(observations),
        rms,
        r_squared,
        tuple(best_matches),
        reason,
    )


def fit_both_scale_hypotheses(
    observations: list[DimensionObservation],
    segments: list[CalibrationSegment],
    **kwargs: object,
) -> tuple[ScaleFit, ScaleFit, Literal["plan", "sloped", "ambiguous", "none"]]:
    plan = fit_scale(observations, segments, hypothesis="plan", **kwargs)  # type: ignore[arg-type]
    sloped = fit_scale(observations, segments, hypothesis="sloped", **kwargs)  # type: ignore[arg-type]
    if plan.status != "calibrated" and sloped.status != "calibrated":
        chosen: Literal["plan", "sloped", "ambiguous", "none"] = "none"
    elif plan.status == "calibrated" and sloped.status != "calibrated":
        chosen = "plan"
    elif sloped.status == "calibrated" and plan.status != "calibrated":
        chosen = "sloped"
    else:
        plan_quality = (plan.inlier_count, plan.r_squared or 0)
        sloped_quality = (sloped.inlier_count, sloped.r_squared or 0)
        if plan_quality == sloped_quality:
            chosen = "ambiguous"
        else:
            chosen = "plan" if plan_quality > sloped_quality else "sloped"
    return plan, sloped, chosen


def register_outlines(
    source_bounds: tuple[float, float, float, float],
    target_bounds: tuple[float, float, float, float],
) -> PageTransform:
    """Register unrotated page diagrams with an isotropic scale + translation fit.

    Bounds are (min_x, min_y, max_x, max_y) of the complete vector roof outline.
    A width/height scale disagreement is preserved as a diagnostic; it is not hidden
    by stretching the drawing non-uniformly.
    """
    source_width = source_bounds[2] - source_bounds[0]
    source_height = source_bounds[3] - source_bounds[1]
    target_width = target_bounds[2] - target_bounds[0]
    target_height = target_bounds[3] - target_bounds[1]
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError("Outline bounds must have positive width and height")
    width_ratio = target_width / source_width
    height_ratio = target_height / source_height
    scale = (width_ratio + height_ratio) / 2
    disagreement_percent = abs(width_ratio - height_ratio) / scale * 100
    return PageTransform(
        scale,
        target_bounds[0] - scale * source_bounds[0],
        target_bounds[1] - scale * source_bounds[1],
        width_ratio,
        height_ratio,
        disagreement_percent,
    )


def _evaluate_scale(
    observations: list[DimensionObservation],
    candidates: dict[str, list[tuple[CalibrationSegment, float, float]]],
    scale: float,
    hypothesis: Literal["plan", "sloped"],
    max_relative_residual: float,
) -> list[ScaleMatch]:
    matches: list[ScaleMatch] = []
    for observation in observations:
        choices: list[tuple[float, float, CalibrationSegment, float]] = []
        lower = (
            observation.value_feet - observation.rounding_step_feet
            if observation.rounded_up
            else observation.value_feet - observation.rounding_step_feet / 2
        )
        upper = (
            observation.value_feet
            if observation.rounded_up
            else observation.value_feet + observation.rounding_step_feet / 2
        )
        for segment, distance, adjusted_length in candidates[observation.id]:
            predicted = scale * adjusted_length
            interval_error = max(lower - predicted, 0.0, predicted - upper)
            allowed = max(observation.rounding_step_feet * max_relative_residual, 0.05)
            if interval_error <= allowed:
                choices.append((interval_error, distance, segment, adjusted_length))
        if choices:
            interval_error, distance, segment, length = min(
                choices, key=lambda value: (value[0], value[1], value[2].id)
            )
            predicted = scale * length
            midpoint = observation.value_feet - (
                observation.rounding_step_feet / 2 if observation.rounded_up else 0
            )
            matches.append(
                ScaleMatch(
                    observation.id,
                    segment.id,
                    observation.value_feet,
                    predicted,
                    predicted - midpoint,
                    distance,
                    interval_error <= 1e-5,
                )
            )
        else:
            matches.append(
                ScaleMatch(observation.id, None, observation.value_feet, None, None, None, False)
            )
    return matches


def _r_squared(matches: list[ScaleMatch]) -> float | None:
    pairs = [
        (match.label_feet - 0.5, match.predicted_feet)
        for match in matches
        if match.inlier and match.predicted_feet is not None
    ]
    if len(pairs) < 2:
        return None
    observed_mean = sum(observed for observed, _ in pairs) / len(pairs)
    total = sum((observed - observed_mean) ** 2 for observed, _ in pairs)
    residual = sum((observed - predicted) ** 2 for observed, predicted in pairs)
    if total <= 1e-12:
        return 1.0 if residual <= 1e-9 else 0.0
    return max(0.0, min(1.0, 1 - residual / total))
