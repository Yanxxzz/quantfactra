"""Durable local execution on the real optional bt engine, not DemoAdapter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import uuid

from quantfactra import configuration_identity
from quantfactra.execution import fingerprint
from quantfactra.bt_engine import normalize, parse_strategy, read_prices, run_engine, sdk_version, targets
from panda_alpha.evaluation import check_window


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class BtAdapter:
    simulation = False

    def __init__(self, config):
        self.root = Path(config["state_dir"]).resolve()
        self.prices_path = Path(config["prices_path"]).resolve()
        self.identity = configuration_identity(config)
        self.sealed = config.get("sealed_windows", [])

    def balance(self):
        return {"schema": "quantfactra-balance-v1", "available": None,
                "unit": self.identity["cost_unit"], "billing_mode": "free"}

    def _inputs(self, candidate, window, cycle, groups):
        spec = parse_strategy(candidate)
        check_window(window["start"], window["end"], self.sealed, spec["input_window"]["start"])
        check_window(spec["input_window"]["start"], spec["input_window"]["end"], self.sealed)
        if sdk_version() != spec.get("bt_version"):
            raise ValueError("Installed bt version differs from the frozen strategy JSON")
        prices = read_prices(self.prices_path, spec["data_sha256"])
        targets(prices, spec, window, cycle, groups, candidate["direction"])
        return spec

    def preflight(self, candidate, window, cycle, groups):
        spec = self._inputs(candidate, window, cycle, groups)
        key = fingerprint(candidate, window, cycle, groups)
        return write(self.root / (key + ".preflight.json"), {
            "schema": "quantfactra-preflight-v1", "fingerprint": key, "passed": True,
            "fields": ["close"], "language": "bt-strategy-v2 JSON", "input_sha256": spec["data_sha256"],
            "engine_version": spec["bt_version"], "input_window": spec["input_window"]})

    def create(self, candidate, window, cycle, groups, name):
        self._inputs(candidate, window, cycle, groups)
        key = fingerprint(candidate, window, cycle, groups)
        factor_id = "bt-factor-" + key
        path = self.root / (factor_id + ".json")
        definition = {"candidate": candidate, "window": window, "cycle": cycle, "groups": groups,
                      "name": name, "fingerprint": key, "backend_identity": self.identity}
        if path.exists():
            previous = read(path)
            if any(previous[field] != definition[field] for field in ("candidate", "window", "cycle", "groups", "backend_identity")):
                raise ValueError("A local object with this fingerprint has a different frozen request")
            return factor_id
        snapshot = self.root / (factor_id + ".prices.csv")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.prices_path, snapshot)
        read_prices(snapshot, parse_strategy(candidate)["data_sha256"])
        write(path, definition)
        return factor_id

    def start(self, factor_id):
        from panda_alpha.backend_contract import reference_id
        reference_id(factor_id)
        definition = read(self.root / (factor_id + ".json"))
        if definition["backend_identity"] != self.identity:
            raise ValueError("The local object belongs to another backend connection")
        run_id = factor_id.replace("bt-factor-", "bt-run-", 1)
        claim = self.root / (run_id + ".claim")
        try:
            with claim.open("x", encoding="utf-8") as handle:
                handle.write(definition["fingerprint"])
        except FileExistsError:
            return run_id  # Original pending/terminal computation, never replay it.
        status_path = self.root / (run_id + ".status.json")
        write(status_path, {"status": "running", "factor_id": factor_id, "fingerprint": definition["fingerprint"]})
        try:
            candidate = definition["candidate"]
            spec = parse_strategy(candidate)
            prices = read_prices(self.root / (factor_id + ".prices.csv"), spec["data_sha256"])
            prepared = targets(prices, spec, definition["window"], definition["cycle"], definition["groups"], candidate["direction"])
            target_binding = write(self.root / (run_id + ".targets.json"), {
                "schema": "quantfactra-bt-targets-v1", "fingerprint": definition["fingerprint"],
                "data_sha256": spec["data_sha256"],
                "weight_basis": "Normalized risky allocation; multiply by each cost's effective scale before trading",
                "effective_weight_scale_by_cost": {str(cost): 1 - 2 * cost / 10000. for cost in spec["cost_bps"]},
                "weights": {name: {"dates": [d.date().isoformat() for d in frame.index],
                                   "assets": list(frame.columns), "values": frame.to_numpy().tolist()}
                            for name, frame in prepared[2].items()}})
            raw = run_engine(prices, candidate, spec, definition["window"], definition["cycle"], definition["groups"], prepared=prepared)
            raw["frozen_targets"] = target_binding
            raw_binding = write(self.root / (run_id + ".raw.json"), raw)
            report = normalize(raw, candidate, definition, self.identity, run_id, raw_binding)
            report["raw_artifacts"].append(target_binding)
            write(self.root / (run_id + ".report.json"), report)
            write(self.root / (run_id + ".logs.json"), {"run_id": run_id, "complete": True, "errors": [],
                "logs": [{"message": "Completed real local bt execution", "engine": raw["engine"]}]})
            write(status_path, {"status": "succeeded", "factor_id": factor_id, "fingerprint": definition["fingerprint"]})
        except Exception as error:
            write(self.root / (run_id + ".logs.json"), {"run_id": run_id, "complete": True,
                "errors": [{"type": type(error).__name__, "message": str(error)}], "logs": []})
            write(status_path, {"status": "failed", "factor_id": factor_id, "fingerprint": definition["fingerprint"]})
        return run_id

    def status(self, run_id):
        from panda_alpha.backend_contract import reference_id
        reference_id(run_id)
        path = self.root / (run_id + ".status.json")
        if not path.exists() and (self.root / (run_id + ".claim")).is_file():
            return "pending"
        return read(path)["status"]

    def result(self, run_id):
        if self.status(run_id) != "succeeded":
            raise ValueError("Original local Run has no successful report yet")
        return read(self.root / (run_id + ".report.json"))

    def billing(self, run_id):
        status = self.status(run_id)
        if status not in {"succeeded", "failed", "cancelled"}:
            return None
        return write(self.root / (run_id + ".bill.json"), {
            "schema": "quantfactra-billing-v1", "backend_identity": self.identity,
            "run_id": run_id, "status": status, "unit": self.identity["cost_unit"], "actual": 0,
            "confirmed": True, "basis": "Local MIT-licensed execution; no platform service account or charge",
            "excludes": "Trading-cost scenarios and unmetered CPU/memory resources"})

    def logs(self, run_id):
        self.status(run_id)  # Validate and resolve only an owned filename.
        return read(self.root / (run_id + ".logs.json"))


def build_client(platform_config):
    return BtAdapter(platform_config)
