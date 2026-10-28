# QuantFactra

A multi-agent research framework for AI developers: structured hand-offs, durable task identities, review, model protocols and replaceable execution backends.

The public project contains framework functionality, authored synthetic tests and explicitly documented public examples. Applications supply their own private strategies, data, credentials and research memory. No personal factor library or research history is bundled.

```bash
python -m pip install -e .
python -m quantfactra demo --output research_runs/quickstart
python -m quantfactra team host-demo --output research_runs/team_demo
python -m quantfactra init --output research_runs/my_agent
```

Open the generated local report. The multi-agent rehearsal uses scripted fixtures; it demonstrates coordination and recovery without real model/account calls. Responses, Chat Completions and Anthropic Messages are opt-in integrations. PandaAI's formula CLI, local bt and custom `module:factory` backends are supported.

[Getting started](docs/quickstart.md) · [Architecture](docs/architecture.md) · [Backend integration](docs/backends.md) · [Model integration](docs/model-providers.md) · [Publication boundary](docs/publication.md)

Only explicitly approved files and hashes can be published. Install the repository's pre-push hook and run the publication validator before submitting changes. Keep private application work in a separate repository.

MIT software license; third-party example data has its own attribution and terms. Upstream QUANTAXIS copyright attribution is retained.
