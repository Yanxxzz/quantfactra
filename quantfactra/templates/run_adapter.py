"""Run the editable synthetic adapter, or recover its existing local Run."""
import json
from pathlib import Path

from quantfactra import create_backend
from quantfactra.execution import ExperimentLedger, dispatch, resume


def main():
    root = Path(__file__).resolve().parent
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    config["platform"]["state_dir"] = str(root / config["platform"]["state_dir"])
    client = create_backend(config["platform"])
    if getattr(client, "simulation", False) is not True:
        raise ValueError("This tutorial only runs synthetic adapters. Use the documented authorization path for real platforms.")
    output = root / "runs" / "adapter"
    output.mkdir(parents=True, exist_ok=True)
    ledger = ExperimentLedger(output / "experiments.sqlite3")
    try:
        before = len(ledger.jobs())
        job = dispatch({"candidate_id": "my-first-adapter", "formula": "RANK(close)", "direction": 1},
                       {"start": "2025-01-02", "end": "2025-01-31"}, config, ledger, client)
        done = resume(job["fingerprint"], ledger, create_backend(config["platform"]), output / "reports")
        result = {"state": done["state"], "run_id": done["run_id"], "actual": done["actual"],
                  "unit": config["platform"]["cost_unit"], "reused_existing": before > 0,
                  "ledger_jobs": len(ledger.jobs()), "synthetic": True,
                  "source_status": "pending", "economic_status": "not_evaluated"}
    finally:
        ledger.db.close()
    (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
