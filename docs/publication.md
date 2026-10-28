# 公开发布边界

公共仓库只发布通用框架、接口、合成测试、示例配置和明确批准的第三方公开经典示例。个人策略、公式、参数、组合、研究记忆、真实回测、账户、数据、运行记录、日志、授权、账单及秘密保存在另一个私有项目。

唯一经所有者明确批准的结果展示例外是 `website/assets/portfolio-performance.png`，具体字节以批准清单的 SHA256 为准。它只展示匿名组合在单一费用口径下的完整历史净值、回撤及必要说明。PNG 已移除元数据；不附逐日数据、SVG、数值汇总文件、生成代码、因子身份、公式、权重或研究历史。修改图像或新增研究结果需要另行明确批准。该展示不是可复现的公开策略案例，也不是框架能带来投资收益的证明。

公共源码使用独立 Git 数据库。不要在研究工作树中修改公共远端，也不要将旧研究分支、标签或历史推到公共仓库。新功能先在独立公共源码区完成。

`publication/allowlist.json` 是明确批准的文件清单，包含文件分类和 SHA256。新增或改动文件须先审阅内容，随后手动更新对应批准项；检查程序不会自动批准。禁止目录和敏感内容即使出现在清单中也会被拒绝。

```bash
git config core.hooksPath .githooks
python scripts/check_publication.py --staged
python scripts/check_publication.py --all-refs
python -B scripts/verify_public_research.py
python -m pytest tests -q
python -m build
python scripts/check_publication.py --artifacts dist
```

预推送检查覆盖 Git 可达历史和将要推送的提交；CI 再检查源码、完整测试和构建产物。公开站点只复制明确映射的文件，离线站点包与批准的构建逐文件比较。发行包中的每个源文件必须匹配批准内容，生成的包元数据也进行敏感内容检查。

变更通过合成或公共数据复现。维护者复查源文件、分支、标签、历史、发行包、CI artifact 和 Pages 后再发布。不要将运行目录或整个研究仓作为发布输入。检查能阻止未批准内容和已定义的敏感模式；人工内容审阅仍然必要，禁止绕过失败。
