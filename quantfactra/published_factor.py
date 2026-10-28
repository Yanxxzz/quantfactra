"""Reproduce a known published factor at portfolio-return level, without accounts."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import html
from importlib.resources import files
import io
import json
import math
from pathlib import Path
import re
import statistics
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def resources():
    return files("quantfactra").joinpath("cases", "french_momentum")


def load_protocol():
    from panda_alpha.evaluation import check_window
    protocol = json.loads(resources().joinpath("protocol.json").read_text(encoding="utf-8"))
    check_window(protocol["window"]["start"], protocol["window"]["end"], protocol["sealed_windows"])
    return protocol


def parse_archive(content, protocol):
    """Read only the monthly Mom block; never mix in the annual return section."""
    if len(content) > 5_000_000:
        raise ValueError("Unexpected source archive size")
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv")]
            if len(members) != 1 or members[0].file_size > 5_000_000:
                raise ValueError("Expected one small official CSV")
            raw = archive.read(members[0])
    except BadZipFile as error:
        raise ValueError("Source must be the official momentum CSV ZIP") from error
    text = raw.decode("utf-8-sig")
    start, end = protocol["window"]["start"][:7], protocol["window"]["end"][:7]
    rows, active, finished = [], False, False
    for row in csv.reader(io.StringIO(text)):
        cells = [cell.strip() for cell in row]
        if cells == ["", "Mom"]:
            if active or finished:
                raise ValueError("Ambiguous monthly Mom section")
            active = True
            continue
        if not active:
            continue
        if not cells or not any(cells) or not re.fullmatch(r"\d{6}", cells[0]):
            if rows:
                finished, active = True, False
                break
            continue
        date = cells[0][:4] + "-" + cells[0][4:]
        if not start <= date <= end:
            continue  # Later, sealed dates are not parsed as numeric observations.
        if len(cells) != 2:
            raise ValueError("Expected a date and one Mom return")
        try:
            value = Decimal(cells[1])
        except Exception as error:
            raise ValueError("Invalid monthly factor return") from error
        if not value.is_finite() or value in {Decimal("-99.99"), Decimal("-999")}:
            raise ValueError("Missing or nonfinite factor return")
        rows.append((date, value))
    year, month = map(int, start.split("-"))
    expected = []
    while f"{year:04}-{month:02}" <= end:
        expected.append(f"{year:04}-{month:02}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if [date for date, _ in rows] != expected or len(rows) != protocol["expected_months"]:
        raise ValueError("Monthly coverage must be complete, ordered and duplicate-free")
    canonical = "".join(f"{date},{format(value.normalize(), 'f')}\n" for date, value in rows).encode()
    source = {"publisher": "Kenneth R. French Data Library", "source_url": protocol["source_url"],
              "archive_sha256": _sha(content), "csv_sha256": _sha(raw), "selected_returns_sha256": _sha(canonical),
              "header": text.splitlines()[0], "months": len(rows), "first_month": start, "last_month": end,
              "input_unit": "percent per month", "historical_vintage_certified": False}
    return rows, source


def summarize(rows, drag_bps):
    """Compound an illustrative factor index, including initial wealth in drawdown."""
    returns = [float(value) / 100 - drag_bps / 10000 for _, value in rows]
    if any(not math.isfinite(r) or r <= -1 for r in returns):
        raise ValueError("Factor index cannot compound a monthly return at or below -100%")
    nav, peak, drawdowns, years = [1.0], 1.0, [], {}
    for (date, _), r in zip(rows, returns):
        nav.append(nav[-1] * (1 + r))
        peak = max(peak, nav[-1])
        drawdowns.append(nav[-1] / peak - 1)
        years[date[:4]] = years.get(date[:4], 1.0) * (1 + r)
    annual = {year: value - 1 for year, value in years.items()}
    worst_year = min(annual, key=annual.get)
    worst_month = min(range(len(returns)), key=returns.__getitem__)
    mean = statistics.mean(returns) * 12
    volatility = statistics.stdev(returns) * math.sqrt(12) if len(returns) > 1 else 0
    metrics = {"months": len(rows), "geometric_annual_return": math.expm1(math.fsum(math.log1p(r) for r in returns) * 12 / len(rows)),
               "annual_mean": mean, "annual_volatility": volatility,
               "mean_to_volatility_zero_benchmark": mean / volatility if volatility else None,
               "terminal_index": nav[-1], "max_drawdown": min(drawdowns),
               "worst_month": {"date": rows[worst_month][0], "return": returns[worst_month]},
               "worst_year": {"year": worst_year, "return": annual[worst_year]},
               "positive_years": sum(value > 0 for value in annual.values()), "calendar_years": len(annual)}
    # A separate high-precision path checks every monthly multiplication and peak.
    with localcontext() as context:
        context.prec = 50
        exact, high, max_nav_error, max_dd_error = Decimal(1), Decimal(1), 0.0, 0.0
        for index, (_, value) in enumerate(rows):
            exact *= 1 + value / 100 - Decimal(drag_bps) / 10000
            high = max(high, exact)
            max_nav_error = max(max_nav_error, abs(float(exact) - nav[index + 1]) / max(1, float(exact)))
            max_dd_error = max(max_dd_error, abs(float(exact / high - 1) - drawdowns[index]))
    if max(max_nav_error, max_dd_error) > 1e-11:
        raise ValueError("High-precision monthly arithmetic verification failed")
    audit = {"method": "Independent Decimal(50) multiplication and running peaks for every month; not an external model review",
             "checked_months": len(rows), "maximum_relative_index_error": max_nav_error,
             "maximum_absolute_drawdown_error": max_dd_error, "tolerance": 1e-11, "passed": True}
    return metrics, nav, drawdowns, audit


def analyze(rows, source, protocol):
    periods, local_paths = [], []
    for period in protocol["periods"]:
        selected = [(date, value) for date, value in rows if period["start"] <= date <= period["end"]]
        paths = []
        for drag in protocol["monthly_drag_bps"]:
            metrics, nav, drawdowns, audit = summarize(selected, drag)
            paths.append({"monthly_drag_bps": drag, "metrics": metrics, "arithmetic_audit": audit})
            if period["label"] == "full":
                local_paths.append({"monthly_drag_bps": drag, "dates": [d for d, _ in selected], "index": nav, "drawdown": drawdowns})
        periods.append({**period, "paths": paths})
    return {"schema": "quantfactra-published-factor-result-v1", "case_id": protocol["case_id"], "source": source,
            "scope": protocol["execution_scope"], "periods": periods,
            "historical_positive_evidence": periods[0]["paths"][0]["metrics"]["geometric_annual_return"] > 0,
            "selection": protocol["selection"], "cost_scope": protocol["cost_scope"],
            "official_runs": 0, "stock_level_replication": False, "clean_oos": False, "pool_admission": False}, local_paths


def render_report(summary, protocol):
    table = []
    for period in summary["periods"]:
        for path in period["paths"]:
            m = path["metrics"]
            cells = [f'{period["start"]} — {period["end"]}', f'{path["monthly_drag_bps"]} bps / 月',
                     f'{m["geometric_annual_return"]:.2%}', f'{m["max_drawdown"]:.2%}',
                     f'{m["worst_year"]["year"]}: {m["worst_year"]["return"]:.2%}']
            table.append('<tr>' + ''.join('<td>' + html.escape(c) + '</td>' for c in cells) + '</tr>')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>QuantFactra · 经典动量因子复现</title><style>body{font:16px/1.8 system-ui;background:#f8f9f5;color:#101d19;max-width:980px;margin:40px auto;padding:20px}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ccd8cf;padding:12px;text-align:left}.scroll{overflow:auto}code{overflow-wrap:anywhere}a{color:#16694b}</style>'
            '<h1>经典动量因子的历史复现</h1><p>Kenneth French · 美股 12–2 动量 · 1927–2016</p>'
            '<p>已发表的组合收益级重放；未从个股重建持仓或交易，未调用 PandaAI 或模型。</p><div class="scroll"><table><thead><tr>'
            '<th>区间</th><th>固定月度扣减</th><th>研究指数年化</th><th>最大回撤</th><th>最差年度</th></tr></thead><tbody>'
            + ''.join(table) + '</tbody></table></div><p>' + html.escape(protocol['cost_scope']) + '</p><p>'
            + html.escape(protocol['post_publication_scope']) + '</p><p>' + html.escape(protocol['index'])
            + '</p><p>数据切片 SHA256：<code>' + summary['source']['selected_returns_sha256']
            + '</code></p><p><a href="summary.json">完整指标与核对</a> · <a href="protocol.json">冻结规则</a> · <a href="DATA_NOTICE.md">数据说明</a> · '
            '<a href="https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor.html">官方定义</a></p></html>')


def run_factor_case(output, *, source_path=None, download=False, resume_existing=False):
    root = Path(output).resolve()
    protocol = load_protocol()  # Always check the frozen research window before acquisition.
    if resume_existing:
        if source_path or download:
            raise ValueError("Resume reads the original output; do not provide a new source")
        delivery = json.loads((root / "delivery.json").read_text(encoding="utf-8"))
        required = {"source.zip", "protocol.json", "DATA_NOTICE.md", "summary.json", "monthly-index.json", "report.html"}
        if delivery.get("case_id") != protocol["case_id"] or set(delivery.get("files", {})) != required:
            raise ValueError("Incomplete factor-case delivery manifest")
        for name, digest in delivery["files"].items():
            target = (root / name).resolve()
            if not target.is_relative_to(root) or _sha(target.read_bytes()) != digest:
                raise ValueError("Saved factor case differs from its delivery hashes")
        return dict(json.loads((root / "summary.json").read_text(encoding="utf-8")), reused_existing=True)
    if root.exists():
        raise FileExistsError(root)
    if bool(source_path) == bool(download):
        raise ValueError("Choose --source for the official ZIP or --download from its publisher")
    reference = json.loads(resources().joinpath("reference.json").read_text(encoding="utf-8"))
    protocol_bytes = resources().joinpath("protocol.json").read_bytes()
    if _sha(protocol_bytes) != reference["protocol_sha256"]:
        raise ValueError("Frozen published-factor protocol has changed")
    if download:
        try:
            with urlopen(Request(protocol["source_url"], headers={"User-Agent": "QuantFactra public factor reproduction"}), timeout=45) as response:
                content = response.read(5_000_001)
        except OSError as error:
            raise RuntimeError("Download failed; save the official ZIP manually and use --source") from error
    else:
        content = Path(source_path).read_bytes()
    rows, source = parse_archive(content, protocol)
    if source["selected_returns_sha256"] != reference["selected_returns_sha256"]:
        raise ValueError("Publisher revised the frozen 1927-2016 slice; this is a different data vintage. Original results were preserved; do not silently replace the reference.")
    summary, paths = analyze(rows, source, protocol)
    summary.update(reproduced_at_utc=datetime.now(timezone.utc).isoformat(), reused_existing=False)
    root.mkdir(parents=True)
    (root / "source.zip").write_bytes(content)
    (root / "protocol.json").write_bytes(protocol_bytes)
    (root / "DATA_NOTICE.md").write_bytes(resources().joinpath("DATA_NOTICE.md").read_bytes())
    _json(root / "summary.json", summary)
    _json(root / "monthly-index.json", paths)
    (root / "report.html").write_text(render_report(summary, protocol), encoding="utf-8")
    _json(root / "delivery.json", {"case_id": protocol["case_id"], "files": {name: _sha((root / name).read_bytes()) for name in
          ("source.zip", "protocol.json", "DATA_NOTICE.md", "summary.json", "monthly-index.json", "report.html")}})
    return summary
