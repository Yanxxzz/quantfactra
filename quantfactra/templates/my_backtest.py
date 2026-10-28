"""Editable eight-method adapter. All current behavior is local and synthetic.

For a real platform replace the methods with its SDK, retain durable IDs and
original reports, and validate its actual charges. Do not relabel this fixture
as a real backtest; the tutorial runner only accepts simulation adapters.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantfactra import configuration_identity
from quantfactra.execution import fingerprint


def write_receipt(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class MyBacktest:
    simulation = True

    def __init__(self, config):
        self.root = Path(config["state_dir"]).resolve()
        self.identity = configuration_identity(config)

    def balance(self):
        return {"schema": "quantfactra-balance-v1", "available": None,
                "unit": self.identity["cost_unit"], "billing_mode": "free"}

    def preflight(self, candidate, window, cycle, groups):
        if candidate.get("formula") != "RANK(close)" or candidate.get("code"):
            raise ValueError("This fixture supports only RANK(close), with no deployed Python code")
        key = fingerprint(candidate, window, cycle, groups)
        return write_receipt(self.root / (key + ".preflight.json"), {
            "schema": "quantfactra-preflight-v1", "fingerprint": key, "passed": True,
            "fields": ["close"], "synthetic": True})

    def create(self, candidate, window, cycle, groups, name):
        key = fingerprint(candidate, window, cycle, groups)
        factor_id = "local-factor-" + key[:16]
        write_receipt(self.root / (factor_id + ".json"), {"candidate": candidate, "window": window,
            "cycle": cycle, "groups": groups, "fingerprint": key, "name": name})
        return factor_id

    def start(self, factor_id):
        definition = json.loads((self.root / (factor_id + ".json")).read_text(encoding="utf-8"))
        run_id = factor_id.replace("factor", "run", 1)
        write_receipt(self.root / (run_id + ".json"), dict(definition, run_id=run_id, status="succeeded"))
        return run_id

    def status(self, run_id):
        return json.loads((self.root / (run_id + ".json")).read_text(encoding="utf-8"))["status"]

    def result(self, run_id):
        run = json.loads((self.root / (run_id + ".json")).read_text(encoding="utf-8"))
        return {"schema": "quantfactra-report-v1", "run_id": run_id, "status": run["status"],
                "fingerprint": run["fingerprint"], "backend_identity": self.identity,
                "window": run["window"], "cycle": run["cycle"], "groups": run["groups"],
                "synthetic": True, "source": {"provider": "my local fixture", "reference": "no market data"},
                "series": {}, "metrics": {}, "raw_artifacts": []}

    def billing(self, run_id):
        return write_receipt(self.root / (run_id + ".bill.json"), {
            "schema": "quantfactra-billing-v1", "backend_identity": self.identity,
            "run_id": run_id, "status": self.status(run_id), "confirmed": True,
            "unit": self.identity["cost_unit"], "actual": 0, "synthetic": True})

    def logs(self, run_id):
        return {"run_id": run_id, "complete": True, "errors": [], "synthetic": True,
                "logs": [{"message": "Local adapter rehearsal; no provider request"}]}


def build_client(platform_config):
    # Construction is inert. Put actual SDK calls inside the explicit methods.
    return MyBacktest(platform_config)
