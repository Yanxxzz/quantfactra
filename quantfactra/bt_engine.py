"""A narrow real-bt execution bridge; no provider, market-data or model requests."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform

import numpy as np
import pandas as pd


def sdk_version():
    try:
        import bt
    except ImportError as error:
        raise RuntimeError('Install bt support from the checkout: python -m pip install -e ".[bt]"') from error
    return bt.__version__


def read_prices(path, digest):
    path = Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Price input SHA256 differs from the exact strategy definition")
    prices = pd.read_csv(path, index_col="date", parse_dates=["date"], float_precision="round_trip")
    if not isinstance(prices.index, pd.DatetimeIndex) or prices.index.tz is not None:
        raise ValueError("Price dates must be timezone-naive ISO observation dates")
    if (prices.empty or prices.shape[1] < 2 or prices.index.has_duplicates
            or not prices.index.is_monotonic_increasing or prices.index.hasnans):
        raise ValueError("Prices require unique increasing dates and at least two assets")
    if any(not isinstance(c, str) or not c.strip() for c in prices.columns) or prices.columns.has_duplicates:
        raise ValueError("Prices require unique named asset columns")
    prices = prices.astype(float).sort_index(axis=1)
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("This bt adapter requires positive finite prices; disclose source cleaning before use")
    return prices


def parse_strategy(candidate):
    if candidate.get("formula"):
        raise ValueError("The bt adapter uses bt-strategy-v2 JSON in candidate.code, not a PandaAI formula")
    try:
        spec = json.loads(candidate["code"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("candidate.code must contain a bt-strategy-v2 JSON object") from error
    if not isinstance(spec, dict) or spec.get("schema") != "bt-strategy-v2":
        raise ValueError("Unsupported bt strategy schema")
    if spec.get("kind") != "momentum_groups" or type(spec.get("lookback")) is not int or spec["lookback"] < 1:
        raise ValueError("The initial bt adapter supports momentum_groups with a positive lookback")
    digest = spec.get("data_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("The exact strategy must bind the price data SHA256")
    costs = spec.get("cost_bps")
    if (not isinstance(costs, list) or not costs
            or any(type(c) not in (int, float) or not math.isfinite(c) or not 0 <= c < 5000 for c in costs)
            or len(set(costs)) != len(costs)
            or spec.get("primary_cost_bps") not in costs):
        raise ValueError("Cost scenarios require distinct finite bps in [0,5000) and the primary cost")
    if spec.get("fee_reserve") != "twice_one_way_rate":
        raise ValueError("The self-funded v2 strategy requires the explicit twice_one_way_rate fee reserve")
    if type(spec.get("initial_capital")) not in (int, float) or not math.isfinite(spec["initial_capital"]) or spec["initial_capital"] <= 0:
        raise ValueError("Initial capital must be positive and finite")
    if type(spec.get("annualization")) is not int or spec["annualization"] < 1:
        raise ValueError("Annualization must be a positive declared observation count")
    if not isinstance(spec.get("input_window"), dict):
        raise ValueError("The strategy must declare its complete input/warmup window")
    if (spec.get("lag") != 1 or spec.get("cash_interest") != 0 or spec.get("currency_interest") != 0
            or spec.get("leverage") is not False or spec.get("integer_positions") is not False):
        raise ValueError("This adapter supports lag1, fractional long-only positions, and no interest/carry")
    return spec


def targets(prices, spec, window, cycle, groups, direction):
    if type(cycle) is not int or cycle < 1 or type(groups) is not int or not 2 <= groups <= prices.shape[1]:
        raise ValueError("Cycle must be positive; groups must be between two and the asset count")
    if spec["input_window"] != {"start": prices.index[0].date().isoformat(), "end": prices.index[-1].date().isoformat()}:
        raise ValueError("Observed prices differ from the declared complete input window")
    evaluation = prices.loc[window["start"]:window["end"]]
    if len(evaluation) < 2:
        raise ValueError("The evaluation window requires at least two received observations")
    # Scores at an execution observation use only earlier received observations.
    scores = prices.pct_change(spec["lookback"], fill_method=None).shift(1)
    if direction == 0:
        scores = -scores
    dates = evaluation.index[::cycle]
    if not np.isfinite(scores.loc[dates].to_numpy()).all():
        raise ValueError("Insufficient earlier input observations for the fixed lookback and one-observation lag")
    weights = {f"group-{g}": pd.DataFrame(0., index=dates, columns=prices.columns)
               for g in range(1, groups + 1)}
    weights["benchmark"] = pd.DataFrame(1. / prices.shape[1], index=dates, columns=prices.columns)
    for date in dates:
        ordered = scores.loc[date].sort_values(kind="stable").index.to_numpy()
        for group, members in enumerate(np.array_split(ordered, groups), 1):
            weights[f"group-{group}"].loc[date, members] = 1. / len(members)
    return evaluation, scores.loc[dates], weights


def metrics(nav, turnover, annualization):
    returns = nav.pct_change(fill_method=None)
    returns.iloc[0] = nav.iloc[0] - 1.
    deviation = float(returns.std(ddof=1))
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1. / 365.25)
    levels = np.concatenate([[1.], nav.to_numpy()])
    return {"total_return": float(nav.iloc[-1] - 1),
            "annualized_return": float(nav.iloc[-1] ** (1. / years) - 1),
            "max_drawdown": float(-min((levels / np.maximum.accumulate(levels) - 1).min(), 0.)),
            "sharpe": float(returns.mean() / deviation * math.sqrt(annualization)) if deviation > 0 else None,
            "turnover": float(turnover.mean()),
            "turnover_unit": "mean per received evaluation date of gross traded notional / pre-trade equity"}


def run_engine(prices, candidate, spec, window, cycle, groups, *, prepared=None):
    import bt
    if sdk_version() != spec["bt_version"]:
        raise ValueError("Installed bt version differs from the frozen strategy definition")
    evaluation, scores, weights = prepared or targets(prices, spec, window, cycle, groups, candidate["direction"])
    paths = []
    for cost in spec["cost_bps"]:
        for name, target in weights.items():
            identifier = f"{name}-cost-{cost:g}"
            # Worst-case sell+buy notional is at most twice pre-trade equity.
            # Keep that declared commission reserve in cash; never finance fees.
            fee_reserve = 2 * cost / 10000.
            effective_target = target * (1. - fee_reserve)
            backtest = bt.Backtest(bt.Strategy(identifier, [bt.algos.WeighTarget(effective_target), bt.algos.Rebalance()]),
                evaluation, initial_capital=spec["initial_capital"],
                commissions=lambda quantity, price, rate=cost / 10000.: abs(quantity * price) * rate,
                integer_positions=False, progress_bar=False)
            backtest.run()
            data = backtest.strategy.data.loc[evaluation.index]
            positions = backtest.positions.reindex(index=evaluation.index, columns=evaluation.columns, fill_value=0.)
            changes = positions.diff()
            changes.iloc[0] = positions.iloc[0]
            gross = (changes.abs() * evaluation).sum(axis=1)
            implied_fees = gross * cost / 10000.
            implied_cash = spec["initial_capital"] - ((changes * evaluation).sum(axis=1) + implied_fees).cumsum()
            implied_value = data["cash"] + (positions * evaluation).sum(axis=1)
            errors = {"cash": float((implied_cash - data["cash"]).abs().max()),
                      "fees": float((implied_fees - data["fees"]).abs().max()),
                      "equity": float((implied_value - data["value"]).abs().max())}
            tolerance = max(1e-7, spec["initial_capital"] * 1e-10)
            if max(errors.values()) > tolerance:
                raise ValueError(f"Independent cash/position/fee reconstruction failed: {errors}")
            if float(data["cash"].min()) < -tolerance or float(positions.min().min()) < -tolerance:
                raise ValueError("The declared long-only, self-funded strategy produced borrowing or short positions")
            previous_positions = positions.shift(1).fillna(0.)
            previous_cash = data["cash"].shift(1).fillna(spec["initial_capital"])
            pretrade = previous_cash + (previous_positions * evaluation).sum(axis=1)
            turnover = gross / pretrade
            nav = data["value"] / spec["initial_capital"]
            trades = []
            for date, row in changes.iterrows():
                for asset, quantity in row.items():
                    if quantity != 0:
                        price = float(evaluation.loc[date, asset])
                        trades.append({"date": date.date().isoformat(), "asset": asset,
                                       "quantity": float(quantity), "price": price,
                                       "fee": abs(float(quantity) * price) * cost / 10000.})
            paths.append({"name": name, "cost_bps": cost, "fee_reserve_fraction": fee_reserve, "nav": nav.tolist(),
                          "cash": data["cash"].tolist(), "fees": data["fees"].tolist(),
                          "positions": {asset: positions[asset].tolist() for asset in evaluation.columns},
                          "trades": trades, "turnover": turnover.tolist(), "total_fees": float(data["fees"].sum()),
                          "audit": {"passed": True, "max_errors": errors, "absolute_tolerance": tolerance,
                                    "minimum_cash": float(data["cash"].min()), "minimum_position": float(positions.min().min()),
                                    "self_funded": True},
                          "metrics": metrics(nav, turnover, spec["annualization"])})
    forward = evaluation.shift(-cycle) / evaluation - 1.
    correlations, omitted = [], []
    for date, score in scores.iterrows():
        target = forward.loc[date]
        pearson, ranked = score.corr(target), score.rank(method="average").corr(target.rank(method="average"))
        if not np.isfinite(target.to_numpy()).all() or not np.isfinite([pearson, ranked]).all():
            omitted.append(date.date().isoformat())
        else:
            correlations.append({"date": date.date().isoformat(), "ic": float(pearson), "rank_ic": float(ranked)})
    implementation = b"".join(path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
                              for path in (Path(__file__), Path(__file__).parent / "adapters" / "bt.py"))
    return {"schema": "quantfactra-bt-raw-v1", "engine": {"name": "bt", "version": bt.__version__,
                "ffn": version("ffn"), "numpy": np.__version__, "pandas": pd.__version__,
                "python": platform.python_version(), "adapter_lf_sha256": hashlib.sha256(implementation).hexdigest(),
                "backtest_count": len(paths)}, "settings": spec,
            "dates": [d.date().isoformat() for d in evaluation.index],
            "assets": list(evaluation.columns), "paths": paths,
            "targets": {name: {"dates": [d.date().isoformat() for d in frame.index],
                                "values": frame.to_numpy().tolist()} for name, frame in weights.items()},
            "correlations": correlations, "undefined_correlation_dates": omitted}


def normalize(raw, candidate, definition, identity, run_id, raw_binding):
    spec, dates = raw["settings"], raw["dates"]
    selected = {p["name"]: p for p in raw["paths"] if p["cost_bps"] == spec["primary_cost_bps"]}
    benchmark = selected["benchmark"]["metrics"]
    table, series = [], []
    for group in range(1, definition["groups"] + 1):
        path = selected[f"group-{group}"]
        table.append({"group": group, **path["metrics"],
                      "excess_annualized_return": path["metrics"]["annualized_return"] - benchmark["annualized_return"]})
        series.append({"group": group, "values": path["nav"]})
    return {"schema": "quantfactra-report-v1", "status": "succeeded", "run_id": run_id,
            "fingerprint": definition["fingerprint"], "backend_identity": identity,
            "window": definition["window"], "cycle": definition["cycle"], "groups": definition["groups"],
            "synthetic": False, "source": {"provider": "bt local engine", "reference": "hash-bound local price input",
                "data_sha256": spec["data_sha256"], "engine": raw["engine"],
                "input_scope": "Caller-supplied prices; source clocks, executable prices and PIT need separate verification"},
            "raw_artifacts": [raw_binding], "code_sha256": hashlib.sha256(candidate["code"].encode()).hexdigest(),
            "series": {"dates": dates, "groups": series,
                       "ic_dates": [r["date"] for r in raw["correlations"]],
                       "rank_ic_dates": [r["date"] for r in raw["correlations"]],
                       "ic": [r["ic"] for r in raw["correlations"]],
                       "rank_ic": [r["rank_ic"] for r in raw["correlations"]]},
            "metrics": {"groups": table, "benchmark": benchmark, "cost_bps": spec["primary_cost_bps"],
                        "units": "returns are ratios; fees and capital use the caller's price numeraire"}}
