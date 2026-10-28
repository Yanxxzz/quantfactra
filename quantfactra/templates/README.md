# Your QuantFactra integration workspace

这是可修改的离线起点。所有输出留在本目录的 `runs/`；默认不读取账户、行情库、模型或已有研究。

在已安装 QuantFactra 的 Python 环境中，从任意工作目录运行以下两个脚本（路径指向本目录）：

```bash
python run_adapter.py
python run_ai.py
```

- `run_adapter.py` 应输出 `SETTLED`、`actual: 0.0`。再次运行应复用原 Run，`ledger_jobs` 仍为 1。
- `run_ai.py` 演练规划接口、命令行 JSON 信封和文件消息：生成 1 个合成候选，角色回复 `no_candidate`。默认固定回复不调用 SDK；报告的 `provider_usage: not_metered` 表示桥接器没有计量供应商用量，不能把它当成真实模型的零费用回执。
- 查看 `runs/adapter/summary.json` 和 `runs/ai/summary.json`。这不是收益测试、完整 Host 会话或第二个真实平台验证。

## 换自己的回测平台

1. 修改 `my_backtest.py` 中八个方法：预检、余额、创建、启动、状态、报告、账单、日志。保留明确的来源、ID、费用和原件。
2. 修改 `config.json` 的平台连接标签和真实计费单位；真实服务的免费/付费模式必须与实际回执一致。
3. 先在本地完成接口测试。真实 SDK 不能继续声称 `simulation = True`，本教程脚本会拒绝非合成客户端。
4. 按主仓库 `docs/backends.md` 的流程，在完整配置和有限授权下验证真实平台。不要把此最小配置当成完整研究配置。

模块在本目录运行时可直接导入。要让其他目录的 CLI 找到插件，可在修改项目名和模块名后执行 `python -m pip install -e <本目录>`；安装不会自动调用平台。同步更新 `config.json` 中的 `module:factory`。

## 换自己的 AI

- **规划接口**：在 `my_planner.py` 中实现 `complete_json(stage, context)`。例子只实现 `plan`；真实工作流还可能请求变异和反思阶段。
- **命令行 Agent**：修改 `worker.py`，按原 packet 的 `action` 和 `reply_contract` 生成 output；保留 work_id，stdout 只写 JSON。当前只支持无提案回复，其他 action 明确失败。
- **文件消息 Agent**：观察 `runs/ai/mailbox/<role>/requests` 与 `responses`。实际 Agent 应读取原请求、原子写同名响应；不能把无响应伪造为完成。

附带的 `model.example.json` 默认关闭。复制为本目录的 `model.local.json`，先运行配置检查（不联网）：

```bash
python -m quantfactra.model_worker --config model.local.json --check
```

主仓库 `docs/model-providers.md` 提供 Responses、Chat Completions 兼容接口、Anthropic Messages、公开案例讨论及完整 Host 接入说明。内置桥接保留请求/响应、供应商用量和未知状态，不自动重试不明请求。也可自行实现 SDK。凭据只通过环境变量或本机存储提供；规划结果不会获得平台执行权限。

替换真实模型后，从 SDK/供应商回执记录实际用量和费用；接口调用一次不代表供应商只请求或计费一次。教程不提供模型账单或跨供应商费用换算。
