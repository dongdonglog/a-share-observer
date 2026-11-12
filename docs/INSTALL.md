# 安装与首次启动

本文是 A股观察站的唯一安装入口。项目需要 Python 3.10 或更新版本，运行时只使用
Python 标准库，不需要 `pip install`。数据、账户快照、AI 配置和报告都保存在本机。

## 安装前准备

- Git，用于下载和更新项目。
- Python 3.10+。macOS 可使用 Homebrew 或 python.org，Windows 建议使用
  [python.org](https://www.python.org/downloads/) 的安装包并勾选 Add Python to PATH。
- 一个现代浏览器。看板默认只监听 `127.0.0.1:8787`，仅本机可访问；
  要开放给内网别人看，把监听地址设为 `0.0.0.0` 即可（见文末「开放给内网」）。
- 首次市场数据采集需要访问公开数据源；基础安装和健康检查不会采集网络数据。

先下载项目：

```bash
git clone https://github.com/dongdonglog/a-share-observer.git
cd a-share-observer
```

如果仓库地址发生变化，请使用项目发布页提供的地址，其余命令不变。

## macOS

推荐先完成离线安装，再由你决定是否采集和安装自启动：

```bash
./setup.sh
./.venv/bin/python run.py doctor
./.venv/bin/python run.py daily
./.venv/bin/python run.py dash
```

打开 <http://127.0.0.1:8787/>。最后一条命令占用当前终端，按 `Ctrl+C` 停止。

希望一次完成首次采集并安装用户级开机自启动时：

```bash
./setup.sh --collect --autostart
./tools/dash.sh status
```

`--collect` 会访问公开行情和新闻数据源；`--autostart` 会写入当前用户的
`~/Library/LaunchAgents/com.chaogu.dashboard.plist`，并启用盘后自动采集。

## Linux

在 Debian/Ubuntu 上如果 `venv` 不可用，先安装系统包：

```bash
sudo apt-get install python3 python3-venv git
```

随后执行：

```bash
./setup.sh
./.venv/bin/python run.py doctor
./.venv/bin/python run.py daily
./.venv/bin/python run.py dash --host 127.0.0.1 --port 8787
```

需要常驻时，建议使用用户级 systemd 服务。把下面的两个绝对路径替换成实际路径：

```ini
# ~/.config/systemd/user/a-share-observer.service
[Unit]
Description=A-share Observer
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/a-share-observer
ExecStart=/absolute/path/to/a-share-observer/.venv/bin/python -u run.py dash --host 127.0.0.1 --port 8787 --autocollect
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now a-share-observer
systemctl --user status a-share-observer
```

要让内网别人也能看，把监听地址改成 `0.0.0.0`（macOS/Windows 脚本用 `DASH_HOST`，
Linux 直接改上面 `ExecStart` 里的 `--host`）。这条路是安全的：账户、盯盘、AI 面板和
所有写接口都按 TCP 来源 IP 挡住，对方只拿到只读的行情看板，前端还会自动收起写入口。
详见 [运行维护与排障](OPERATIONS.md) 的「数据与隐私」一节。

**但不要改用「套一层反向代理」来实现对外访问**：同机反代会让账户接口的来源 IP
判断失效，若反代还把 Host 改写成 `127.0.0.1:8787`（nginx `proxy_pass` 的默认行为），
账户数据会直接暴露给能访问反代的任何人。需要对外就用 `--host 0.0.0.0`。

## Windows

在 PowerShell 中运行：

```powershell
git clone https://github.com/dongdonglog/a-share-observer.git
Set-Location a-share-observer
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\.venv\Scripts\python.exe run.py daily
.\.venv\Scripts\python.exe run.py dash
```

打开 <http://127.0.0.1:8787/>。也可让安装脚本在后台启动：

```powershell
.\setup.ps1 -Collect -Start                 # 默认端口 8787
.\setup.ps1 -Start -Port 8788              # 端口冲突时指定其他端口
```

日志位于 `.dash.log` 和 `.dash-error.log`。基础项目不修改系统级 Python 环境。

## 验证安装

macOS/Linux：

```bash
./.venv/bin/python run.py doctor
./.venv/bin/python run.py status
```

Windows：

```powershell
.\.venv\Scripts\python.exe run.py doctor
.\.venv\Scripts\python.exe run.py status
```

`doctor` 不采集数据，会检查 Python 版本、程序文件、目录写入权限、SQLite 完整性、
可选 AI 配置权限和 8787 端口。如果看板已经启动，它还会确认该端口返回本项目的 API。
启动脚本使用 `doctor --require-running`，要求 API 已经可以访问才报告启动成功。
健康检查不会连接模型服务或验证真实券商账号。
`status` 显示本地数据表行数和日期范围。刚完成离线安装时数据为零属于正常情况。

## 可选 AI 功能

不配置模型也能使用行情、事件、账户和规则版分析。需要 AI 面板时，在本机运行：

```bash
./.venv/bin/python run.py llm-setup --help
./.venv/bin/python run.py llm-setup --api openai --base https://example.com/v1 --model model-name
```

Windows 把可执行文件替换为 `.\.venv\Scripts\python.exe`。密钥写入本机忽略的
`data/llm.json`。命令会隐藏输入 Key；本地无认证端点显式加 `--no-key`。不要把 Key 放入
命令参数、提交、截图或粘贴到聊天中。配置后重新运行 `doctor`。

## 首次历史数据（可选）

`daily` 只完成当日流程。需要更完整的历史统计时，再执行：

```bash
./.venv/bin/python run.py backfill --days 90
./.venv/bin/python run.py prices --scope limitup --days 160
./.venv/bin/python run.py recompute
./.venv/bin/python run.py lhb --backfill --start 2026-01-01
```

这些命令会访问公开数据源并可能运行数分钟。中断不会删除既有数据，可重新执行补齐。

安装后的启动、停止、备份、升级和排障见[运行维护](OPERATIONS.md)。
