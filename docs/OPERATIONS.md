# 运行维护与排障

## 日常命令

所有命令都应从仓库根目录运行，并优先使用项目自己的虚拟环境。

```bash
./.venv/bin/python run.py doctor       # 离线健康检查
./.venv/bin/python run.py status       # 数据覆盖范围
./.venv/bin/python run.py daily        # 收盘后采集、分析、生成报告
./.venv/bin/python run.py report       # 使用已有数据重新生成报告
```

macOS 后台服务：

```bash
./tools/dash.sh start
./tools/dash.sh status
./tools/dash.sh logs
./tools/dash.sh restart
./tools/dash.sh uninstall
```

安装了 launchd 后，`KeepAlive` 会把被停止的进程重新拉起。需要真正停止时使用
`uninstall`；它只移除自启动服务，不删除项目和数据。

## 数据与隐私

以下内容只属于本机，不应提交到 Git 或上传给别人：

- `data/`：市场数据库、账户数据库、缓存、看板布局和 AI 配置。
- `reports/`：本机生成的复盘报告。
- `.dash.log`、`.dash-error.log`、`.dash.pid`：进程状态与日志。
- `.workflows/`、`_incoming/`、`.topic-canon.txt`：本地工作资料。

服务默认绑定 `127.0.0.1`，只有本机能打开。要让内网别人也能看，用 `DASH_HOST`：

```bash
DASH_HOST=0.0.0.0 ./tools/dash.sh install   # 写进开机自启，长期开放
DASH_HOST=0.0.0.0 ./tools/dash.sh start     # 只本次启动，重启就没了
```

启动后会打印本机和内网的访问地址（绑 `0.0.0.0` 时 `http://0.0.0.0:8787/` 不是
能用的地址，所以脚本会列出真实网卡 IP）。项目没有下单、撤单或转账接口，
账户同步仍应逐项核对券商官方页面。

**开放 `0.0.0.0` 后，对方能看到什么**（按 TCP 来源 IP 区分，不靠对方自觉）：

| 对方能看到 | 对方看不到（403） |
| --- | --- |
| 市场温度计、今日结论、明日关注、主线与催化、涨停梯队、情绪周期、快讯、事件中心、席位 | 账户与持仓、研究与资金规划工作台、盯盘中心、AI 面板与 AI 生成、所有写接口 |

- 前端会自动进入**只读模式**：`/api/meta` 返回 `local: false`，据此收起「新建看板」
  「添加面板」「编辑布局」「提醒」等入口，并停掉对 `/api/watch/status` 的轮询。
  与其让人点一下才弹 403，不如根本不显示。
- 「长期理财」整个看板连同 `watch_center`、`ai_brief` 面板都不会出现在对方的
  看板列表和面板目录里（`panels.public_dashboards` 会连带重排坐标，不留空洞）。
- AI 面板对非本机也隐藏，是因为打开一次就真实消耗一次模型额度，不只是隐私问题。

**三种开放方式的实测差别**：

| 做法 | 别人能打开界面 | 能看到账户与持仓 |
| --- | --- | --- |
| 默认绑 `127.0.0.1` | 否 | 否 |
| 改成 `0.0.0.0`（上方命令） | 能 | **否**，私密接口按来源 IP 判断，非 loopback 一律 403 |
| **同机反向代理 / 隧道**（nginx、frp、cloudflared、tailscale serve、SSH `-L`） | 能 | **可能全看到** |

最后一行要特别当心，**不要为了对外访问去套反向代理**。账户接口靠四道检查挡住远程
访问：来源 IP 必须是 loopback、`Host` 必须是 `127.0.0.1`/`localhost`、`Origin` 同源、
`Sec-Fetch-Site` 不能是 `cross-site`。同机反代让来源 IP 变成 `127.0.0.1`，而浏览器
顶层导航不带 `Origin`、`Sec-Fetch-Site` 是 `none`，四道里只剩 `Host` 一道还起作用。
实测结果：

- 反代**保留**原始 Host（`proxy_set_header Host $host`）→ 账户接口 403，挡住了；
- 反代**改写** Host（nginx `proxy_pass http://127.0.0.1:8787;` 的**默认**行为是
  `proxy_set_header Host $proxy_host`，即 `127.0.0.1:8787`）→ 账户接口全部 200，
  资金、总资产、逐笔持仓、盯盘规则、AI 配置全部可见。

也就是说：**这层保护在反代场景下只靠一行配置差着，不能当安全边界。** 要给别人看
就用上面的 `DASH_HOST=0.0.0.0`，那条路是可靠的——它依赖的是 TCP 来源 IP，
反代改不了。

另外，`data/portfolio.db` 里的券商账户数据会在**退出登录时清除**（见
[东方财富接入](EASTMONEY.md)）。这意味着即使中间套了反代，只要账户处于已退出状态，
磁盘上就没有可泄露的账户数据——但**只要会话还活着，反代场景下它就是敞开的**。

**2026-10-13 修复：看板写接口与 AI 编排接口此前完全不受保护。**
`POST /api/dashboards/reset` / `order` / `duplicate` / `import/preview`、
`DELETE /api/dashboards`、`POST /api/ai/build`、`POST /api/ai/panels` 都不在
`PRIVATE_API_PREFIXES` 里，任何能连上端口的人都能一键删光看板或消耗模型额度
（带内网 `Host` 实测 `reset` 返回 200 并真的清空了自定义看板）。现已一并登记。
`tools/test_llm_config.py::test_every_write_route_is_registered_as_local_only`
会扫描 `webapi.py` 里 `handle_post` / `handle_delete` 的每一条路由，
新增写接口忘记登记时测试直接失败，不必靠人记得看注释。

要注意这份名单仍是**黑名单**：没登记进来的接口默认公开。上面关于反代的那段结论
不受本次修复影响——保护边界依然依赖 `Host` 这一道检查。

## 备份与恢复

升级、迁移电脑或调整账户接入前，先停止看板并备份整个 `data/`：

```bash
./tools/dash.sh uninstall        # macOS 已安装自启动时使用
./tools/dash.sh stop             # 未安装自启动、由脚本后台运行时使用
cp -a data "data.backup-$(date +%Y%m%d-%H%M%S)"
```

上面两个停止命令按实际启动方式选择一个。前台运行时按 `Ctrl+C`。Windows 若通过
`setup.ps1 -Start` 启动，可在 PowerShell 中停止脚本记录的进程：

```powershell
$DashboardPid = Get-Content .dash.pid
Stop-Process -Id $DashboardPid
Remove-Item .dash.pid
```

Windows 停止看板后再复制 `data` 文件夹。备份中可能包含账户快照和 AI Key，应只保存在
受信任的本地磁盘或加密备份中。

恢复时先退出看板，将损坏或不需要的数据目录改名，再把完整备份复制回仓库根目录的
`data/`。运行 `run.py doctor`，确认 SQLite 完整性通过后再启动。

## 安全升级

先确认工作区没有自己未保存的代码改动：

```bash
git status --short
```

备份 `data/` 后更新并重新执行安装脚本：

```bash
git pull --ff-only
./setup.sh
./.venv/bin/python run.py doctor
```

项目没有第三方 Python 包，因此不需要升级全局环境。若项目路径发生变化，macOS 必须重新运行
`PYTHON="$PWD/.venv/bin/python" ./tools/dash.sh install`，因为 LaunchAgent 保存绝对路径。

## 常见问题

### `Python 3.10 or newer is required`

确认 `python3 --version`。Linux 若缺少虚拟环境模块，安装 `python3-venv`；Windows 重新运行
Python 安装器并启用 PATH，或使用 `py -3`。

### 页面能打开但没有数据

先运行 `run.py status`。新安装且尚未采集时空数据是正常的；运行一次 `run.py daily`。
非交易日仍可能没有当日市场快照，但新闻和既有历史可继续展示。不要删除数据库重装。

### 8787 端口被占用

运行 `run.py doctor`。如果提示被其他程序占用，可换端口：

```bash
./.venv/bin/python run.py dash --port 8788
```

然后访问 <http://127.0.0.1:8788/>。macOS 后台脚本可用
`DASH_PORT=8788 ./tools/dash.sh start`。

### 后台服务启动后立即退出

```bash
./tools/dash.sh logs 100
./.venv/bin/python run.py doctor
```

常见原因是项目被移动、Python 被卸载、端口冲突或目录没有写权限。移动项目后重新安装
LaunchAgent。不要先删除 `data/`，数据库通常不是启动失败的原因。

### 公开数据采集超时或 TLS 失败

确认系统时间正确、浏览器能访问公开数据源，并检查代理、防火墙和公司网络策略。项目使用系统
证书校验，不应通过关闭 TLS 验证解决。网络恢复后重新运行原采集命令，已有成功数据会保留。

### AI 功能不可用

AI 是可选功能。看板右上角 `⋯` → `AI 设置…` 可以在界面里改配置并当场点「测试连通性」；
命令行等价入口是 `run.py doctor` 检查配置完整性、`run.py llm-setup` 重新配置。
开机服务通常读不到交互式 shell 的环境变量，因此常驻使用应将配置保存到本机
`data/llm.json`（权限 600）。不要把 Key 写进源代码或提交到 Git。

Key 只写不读：接口只回掩码，页面拿不到明文；抽屉里 Key 输入框留空即表示沿用已保存的那把。
优先级为 `CHAOGU_LLM_*` > `data/llm.json` > `ANTHROPIC_*`；存在 `CHAOGU_LLM_*` 时保存会返回
409 且不生效，抽屉会提前警告并禁用保存按钮。

失败提示按 13 类归类，每条带「下一步做什么」、HTTP 状态和可折叠的原始报文：

| 提示 | 常见原因 | 处理 |
| --- | --- | --- |
| AI 凭证无效或已过期 | 网关侧 token 被吊销、过期、复制不全 | 到网关控制台重新签发，填回「AI 设置」 |
| 模型不存在或无权限 | 模型名 / 接入点写错，或账号未开通 | 核对模型名；openai 协议注意 base 是否已含 `/v1` |
| 触发限流 | 请求过密或额度耗尽 | 稍后重试，或在「AI 设置」里换模型 |
| 网关返回 5xx | 上游服务故障 | 不是本机问题，稍后重试 |
| 网络不可达 / 超时 | DNS、代理、防火墙、内网策略 | 确认系统时间与网络；不要用关闭 TLS 校验绕过 |

排查凭证问题的一个有效手段是**换一把明显不存在的 Key 再试**：如果假 Key 和真 Key 拿到
**逐字节相同**的报错，就说明网关在查表阶段就拒绝了，问题在网关侧，与本项目代码、
协议选择、请求头或模型名都无关。

失败时保留上一份完整结果，不会用零值或空报告覆盖；换凭证或换模型后，AI 面板会强制重算。

### 东方财富登录失效

券商会话不会持久化，服务重启后需要本人重新登录。真实登录、短信、验证码、持仓核对和兼容性
边界见[东方财富本机只读账户接入](EASTMONEY.md)。任何查询错误都应保留上一份成功快照，
不能用删除本地账户数据库的方式修复。

## 获取有效的排障信息

反馈问题时可提供以下不含凭证的结果：

```bash
./.venv/bin/python run.py doctor
./.venv/bin/python run.py status
./tools/dash.sh logs 100
```

发送前仍要检查日志，移除账户标识、持仓、资金、Cookie、验证码、手机号、API Key 和本机绝对
路径。不要发送 `data/`、数据库文件或原始券商响应。

---

## 长期驻留与看板运维

以下内容原本写在 README 里，属于"让它一直跑着"的运维细节，归到这里。

### 看板总览

一个跑在本机、长期开着的 Web 看板，打开就有当下情绪与候选。
**这是日常最该用的入口**，报告是它的离线快照。

```bash
./tools/dash.sh start      # 启动 → http://127.0.0.1:8787/
./tools/dash.sh status     # 状态
./tools/dash.sh logs       # 日志
./tools/dash.sh stop       # 停止
./tools/dash.sh install    # 安装开机自启（重启后自动恢复）
./tools/dash.sh autostart  # 查看自启状态
```

或直接 `./.venv/bin/python run.py dash --port 8787`。


### 四个为"长期运行"专门做的设计

1. **数据新鲜度指示灯**。页头有绿/黄圆点，黄点时会醒目提示
   「数据不是今天」并列出各数据源的实际日期与最后采集时间。
   让你一眼看出是不是对着旧数据做决策。
2. **可选自动采集**（默认**关闭**）。加上 `--autocollect` 后，
   每个工作日 **15:30** 之后自动跑一次 `run.py daily`，看板自己保持最新：

   ```bash
   DASH_EXTRA="--autocollect" ./tools/dash.sh start
   # 或自定义时间
   ./.venv/bin/python run.py dash --autocollect --at 15:45
   ```

   **为什么默认关闭**：采集会打外部接口（东财/新浪/腾讯），
   属于有副作用的网络行为，不该在看板一启动就悄悄发生。

   三道防重复守卫（都实测过）：
   - 当天已成功采集（`collect_log` 有记录）→ 不再跑；
   - 非交易日（工作日但无行情，如节假日）→ 落盘标记，**跨重启也只试一次**；
   - 同一天无论成败只尝试一次，避免崩溃循环打满外部接口。

   > 这个守卫是修出来的，不是一开始就对：最初只用
   > `collect_log` 判断，而**节假日采集"成功但无数据"**，
   > 判定恒为 False，会导致每 5 分钟重试一整天、反复打接口。
3. **数据按天变化，所以 15 秒 TTL 缓存**：连点刷新 / 多标签页 /
   API 轮询不再重复打重查询。
   实测 20 并发：总耗时 **11.20s → 1.59s**，尾延迟 **5826ms → 1468ms**
   （瓶颈是 `_seats` 单次约 500ms，占整页 90%）。
   缓存有**容量上限**——键含日期而日期每天都变，只按 TTL 淘汰会
   导致旧键永不淘汰、长期运行缓慢泄漏（实测 9 天攒 36 个键）。
4. **页面每 60 秒自刷新**；日志用 `-u` 无缓冲，出问题能立刻看到。


### 健壮性（长期运行必须处理的事）

| 问题 | 处理 |
|---|---|
| 节假日重复采集 | 落盘 `autocollect_attempt`，跨重启也只试一次 |
| 日志不落盘 | `-u` 无缓冲（重定向时默认块缓冲会让日志迟迟不可见） |
| 并发尾延迟 | 15s TTL 缓存，20 并发 11.20s → 1.59s |
| 缓存内存泄漏 | 容量上限 48 键，按写入时间淘汰最旧 |
| 留痕重复写库 | `collect_log` 标记"今日已记"，每请求不再重试 INSERT |
| 渲染异常 | 返回 500 + 错误详情页，服务不退出 |


### 开机自启（可选，但"长期运行"建议装）

**不装的话重启电脑看板就没了。** 一条命令装好：

```bash
./tools/dash.sh install      # 装 + 立即启动
./tools/dash.sh autostart    # 查状态
./tools/dash.sh uninstall    # 卸载
```

底层是 launchd：`RunAtLoad`（开机自启）+ `KeepAlive`（崩溃自动拉起）
+ `ThrottleInterval=10`（避免崩溃循环打满 CPU）。
会写 `~/Library/LaunchAgents/com.chaogu.dashboard.plist`（工作区外），
所以做成显式命令，**不偷偷装**。

**装的时候踩到 4 个坑，都已修**：

| 坑 | 后果 | 修法 |
|---|---|---|
| `PY="${PYTHON:-python3}"` 回退成**裸命令** | launchd 没有 PATH，**开机永远起不来** | 强制解析绝对路径并校验可执行 |
| plist 模板**不含 `--autocollect`** | 看板常驻但**永远显示旧数据** | 模板加 `--autocollect`；`DASH_EXTRA` 覆盖时同步替换 |
| `autostart` 用 `launchctl list` 判断 | 沙箱下被挡，**误报"未安装"** | 改查 plist 文件是否存在 |
| 写 `~/Library/LaunchAgents/` 被沙箱**静默拒绝** | 命令打印"已安装"，磁盘上还是**旧文件** | 复查 plist 内容而非只看输出 |

> ⚠️ 第 4 个坑最危险：**命令成功 ≠ 文件写入成功**。
> 修完 bug 后第二次安装其实没落盘，是 `plistlib` 读出 `python3`
> 才发现。看板装了自启却起不来时，先核对 plist 内容。


### 自动采集的两个致命时序 bug（都已修）

这两个 bug 让看板"看起来在自动更新、实际数据是错的"，比崩溃更危险：

| bug | 症状 | 修法 |
|---|---|---|
| **盘中采的数据被当成当日终值** | 2026-09-30 的 `limit_up`/`theme` 是 **14:58** 采的（收盘前 2 分钟），`turnover` 状态 ok → 15:30 自动采集判定"今天已采过"→ **直接跳过**，看板一整天显示盘中未定稿数据且**不会自愈** | `_already_collected` 增加**"采集时刻 >= 15:00"**要求；盘中记录一律视为未采集 |
| **龙虎榜永远采不到** | 龙虎榜 **17:30** 才披露，15:30 那轮必然拿到 0 行且不重试 → ④栏席位一直停在前一天 | 新增 **18:00 晚间补采**，有界重试（`_LHB_MAX_ATTEMPTS=12`） |

> ⚠️ 补采这里我自己也写错过一次：**0 行时无条件标记 `ok`**，
> 于是 15:41 试采拿到 0 行就"完成"了，18:00 真正披露时不再补采。
> 现在只有 **真拿到行** 才记 `ok`，否则记 `empty` 继续试，
> 并在有上界（避免休市日从 18:00 打到午夜）。

> ⚠️ 顺带修了个精度问题：`collect_log` 主键含 `ts`，秒级精度下
> 同一秒内的两次标记会**冲突并被静默吞掉**，导致重试计数偏小。
> 已改为微秒。

**装了自启之后 `stop` 会被 launchd 覆盖**：实测打印"已停止"，
8 秒后进程又被 `KeepAlive` 拉起（pid 变了）。现在 `stop` 会显式警告，
真正停止要用 `./tools/dash.sh uninstall`。

> ⚠️ 看板是**只读展示**，不含任何下单功能。它给的是概率优势，
> 不是保证——胜率 61% 意味着三次里还有一次多是亏的。

---
