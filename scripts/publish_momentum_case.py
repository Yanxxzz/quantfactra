"""Publish derived French factor statistics and a year-end sampled scientific plot.

Run only with an already reproduced local case; never copies source return rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def publish(case_output):
    from quantfactra.published_factor import run_factor_case
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    root = Path(case_output)
    summary = run_factor_case(root, resume_existing=True)
    summary.pop("reused_existing")
    summary["implementation_lf_sha256"] = hashlib.sha256((ROOT / "quantfactra/published_factor.py").read_text(encoding="utf-8").replace("\r\n", "\n").encode()).hexdigest()
    summary["protocol_sha256"] = hashlib.sha256((root / "protocol.json").read_bytes()).hexdigest()
    summary["publication_scope"] = "Derived statistics and year-end sampled plot only; original ZIP and monthly source returns are not redistributed."
    paths = json.loads((root / "monthly-index.json").read_text(encoding="utf-8"))
    # A third calculation checks the aggregate metrics by NumPy from local inputs.
    from quantfactra.published_factor import load_protocol, parse_archive
    rows, _ = parse_archive((root / "source.zip").read_bytes(), load_protocol())
    differences = []
    for period in summary["periods"]:
        selected = np.array([float(v) / 100 for d, v in rows if period["start"] <= d <= period["end"]])
        for path in period["paths"]:
            r = selected - path["monthly_drag_bps"] / 10000
            wealth = np.r_[1.0, np.cumprod(1 + r)]
            oracle = {"geometric_annual_return": wealth[-1] ** (12 / len(r)) - 1,
                      "annual_mean": r.mean() * 12, "annual_volatility": r.std(ddof=1) * np.sqrt(12),
                      "max_drawdown": np.min(wealth / np.maximum.accumulate(wealth) - 1)}
            differences.extend(abs(float(v) - path["metrics"][k]) for k, v in oracle.items())
    if max(differences) > 1e-11:
        raise ValueError("Independent NumPy aggregate verification failed")
    summary["aggregate_audit"] = {"method": "NumPy from original decimal inputs; every period and deduction", "comparisons": len(differences), "maximum_absolute_error": max(differences), "passed": True}
    plt.rcParams.update({"svg.fonttype": "none", "svg.hashsalt": "quantfactra-french-momentum-v1", "font.family": "DejaVu Sans", "font.size": 10})
    fig, axes = plt.subplots(2, 1, figsize=(10.8, 6.6), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    fig.patch.set_facecolor("#f8f9f5")
    for ax in axes:
        ax.set_facecolor("#f8f9f5")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(alpha=.18)
    for path, color in zip(paths, ["#167b62", "#6376a6", "#ae6b50"]):
        years = [1926] + [int(d[:4]) for d in path["dates"] if d.endswith("-12")]
        wealth = [1.0] + [path["index"][i + 1] for i, d in enumerate(path["dates"]) if d.endswith("-12")]
        dd = [0.0] + [100 * path["drawdown"][i] for i, d in enumerate(path["dates"]) if d.endswith("-12")]
        label = "Published gross Mom" if path["monthly_drag_bps"] == 0 else f'{path["monthly_drag_bps"]} bps monthly deduction'
        axes[0].plot(years, wealth, lw=1.8, color=color, label=label)
        axes[1].plot(years, dd, lw=1.4, color=color)
    axes[0].set_yscale("log")
    axes[0].set_ylabel("Illustrative index (log scale)")
    axes[0].legend(frameon=False, loc="upper left", fontsize=9)
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].set_xlabel("Calendar year")
    axes[1].set_xlim(1926, 2016)
    axes[1].set_xticks([1927, 1940, 1960, 1980, 2000, 2016])
    fig.suptitle("Published US momentum (12–2)  |  January 1927 – December 2016", x=.09, ha="left", fontweight="bold")
    fig.text(.09, .02, "Source: Kenneth R. French Data Library (202608 vintage). Year-end samples; statistics use all 1,080 months.\nPortfolio-return replay; deductions are assumptions. 200% gross notional; financing/borrow/turnover not modeled.", fontsize=8, color="#495c54")
    fig.tight_layout(rect=(0, .08, 1, .95))
    fig.savefig(ROOT / "docs/assets/french-momentum.svg", metadata={"Date": None, "Creator": "QuantFactra"})
    plt.close(fig)
    figure = ROOT / "docs/assets/french-momentum.svg"
    # Git and Pages use canonical LF bytes on Windows and Linux.
    figure.write_text(figure.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    summary["figure_sha256"] = hashlib.sha256(figure.read_bytes()).hexdigest()
    (ROOT / "docs/published-momentum-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"positive": summary["historical_positive_evidence"], "audit": summary["aggregate_audit"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-output", type=Path, required=True)
    publish(parser.parse_args().case_output)
