"""PlanningBackend example. Replace the fixed JSON with your model callable."""


class MyPlanner:
    def complete_json(self, stage, context):
        # For a real model, map this stage/context to its API and validate JSON.
        # Never return credentials, execution permissions or fabricated evidence.
        if stage != "plan":
            raise ValueError("This first example implements only the plan stage")
        return {"candidates": [{
            "hypothesis": "Synthetic integration fixture; not an economic hypothesis",
            "mechanism": "onboarding_fixture",
            "source": "synthetic_fixture",
            "formula": "RANK(close)",
            "direction": 1,
            "data_requirements": ["close"],
            "falsification_plan": ["Replace this fixture with a falsifiable research hypothesis"]
        }]}
