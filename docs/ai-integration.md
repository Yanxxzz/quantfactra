# 接入已有 Agent

`quantfactra.workflow` 暴露 `Host`、`CommandAgents`、`MailboxAgents` 和 `ModelAgents`。工作包有稳定的 `work_id`、角色、动作、任务与回复契约。回复必须绑定原 `work_id`；中断恢复复用原请求，不另造同一模型调用。

运行 `quantfactra init` 后修改生成的 `worker.py`，把 stdin JSON 转成 stdout JSON 信封。`run_ai.py` 演示命令和文件消息的等价回复。默认 Worker 明确回复 `no_candidate`；它不假装完成真实研究。

需要模型服务时使用 [模型协议](model-providers.md)。需要自己的数据源、本地筛选或策略，放在私有应用模块中，由调用者显式接入。框架不自带个人研究记忆、因子库或实际账户授权。

`team host-demo` 提供三轮脚本化角色协作与两次中断恢复演练。角色代码与配置可修改；真实使用前应明确轮次、预算、终止时间和可执行动作。公共框架不安装自动启动或定时任务。
