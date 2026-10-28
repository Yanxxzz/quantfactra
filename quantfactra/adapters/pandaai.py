"""The built-in PandaAI adapter, preserving its original CLI and receipt contracts."""
from panda_alpha.platform import PandaClient


def build_client(platform_config):
    return PandaClient(platform_config)


__all__ = ["PandaClient", "build_client"]
