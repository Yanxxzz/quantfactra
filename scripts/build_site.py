"""Build only explicitly public assets for GitHub Pages or an offline demo ZIP.

Standard library only. No research execution, provider calls or runtime reads.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from urllib.parse import unquote, urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/Yanxxzz/quantfactra"
SITE = "https://yanxxzz.github.io/quantfactra/"
# Never glob the checkout or copy a local case-output directory into a site.
PUBLIC_FILES = {
    "website/assets/site.css": "assets/site.css",
    "website/assets/site.js": "assets/site.js",
    "website/assets/favicon.svg": "assets/favicon.svg",
    "website/assets/social-card.png": "assets/social-card.png",
    "website/assets/social-card.svg": "assets/social-card.svg",
    "docs/assets/quantfactra-architecture.svg": "assets/quantfactra-architecture.svg",
    "docs/assets/fx-case-nav.svg": "assets/fx-case-nav.svg",
    "docs/assets/french-momentum.svg": "assets/french-momentum.svg",
    "docs/published-momentum-result.json": "momentum/data/result.json",
    "quantfactra/cases/french_momentum/protocol.json": "momentum/data/protocol.json",
    "quantfactra/cases/french_momentum/reference.json": "momentum/data/reference.json",
    "quantfactra/cases/french_momentum/DATA_NOTICE.md": "momentum/data/DATA_NOTICE.md",
    "docs/diagrams/quantfactra-architecture.html": "architecture/index.html",
    "docs/diagrams/ARCHIFY_LICENSE.txt": "architecture/ARCHIFY_LICENSE.txt",
    "docs/public-case-result.json": "case/data/result-v2.json",
    "docs/public-case-result-v1.json": "case/data/result-v1.json",
    "quantfactra/cases/fx_momentum/protocol-v2.json": "case/data/protocol-v2.json",
    "quantfactra/cases/fx_momentum/protocol.json": "case/data/protocol-v1.json",
    "quantfactra/cases/fx_momentum/execution-revision.json": "case/data/execution-revision.json",
    "quantfactra/cases/fx_momentum/source.json": "case/data/source.json",
    "quantfactra/cases/fx_momentum/rates.json": "case/data/rates.json",
    "quantfactra/cases/fx_momentum/DATA_NOTICE.md": "case/data/DATA_NOTICE.md",
    "LICENSE": "LICENSE.txt",
}
PAGES = {"website/index.html": "index.html", "website/case.html": "case/index.html",
         "website/momentum.html": "momentum/index.html",
         "website/404.html": "404.html"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_reference(root=ROOT):
    root = Path(root)
    resources = root / "quantfactra/cases/fx_momentum"
    source = read_json(resources / "source.json")
    revision = read_json(resources / "execution-revision.json")
    if (digest(resources / "rates.json") != source["rates_sha256"]
            or digest(resources / "protocol.json") != source["research_protocol_sha256"]
            or digest(resources / "protocol-v2.json") != revision["current_protocol_sha256"]):
        raise ValueError("Published data/protocol bytes differ from their frozen receipts")
    result = read_json(root / "docs/public-case-result.json")
    if result["case_id"] != "ecb-fx-momentum-v2" or not result["report_delivery_verified"] or result["pool_admission"]:
        raise ValueError("Expected the complete, non-admitted public V2 record")
    implementation = b"".join((root / path).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
                              for path in ("quantfactra/bt_engine.py", "quantfactra/adapters/bt.py"))
    if hashlib.sha256(implementation).hexdigest() != result["engine"]["adapter_lf_sha256"]:
        raise ValueError("Public result is not bound to the current bt adapter implementation")
    if {(p["name"], p["cost_bps"]) for p in result["paths"]} != {
            (name, cost) for name in ("group-1", "group-2", "benchmark") for cost in (0, 10)}:
        raise ValueError("The public comparison must retain all six paths")
    if not all(p["audit"]["passed"] and p["audit"]["self_funded"]
               and p["audit"]["minimum_cash"] >= -p["audit"]["absolute_tolerance"] for p in result["paths"]):
        raise ValueError("A public cost path has not passed its cash/funding checks")
    return result


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.targets, self.ids = [], set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if attrs.get(key):
                self.targets.append(attrs[key])


def validate_site(output):
    """Check authored pages locally, including project-path links and fragments."""
    output = Path(output).resolve()
    parsed = {name: Links((output / name).read_text(encoding="utf-8")) for name in PAGES.values()}
    for name, page in parsed.items():
        text = (output / name).read_text(encoding="utf-8")
        if re.search(r"@@[A-Z_]+@@", text):
            raise ValueError(f"Unresolved page template in {name}")
        for link in page.targets:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (output / name).parent / unquote(url.path) if url.path else output / name
            target = target.resolve()
            if not target.is_relative_to(output):
                raise ValueError(f"Link escapes the public site: {link}")
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                raise ValueError(f"Broken site link in {name}: {link}")
            relative = target.relative_to(output).as_posix()
            if url.fragment and relative in parsed and unquote(url.fragment) not in parsed[relative].ids:
                raise ValueError(f"Broken site anchor in {name}: {link}")
    return True


def build(output, *, revision="master", root=ROOT):
    root, output = Path(root), Path(output).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}|master", revision):
        raise ValueError("revision must be master or an exact Git commit SHA")
    result = public_reference(root)
    momentum = read_json(root / "docs/published-momentum-result.json")
    reference = read_json(root / "quantfactra/cases/french_momentum/reference.json")
    implementation = (root / "quantfactra/published_factor.py").read_text(encoding="utf-8").replace("\r\n", "\n").encode()
    if (digest(root / "quantfactra/cases/french_momentum/protocol.json") != reference["protocol_sha256"]
            or momentum["protocol_sha256"] != reference["protocol_sha256"]
            or momentum["source"]["selected_returns_sha256"] != reference["selected_returns_sha256"]
            or hashlib.sha256(implementation).hexdigest() != momentum["implementation_lf_sha256"]
            or digest(root / "docs/assets/french-momentum.svg") != momentum["figure_sha256"]):
        raise ValueError("Published momentum result differs from its source/protocol/code/figure bindings")
    if (momentum["stock_level_replication"] or momentum["clean_oos"] or momentum["pool_admission"]
            or not momentum["aggregate_audit"]["passed"]
            or [p["label"] for p in momentum["periods"]] != ["full", "post_publication"]
            or any([p["monthly_drag_bps"] for p in period["paths"]] != [0, 10, 25] for period in momentum["periods"])):
        raise ValueError("Published momentum must retain its scope and all six comparisons")
    version = re.search(r'__version__ = "([^"]+)"', (root / "panda_alpha/__init__.py").read_text(encoding="utf-8")).group(1)
    for source in PUBLIC_FILES:
        if not (root / source).is_file():
            raise FileNotFoundError(source)
    labels = {"group-1": "低动量组 · group-1", "group-2": "高动量组 · group-2", "benchmark": "等权基准"}
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in (
        labels[p["name"]], f'{p["cost_bps"]:g} bps', f'{p["metrics"]["total_return"]:.2%}',
        f'{p["metrics"]["max_drawdown"]:.2%}', f'{p["total_fees"]:,.2f}')) + "</tr>" for p in result["paths"])
    momentum_rows = "".join("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in (
        f'{period["start"]} — {period["end"]}', "原始毛收益" if p["monthly_drag_bps"] == 0 else f'{p["monthly_drag_bps"]} bps / 月',
        f'{p["metrics"]["geometric_annual_return"]:.2%}', f'{p["metrics"]["max_drawdown"]:.2%}',
        f'{p["metrics"]["mean_to_volatility_zero_benchmark"]:.2f}',
        f'{p["metrics"]["worst_year"]["year"]} / {p["metrics"]["worst_year"]["return"]:.2%}')) + "</tr>"
        for period in momentum["periods"] for p in period["paths"])
    mom_main = momentum["periods"][0]["paths"][0]["metrics"]
    values = {"REPO": REPO, "SITE": SITE, "VERSION": version, "REVISION": revision,
              "SHORT_REVISION": revision[:8], "CASE_ROWS": rows,
              "EXCESS_PP": f'{result["primary_excess_total_return"] * 100:.2f}',
              "EVAL_DATES": str(result["evaluation_dates"]), "REBALANCE_DATES": str(result["rebalance_dates"]),
              "MOM_ROWS": momentum_rows, "MOM_CAGR": f'{mom_main["geometric_annual_return"]:.2%}',
              "MOM_DRAWDOWN": f'{mom_main["max_drawdown"]:.2%}', "MOM_MONTHS": str(mom_main["months"])}
    output.mkdir(parents=True, exist_ok=False)
    for source, target in PUBLIC_FILES.items():
        path = output / target
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, path)
    for source, target in PAGES.items():
        text = (root / source).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("@@" + key + "@@", value)
        (output / target).write_text(text, encoding="utf-8", newline="\n")
    (output / ".nojekyll").write_bytes(b"")
    (output / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}sitemap.xml\n", encoding="utf-8", newline="\n")
    (output / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{SITE}{page}</loc></url>" for page in ("", "case/", "momentum/", "architecture/")) + "</urlset>\n", encoding="utf-8", newline="\n")
    (output / "OFFLINE-README.txt").write_text(
        "QuantFactra v" + version + "\nOpen index.html for an offline preview and case/index.html for all six recorded paths.\n"
        "This is a precomputed public V2 record, not a live backtest.\n"
        "Reproduce after installing QuantFactra with bt: python -m quantfactra case --output research_runs/fx_case\n"
        "Full local outputs include daily positions, cash, trades, fees and frozen targets.\n"
        "Reference-rate fills are hypothetical; no carry/interest or historical-vintage certification.\n"
        "Source: European Central Bank. Free source and reuse terms: case/data/DATA_NOTICE.md\n"
        "Second case: momentum/index.html. Published Kenneth French portfolio-return replay, not stock-level backtesting.\n"
        "Original monthly French data is NOT bundled; download from the publisher. See momentum/data/DATA_NOTICE.md.\n"
        "Software: MIT; upstream QUANTAXIS attribution retained in LICENSE.txt.\n"
        "Architecture renderer: architecture/ARCHIFY_LICENSE.txt.\n" + REPO + "\n", encoding="utf-8", newline="\n")
    validate_site(output)
    files = {p.relative_to(output).as_posix(): digest(p)
             for p in sorted(output.rglob("*"), key=lambda item: item.relative_to(output).as_posix())
             if p.is_file()}
    manifest = {"schema": "quantfactra-public-site-v1", "version": version, "revision": revision,
                "case_id": result["case_id"], "case_run_id": result["run_id"], "case_ids": [result["case_id"], momentum["case_id"]], "files": files,
                "scope": "Explicit public assets only; no local runtime, credentials or private research"}
    (output / "site-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def archive_site(output, destination):
    output, destination = Path(output), Path(destination)
    manifest = read_json(output / "site-manifest.json")
    names = sorted([*manifest["files"], "site-manifest.json"])
    for name in manifest["files"]:
        if digest(output / name) != manifest["files"][name]:
            raise ValueError("Public site changed after its manifest was created")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            info = zipfile.ZipInfo(name)  # Stable ZIP timestamps; content retains original evidence bytes.
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (output / name).read_bytes())
    return {"name": destination.name, "sha256": digest(destination), "files": len(names)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new output directory")
    parser.add_argument("--revision", default="master")
    parser.add_argument("--zip", type=Path, help="Create a portable offline demo archive")
    args = parser.parse_args(argv)
    manifest = build(args.output, revision=args.revision)
    answer = {"version": manifest["version"], "files": len(manifest["files"]), "links": "passed"}
    if args.zip:
        answer["archive"] = archive_site(args.output, args.zip)
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    main()
