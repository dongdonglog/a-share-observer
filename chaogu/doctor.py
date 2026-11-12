"""Local, offline installation and runtime checks."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import config, db, llm


def _writable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".chaogu-check-", dir=path, delete=True):
        pass


def _dashboard(host: str, port: int, require_running: bool = False) -> tuple[bool, str]:
    """Return whether the port is available or serves this dashboard."""
    if host not in {"127.0.0.1", "localhost"}:
        return False, "健康检查只允许本机地址 127.0.0.1 或 localhost"
    if not 1 <= port <= 65535:
        return False, "端口必须在 1 到 65535 之间"
    try:
        with socket.create_connection((host, port), timeout=0.4):
            pass
    except OSError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
            if require_running:
                return False, f"看板未在 {host}:{port} 运行"
            return True, f"端口 {host}:{port} 可用"
        except OSError as exc:
            return False, f"端口 {host}:{port} 不可用：{exc}"

    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/meta",
            headers={"Accept": "application/json", "User-Agent": "chaogu-doctor"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and isinstance(payload.get("catalog"), list):
            return True, f"看板正在运行：http://{host}:{port}/"
    except Exception:
        pass
    return False, f"端口 {host}:{port} 已被其他程序占用"


def run(host: str = "127.0.0.1", port: int = 8787,
        require_running: bool = False) -> bool:
    """Run checks without network collection; print a credential-free report."""
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python", sys.version_info >= (3, 10),
                   f"{sys.version.split()[0]}（需要 3.10+）"))

    required = [
        (config.ROOT / "run.py", 1_000),
        (config.ROOT / "chaogu" / "web" / "index.html", 1_000),
        (config.ROOT / "chaogu" / "web" / "app.js", 10_000),
        (config.ROOT / "chaogu" / "web" / "workbench.js", 10_000),
        (config.ROOT / "chaogu" / "web" / "style.css", 1_000),
        (config.ROOT / "chaogu" / "web" / "vendor" / "echarts.min.js", 100_000),
        (config.ROOT / "chaogu" / "web" / "vendor" / "jsencrypt-eastmoney.js", 10_000),
    ]
    missing = [str(path.relative_to(config.ROOT)) for path, minimum in required
               if not path.is_file() or path.stat().st_size < minimum]
    checks.append(("程序文件", not missing,
                   "完整" if not missing else "缺少 " + "、".join(missing)))

    try:
        _writable(config.DATA_DIR)
        _writable(config.REPORT_DIR)
        checks.append(("本地目录", True, "data/ 与 reports/ 可写"))
    except OSError as exc:
        checks.append(("本地目录", False, str(exc)))

    conn = None
    try:
        conn = db.init()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        checks.append(("SQLite", integrity == "ok",
                       f"完整性 {integrity}，{tables} 张表，路径 {config.DB_PATH}"))
    except (OSError, sqlite3.Error) as exc:
        checks.append(("SQLite", False, str(exc)))
    finally:
        if conn is not None:
            conn.close()

    cfg = llm.load_cfg()
    if cfg:
        valid = bool(cfg.get("base") and cfg.get("model") and
                     cfg.get("api") in {"anthropic", "openai"})
        detail = f"已配置 {cfg.get('api', 'anthropic')} / {cfg.get('model', '未指定模型')}"
        checks.append(("AI（可选）", valid, detail if valid else "配置不完整，请重新运行 llm-setup"))
        if os.name != "nt" and llm.CFG_PATH.exists():
            mode = llm.CFG_PATH.stat().st_mode & 0o777
            checks.append(("AI 配置权限", mode == 0o600, f"{mode:03o}（应为 600）"))
    else:
        checks.append(("AI（可选）", True, "未配置；规则版功能仍可使用"))

    port_ok, port_detail = _dashboard(host, port, require_running)
    checks.append(("看板端口", port_ok, port_detail))

    print("== A股观察站健康检查（不采集网络数据） ==")
    for name, ok, detail in checks:
        print(f"  {'通过' if ok else '失败'}  {name}: {detail}")
    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print("\n检查未通过：" + "、".join(failed))
        return False
    print("\n全部必要检查通过。")
    return True
