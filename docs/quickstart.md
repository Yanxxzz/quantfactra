# 第一次运行

Python 3.11 / 3.12。克隆后创建独立虚拟环境，安装当前源码：

```bash
python -m venv .venv
# macOS / Linux: source .venv/bin/activate
python -m pip install -e .
python -m quantfactra demo --output research_runs/quickstart
python -m quantfactra team host-demo --output research_runs/team_demo
```

PowerShell 未激活环境时，将 `python` 换成 `.\.venv\Scripts\python.exe`，无需修改系统执行策略。

打开 `research_runs/quickstart/report.html`。适配器演示使用合成数据，输出零服务费、稳定任务身份和本地报告。多 Agent 演练使用脚本化回复，验证交接、复核、有限轮次及中断恢复，最终输出 `report.json`。它不是真实模型研究或投资收益。

已有目录拒绝覆盖。新运行换目录；[ECB 案例](public-research-case.md)和[已发表动量案例](published-momentum-case.md)提供明确的 `--resume`。

```bash
python -m quantfactra init --output research_runs/my_agent
python research_runs/my_agent/run_adapter.py
python research_runs/my_agent/run_ai.py
```

生成目录可自己修改，默认留在本机。[平台接口](backends.md)、[模型协议](model-providers.md)、[协作消息](ai-integration.md)说明替换位置。个人策略与运行数据请使用另一个私有仓库。

维护者首次安装后启用发布检查：

```bash
git config core.hooksPath .githooks
python scripts/check_publication.py --all-refs
```
