"""A first-run artifact without website accounts, market data or model requests."""
from __future__ import annotations

import html
import json
from pathlib import Path

from panda_alpha.backends import create_backend
from panda_alpha.platform import ExperimentLedger, dispatch, resume


def run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    platform = {"backend": "quantfactra.adapters.demo:build_client", "contract": "quantfactra-v1",
                "connection_id": "offline-demo", "cost_unit": "credits", "state_dir": str(output / "adapter")}
    config = {"platform": platform, "research": {"cycle": 5, "groups": 10},
              "compute": {"billing_mode": "free", "planning_credit_estimate": 1,
                          "daily_credit_limit": 10, "recharge_credit_limit": 0}}
    (output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    client = create_backend(platform)
    ledger = ExperimentLedger(output / "experiments.sqlite3")
    candidate = {"candidate_id": "first-adapter-demo", "formula": "RANK(close)", "direction": 1}
    window = {"start": "2025-01-02", "end": "2025-01-31"}
    try:
        job = dispatch(candidate, window, config, ledger, client)
        settled = resume(job["fingerprint"], ledger, client, output / "reports")
        result = {"project": "QuantFactra", "mode": "synthetic adapter rehearsal", "state": settled["state"],
                  "fingerprint": job["fingerprint"], "run_id": job["run_id"], "actual": settled["actual"],
                  "cost_unit": "credits", "external_calls": 0, "llm_calls": 0,
                  "source_status": "pending", "economic_status": "not_evaluated", "pool_admission": False,
                  "report": str(output / "report.html"), "output": str(output)}
    finally:
        ledger.db.close()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    source = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QuantFactra · First run</title><style>
:root{color-scheme:light dark;font:16px/1.6 system-ui,sans-serif;background:#10151c;color:#e5eaf1}
body{max-width:880px;margin:64px auto;padding:0 24px}a{color:#75c6b5}h1{font-size:40px;letter-spacing:-1.5px;margin-bottom:8px}
.label{color:#75c6b5;text-transform:uppercase;letter-spacing:.14em;font-size:12px}.lede{max-width:650px;color:#aeb9c9}
.flow{display:flex;flex-wrap:wrap;gap:10px;margin:32px 0}.flow span{padding:8px 14px;border:1px solid #364454;border-radius:6px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#18212d;padding:24px;border-radius:8px;font-size:13px}
footer{border-top:1px solid #364454;margin-top:32px;padding-top:16px;color:#aeb9c9}
</style><div class="label">QuantFactra / offline adapter rehearsal</div><h1>Your first run is complete.</h1>
<p class="lede">已通过可配置适配器完成创建、启动、原任务恢复及零费用回执结算。所有文件留在本次隔离目录；没有访问模型、行情库或回测官网。</p>
<div class="flow"><span>Preflight</span><span>Create</span><span>Start</span><span>Resume</span><span>SETTLED · 0 credits</span></div>
<h2>Actual run receipt</h2><pre>""" + source + """</pre><footer>这是接口演练，不生成投资收益，也不代表来源、经济有效性或正式入池通过。</footer></html>"""
    (output / "report.html").write_text(page, encoding="utf-8")
    return result
