# 接入自己的回测平台

安装自己的模块，在平台配置里显式选择 `module:factory`，并指定稳定、不含秘密的 `connection_id`。构造客户端时不要访问账户或启动任务。

`quantfactra-v1` 客户端提供 `balance / create / start / status / result / logs / preflight / billing`。`preflight` 返回定义和输入检查；`billing` 返回原 Run 的实际回执或未知状态。未知费用保持预约，不自动重新启动。

使用 `python -m quantfactra init --output research_runs/my_agent` 生成 `my_backtest.py`、`config.json` 和 `run_adapter.py`。先运行默认合成版本，再替换自己的服务实现。`run_adapter.py` 重复运行应读取同一任务和原 Run。

身份、任务定义、输入区间、费用单位与报告绑定共同参与恢复判断。不能把另一个平台或连接的结果接到已有 Run 上。真实执行的授权与预算由调用应用提供。

PandaAI 保留公式 CLI 适配器。公共 SDK 不包含个人 Python 因子预检或数据源；需要这类功能时实现自己的明确预检，并通过标准适配合同接入。不要上传自己的策略作为平台接入测试，使用合成定义。

真实本地 bt 适配器见 [bt 指南](bt-backend.md)，模型协作见 [AI 接入](ai-integration.md)。
