---
name: a-share-observer
description: 在本机部署、运行、打包和排障 A股观察站（a-share-observer，A 股信息/学习/理财/复盘工作台）。当用户提到 a-share-observer、A股观察站、chaogu、本地行情看板、情绪周期看板、涨停复盘工具、盯盘提醒、席位跟随、让内网别人看行情看板，或要打包这个项目发给别人、把看板装到新电脑上时使用。
---

# A股观察站（a-share-observer）

一个在本机运行的 A 股信息、学习、理财和复盘工作台。纯 Python 标准库，**没有第三方
Python 依赖**，前端是原生 JS 无构建流程。对外只做研究和复盘，不承诺收益，不提供
自动下单、撤单或转账能力——这句话不是免责套话，改动功能时不要让它变成交易工具。

## 先跑诊断，再动手

任何操作之前先确认现状。`doctor` 是离线的，不联网、不采集、不写数据库：

```bash
./.venv/bin/python run.py doctor          # 已建好虚拟环境
./setup.sh                                 # 还没建环境（会自动建 .venv 再 doctor）
```

它会检查 Python 版本、程序文件完整性、`data/` 与 `reports/` 可写、SQLite 完整性
（正常是 19 张表）、AI 配置权限、看板端口占用。**看板起不来时第一步就是它**，
输出会直接指出是哪一项不过。

## 安装

macOS / Linux：

```bash
./setup.sh                    # 只建环境 + 初始化数据库 + 离线自检
./setup.sh --collect          # 额外跑首次公开行情采集（联网）
./setup.sh --start            # 装完直接把看板跑起来
./setup.sh --collect --autostart   # 采集 + 装 macOS 开机自启（launchd）
```

Windows（PowerShell）：

```powershell
.\setup.ps1                   # 建环境 + 自检
.\setup.ps1 -Collect -Start   # 采集并启动
```

`setup.sh` 本身**不联网**；采集和注册常驻服务是两件有副作用的事，所以做成显式开关。
需要第三方批准时，先把这两项单独说明再执行。

macOS 开机自启额外一步（`--autostart` 内部会调它）：`./tools/dash.sh install`。
注意 **`launchctl load` 失败时退出码仍是 0**，所以脚本装完会回查
`launchctl list | grep chaogu` 来确认真的注册上了；在受限环境里它可能报
"Load failed: 5: Input/output error"，那是拿不到用户的 launchd domain，
plist 已经写好了，用户重启或重新登录后 macOS 会自己加载。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 建环境 + 离线自检 | `./setup.sh` |
| 首次/日常采集 | `./.venv/bin/python run.py daily` |
| 启动看板 | `./.venv/bin/python run.py dash` |
| 看板常驻管理（macOS） | `./tools/dash.sh status\|start\|stop\|restart\|logs\|install\|uninstall` |
| 数据状态 | `./.venv/bin/python run.py status` |
| 生成报告 | `./.venv/bin/python run.py report` |
| 回填历史 | 见 `docs/INSTALL.md`（涨停/炸板/题材约 10 分钟，另需回填价格与席位） |
| 配置 AI（可选） | `./.venv/bin/python run.py llm-setup` |
| 打包发给别人 | `./tools/pack.sh` |

看板默认 <http://127.0.0.1:8787/>，旧版在 `/legacy`。

## 打包发给别人

**不要用 `git archive`**：这个仓库跟踪的文件远少于工作区，`chaogu/portfolio.py`、
`watch.py`、`doctor.py`、`docs/INSTALL.md`、`setup.ps1` 等都可能还没入库，
打出来的是跑不起来的旧版本。

用项目自带的脚本，它从工作区拷贝并做三重自检：

```bash
./tools/pack.sh            # → dist/a-share-observer-YYYYMMDD.zip
./tools/pack.sh --no-zip   # 只准备目录，调试用
```

它会排除本地状态（`data/`、`reports/`、日志）、凭据（`data/llm.json`、
`eastmoney-asset-confirmation.json`）、隐私目录（`.workbuddy/`、`_incoming/`），
抹掉本机绝对路径，然后断言包内没有 `*.db`、没有真实 API Key、关键启动文件齐全、
Python 语法可编译。**自检不过就不出包**——这是刻意的，宁可失败也不要发出去一份
带账户数据的压缩包。

对方拿到后：解压 → `./setup.sh` → `run.py daily`（联网采集，包里不含行情数据，
不跑这步看板是空的）→ `run.py dash`。Windows 解压可能丢可执行位，
`chmod +x setup.sh` 一下。

## 硬性边界

这些不是建议，是踩过的坑划出来的线：

- **不要清空或替换 `data/`。** 里面有用户的账户库（`portfolio.db`）、盯盘库
  （`watch.db`）、看板布局（`dashboards.json`）。`chaogu.db` 重建代价是几十万行
  行情，`portfolio.db` 是手工录入的、丢了不可恢复。要迁移先备份。
- **不要读、打印、上传或提交** `data/`、`reports/`、日志里的账号、密码、验证码、
  Cookie、资金持仓、API Key。`data/llm.json` 存的是明文 Key（权限 600）。
- **不要为了远程访问把它绑到 `0.0.0.0` 再套一层同机反向代理或隧道。**
  账户类接口按 **TCP 源 IP** 判断是不是本机调用，反代会让源 IP 变成 `127.0.0.1`
  同时改写 Host，四道检查全部失效，账户直接暴露。要给别人看就用
  `DASH_HOST=0.0.0.0 ./tools/dash.sh install` 直连——这条路上程序会把私密接口
  按来源挡住，并把前端切到只读模式。
- **未经确认不跑** `--collect`、`daily`、历史回填，不装常驻服务。它们会访问
  公开数据源、注册开机自启、写数据库。
- **不要把接口返回的「登录成功」说成「持仓已同步」。** 东方财富接入是实验性只读，
  模拟测试、端口打开、登录页可见都不能冒充真实资金快照。

## 内网给别人看

只读分享用 `DASH_HOST=0.0.0.0`（跑 `./tools/dash.sh status` 会打印该发给对方的内网地址；
这个命令是读**实际监听 socket** 反推的，不是回读配置参数，所以不会出现
"配置说本机、实际开着内网"的错报）：

```bash
DASH_HOST=0.0.0.0 ./tools/dash.sh install      # 装好并常驻
./tools/dash.sh status                          # 拿内网地址 + 确认真实监听
```

对方看到的是公开行情看板，界面标「只读」并收起全部写入口；
`portfolio_` / `watch_` / `ai_` 前缀的面板、`/api/portfolio`、`/api/watch`、
`/api/ai/*`、`/api/dashboards` 对非本机来源**全部 403**。
`ai_` 面板内容本身是市场数据，但它每打开一次就真实消耗用户的模型额度，
所以按私密处理。

## 排障顺序

按这个顺序查，别跳步：

1. `./.venv/bin/python run.py doctor` —— 多数问题（Python 版本、目录权限、
   数据库损坏、端口被占）它直接给结论。
2. 服务在不在：`./tools/dash.sh status`。注意 `stop` 之后进程可能被 launchd 拉回来，
   那是 `KeepAlive` 在起作用，要真正停掉得先 `uninstall`。
3. 看日志：`./tools/dash.sh logs 80`，或 `tail -n 50 .dash.log`。
4. 页面能开但面板空 —— 多半是没数据。`run.py status` 看最新交易日，
   没数据就 `run.py daily`。
5. 页面行为异常先怀疑缓存：前端资源带版本戳（`?v=workspace-ui-N`），
   改完 `app.js`/`style.css` 要同步把 `index.html` 里两处戳一起升。
6. 改了代码跑一遍 `tools/test_*.py`，它们是纯标准库的 unittest，不需要 pytest。

## 数据与目录

```
data/chaogu.db      市场数据（大，可重建）
data/portfolio.db   账户与持仓（手工录入，丢了不可恢复）
data/watch.db       盯盘规则与监控
data/dashboards.json 看板布局与自定义面板
data/llm.json       AI 配置，含明文 Key，权限 600
reports/            生成的复盘报告
```

三个库是物理隔离的：`chaogu.db` 是市场，`portfolio.db` 是账户，`watch.db` 是盯盘。
做「对外只读」这类改动时，隔离边界比功能实现更重要——先确认接口和面板的可见范围，
再动前端。

## 文档导航

`README.md`（总览与实证结论）· `docs/INSTALL.md`（安装与首次启动）·
`docs/OPERATIONS.md`（运行维护与排障，含数据与隐私边界）·
`docs/ARCHITECTURE.md`（系统架构）· `docs/AI_SETUP.md`（交给 AI 助手部署）·
`docs/WATCH.md`（盯盘）· `docs/EASTMONEY.md`（东方财富只读接入边界）·
`AGENTS.md`（给编码代理的英文速查）。
