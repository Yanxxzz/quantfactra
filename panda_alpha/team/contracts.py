"""研究请求和角色交接的不可变契约；在写状态或收费前校验边界。

这里只定义结构和身份，不计算因子、不裁定经济有效性。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import ast
import hashlib
import json
from pathlib import Path

ROLES = ("lead", "market", "fundamental", "skeptic")
INFORMATION = {"market": "trading_price_discovery", "fundamental": "business_capital_allocation"}
TASKS = {"native_screen": "exploration", "source_diagnostic": "source_probe", "key_confirmation": "validation"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


def text_fields(value: dict, fields) -> None:
    for field in fields:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"Nonempty {field} is required")


def strings(value, name: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{name} requires a nonempty string list")


def verify_artifacts(artifacts, *, required=True) -> None:
    from ..registry import verify_artifact
    if not isinstance(artifacts, list) or (required and not artifacts):
        raise ValueError("Existing hashed artifacts are required")
    for artifact in artifacts:
        verify_artifact(artifact)


def input_fields(request):
    """Actual formula names or the hashed native replay input contract.

Python remains trusted code, not a static security sandbox. The legacy
preflight re-executes this receipt before dispatch and the steward compares
its verified input_fields again inside the reservation transaction.
"""
    candidate = request["candidate"]
    if candidate.get("formula"):
        try:
            ast.parse(candidate["formula"], mode="eval")
        except SyntaxError as exc:
            raise ValueError("Formula must be a parseable expression") from exc
        from ..diversity import expression_features
        fields, _ = expression_features(candidate["formula"])
    else:
        path = Path(candidate.get("native_preflight_path", ""))
        if not path.is_file():
            raise ValueError("Python request requires its native preflight receipt")
        binding = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        refs = [{"path": str(Path(a["path"]).resolve()), "sha256": a["sha256"]} for a in request["artifacts"]]
        if binding not in refs:
            raise ValueError("Native receipt must be bound in request artifacts")
        fields = json.loads(path.read_text(encoding="utf-8"))["fields"]
        strings(fields, "native input fields")
    return set(fields)


def validate_request(request: dict) -> dict:
    """Validate declared scope, not economic truth or field availability."""
    from ..platform import fingerprint
    text_fields(request, ("task_id", "owner", "mechanism_id", "information_family", "availability_clock",
                          "hypothesis", "control", "falsifier"))
    owner = request["owner"]
    if owner not in INFORMATION or request["information_family"] != INFORMATION[owner]:
        raise ValueError("Research line must match its distinct information family")
    for field in ("primary_fields", "sources", "risk_exposures"):
        strings(request.get(field), field)
    if "source_requirements" in request:
        strings(request["source_requirements"], "source_requirements")
    candidate = request.get("candidate", {})
    text_fields(candidate, ("candidate_id", "budget_mechanism_id", "official_task", "research_question",
                            "decision_if_pass", "decision_if_fail", "official_information_gap"))
    if candidate["budget_mechanism_id"] != request["mechanism_id"]:
        raise ValueError("Stable mechanism identity must agree across request and candidate")
    if candidate["official_task"] not in TASKS or candidate.get("economic_evaluation_type") == "stateful_account":
        raise ValueError("Team lane supports factor tasks; stateful strategies use their dedicated executor")
    if bool(candidate.get("formula")) == bool(candidate.get("code")):
        raise ValueError("Freeze exactly one formula or Python code definition")
    controls = request.get("control_fields", [])
    if not isinstance(controls, list) or any(not isinstance(f, str) or not f.strip() for f in controls):
        raise ValueError("control_fields must be a list of field names")
    if type(candidate.get("direction")) is not int or candidate["direction"] not in (0, 1):
        raise ValueError("Freeze direction before review")
    window = request.get("window", {})
    for key in ("start", "end", *( ("warmup_start",) if "warmup_start" in window else ())):
        if date.fromisoformat(window[key]).isoformat() != window[key]:
            raise ValueError("Research dates must use YYYY-MM-DD")
    if date.fromisoformat(window["start"]) > date.fromisoformat(window["end"]):
        raise ValueError("Invalid research window")
    for key in ("cycle", "groups"):
        if type(request.get(key)) is not int or request[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    # Local artifacts check compatibility only; no Sharpe threshold here.
    verify_artifacts(request.get("artifacts"))
    actual = input_fields(request)
    primary = set(request["primary_fields"])
    declared_controls = set(controls)
    if candidate.get("formula"):
        # The formula evaluator and native daily-field provider resolve names
        # without case distinctions. Preserve the original frozen request and
        # its hash; compare only the actual input identities canonically.
        actual = {name.casefold() for name in actual}
        primary = {name.casefold() for name in primary}
        declared_controls = {name.casefold() for name in declared_controls}
    if not primary.issubset(actual) or actual - primary - declared_controls:
        raise ValueError("Declared primary/control fields differ from executable inputs: "
                         f"actual={sorted(actual)}, primary={sorted(primary)}, controls={sorted(declared_controls)}")
    return {"request_hash": digest(request), "fingerprint": fingerprint(candidate, window, request["cycle"], request["groups"])}
