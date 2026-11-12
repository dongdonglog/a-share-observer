# 系统架构与开发约定

## 数据流

```text
公开行情、题材、龙虎榜、新闻
          ↓
  chaogu/sources/ + collect.py
          ↓
     data/chaogu.db (SQLite/WAL)
          ↓
 metrics / theme / outlook / news_impact
          ↓
 panels.py + webapi.py + dashboard.py
          ↓
      本机 Web 看板（127.0.0.1）
```

独立后台线程每 10 分钟读取东方财富、同花顺公开榜单供人气异动页展示和收盘基准使用；只有启用行情规则后才读取腾讯报价。规则状态、排名快照和触发记录写入 `data/watch.db`。

账户数据走独立路径：手动账本或经过严格校验的东方财富只读快照写入
`data/portfolio.db`，再由 `portfolio.py` 和 `workbench.py` 生成资金边界与研究视图。
凭证和券商会话只存在进程内存；AI 只接收显式白名单字段。

## 主要模块

| 位置 | 职责 |
| --- | --- |
| `run.py` | 唯一命令行入口；采集、回填、报告、看板、健康检查和 AI 配置 |
| `chaogu/sources/` | 公开数据源适配，只负责取得和规范化上游数据 |
| `chaogu/db.py` | 市场 SQLite schema 与连接 |
| `chaogu/collect.py` | 日常采集编排与写库 |
| `chaogu/metrics.py`、`theme.py`、`outlook.py` | 情绪、题材和次日观察统计 |
| `chaogu/news_impact.py` | 新闻事件聚合、稳定事件 ID、AI 报告缓存和事件生命周期 |
| `chaogu/watch.py`、`chaogu/sources/popularity.py` | 行情与公开榜单规则、收盘排名基准、来源状态和本地提醒 |
| `chaogu/portfolio.py`、`workbench.py` | 手动账户、只读券商快照、资金边界和长期规划 |
| `chaogu/eastmoney.py` | 隔离的东方财富会话与只读资产查询协议 |
| `chaogu/panels.py`、`webapi.py` | 面板数据契约和本地 HTTP API |
| `chaogu/dashboard.py`、`chaogu/web/` | Web 服务、页面、样式和交互 |
| `tools/` | 后台服务脚本、离线测试和受控导入工具 |

## 本地状态

代码与本地状态必须分开：

| 路径 | 内容 | 可提交 |
| --- | --- | --- |
| `data/chaogu.db` | 公开市场历史 | 否 |
| `data/portfolio.db` | 账户、持仓、资金规划 | 否 |
| `data/watch.db` | 自选股、盯盘规则、排名快照和触发记录 | 否 |
| `data/llm.json` | 模型地址、模型名和 API Key | 否 |
| `data/news-impact/` | 事件分析缓存 | 否 |
| `reports/` | 生成的复盘报告 | 否 |
| `chaogu/web/vendor/` | 经审查的前端运行依赖及许可证 | 是 |

新增本地数据库表前要使用 SQLite backup API 备份已有数据库，包括 WAL 中已提交的数据。
迁移必须保留用户原有记录，失败时不得用重建空库代替迁移。

## 运行边界

- 默认监听 `127.0.0.1`；涉及账户的接口还检查 loopback、Host、Origin 和 JSON 请求。
- 基础安装与 `doctor` 不访问市场数据源。联网采集、历史回填和常驻自动采集必须显式启用。
- 页面刷新读取已有数据；只有用户主动操作或明确配置的后台计划才应触发外部请求或 AI。
- 数据源字段缺失、分页不明、金额不平或模型输出截断时保留上一份完整结果，不用零值补齐。
- 系统提供研究、学习和资金管理工具，不生成保证收益的结论，也不实现交易动作。

## 新增面板

1. 在 `chaogu/panels.py` 的 `CATALOG` 添加条目，并在 `QUERIES` 注册查询函数。
2. 在 `META` 登记 `trust`（可信度）、`basis`（口径与局限）；需要校验的参数写入 `param_types`。
3. 未登记的面板在启动时报错，离线测试会检查每个面板都有元数据。
4. 可信度只能依据[研究笔记](RESEARCH.md)中已记录的检验结论设定，不能因为面板"看起来有用"而升级为已验证。

## 扩展新数据源

1. 明确官方或可验证的上游契约、时间口径和停止条件。
2. 在来源模块完成解析与字段校验，再把规范化结构交给业务层。
3. 保存原始来源、实际数据日期和抓取时间，不能把抓取时间冒充行情时间。
4. 对重复、缺失、未来日期、分页和部分成功设计失败关闭行为。
5. 网络测试使用固定模拟响应；真实账号或真实资产只能由用户在本机验收。
6. 更新 README 的能力边界、对应专题文档和离线测试。

## 验证

最小检查：

```bash
./.venv/bin/python -m py_compile run.py chaogu/*.py chaogu/sources/*.py
node --check chaogu/web/app.js
node --check chaogu/web/workbench.js
./.venv/bin/python run.py doctor
```

核心离线回归：

```bash
./.venv/bin/python tools/test_stats.py
./.venv/bin/python tools/test_news_impact.py
./.venv/bin/python tools/test_portfolio.py
./.venv/bin/python tools/test_workbench.py
./.venv/bin/python tools/test_watch.py
./.venv/bin/python tools/test_dashboards.py
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost ./.venv/bin/python tools/test_eastmoney.py
node tools/test_eastmoney_crypto.js
```

测试必须使用临时数据库和模拟上游响应，不登录真实账户、不触发真实交易，也不覆盖用户的
`data/`。Windows 将 `./.venv/bin/python` 替换为 `.\.venv\Scripts\python.exe`。界面变更还应
分别检查桌面与移动视口，确认文字、表格、弹窗和图表没有重叠。

## 数据模型

```
daily_market    每日市场快照（涨停数/最高板/涨跌家数/成交额/指数）
limit_up        涨停股明细（连板数/换手率/封单额/首末封板时间/开板次数）
broken_board    炸板股
limit_down      跌停股
theme_block     题材板块（涨停家数/连板数/最高板/活跃天数）
theme_stock     题材成分股（题材归因 reason / 催化原因 reason_info）
theme_daily     题材日聚合（含量化后的延续性分项）
sector_daily    概念/行业板块行情
quote_daily     全市场每日行情（算溢价/晋级率/涨跌家数）
global_market   外围市场
news            财经快讯
emotion_daily   计算后的情绪指标（温度/阶段/溢价/晋级率）
forecast_log    预测记录（含事后回验字段）
collect_log     采集日志（每步成败）
stock_basic     股票名录
billboard       龙虎榜个股（含 d1/d5/d10/d20/d30 前向涨幅）
                ⚠️ 主键含 reason，同股同日可能多行（13.4%）
billboard_uniq  【视图】按 (日期,代码) 去重；前向收益取 MIN，金额取 SUM，
                多个上榜原因合并进 reasons 字段。**统计一律用这个视图**
seat_trade      席位买卖明细（含席位3日胜率 rep_prob）
                ⚠️ 主键含 side
seat_buy_uniq   【视图】仅买方、按 (日期,席位,代码) 去重
seat_daily      活跃席位榜
```

**为什么必须有去重视图**：`billboard` 主键是
`(trade_date, code, reason)`，一只股票同日因"日涨幅达15%"和
"日换手率达20%"两个原因上榜就会有**两行**。若直接
`seat_trade JOIN billboard`，这只股票的所有席位记录会被**重复计数**，
权重凭空翻倍。实测这会把可执行样本的 D1 从 **+1.880% 拉偏到 +1.768%**，
并让 DSR 从 0.9757（通过）掉到 0.9403（未通过）——**足以改变结论**。
    （DSR 随历次缺陷修复从 0.9403 → 0.9679 → 0.9757 逐步上升，
    每次都是修正 bug 而非挑参数，但这个变化轨迹本身值得读者警惕。）

数据库是单个 SQLite 文件：`data/chaogu.db`。

---

### signal_log（看板信号留痕）

每次看板渲染都会写入当日候选，之后用真实行情回填。

| 列 | 含义 |
|---|---|
| `signal_date` | 看板给出信号的日子（**收盘后**才可计算） |
| `code` / `name` / `seat` | 标的与席位 |
| `rep_prob` / `activity` | 当时平台胜率、当时 60 日活跃度 |
| `entry_close` | 信号日收盘价。**只是收益基准，不是你成交的价格** |
| `d1_ret` / `d5_ret` / `d10_ret` | 次日/5日/10日收盘涨幅%。**NULL = 未来尚未发生，不是缺失** |
| `d1_locked` | 次日是否又涨停（1 = 买不进）。诚实性的关键一列 |

主键 `(signal_date, code)`，重复留痕幂等。
回填**只填空字段**，不重写已有值（否则"新填 N 条"的计数是假的）。

> 为什么不用 `seat_trade` 代替：`seat_trade` 是券商披露的**事后**数据，
> 本表记的是**当时**看板看到了什么。时间戳含义不同，混在一起无法审计。
