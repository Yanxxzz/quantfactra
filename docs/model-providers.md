# 模型协议与使用方式

支持 Responses、Chat Completions 兼容端点及 Anthropic Messages。`quantfactra init` 生成默认关闭的 `model.example.json`；先复制到本机 `model.local.json`，设置协议、模型、HTTPS 端点和 `api_key_env`，通过环境变量提供密钥。

```bash
python -m quantfactra.model_worker --config research_runs/my_agent/model.local.json --check
```

`--check` 仅核对配置，不发请求。显式启用后由应用调用 `JSONProvider`、`ProviderPlanner` 或 `ModelAgents`。每个请求保留原输入、供应商用量和终态；超时、拒绝或含糊状态不自动重复 POST。运行目录和原始回复保存在本机私有应用区。

规划器通过 `complete_json(stage, context)` 提供候选或复核。框架没有默认个人因子，调用者必须提供自己的规划器或明确记忆。已有工具可以实现 `CommandAgents` / `MailboxAgents` 的回复协议，而无需改平台账本。

自动测试使用本地 HTTP fixture；协议测试不认证真实供应商账户或模型回答质量。不要把密钥、真实研究上下文或供应商回执上传到 issue、PR、Pages 或发行包。
