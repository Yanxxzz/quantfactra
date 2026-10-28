"""Readable offline report for the public case; original engine files stay linked."""
from __future__ import annotations

import html
import io
from pathlib import Path


def render(root, protocol, provenance, raw, summary):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    root = Path(root)
    primary = protocol["primary_cost_bps"]
    matplotlib.rcParams["svg.hashsalt"] = protocol["case_id"]
    figure, axis = plt.subplots(figsize=(9, 4.2), layout="constrained")
    colors = {"benchmark": "#77849a", "group-1": "#aa829f", "group-2": "#139a85"}
    for path in raw["paths"]:
        if path["cost_bps"] == primary:
            axis.plot(pd.to_datetime(raw["dates"]), path["nav"], label=path["name"], color=colors[path["name"]], linewidth=1.6)
    axis.set(title=f"ECB reference-rate basket | {primary:g} bps one-way cost scenario", ylabel="Net asset value / initial capital")
    axis.grid(alpha=.18)
    axis.legend(frameon=False)
    buffer = io.StringIO()
    figure.savefig(buffer, format="svg", metadata={"Date": None})
    plt.close(figure)
    (root / "nav.svg").write_text(buffer.getvalue(), encoding="utf-8")
    e = lambda value: html.escape(str(value), quote=True)
    rows = "".join("<tr>" + "".join(f"<td>{e(value)}</td>" for value in (
        p["name"], f'{p["cost_bps"]:g}', f'{p["metrics"]["total_return"]:.2%}',
        f'{p["metrics"]["max_drawdown"]:.2%}', f'{p["total_fees"]:.2f}')) + "</tr>" for p in raw["paths"])
    limits = "".join(f"<li>{e(line)}</li>" for line in protocol["limitations"])
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QuantFactra · Public FX case</title><style>
:root{{color-scheme:light dark;--bg:#f6f8fb;--panel:#fff;--ink:#172331;--muted:#5c6878;--line:#dce3eb;--accent:#087f70;font:16px/1.7 system-ui,sans-serif}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d141d;--panel:#141f2c;--ink:#edf3f9;--muted:#adbacb;--line:#314153;--accent:#72d7c3}}}}
body{{max-width:1040px;margin:48px auto;padding:0 22px;background:var(--bg);color:var(--ink)}}h1{{font-size:36px;line-height:1.2;letter-spacing:-1px}}h2{{font-size:22px;margin-top:0}}a{{color:var(--accent)}}
.tag{{color:var(--accent);font-size:12px;letter-spacing:.13em}}.lede{{color:var(--muted)}}section{{background:var(--panel);border:1px solid var(--line);padding:24px;border-radius:12px;margin:20px 0}}
img{{width:100%;background:white;border-radius:8px}}table{{border-collapse:collapse;width:100%;min-width:560px;font-size:14px}}th,td{{text-align:left;white-space:nowrap;border-bottom:1px solid var(--line);padding:10px 8px}}code{{overflow-wrap:anywhere}}.scroll{{overflow-x:auto}}
</style><header><div class="tag">QUANTFACTRA / REAL LOCAL BT EXECUTION</div><h1>从固定研究规则到可核验的回测结果</h1>
<p class="lede">ECB 公开参考汇率 · 四币种 · 真实 bt 引擎 · 独立现金与费用核对 · 原 Run 恢复</p></header>
<section><h2>这次研究得到了什么？</h2><p>{e(summary['finding_zh'])}</p><p>这是固定样本中的描述结论。来源时钟、可成交性与正式入池资格仍需独立验证。</p></section>
<section><h2>问题与预先固定的规则</h2><p>{e(protocol['question'])}</p><p>信号为 {e(protocol['formula'])}，滞后一个已收到的有效参考日；每 {protocol['cycle']} 个有效观察日调仓。四币种分为两组，等权对照在相同日期调仓。方向、窗口、成本与分组未按结果调整。</p>
<p>研究区间：{e(protocol['window']['start'])} 至 {e(protocol['window']['end'])}；初始资本 {protocol['initial_capital']:,} EUR。交易成本为假设情景，不是经纪商账单。</p></section>
<section><h2>费用怎样支付？</h2><p>当前 V2 每次调仓预留两倍单边费率的现金（10 bps 情景为 0.2%），再扣实际交易费；组内等权用于预留后的风险资金。逐日检查现金和仓位不为实质负值，禁止借款支付费用。</p><p>V1 曾产生少量负现金，与无杠杆声明不符。原协议与结果保留，修订在 V2 计算前冻结；公式、日期、分组、方向和成本没有按结果调整。这是已知旧结果后的工程修正，不是盲测。</p></section>
<section><h2>净值与成本对照</h2><img src="nav.svg" alt="相同成本场景下的两个动量分组与等权基准净值">
<div class="scroll"><table><thead><tr><th>组合</th><th>单边成本 bps</th><th>总回报</th><th>最大回撤</th><th>累计交易费 EUR</th></tr></thead><tbody>{rows}</tbody></table></div>
<p>收益为资本净值比率；group-2 是较高动量组，group-1 是较低动量组。较高动量组相对等权的净总回报差为 {summary['primary_excess_total_return'] * 100:.2f} 个百分点。窄屏可横向滚动表格。</p></section>
<section><h2>数据是什么，做过哪些处理？</h2><p>Source: European Central Bank. 数据可从 <a href="https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html">ECB 免费获得</a>，遵守其<a href="https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html">复用条件</a>。</p>
<p>保留原报价与状态码，取倒数转换为 EUR/单位货币。收到 {provenance['raw_dates']} 个源日期，{len(provenance['excluded_dates'])} 个日期因当日缺少报价排除；没有补值。输入 SHA256：<code>{e(summary['prices_sha256'])}</code>。</p>
<p>完整转换与排除日期见 <a href="inputs/provenance.json">来源记录</a>；规则见 <a href="protocol.json">固定协议</a>。参考汇率并非可成交买卖报价，也未建模货币利息或 carry。</p></section>
<section><h2>怎样确认它确实跑过？</h2><p>引擎 bt {e(raw['engine']['version'])} 实际执行了 {raw['engine']['backtest_count']} 条路径。逐日期独立核对持仓×价格、现金与交易费用，全部在公开数值容差内。</p>
<p><a href="engine-result.json">原引擎结果、交易与核对</a> · <a href="summary.json">摘要</a> · <a href="case-spec.json">任务配置与原件绑定</a>。平台服务费为本地免费执行的 0；累计交易费另列，CPU 与内存成本未货币计量。</p>
<p>再次运行 <code>python -m quantfactra case --output &lt;本目录&gt; --resume</code> 会读取这个原 Run，不新增一次回测。</p></section>
<section><h2>如何继续研究？</h2><p>先检查上述对照与局限，再提出一项清楚的新假说。修改协议后使用新的输出目录，保留旧结果；自己的平台可沿统一执行合同接入。</p><ul>{limits}</ul></section></html>"""
    (root / "report.html").write_text(page, encoding="utf-8")
