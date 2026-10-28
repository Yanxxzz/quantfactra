# 本地 bt 适配器

```bash
python -m pip install -e ".[bt]"
python -m quantfactra case --output research_runs/fx_case
python -m quantfactra case --output research_runs/fx_case --resume
```

`quantfactra.adapters.bt:build_client` 读取调用者提供的价格 CSV，实际执行 bt 1.4.1。公开 ECB 示例的全部规则和来源见 [案例说明](public-research-case.md)。用户自己的价格、策略和输出保存在私有应用目录，勿复制进公共仓库。

平台配置需显式指定 `backend`、`contract: quantfactra-v1`、`connection_id`、`cost_unit`、`state_dir`、`prices_path`；示例使用免费服务费单位，模拟交易费用另由固定策略定义。

框架报告保留原 Run、输入哈希、成本路径、现金和费用核对；软件核对通过不代表来源、投资表现或模型质量已经认证。
