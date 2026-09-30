# 给 AI 助手的部署说明

本文可直接交给 Codex、Claude Code 或其他能操作本机终端的 AI 助手，让它
在用户自己的电脑上安装和运行 A股观察站。

## 可复制给 AI 的请求

```text
请在这台电脑上部署 https://github.com/dongdonglog/a-share-observer 。
先阅读仓库中的 AGENTS.md 和 docs/AI_SETUP.md，再按当前操作系统执行。
先告诉我安装步骤；首次采集会访问公开行情数据源，开机自启会注册常驻服务，
这两项请在执行前分别向我说明并确认。不要提交或上传本机 data/、reports/、
日志、看板布局或任何 API Key。完成后告诉我访问地址、启动和停止方法。
```

## macOS 一键部署

```bash
git clone https://github.com/dongdonglog/a-share-observer.git
cd a-share-observer
./setup.sh --collect --autostart
```

这会创建项目自己的 `.venv`，抓取首批公开行情，并安装用户级 LaunchAgent。
常驻看板默认只监听本机 `127.0.0.1:8787`；LaunchAgent 在用户登录后启动，
异常退出后会自动拉起，并在交易日收盘后自动采集一次。

首次部署时若只想检查或启动空看板：

```bash
./setup.sh
./.venv/bin/python run.py dash
```

看板地址是 <http://127.0.0.1:8787/>。完成安装后可用以下命令管理：

```bash
./tools/dash.sh status
./tools/dash.sh logs
./tools/dash.sh uninstall
```

`uninstall` 会移除开机自启服务，不会删除项目数据。要重新安装自启，运行
`PYTHON="$PWD/.venv/bin/python" ./tools/dash.sh install`。

## Linux 或其他 Unix 系统

准备环境并初始化本地数据库：

```bash
./setup.sh
./.venv/bin/python run.py daily
./.venv/bin/python run.py dash --host 127.0.0.1 --port 8787
```

`dash` 前台进程停止后看板会退出。若需要登录后或重启后自动运行，请让 AI
根据该系统的服务管理器配置用户级服务，并将工作目录设为仓库目录、执行命令
设为 `.venv/bin/python -u run.py dash --host 127.0.0.1 --port 8787 --autocollect`。
不要为了远程访问直接绑定 `0.0.0.0`；先配置访问控制和 HTTPS。

## Windows

在 PowerShell 中运行以下命令。需要先安装 Python 3.10 或更新版本，并在首次采集前
告知用户会访问公开行情数据源：

```powershell
git clone https://github.com/dongdonglog/a-share-observer.git
Set-Location a-share-observer
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe run.py status
.\.venv\Scripts\python.exe run.py daily
.\.venv\Scripts\python.exe run.py dash --host 127.0.0.1 --port 8787
```

看板运行期间保持终端打开。若要登录后自动启动，AI 助手可以为当前用户创建
Windows 任务计划，执行 `.venv\Scripts\python.exe -u run.py dash --host 127.0.0.1
--port 8787 --autocollect`，工作目录设为仓库目录；卸载时删除该任务即可。

## 可选的 LLM 功能

行情看板不配置 LLM 也能使用。需要 AI 生成面板或文字解读时，在本机执行：

```bash
./.venv/bin/python run.py llm-setup
```

也可按 `run.py llm-setup --help` 配置兼容的 Anthropic 或 OpenAI 接口。密钥只保存在
本机忽略的 `data/llm.json` 中，不要将它复制到仓库或发给 AI 对话窗口。
