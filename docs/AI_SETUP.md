# 交给 AI 助手部署

本页供 Codex、Claude Code 等可以操作本机终端的助手使用。人工安装请直接阅读
[安装与首次启动](INSTALL.md)，日常维护见[运行维护与排障](OPERATIONS.md)。

## 可复制的部署请求

```text
请在这台电脑上部署 https://github.com/dongdonglog/a-share-observer 。

先阅读仓库中的 AGENTS.md、docs/INSTALL.md 和 docs/OPERATIONS.md。识别当前操作系统，
使用项目提供的 setup.sh 或 setup.ps1，不安装不必要的第三方依赖。先做离线安装并运行
run.py doctor。首次采集会访问公开行情和新闻数据源，开机自启动会注册常驻服务；执行这两项
前分别向我说明。默认只监听 127.0.0.1，不要开放到局域网或公网。

不要删除或替换已有 data/。升级或迁移前先备份 data/。不要读取、输出、提交或上传账号、
密码、验证码、Cookie、账户资金持仓、data/、reports/、日志中的敏感内容或任何 API Key。
完成后验证首页和 /api/meta，告诉我访问地址、健康检查结果、启动、停止、升级和备份方法。
```

## 助手执行顺序

1. 检查 Git、Python 3.10+、项目目录和现有 `data/`，不得清空用户数据库。
2. macOS/Linux 运行 `./setup.sh`；Windows PowerShell 运行 `.\setup.ps1`。
3. 运行虚拟环境内的 `run.py doctor`。它不触发市场数据采集。
4. 用户要求采集时，运行 `run.py daily`；需要历史时再运行 INSTALL 中的回填命令。
5. 用户要求自启动时，macOS 使用 `./setup.sh --autostart` 或 `tools/dash.sh install`；
   Linux 使用用户级 systemd；Windows 使用任务计划或保持前台运行。
6. 启动后再次运行 `doctor`，并通过浏览器访问 <http://127.0.0.1:8787/>。
7. 报告实际完成的步骤。模拟测试、端口打开和登录页可见都不能冒充真实券商持仓已同步。

## 不应自动执行的操作

- 未经用户选择就运行 `--collect`、`daily`、历史回填或安装常驻服务。
- 为了远程访问把服务绑定到 `0.0.0.0`、或套一层同机反向代理/隧道，或关闭 TLS/证书校验来绕过网络错误。
  （同机反代会让账户接口的本机判断失效，实测见[运行维护与排障](OPERATIONS.md)「数据与隐私」。）
- 删除数据库来处理空页面、接口失败或迁移错误。
- 在命令参数、终端输出、聊天消息或源文件中放入密码、Cookie、验证码和 API Key。
- 把第三方登录成功解释成已取得完整、正确的资金和持仓快照。

项目使用 Python 标准库，正常安装不需要联网下载 Python 包。AI 功能可选，密钥应由用户在
本机执行 `run.py llm-setup` 写入被 Git 忽略的 `data/llm.json`。
