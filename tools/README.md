# tools/ — 研究脚本与校验工具

这些脚本分成三类：**复现结论**、**数值自检**、**契约校验**。
都不属于日常流程（日常只需 `run.py`）。

---

## 一、先跑自检（改动统计代码后必跑）

```bash
python3 tools/test_stats.py        # 14 项数值自检
python3 tools/validate_dim.py all  # heavy-research 维度报告契约校验
```

`test_stats.py` 用**已知答案**检验统计设施：

| 检验对象 | 已知答案 | 实测 |
|---|---|---|
| `ppf(0.5)` / `ppf(0.975)` | 0 / 1.959964 | ✅ |
| PSR（纯噪声，n=200 次） | 服从 U(0,1)，均值 0.5 | 均值 0.510 |
| PSR（强收益 mean=0.5） | → 1 | 1.000000 |
| DSR（纯噪声，10 次） | 应不显著（<0.95） | 均值 0.506 |
| SR0（试验数 N 递增） | 单调上升 | +0.028→+0.105 |
| PBO（纯噪声，n=10） | ≈0.5 | 均值 0.553 |
| PBO（含 1 个真优策略） | → 0，样本外排名 1.0 | 0.0000 / 1.000 |

> **这个自检抓出过我自己写错的断言**：原本断言"单次纯噪声 PSR 应≈0.5"，
> 但真实均值为 0 时 PSR 服从**均匀分布**（5% 分位 0.04、95% 分位 0.95），
> 单次取值方差极大。代码是对的，测试期望是错的——已改为对分布断言。

`validate_dim.py` 机械校验 heavy-research 报告：叶节点编号与提纲逐字一致、
优先级匹配、置信度合法、三个必需小节齐全、无模板残留、`run_id` 与当前轮次一致。
它**曾成功拦截**一份"头部被改成新 run_id、正文仍是旧轮次"的报告。

---

## 二、复现 README 第五节的证据链

```bash
# 1. 抓龙虎榜个股明细（含 D1/D5/D10/D20/D30 前向涨幅）
python3 -c "
import sys; sys.path.insert(0,'.')
from chaogu.sources import lhb
import json
rows = lhb.fetch_billboard('2026-01-01','2026-09-30', max_pages=80)
json.dump(rows, open('/tmp/lhb_2026.json','w'), ensure_ascii=False)
print(len(rows))
"

# 2. 抓席位-个股明细并关联前向收益（内置增量落盘，中断可续跑）
python3 tools/fetch_seat_history.py

# 3. 导入数据库（含名称回填）
python3 tools/import_seat_sample.py
```

**为什么分两步抓**：`billboard` 与 `seat_trade` 来自不同报表，
席位明细既不含前向收益、也**不含股票名称**，必须在本地按
`(code, trade_date)` 关联补齐。

### 然后逐层验证（这是本项目的核心方法学）

```bash
python3 run.py seat                  # 1. 分档是否单调
python3 run.py seat --validate       # 2. 样本外持续性（秩相关 ≈ +0.40）
python3 run.py seat --permute        # 3. 置换检验（p ≈ 0.0015）
python3 run.py seat --monthly        # 4. 逐月稳定性（9/9 为正）
python3 run.py seat --walkforward    # 5. 滚动前向（不用平台指标）
python3 run.py seat --executable     # 6. 剔除买不进的（跳空高估 2.91%）
python3 run.py seat --honest         # 7. 诚实协议（前段选阈值、后段测量）
python3 run.py seat --activity       # 8. 活跃度第二维度
python3 run.py seat --risk           # 9. 分布与尾部风险
python3 run.py seat --overfit        # 10. DSR + PBO 过拟合校正
```

---

## 三、已知的口径缺口

1. **开盘价缺失** → 无法精确分解跳空。原计划用日K开盘价做"次日开盘买入"
   的更严格回测，但东财/腾讯/新浪三个 K 线源同时限流，且请求量过大
   （上万只股票 × 20 天），该脚本已删除。
   现用 `d1_adj >= 涨停阈值-0.3` 作"买不进"的**代理**，偏保守。
   彻底解决需要先回填全市场开盘价（`quote_daily` 目前只有收盘价）。

2. **DSR/PBO 的重叠持仓问题**。当前按周聚合 `d5_adj`，持仓期重叠、
   收益自相关，会系统性**抬高** Sharpe 显著性。
   严格计算需要把持仓错开（如每日只开 1/5 仓位）。

3. **样本期仅 2026 年 1-9 月一轮行情**，未覆盖完整牛熊。