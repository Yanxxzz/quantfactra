# 架构与公开边界

应用通过 Agent 提出和复核任务，协调器把不可变请求写入任务板；执行层绑定平台身份、费用和原 Run，结果沿同一任务恢复。

- `quantfactra/workflow.py`：Host、命令行 Agent、文件消息及模型 Agent 的公开入口。
- `panda_alpha/team/`：通用任务板、角色契约、协调器、回复缓存与有限监督进程。该目录保留兼容命名，不包含个人研究记录。
- `quantfactra/providers.py`：显式启用的三类模型协议；请求身份与用量回执持久化，失败不自动重复收费调用。
- `quantfactra/execution.py` 和 `adapters/`：执行账本、适配器、平台与计费身份；PandaAI 公式、bt 与自定义客户端。
- `quantfactra/onboarding.py` 和 `templates/`：可编辑的合成接入示例。
- `quantfactra/cases/`：有单独来源说明的公开经典示例。
- `publication/` 和 `scripts/check_publication.py`：逐文件批准与发布检查。

研究策略、数据采集器、本地筛选规则和记忆由应用提供。公共规划器没有默认因子库；未配置自己的规划器或记忆时不生成内置候选。数据采集与私有 Python 因子预检不随框架发布，使用者通过自己的 Worker 或 `quantfactra-v1` 平台适配器实现。

本项目不发布历史研究 Git 对象，也不在公共代码中维护个人研究配置。开发过程遵守 [发布边界](publication.md)。
