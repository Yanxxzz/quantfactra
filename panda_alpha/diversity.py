"""Structural novelty and cross-sectional *factor-value* redundancy checks.

This module deliberately does not accept IC, Sharpe or return summaries as a
substitute for a date-by-security factor panel. A pass is a diversity pass only;
it says nothing about a factor's economic usefulness.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import io
import tokenize
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


_ALIASES = {"REF": "DELAY", "DIFF": "DELTA", "MA": "TS_MEAN",
            "STD": "STDDEV", "HHV": "TS_MAX", "LLV": "TS_MIN"}
_DSL_IDENTIFIERS = {"OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT", "VWAP", "TURNOVER",
                    "RANK", "DELAY", "DELTA", "TS_MEAN", "STDDEV", "TS_MAX", "TS_MIN", "TS_ZSCORE",
                    "SUM", "COUNT", "CORR", "IF", "ABS", "MIN", "MAX", "LOG", "SQRT"}


def _ast_key(node: ast.AST, numeric_family: bool) -> Any:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ("number", "parameter" if numeric_family else float(value))
        return ("constant", repr(value))
    if isinstance(node, ast.Name):
        upper = node.id.upper()
        return ("name", _ALIASES.get(upper, upper if upper in _DSL_IDENTIFIERS else node.id))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
        # Sorting is safe for ordinary declarative arithmetic factor formulas.
        # It is only used for deduplication; it never rewrites executable code.
        terms: list[Any] = []
        def collect(item: ast.AST) -> None:
            if isinstance(item, ast.BinOp) and type(item.op) is type(node.op):
                collect(item.left)
                collect(item.right)
            else:
                terms.append(_ast_key(item, numeric_family))
        collect(node)
        return (type(node.op).__name__, tuple(sorted(terms, key=repr)))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return _ast_key(node.operand, numeric_family)
    return (type(node).__name__, tuple(
        (name, tuple(_ast_key(v, numeric_family) if isinstance(v, ast.AST) else v
                     for v in value) if isinstance(value, list)
         else _ast_key(value, numeric_family) if isinstance(value, ast.AST) else value)
        for name, value in ast.iter_fields(node)
        if name not in {"ctx", "type_comment"}
        # Python 3.12 adds an empty type_params field to ordinary functions and
        # classes. Preserve their stored 3.11 identities; retain real generics.
        and not (name == "type_params" and value == [])
    ))


def canonical_signature(code_or_formula: str, ignore_numeric_parameters: bool = False) -> str:
    """Stable AST fingerprint; tokenize unsupported DSL syntax without executing it.

    ``ignore_numeric_parameters`` groups window/threshold grids into one family.
    This conservative grouping prevents endless small numeric mutations; it is
    not a claim that every member of that family is mathematically equivalent.
    """
    source = code_or_formula.strip()
    if not source:
        raise ValueError("A canonical signature requires a formula or code")
    try:
        key = _ast_key(ast.parse(source), ignore_numeric_parameters)
    except SyntaxError:
        try:
            key = [(t.type, "parameter" if ignore_numeric_parameters and t.type == tokenize.NUMBER
                    else t.string) for t in tokenize.generate_tokens(io.StringIO(source).readline)
                   if t.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                     tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}]
        except (tokenize.TokenError, IndentationError):
            key = ("unparsed", " ".join(source.split()))
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()


def expression_features(expression: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract identifiers and calls for metadata; explicit requirements remain authoritative."""
    try:
        tree = ast.parse(expression)
    except SyntaxError:
        return (), ()
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - calls
    return tuple(sorted(names)), tuple(sorted(calls))


@dataclass(frozen=True)
class DiversityPolicy:
    max_abs_correlation: float = 0.85
    min_assets: int = 20
    min_days: int = 20
    min_daily_coverage: float = 0.70
    min_valid_day_fraction: float = 0.60

    def __post_init__(self) -> None:
        if not 0 < self.max_abs_correlation <= 1:
            raise ValueError("max_abs_correlation must be in (0, 1]")
        if self.min_assets < 3 or self.min_days < 1:
            raise ValueError("Need at least 3 assets and 1 day")
        if not 0 < self.min_daily_coverage <= 1 or not 0 < self.min_valid_day_fraction <= 1:
            raise ValueError("Coverage fractions must be in (0, 1]")


@dataclass
class PairCorrelation:
    pool_id: str
    status: str
    valid_days: int
    comparison_days: int
    mean_abs_correlation: float | None = None
    signed_mean_correlation: float | None = None
    median_abs_correlation: float | None = None
    minimum_daily_assets: int = 0
    median_coverage: float | None = None
    daily: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiversityAssessment:
    status: str
    reasons: list[str]
    correlations: list[PairCorrelation] = field(default_factory=list)
    max_abs_correlation: float | None = None
    economic_status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def as_factor_panel(values: pd.DataFrame | pd.Series) -> pd.DataFrame:
    """Accept wide date x security values or a two-level date/security Series.

    A long table with ``date``, ``symbol``/``asset``/``code`` and ``value`` is also
    accepted. Duplicate observations are errors rather than silently averaged.
    """
    if isinstance(values, pd.Series):
        if not isinstance(values.index, pd.MultiIndex) or values.index.nlevels != 2:
            raise ValueError("Factor Series must have a two-level date/security index")
        names = [str(n).lower() for n in values.index.names]
        date_levels = [i for i, n in enumerate(names) if n in {"date", "datetime", "trade_date"}]
        if len(date_levels) != 1:
            raise ValueError("Name the date level 'date', 'datetime' or 'trade_date'")
        if values.index.has_duplicates:
            raise ValueError("Duplicate date/security factor observations")
        panel = values.unstack(level=1 - date_levels[0])
    elif isinstance(values, pd.DataFrame):
        if {"date", "value"}.issubset(values.columns):
            asset = next((c for c in ("symbol", "asset", "code") if c in values.columns), None)
            if asset is None:
                raise ValueError("Long factor table needs a security column")
            if values.duplicated(["date", asset]).any():
                raise ValueError("Duplicate date/security factor observations")
            panel = values.pivot(index="date", columns=asset, values="value")
        else:
            panel = values.copy()
    else:
        raise TypeError("Factor values must be a pandas DataFrame or Series, not summary metrics")
    if isinstance(panel.index, pd.MultiIndex) or pd.api.types.is_numeric_dtype(panel.index):
        raise ValueError("Wide panel requires a date index, not aggregate metric rows")
    dates = pd.to_datetime(panel.index, errors="coerce")
    if dates.isna().any():
        raise ValueError("Factor panel contains invalid dates")
    panel.index = dates
    if panel.index.has_duplicates or panel.columns.has_duplicates:
        raise ValueError("Duplicate dates or securities in wide factor panel")
    if panel.empty:
        return panel.astype(float)
    try:
        panel = panel.astype(float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Factor values must be numeric") from exc
    return panel.replace([np.inf, -np.inf], np.nan).sort_index()


def cross_sectional_rank_correlation(candidate_values: pd.DataFrame | pd.Series,
                                     pool_values: pd.DataFrame | pd.Series,
                                     policy: DiversityPolicy | None = None,
                                     pool_id: str = "pool") -> PairCorrelation:
    """Spearman per date on shared securities; aggregate absolute correlation.

    Absolute values are aggregated before averaging so alternating signs cannot
    hide redundancy. Sparse/constant dates are excluded and cannot produce a pass.
    """
    policy = policy or DiversityPolicy()
    left, right = as_factor_panel(candidate_values), as_factor_panel(pool_values)
    dates = left.index.union(right.index)
    records: list[dict[str, Any]] = []
    valid: list[dict[str, Any]] = []
    for date in dates:
        record: dict[str, Any] = {"date": date.isoformat(), "status": "insufficient_overlap"}
        if date not in left.index or date not in right.index:
            record.update(assets=0, coverage=0.0)
            records.append(record)
            continue
        x, y = left.loc[date].dropna(), right.loc[date].dropna()
        securities = x.index.intersection(y.index)
        coverage = len(securities) / max(len(x), len(y), 1)
        record.update(assets=len(securities), coverage=float(coverage))
        if len(securities) < policy.min_assets or coverage < policy.min_daily_coverage:
            records.append(record)
            continue
        x, y = x.loc[securities], y.loc[securities]
        if x.nunique() < 2 or y.nunique() < 2:
            record["status"] = "constant_cross_section"
        else:
            # Ranking and Pearson need only numpy/pandas, not optional scipy.
            correlation = float(x.rank(method="average").corr(y.rank(method="average")))
            if np.isfinite(correlation):
                record.update(status="valid", correlation=correlation)
                valid.append(record)
            else:
                record["status"] = "nonfinite_correlation"
        records.append(record)
    enough = (len(valid) >= policy.min_days
              and len(valid) / max(len(dates), 1) >= policy.min_valid_day_fraction)
    if not enough:
        return PairCorrelation(pool_id, "pending", len(valid), len(dates), daily=records,
                               reason="Insufficient overlapping nonconstant daily cross sections")
    correlations = np.asarray([r["correlation"] for r in valid])
    return PairCorrelation(
        pool_id, "complete", len(valid), len(dates),
        mean_abs_correlation=float(np.abs(correlations).mean()),
        signed_mean_correlation=float(correlations.mean()),
        median_abs_correlation=float(np.median(np.abs(correlations))),
        minimum_daily_assets=min(r["assets"] for r in valid),
        median_coverage=float(np.median([r["coverage"] for r in valid])), daily=records)


def _get(candidate: Any, name: str, default: Any = None) -> Any:
    return candidate.get(name, default) if isinstance(candidate, Mapping) else getattr(candidate, name, default)


def _definition(candidate: Any) -> str:
    return _get(candidate, "formula", "") or _get(candidate, "code", "")


def assess_candidate(candidate: Any, values: pd.DataFrame | pd.Series | None,
                     pool_values: Mapping[str, pd.DataFrame | pd.Series],
                     policy: DiversityPolicy | None = None,
                     pool_candidates: Iterable[Any] = ()) -> DiversityAssessment:
    policy = policy or DiversityPolicy()
    signature = canonical_signature(_definition(candidate))
    known_ids = set(pool_values)
    for prior in pool_candidates:
        prior_id = str(_get(prior, "candidate_id", _get(prior, "id", "unknown")))
        known_ids.add(prior_id)
        if canonical_signature(_definition(prior)) == signature:
            return DiversityAssessment("reject", [f"Canonical duplicate of {prior_id}"])
    if values is None:
        return DiversityAssessment("pending", ["Actual date/security factor values are required"])
    try:
        panel = as_factor_panel(values)
    except (TypeError, ValueError) as exc:
        return DiversityAssessment("pending", [str(exc)])
    finite_counts = panel.notna().sum(axis=1)
    nonconstant = panel.nunique(axis=1, dropna=True) >= 2
    usable = (finite_counts >= policy.min_assets) & nonconstant
    if usable.sum() < policy.min_days or usable.sum() / max(len(panel), 1) < policy.min_valid_day_fraction:
        return DiversityAssessment("pending", ["Candidate values lack minimum coverage or nonconstant cross sections"])
    comparisons: list[PairCorrelation] = []
    for pool_id, prior_values in pool_values.items():
        try:
            comparisons.append(cross_sectional_rank_correlation(panel, prior_values, policy, str(pool_id)))
        except (TypeError, ValueError) as exc:
            comparisons.append(PairCorrelation(str(pool_id), "pending", 0, 0, reason=str(exc)))
    correlations = [p.mean_abs_correlation for p in comparisons if p.status == "complete"]
    maximum = max(correlations) if correlations else None
    redundant = [p.pool_id for p in comparisons if p.mean_abs_correlation is not None
                 and p.mean_abs_correlation >= policy.max_abs_correlation]
    if redundant:
        return DiversityAssessment("reject", ["Redundant factor-value ranks with " + ", ".join(redundant)],
                                   comparisons, maximum)
    missing = known_ids - {str(k) for k in pool_values}
    incomplete = [p.pool_id for p in comparisons if p.status != "complete"]
    if missing or incomplete:
        return DiversityAssessment("pending", ["Pool comparison lacks sufficient actual values: "
                                                + ", ".join(sorted(missing) + incomplete)], comparisons, maximum)
    return DiversityAssessment("accept", ["Structural and observed factor-value diversity checks passed"],
                               comparisons, maximum)


def select_diverse(candidates: Iterable[Any], limit: int,
                   prior_candidates: Iterable[Any] = ()) -> list[Any]:
    """Greedy mechanism/field/operator coverage with exact and parameter-grid caps.

    This allocates exploratory opportunities; empirical pool correlation and
    economic acceptance must still be checked separately.
    """
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    prior = list(prior_candidates)
    signatures = {canonical_signature(_definition(c)) for c in prior}
    families = {canonical_signature(_definition(c), True) for c in prior}
    mechanisms = Counter(str(_get(c, "mechanism", "unknown")) for c in prior)
    fields = set().union(*(set(_get(c, "fields", ())) for c in prior)) if prior else set()
    operators = set().union(*(set(_get(c, "operators", ())) for c in prior)) if prior else set()
    remaining = list(candidates)
    chosen: list[Any] = []
    while remaining and len(chosen) < limit:
        scored: list[tuple[float, str, Any]] = []
        for item in remaining:
            expression = _definition(item)
            sig, family = canonical_signature(expression), canonical_signature(expression, True)
            if sig in signatures or family in families:
                continue
            mechanism = str(_get(item, "mechanism", "unknown"))
            item_fields = set(_get(item, "fields", ()))
            item_ops = set(_get(item, "operators", ()))
            score = (4.0 / (1 + mechanisms[mechanism])
                     + len(item_fields - fields) / max(len(item_fields), 1)
                     + len(item_ops - operators) / max(len(item_ops), 1))
            scored.append((score, sig, item))
        if not scored:
            break
        _, _, winner = max(scored, key=lambda item: (item[0], item[1]))
        chosen.append(winner)
        remaining = [c for c in remaining if c is not winner]
        signatures.add(canonical_signature(_definition(winner)))
        families.add(canonical_signature(_definition(winner), True))
        mechanisms[str(_get(winner, "mechanism", "unknown"))] += 1
        fields.update(_get(winner, "fields", ()))
        operators.update(_get(winner, "operators", ()))
    return chosen
