"""Real-engine public evidence, source handling and recovery from an installed API."""
from importlib.resources import files
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("bt")

from quantfactra.public_case import run_case


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    root = tmp_path_factory.mktemp("public case 中文") / "case"
    return root, run_case(root)


def test_actual_case_preserves_all_cost_paths_and_source_missingness(completed):
    root, result = completed
    assert result["state"] == "SETTLED" and result["engine_backtests"] == 6
    assert result["ledger_jobs"] == 1 and result["provider_service_fee"]["actual"] == 0
    assert result["pool_admission"] is False and result["report_delivery_verified"] is True
    assert result["evaluation_dates"] == 1278 and result["rebalance_dates"] == 256
    assert result["case_id"] == "ecb-fx-momentum-v2"
    assert result["primary_excess_total_return"] == pytest.approx(-.15843289753661793, abs=1e-8)
    raw = read(root / "engine-result.json")
    assert {(p["name"], p["cost_bps"]) for p in raw["paths"]} == {
        (name, cost) for name in ["group-1", "group-2", "benchmark"] for cost in [0, 10]}
    assert all(p["audit"]["passed"] for p in raw["paths"])
    assert all(p["audit"]["self_funded"] and min(p["cash"]) >= -p["audit"]["absolute_tolerance"] for p in raw["paths"])
    provenance = read(root / "inputs/provenance.json")
    assert provenance["raw_dates"] == 1347 and provenance["valid_dates"] == 1343
    assert len(provenance["excluded_dates"]) == 4
    rates = read(root / "inputs/rates.json")
    assert sum(status == "H" for row in rates["status_rows"] for status in row[1:]) == 16
    source = read(root / "inputs/source.json")
    assert hashlib.sha256((root / "inputs/rates.json").read_bytes()).hexdigest() == source["rates_sha256"]
    assert read(root / "evidence.json")["report_delivery_verified"]
    assert "MOMENTUM(close,20)" in (root / "report.html").read_text(encoding="utf-8")
    # Sparse IC uses its own real rebalance axis, never daily padding with fake zeros.
    normalized = read(next((root / "adapter").glob("*.report.json")))
    assert len(normalized["series"]["ic_dates"]) == len(normalized["series"]["ic"])
    assert 0 < len(normalized["series"]["ic_dates"]) < len(raw["dates"])
    targets = read(next((root / "adapter").glob("*.targets.json")))
    assert targets["effective_weight_scale_by_cost"] == {"0": 1, "10": .998}
    for name, frozen in targets["weights"].items():
        assert frozen["assets"] == raw["assets"]
        assert {key: frozen[key] for key in ("dates", "values")} == raw["targets"][name]


def test_resume_uses_original_run_and_keeps_delivery_bytes(completed, monkeypatch):
    root, original = completed
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
              and p.suffix in {".json", ".html", ".svg"}}
    monkeypatch.setattr("quantfactra.adapters.bt.run_engine", lambda *a, **kw: pytest.fail("Duplicate bt run"))
    reused = run_case(root, resume_existing=True)
    assert reused["run_id"] == original["run_id"] and reused["reused_existing"]
    assert reused["ledger_jobs"] == 1
    assert all((root / path).read_bytes() == data for path, data in before.items())


def test_altered_input_cannot_resume_an_old_receipt(tmp_path, monkeypatch):
    from quantfactra.public_case import _prepare
    root = tmp_path / "tampered"
    _prepare(root)
    path = root / "inputs/prices.csv"
    path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setattr("quantfactra.adapters.bt.run_engine", lambda *a, **kw: pytest.fail("Read altered input"))
    with pytest.raises(ValueError, match="hash|SHA|sha|digest"):
        run_case(root, resume_existing=True)


def test_missing_optional_engine_leaves_no_partial_output(tmp_path, monkeypatch):
    def missing():
        raise RuntimeError("Install bt support")
    monkeypatch.setattr("quantfactra.public_case.sdk_version", missing)
    with pytest.raises(RuntimeError, match="Install bt"):
        run_case(tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def test_cli_is_utf8_even_when_parent_requests_ascii(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
    child = subprocess.run([sys.executable, "-m", "quantfactra", "demo", "--output", str(tmp_path / "中文 路径")],
                           capture_output=True, env=env, check=True)
    assert "中文 路径" in child.stdout.decode("utf-8")


def test_packaged_original_protocol_and_explicit_revision_are_bound():
    resource = files("quantfactra").joinpath("cases", "fx_momentum")
    source = json.loads(resource.joinpath("source.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(resource.joinpath("protocol.json").read_bytes()).hexdigest() == source["research_protocol_sha256"]
    revision = json.loads(resource.joinpath("execution-revision.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(resource.joinpath("protocol-v2.json").read_bytes()).hexdigest() == revision["current_protocol_sha256"]
    assert "European Central Bank" in resource.joinpath("DATA_NOTICE.md").read_text(encoding="utf-8")


def test_public_case_model_context_is_read_only_and_keeps_all_comparisons(completed):
    from quantfactra.public_case import review_packet
    root, _ = completed
    before = (root / "delivery.json").read_bytes()
    packet = review_packet(root)
    assert packet["action"] == "source_analysis" and packet["funding_authorization"] is False
    assert len(packet["context"]["paths"]) == 6
    assert packet == review_packet(root)
    assert (root / "delivery.json").read_bytes() == before
    assert all("positions" not in path and "trades" not in path for path in packet["context"]["paths"])


def test_execution_revision_keeps_original_alpha_and_sample_definition():
    resource = files("quantfactra").joinpath("cases", "fx_momentum")
    old = json.loads(resource.joinpath("protocol.json").read_text(encoding="utf-8"))
    new = json.loads(resource.joinpath("protocol-v2.json").read_text(encoding="utf-8"))
    for key in ("formula", "direction", "cycle", "groups", "currencies", "source_window", "window", "one_way_cost_bps", "primary_cost_bps"):
        assert old[key] == new[key]
    assert new["fee_reserve"] == "twice_one_way_rate"
