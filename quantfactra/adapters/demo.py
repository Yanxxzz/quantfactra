"""Offline adapter example. It exercises contracts, not investment performance.

Replace this class's methods with a provider SDK when writing a real adapter.
All demo state is persistent and isolated under the explicitly chosen folder.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from panda_alpha.backends import configured_identity
from panda_alpha.platform import fingerprint


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class DemoAdapter:
    simulation = True

    def __init__(self, config):
        self.root = Path(config["state_dir"]).resolve()
        self.identity = configured_identity(config)

    def balance(self):
        return {"schema": "quantfactra-balance-v1", "available": None,
                "unit": "credits", "billing_mode": "free"}

    def preflight(self, candidate, window, cycle, groups):
        if candidate.get("formula") != "RANK(close)" or candidate.get("code"):
            raise ValueError("The offline example supports only its explicitly declared demo formula")
        key = fingerprint(candidate, window, cycle, groups)
        return _write(self.root / (key + ".preflight.json"), {
            "schema": "quantfactra-preflight-v1", "fingerprint": key, "passed": True,
            "fields": ["close"], "synthetic": True,
            "scope": "Check the declared demo interface only; no market data or economic labels"})

    def create(self, candidate, window, cycle, groups, name):
        key = fingerprint(candidate, window, cycle, groups)
        factor_id = "demo-factor-" + key[:16]
        _write(self.root / (factor_id + ".json"), {"candidate": candidate, "window": window,
            "cycle": cycle, "groups": groups, "fingerprint": key, "name": name})
        return factor_id

    def start(self, factor_id):
        definition = json.loads((self.root / (factor_id + ".json")).read_text(encoding="utf-8"))
        run_id = factor_id.replace("factor", "run", 1)
        _write(self.root / (run_id + ".json"), dict(definition, run_id=run_id, status="succeeded"))
        return run_id

    def status(self, run_id):
        return json.loads((self.root / (run_id + ".json")).read_text(encoding="utf-8"))["status"]

    def result(self, run_id):
        run = json.loads((self.root / (run_id + ".json")).read_text(encoding="utf-8"))
        return {"schema": "quantfactra-report-v1", "run_id": run_id, "status": run["status"],
                "fingerprint": run["fingerprint"], "backend_identity": self.identity,
                "window": run["window"], "cycle": run["cycle"], "groups": run["groups"],
                "synthetic": True, "source": {"provider": "local demo", "reference": "isolated synthetic rehearsal"},
                "series": {}, "metrics": {}, "raw_artifacts": [],
                "scope": "Adapter lifecycle only; no returns, source certification or pool admission"}

    def billing(self, run_id):
        return _write(self.root / (run_id + ".bill.json"), {"schema": "quantfactra-billing-v1",
            "run_id": run_id, "status": self.status(run_id), "backend_identity": self.identity,
            "unit": "credits", "actual": 0, "confirmed": True, "synthetic": True})

    def logs(self, run_id):
        return {"run_id": run_id, "complete": True, "errors": [], "synthetic": True,
                "logs": [{"message": "Offline adapter rehearsal completed"}]}


def build_client(platform_config):
    return DemoAdapter(platform_config)
