"""Public AI integration contracts; callers own provider access and model budgets."""
from panda_alpha.evolution import Candidate, OpenAICompatibleBackend, PlanningBackend, ResearchEngine
from .providers import JSONProvider, ProviderPlanner

__all__ = ["Candidate", "OpenAICompatibleBackend", "PlanningBackend", "ResearchEngine", "JSONProvider", "ProviderPlanner"]
