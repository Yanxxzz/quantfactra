# Public framework boundary

This repository contains reusable multi-agent orchestration, model protocols, execution adapters and explicitly public examples. Treat this boundary as part of correctness.

- Develop only in this independent public checkout. Never merge, push or import history from a research repository or research worktree.
- Never copy personal factors, parameter sets, strategy combinations, account/position/trade data, research memory, real backtests, task receipts, logs, credentials, grants, bills or machine-specific configuration into this project, its branches, issues, PRs, releases, CI logs or Pages.
- Sole owner-approved display exception: `website/assets/portfolio-performance.png`, at the exact reviewed SHA256 in `publication/allowlist.json`, is an anonymous historical performance image. It contains one cost assumption, the complete period, return/drawdown and scope labels. No source series, SVG, numeric companion file, generator, factor identities, formulas, weights or research history are approved by this exception. Replacing the image or adding another research output requires a new explicit owner approval; the other publication rules remain unchanged.
- Do not add built-in research formulas. Applications provide their own planner, memory, data and strategy plugins. Examples use authored synthetic fixtures or the explicitly documented ECB/French public cases.
- Publication is default-deny. Each file must appear in `publication/allowlist.json` with its reviewed SHA256 and classification. Unknown paths, symlinks, research directories and sensitive content fail publication. A changed file requires content review before updating its approval hash.
- Run `python scripts/check_publication.py --all-refs`, `python -B scripts/verify_public_research.py` and `python -m pytest tests -q` before publishing. Build distributions and inspect them with `check_publication.py --artifacts dist`.
- Keep `.githooks/pre-push` enabled. Do not bypass publication failures with `--no-verify`, altered hooks, API writes, new remotes or a weakened validator.
- Provider and PandaAI calls are opt-in application actions. Tests and demos use local fixtures. Never imply that protocol tests prove real model quality or profitable research.
- Preserve upstream license attribution. Public classic examples do not establish the framework's investment performance.
