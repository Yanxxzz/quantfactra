# 展示页与发行包

网站使用 `website/` 模板、明确列出的公共资源及公共案例统计构建，不读取本机运行目录或研究仓库。

```bash
python scripts/build_site.py --output .runtime/site --revision master --zip .runtime/demo.zip
```

构建输入必须先通过 [发布检查](publication.md)。站点只介绍多 Agent 框架、接口和明确可公开的经典案例；第三方数据来源与使用条款随案例保留。不要加入个人策略或私人绩效展示。

发行包只从独立公共源码区构建。发布 wheel、sdist、演示 ZIP 和校验和前，运行产物检查；验证安装后的导入路径和离线演示。版本为公开框架版本，不能把私人研究结果写入 Release、Actions 日志或 artifact。
