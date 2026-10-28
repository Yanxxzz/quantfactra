"""Explicit execution adapters for the existing job and evidence contracts.

An installed ``module:factory`` receives the platform configuration and returns
the six-method client below. Adapters still normalize their wallet, statuses and
reports to the existing platform contract; this is not a new backtest engine.
"""
from __future__ import annotations

from functools import wraps
import importlib
import json
from typing import Any, Protocol


_METHODS = ("balance", "create", "start", "status", "result", "logs")
_DEFAULT_IDENTITY = {"backend": "pandaai", "connection_id": None}
NATIVE_CONTRACT = "quantfactra-v1"


class ExecutionBackend(Protocol):
    def balance(self) -> dict: ...
    def create(self, candidate: dict, window: dict, cycle: int, groups: int, name: str) -> str: ...
    def start(self, factor_id: str) -> str: ...
    def status(self, run_id: str) -> Any: ...
    def result(self, run_id: str) -> dict: ...
    def logs(self, run_id: str) -> dict: ...


class NativeExecutionBackend(ExecutionBackend, Protocol):
    """Provider-neutral status, preflight and explicit billing receipts."""
    def preflight(self, candidate: dict, window: dict, cycle: int, groups: int) -> dict: ...
    def billing(self, run_id: str) -> dict | None: ...


def configured_identity(config: dict) -> dict:
    """A connection label is stable public metadata, never credentials or a URL."""
    backend = config.get("backend", "pandaai")
    if not isinstance(backend, str) or not backend or backend != backend.strip():
        raise ValueError("platform.backend must be pandaai or module:factory")
    if backend != "pandaai":
        pieces = backend.split(":")
        if (len(pieces) != 2 or not all(part.isidentifier() for part in pieces[0].split("."))
                or not pieces[1].isidentifier()):
            raise ValueError("Custom platform.backend must use module:factory")
    connection = config.get("connection_id")
    if connection is not None and (not isinstance(connection, str) or not connection.strip()
                                   or connection != connection.strip()):
        raise ValueError("platform.connection_id must be a nonempty stable non-sensitive label")
    if backend != "pandaai" and connection is None:
        raise ValueError("Custom backend requires platform.connection_id")
    contract = config.get("contract", "pandaai-v1")
    if contract not in {"pandaai-v1", NATIVE_CONTRACT}:
        raise ValueError("Unknown execution backend contract")
    identity = {"backend": backend, "connection_id": connection}
    if contract == NATIVE_CONTRACT:
        if backend == "pandaai":
            raise ValueError("The built-in PandaAI adapter retains its original contract")
        unit = config.get("cost_unit", "credits")
        if not isinstance(unit, str) or not unit.strip() or unit != unit.strip():
            raise ValueError("Native adapters require a stable cost_unit")
        identity.update(contract=contract, cost_unit=unit)
    return identity


class _ConfiguredBackend:
    def __init__(self, backend, identity, before_call):
        self._backend, self._identity, self._before_call = backend, identity, before_call

    @property
    def backend_identity(self):
        return dict(self._identity)

    @property
    def backend_contract(self):
        return self._identity.get("contract", "pandaai-v1")

    def __getattr__(self, name):
        value = getattr(self._backend, name)
        if not callable(value):
            return value
        @wraps(value)
        def guarded(*args, **kwargs):
            if self._before_call is not None:
                self._before_call()
            return value(*args, **kwargs)
        return guarded


def create_backend(platform_config: dict, *, before_call=None) -> ExecutionBackend:
    """Load only the explicitly configured installed factory; never install one.

    Factories construct clients without performing service calls. The wrapper
    applies the host's execution guard before each adapter method invocation.
    """
    identity = configured_identity(platform_config)
    if identity["backend"] == "pandaai":
        from .platform import PandaClient
        return PandaClient(platform_config, before_call=before_call)
    module, name = identity["backend"].split(":")
    factory = getattr(importlib.import_module(module), name)
    if not callable(factory):
        raise ValueError("Configured backend factory is not callable")
    backend = factory(dict(platform_config))
    methods = _METHODS + (("preflight", "billing") if identity.get("contract") == NATIVE_CONTRACT else ())
    missing = [name for name in methods if not callable(getattr(backend, name, None))]
    if missing:
        raise ValueError("Backend is missing callable methods: " + ", ".join(missing))
    return _ConfiguredBackend(backend, identity, before_call)


def backend_identity(client) -> dict:
    identity = getattr(client, "backend_identity", None)
    if identity is not None:
        return configured_identity(identity)
    from .platform import PandaClient
    if isinstance(client, PandaClient):
        return configured_identity({"connection_id": client.config.get("connection_id")})
    # Preserve existing direct test/synthetic clients and historical callers.
    return dict(_DEFAULT_IDENTITY)


def check_backend_config(client, platform_config: dict) -> dict:
    identity = backend_identity(client)
    if identity != configured_identity(platform_config):
        raise ValueError("Execution backend identity differs from platform configuration")
    return identity


def bind_backend(definition: dict, identity: dict):
    # Default PandaAI definitions retain their historical bytes and identity.
    if identity != _DEFAULT_IDENTITY:
        definition["backend_identity"] = dict(identity)


def check_job_backend(job: dict, client):
    definition = json.loads(job.get("definition") or "{}")
    persisted = definition.get("backend_identity", _DEFAULT_IDENTITY)
    if persisted != backend_identity(client):
        raise ValueError("Execution backend/connection differs from the persisted job")


def uses_native_contract(client) -> bool:
    return backend_identity(client).get("contract") == NATIVE_CONTRACT
