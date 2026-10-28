"""QuantFactra: reproducible factor research with replaceable execution adapters."""

from panda_alpha import __version__
from panda_alpha.backends import ExecutionBackend, NativeExecutionBackend, create_backend
from panda_alpha.backends import configured_identity as configuration_identity

__all__ = ["ExecutionBackend", "NativeExecutionBackend", "configuration_identity", "create_backend", "__version__"]
