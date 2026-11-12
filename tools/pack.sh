#!/usr/bin/env bash
#
# 打包给别人：产出一个干净、可离线安装的发行包。
#
# 为什么要有这个脚本，而不是手动右键压缩：
#   1. 项目根目录混着本地状态——data/ 里有账户库、llm.json 里有 API Key、
#      reports/ 里有个人复盘、.workbuddy/ 里有带 Key 掩码的工作日志。
#      手选文件一定会漏，漏一个就是把账户发给别人。
#   2. git archive 在这里不可用：仓库跟踪的文件远少于工作区
#      （chaogu/portfolio.py、watch.py、doctor.py、docs/INSTALL.md 等
#      都还没入库），打出来的是跑不起来的旧版本。
#   所以这里从**工作区**拷贝，用排除清单做减法，并在打包后做一次自检。
#
# 用法：
#   ./tools/pack.sh              # 产出 dist/a-share-observer-YYYYMMDD.zip
#   ./tools/pack.sh --no-zip     # 只准备目录，不压缩（调试用）
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
NAME="a-share-observer-$STAMP"
OUT_DIR="$ROOT/dist"
STAGE="$OUT_DIR/$NAME"
ZIP="$OUT_DIR/$NAME.zip"
MAKE_ZIP=1

for arg in "$@"; do
  case "$arg" in
    --no-zip) MAKE_ZIP=0 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

# 排除清单。每一条都对应一类"不该跟着源码走"的东西：
#   本地状态   data/ reports/ .dash.log .dash.pid
#   凭据       data/llm.json（含 API Key）、eastmoney-asset-confirmation.json
#   隐私       .workbuddy/（含密钥掩码的工作日志）、_incoming/
#   工具残留   .git .venv __pycache__ dist .DS_Store
#   个人脚本   .workflows/ .kilo/ .topic-canon.txt
# 注意 data/ 与 reports/ 是整目录排除：程序首次启动会自己建这两个目录并
# 初始化数据库（setup.sh 实测如此），所以发行包里不需要带任何本地库。
EXCLUDES=(
  '.git/'
  '.venv/'
  'venv/'
  '__pycache__/'
  '*.py[cod]'
  '*.egg-info/'
  'data/'
  'reports/'
  'dist/'
  '.dash.log'
  '.dash-error.log'
  '.dash.pid'
  '.workbuddy/'
  '.workflows/'
  '.kilo/'
  '_incoming/'
  '.topic-canon.txt'
  '.DS_Store'
  '*.bak'
)

if [ "$ROOT" = "/" ] || [ ! -d "$ROOT/chaogu" ]; then
  echo "定位到的项目根目录不对：$ROOT" >&2
  exit 1
fi

echo "=== 准备发行目录 ==="
rm -rf "$STAGE"
mkdir -p "$STAGE"

# rsync 的 --exclude 用在源路径末尾带斜杠时是相对根的路径，正好匹配上面的写法。
# 不用 cp -R 是因为它没有排除能力；不用 tar 管道是因为这里要能逐步检查。
_rsync_args=()
for e in "${EXCLUDES[@]}"; do _rsync_args+=(--exclude="$e"); done
if ! rsync -a "${_rsync_args[@]}" "$ROOT/" "$STAGE/"; then
  echo "拷贝失败" >&2
  exit 1
fi

# 兜底清一次字节码。排除清单里已经有 __pycache__/ 和 *.py[cod]，
# 但这一步是防御性的：.pyc 里会嵌编译时的绝对路径，
# 一旦漏进包就是实打实的路径泄露，且体积白涨。
# 实测踩到——语法预检若用 compileall，它会在每个包目录下重新生成 __pycache__，
# 只删顶层那两处是不够的（chaogu/sources/ 下还有一层）。
find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null
find "$STAGE" -type f -name '*.py[co]' -delete 2>/dev/null

# 权限规范化。工作区里 run.py / README.md 是 600、dash.sh 是 711，
# 这些私人权限不该跟着发行包走。a+rX 的含义：所有文件可读，
# 只有目录和原本就可执行的文件保留执行位——正好让 .sh 保持可执行、
# 同时让 600 的源码变成可读。
chmod -R a+rX "$STAGE" 2>/dev/null

# 抹掉本机绝对路径。模板和注释里会带作者机器的路径
# （如 tools/com.chaogu.dashboard.plist 的安装示例），
# 发给别人既没用又暴露用户名。setup/install 流程用的是占位符，不受影响。
_clean_files=(
  "$STAGE/tools/com.chaogu.dashboard.plist"
  "$STAGE/tools/dash.sh"
  "$STAGE/docs/INSTALL.md"
  "$STAGE/docs/OPERATIONS.md"
  "$STAGE/setup.sh"
  "$STAGE/AGENTS.md"
)
for f in "${_clean_files[@]}"; do
  [ -f "$f" ] || continue
  # 只在文件确实含本机路径时改，避免无谓重写（保持 mtime 稳定）。
  if grep -q "$ROOT" "$f" 2>/dev/null; then
    sed -i "" "s|$ROOT|/absolute/path/to/a-share-observer|g" "$f" 2>/dev/null \
      || sed -i "s|$ROOT|/absolute/path/to/a-share-observer|g" "$f"
    echo "  已抹除本机路径：${f#$STAGE/}"
  fi
done

echo
echo "=== 安全自检 ==="
FAIL=0

# ① 绝不能出现的本地文件
_forbidden=(
  'data'
  'data/chaogu.db'
  'data/portfolio.db'
  'data/watch.db'
  'data/llm.json'
  'data/eastmoney-asset-confirmation.json'
  'data/dashboards.json'
  'reports'
  '.workbuddy'
  '.workflows'
  '.kilo'
  '_incoming'
  '.topic-canon.txt'
  '.dash.log'
  '.dash.pid'
  '.git'
  '.venv'
)
for p in "${_forbidden[@]}"; do
  if [ -e "$STAGE/$p" ]; then
    echo "  ✗ 不该出现的路径：$p" >&2
    FAIL=1
  fi
done
[ "$FAIL" = "0" ] && echo "  ✓ 无本地数据 / 凭据 / 隐私目录"

# ② 兜底扫一遍：任何漏网的数据库与密钥文件。
# 用 find 而不是靠上面的固定清单，防的是"以后新增了某个 .db"。
_leak="$(find "$STAGE" \( -name '*.db' -o -name '*.db-*' -o -name 'llm.json' \
          -o -name '*asset-confirmation*' -o -name '.env*' \
          -o -name '*.py[co]' -o -name '__pycache__' \) -print 2>/dev/null)"
if [ -n "$_leak" ]; then
  echo "  ✗ 发现疑似凭据/数据库/字节码文件：" >&2
  echo "$_leak" | sed 's|^|      |' >&2
  FAIL=1
else
  echo "  ✓ 无 *.db / llm.json / .env / __pycache__ 残留"
fi

# ③ 文本内容里的真实密钥。掩码形式（sk-XXXXX....XXXX）字符集短、不含连字符长串，
# 用「sk- 后跟 20 位以上字母数字」能把它和真 Key 区分开。
# 测试装置例外：tools/test_llm_config.py 里有一个自造的假 Key。
# 之所以按**值**白名单而不是按文件豁免——万一以后真 Key 被写进测试文件，
# 按文件豁免会把它一起放过去，按值豁免不会。下面只报"文件:行号"，
# 不回显命中的字符串，避免自检日志自己变成泄露源。
KNOWN_FAKE_KEYS=(
  'sk-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
)
_keyhits=""
while IFS= read -r _f; do
  [ -n "$_f" ] || continue
  while IFS= read -r _line; do
    [ -n "$_line" ] || continue
    _val="$(printf '%s' "$_line" | grep -oE 'sk-[A-Za-z0-9_-]{20,}' | head -1)"
    _is_fake=0
    for _fake in "${KNOWN_FAKE_KEYS[@]}"; do
      [ "$_val" = "$_fake" ] && _is_fake=1 && break
    done
    [ "$_is_fake" = "1" ] && continue
    _keyhits="${_keyhits}${_f}:${_line%%:*}
"
  done < <(grep -nE 'sk-[A-Za-z0-9_-]{20,}' "$_f" 2>/dev/null || true)
done < <(grep -rEl 'sk-[A-Za-z0-9_-]{20,}' "$STAGE" 2>/dev/null || true)

if [ -n "$_keyhits" ]; then
  echo "  ✗ 疑似真实 API Key（位置）：" >&2
  printf '%s' "$_keyhits" | sed 's|^|      |' >&2
  FAIL=1
else
  echo "  ✓ 无真实 API Key（已忽略测试装置的假值）"
fi

# ④ 本机绝对路径残留（抹除步骤漏掉的文件会在这里现形）
_pathhit="$(grep -rEl '/(Users|home)/[A-Za-z][A-Za-z0-9._-]*/' "$STAGE" \
            --exclude-dir=vendor 2>/dev/null || true)"
if [ -n "$_pathhit" ]; then
  echo "  ⚠ 仍含本机绝对路径（不一定有害，但发出去前值得看一眼）：" >&2
  echo "$_pathhit" | sed 's|^|      |' >&2
fi

if [ "$FAIL" != "0" ]; then
  echo >&2
  echo "自检未通过，已放弃打包。上面列出的路径必须先处理。" >&2
  echo "目录保留在 $STAGE 供排查。" >&2
  exit 1
fi

# 包内必须存在的文件。少了任何一个，别人解压后都跑不起来——
# 这类缺失不会被上面的安全自检发现，必须单独断言。
echo
echo "=== 完整性自检 ==="
_required=(
  'run.py' 'setup.sh' 'setup.ps1' 'README.md' 'AGENTS.md'
  'chaogu/__init__.py' 'chaogu/dashboard.py' 'chaogu/webapi.py'
  'chaogu/web/index.html' 'chaogu/web/app.js' 'chaogu/web/style.css'
  'tools/dash.sh'
)
_missing=0
for p in "${_required[@]}"; do
  if [ ! -e "$STAGE/$p" ]; then
    echo "  ✗ 缺少关键文件：$p" >&2
    _missing=1
  fi
done
if [ "$_missing" != "0" ]; then
  echo >&2
  echo "关键文件缺失，已放弃打包。" >&2
  exit 1
fi
echo "  ✓ 启动、安装、前端资源齐备"

# Python 语法预检：别人解压后才炸不如现在炸。不碰数据库、不联网。
# 必须用 ast.parse 而不是 compileall/py_compile —— 后两者会写 __pycache__，
# 等于在"已清理干净"的发行目录里重新制造 .pyc（里面还嵌着绝对路径）。
# ast 只解析不编译，零副作用。
if command -v python3 >/dev/null 2>&1; then
  if _err="$(python3 - "$STAGE" <<'PY'
import ast, pathlib, sys
root = pathlib.Path(sys.argv[1])
bad = []
for p in sorted(root.rglob("*.py")):
    try:
        ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
    except SyntaxError as e:
        bad.append(f"{p.relative_to(root)}:{e.lineno}: {e.msg}")
if bad:
    print("\n".join(bad[:10]))
    sys.exit(1)
PY
  )"; then
    echo "  ✓ Python 语法检查通过（未生成字节码）"
  else
    echo "  ✗ Python 语法有错：" >&2
    echo "$_err" | sed 's|^|      |' >&2
    exit 1
  fi
else
  echo "  ⚠ 找不到 python3，跳过语法检查"
fi

echo
echo "=== 打包 ==="
FILE_COUNT="$(find "$STAGE" -type f | wc -l | tr -d ' ')"
if [ "$MAKE_ZIP" = "1" ]; then
  command -v zip >/dev/null 2>&1 || { echo "找不到 zip 命令，加 --no-zip 只准备目录" >&2; exit 1; }
  rm -f "$ZIP"
  # 在 dist/ 下打包，让压缩包内保留一层顶层目录，别人解压不会散一地。
  # -X 去掉 macOS 扩展属性，避免 Windows 解出 __MACOSX 和 ._ 文件。
  ( cd "$OUT_DIR" && zip -rqX "$NAME.zip" "$NAME" ) || { echo "打包失败" >&2; exit 1; }
  SIZE="$(du -h "$ZIP" | cut -f1)"
  echo "  $ZIP"
  echo "  ${SIZE} ｜ ${FILE_COUNT} 个文件"
else
  echo "  已跳过压缩（--no-zip）"
  echo "  $STAGE"
  echo "  ${FILE_COUNT} 个文件"
fi

cat <<EOF

发给别人之后，对方这样跑：

  解压 → 进入目录
  macOS / Linux:   ./setup.sh && ./.venv/bin/python run.py daily && ./.venv/bin/python run.py dash
  Windows:         .\\setup.ps1 -Collect -Start

  然后打开 http://127.0.0.1:8787/
  （Windows 解压可能丢可执行位，setup.sh 报权限不足就先 chmod +x setup.sh）

注意：首次采集需要联网，且发行包内不含任何行情数据，所以要跑一次 daily 才有内容。
EOF
