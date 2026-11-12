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
#              改成 0.0.0.0 就是让内网其他人也能打开。公开面板（涨停、主线、
#              快讯）谁都能看；账户、盯盘和 AI 面板仍按来源挡住，不会外泄。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${DASH_PORT:-8787}"
HOST="${DASH_HOST:-127.0.0.1}"
# 0.0.0.0 / :: 表示"监听所有网卡"，它不是可以拿来连的地址：
# curl http://0.0.0.0:8787/ 在 macOS 上行为不确定，探测一律走 loopback。
case "$HOST" in
  0.0.0.0|::|"*"|"") PROBE_HOST="127.0.0.1" ;;
  *) PROBE_HOST="$HOST" ;;
esac

_local_ips() {
  # 优先问默认网卡（最准，Mac 上通常是 en0），拿不到再扫所有网卡。
  # 过滤掉环回、链路本地和 198.18/198.19 —— 后者是代理的 fake-ip 段，
  # 报给用户只会让人白试一次。
  local ip
  ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [ -n "$ip" ]; then echo "$ip"; return; fi
  ifconfig 2>/dev/null | awk '
    /^[a-z]/ { dev=$1 }
    /inet / {
      if ($2 ~ /^127\./ || $2 ~ /^169\.254\./ || $2 ~ /^198\.1[89]\./) next
      if (dev ~ /^utun/) next
      print $2
    }'
}

_listen_addr() {
  # 从**实际监听中的 socket** 反推绑定地址。
  # 不能拿 $HOST 当答案：那只是本次 shell 的配置意图（默认 127.0.0.1），
  # 跟此刻正在跑的那个进程无关。实测踩到——手动用 --host 0.0.0.0 起的服务，
  # status 只看 DASH_HOST 就报 "打开：http://127.0.0.1:8787/"，
  # 恰好把真正该发出去的内网地址藏了，比不打印更有害。
  local name
  name="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9; exit}')"
  case "$name" in
    "")        echo "" ;;
    \**:*|\[::\]:*) echo "0.0.0.0" ;;
    *)         echo "${name%:*}" ;;
  esac
}

_access_hint() {
  # 用户真正需要的是"发哪个链接给同事"，而不是 http://0.0.0.0:8787/
  if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
    echo "  本机：http://127.0.0.1:$PORT/"
    local ips; ips="$(_local_ips)"
    if [ -n "$ips" ]; then
      while IFS= read -r ip; do
        [ -n "$ip" ] && echo "  内网：http://$ip:$PORT/"
      done <<< "$ips"
    else
      echo "  内网：没找到内网 IPv4，检查网络连接"
    fi
    echo "  ⚠️ 正在监听所有网卡：能连上这台机器的任何人都能打开公开面板"
  else
    echo "  打开：http://$HOST:$PORT/"
  fi
}
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
      echo "看板已在运行"
      _access_hint
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
      echo "看板已启动"
      _access_hint
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
      echo "运行中"
      # 提示以真实监听地址为准，避免"配置说本机、实际开着内网"这类错报。
      _listen="$(_listen_addr)"
      [ -n "$_listen" ] && HOST="$_listen"
      _access_hint
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -2
      if command -v curl >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://$PROBE_HOST:$PORT/" || true)"
        # 必须写成 ${code}：变量名后面紧跟全角括号时，bash 会把多字节字符的
        # 首字节当成变量名的一部分，报 "code: unbound variable"。
        echo "  HTTP ${code}（本机探测）"
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
    sed "s|__ROOT__|$ROOT|g; s|__PY__|$PY|g; s|__HOST__|$HOST|g" \
      "$PLIST_SRC" > "$PLIST_DST"
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
    # 防"以为开了对外、其实还是本机"：核对 plist 里 --host 的真实取值。
    # 旧模板没有 __HOST__ 占位符，DASH_HOST 会被静默忽略，这里必须叫出来。
    _plist_host="$(awk '/<string>--host<\/string>/{
        getline; gsub(/^[ \t]*<string>/, ""); gsub(/<\/string>[ \t]*$/, ""); print; exit
      }' "$PLIST_DST")"
    if [ "$_plist_host" != "$HOST" ]; then
      echo "⚠️ plist 里 --host 实际是 '$_plist_host'，与期望的 '$HOST' 不一致" >&2
      echo "   模板可能缺少 __HOST__ 占位符，本次开机自启的监听地址不是你要的" >&2
    fi
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    # 坑：launchctl load 失败时**退出码仍是 0**（实测报 "Load failed: 5:
    # Input/output error" 却返回 0），所以 `|| { 报错; exit 1; }` 这种写法
    # 根本拦不住。唯一可靠的验证是回头问 launchd 这个 job 在不在。
    launchctl load "$PLIST_DST" 2>/dev/null || true
    if launchctl list 2>/dev/null | grep -q "com.chaogu.dashboard"; then
      launchctl start com.chaogu.dashboard 2>/dev/null || true
      "$0" stop >/dev/null 2>&1   # 让 launchd 接管端口，避免抢占
      echo "已安装开机自启：$PLIST_DST"
      echo "  端口 $PORT ｜ 监听 $HOST ｜ 崩溃会自动拉起 ｜ 日志 $LOGFILE"
      _access_hint
      echo "  卸载：$0 uninstall"
    else
      echo "⚠️ 开机自启没能注册（launchctl load 失败）。" >&2
      echo "   plist 已经写好，重启或重新登录后 macOS 会自动加载它。" >&2
      echo "   本次先手动把看板跑起来：" >&2
      "$0" start
    fi
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