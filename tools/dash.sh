#!/bin/bash
# 短线情绪看板启动脚本（长期驻留）
#
# 用法：
#   ./tools/dash.sh start     启动（已在跑则提示）
#   ./tools/dash.sh stop      停止
#   ./tools/dash.sh status    查看状态
#   ./tools/dash.sh restart   重启
#   ./tools/dash.sh logs      查看日志
#   ./tools/dash.sh install   安装开机自启（写 ~/Library/LaunchAgents/）
#   ./tools/dash.sh uninstall 卸载开机自启
#   ./tools/dash.sh autostart 查看自启状态
#
# 环境变量：
#   DASH_PORT  监听端口（默认 8787）
#   DASH_HOST  监听地址（默认 127.0.0.1，只本机可访问）

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${DASH_PORT:-8787}"
HOST="${DASH_HOST:-127.0.0.1}"
# 额外的 run.py dash 参数，例如 DASH_EXTRA="--autocollect --at 15:30"
# shellcheck disable=SC2206
DASH_EXTRA=(${DASH_EXTRA:-})
# 必须是**绝对路径**：launchd 启动时没有 shell PATH，
# 如果这里留成裸 "python3"，开机自启会直接失败（实测踩到）。
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ]; then
    for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
      [ -x "$c" ] && PY="$c" && break
    done
  fi
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "找不到可用的 python3，请设 PYTHON=/path/to/python3" >&2
  exit 1
fi
PIDFILE="$ROOT/.dash.pid"

# launchd 装上之后，KeepAlive 会在进程被杀后自动拉起。
# 这时 stop/start 的直接操作会被 launchd 覆盖：
# 实测 stop 打印"已停止"，8 秒后进程又回来了（pid 变了），
# 用户会以为 stop 坏了。所以先探测，再给出正确指引。
_launchd_installed() {
  [ -f "$HOME/Library/LaunchAgents/com.chaogu.dashboard.plist" ]
}
LOGFILE="$ROOT/.dash.log"

is_up() {
  # 优先用 PID 文件；同时兜底用端口探测
  if [ -f "$PIDFILE" ]; then
    local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
  fi
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

case "${1:-status}" in
  start)
    if is_up; then
      echo "看板已在运行：http://$HOST:$PORT/"
      exit 0
    fi
    cd "$ROOT" || exit 1
    # 先做一次数据可用性检查，避免起了个空白看板
    if ! "$PY" -c "import sys;sys.path.insert(0,'.');from chaogu import db;c=db.init();print(c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0])" >/dev/null 2>&1; then
      echo "警告：数据库不可读，看板可能显示为空。先跑 python3 run.py daily" >&2
    fi
    # -u：关闭 stdout 缓冲。长期驻留进程若被重定向，默认块缓冲会让
    # 日志迟迟不落盘，出问题时看不到任何线索。
    # 用数组安全展开；DASH_EXTRA 为空时展开为 0 个参数
    # （不能写 "${DASH_EXTRA[@]:-}"，那样会塞进一个空字符串参数）
    if [ "${#DASH_EXTRA[@]}" -gt 0 ]; then
      nohup "$PY" -u run.py dash --host "$HOST" --port "$PORT" \
        "${DASH_EXTRA[@]}" >>"$LOGFILE" 2>&1 &
    else
      nohup "$PY" -u run.py dash --host "$HOST" --port "$PORT" \
        >>"$LOGFILE" 2>&1 &
    fi
    echo $! >"$PIDFILE"
    sleep 2
    if is_up; then
      echo "看板已启动：http://$HOST:$PORT/"
      echo "  日志：$LOGFILE"
    else
      echo "启动失败，见日志：$LOGFILE" >&2
      tail -20 "$LOGFILE" >&2
      exit 1
    fi
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      pid="$(cat "$PIDFILE" 2>/dev/null || true)"
      if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" && echo "已停止（pid ${pid}）"
      fi
      rm -f "$PIDFILE"
    fi
    # 兜底：按端口找残留进程
    if _launchd_installed; then
      cat >&2 <<'EOS'
⚠️ 开机自启（launchd）已安装：KeepAlive 会自动把看板拉起来，
   所以 stop 之后它几秒内就会回来。要**真正停掉**请用：
       ./tools/dash.sh uninstall     # 卸载自启并停止
EOS
    fi
    leftover="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$leftover" ]; then
      kill $leftover 2>/dev/null && echo "已停止残留进程（pid ${leftover}）"
    fi
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    if is_up; then
      echo "运行中：http://$HOST:$PORT/"
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -2
      if command -v curl >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/" || true)"
        echo "  HTTP $code"
      fi
    else
      echo "未运行。启动：$0 start"
    fi
    ;;
  logs)
    tail -n "${2:-40}" "$LOGFILE" 2>/dev/null || echo "暂无日志"
    ;;
  install)
    # 安装开机自启（macOS launchd）。会写工作区外的 ~/Library/LaunchAgents/，
    # 所以单独做成一个显式命令，不偷偷装。
    PLIST_SRC="$ROOT/tools/com.chaogu.dashboard.plist"
    PLIST_DST="$HOME/Library/LaunchAgents/com.chaogu.dashboard.plist"
    [ -f "$PLIST_SRC" ] || { echo "缺少模板 $PLIST_SRC" >&2; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents"
    sed "s|__ROOT__|$ROOT|g; s|__PY__|$PY|g" "$PLIST_SRC" > "$PLIST_DST"
    # 若 DASH_EXTRA 指定了额外参数（如换采集时间），替换掉模板里的默认值。
    # 默认模板已含 --autocollect：长期驻留不自动采集 = 永远看旧数据。
    if [ "${#DASH_EXTRA[@]}" -gt 0 ]; then
      sed -i "" "/<string>--autocollect<\/string>/d" "$PLIST_DST"
      for _a in "${DASH_EXTRA[@]}"; do
        sed -i "" "s|<string>-u</string>|<string>-u</string>\n        <string>${_a}</string>|" "$PLIST_DST"
      done
    fi
    # 模板端口是 8787；若用户改了端口，同步替换
    if [ "$PORT" != "8787" ]; then
      sed -i "" "s|<string>8787</string>|<string>$PORT</string>|g" "$PLIST_DST"
    fi
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST" || { echo "launchctl load 失败" >&2; exit 1; }
    launchctl start com.chaogu.dashboard 2>/dev/null || true
    "$0" stop >/dev/null 2>&1   # 让 launchd 接管端口，避免抢占
    echo "已安装开机自启：$PLIST_DST"
    echo "  端口 $PORT ｜ 崩溃会自动拉起 ｜ 日志 $LOGFILE"
    echo "  卸载：$0 uninstall"
    ;;
  uninstall)
    PLIST_DST="$HOME/Library/LaunchAgents/com.chaogu.dashboard.plist"
    if [ -f "$PLIST_DST" ]; then
      launchctl unload "$PLIST_DST" 2>/dev/null || true
      rm -f "$PLIST_DST"
      echo "已卸载开机自启。"
    else
      echo "未安装过开机自启。"
    fi
    ;;
  autostart)
    # 用 plist 文件是否存在判断，而不是 launchctl list ——
    # 后者在某些沙箱/权限环境下被挡，会误报"未安装"。
    _p="$HOME/Library/LaunchAgents/com.chaogu.dashboard.plist"
    if [ -f "$_p" ]; then
      echo "开机自启：已安装"
      echo "  plist: $_p"
      grep -o '<string>[^<]*python[^<]*</string>' "$_p" | head -1 | sed 's/^/  python: /'
    else
      echo "开机自启：未安装（$0 install 安装）"
    fi
    ;;
  *)
    echo "用法：$0 {start|stop|restart|status|logs|install|uninstall|autostart}" >&2
    exit 2
    ;;
esac