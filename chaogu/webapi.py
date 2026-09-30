#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新版看板的 HTTP 接口（挂在 dashboard._Handler 上）。

  GET  /                     新版看板页面
  GET  /static/<file>        前端静态文件（chaogu/web/）
  GET  /api/meta             面板目录、看板列表、可选日期、AI 是否可用
  GET  /api/panel?id=&date=&params=<json>   单个面板的数据
  GET  /api/stock?code=&date=               个股详情（催化原文、历史涨停、快讯）
  GET  /api/live             实时刷新状态
  POST /api/dashboards       保存看板          DELETE /api/dashboards?id=
  POST /api/dashboards/reset 恢复默认看板
  POST /api/ai/build         {prompt} -> 一句话生成看板

每个请求自己开 sqlite 连接（WAL 下读写互不阻塞），不再全局串行：
AI 面板一次要 20 秒，串行的话会把整个看板卡住。
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs

from . import catalyst as cat
from . import db, live, llm, panels

WEB = Path(__file__).resolve().parent / "web"


def _json(obj, code=200):
    return code, "application/json; charset=utf-8", json.dumps(
        obj, ensure_ascii=False, default=str)


def _static(rel):
    f = (WEB / rel).resolve()
    if WEB not in f.parents or not f.is_file():
        return 404, "text/plain; charset=utf-8", "not found"
    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype.endswith("javascript"):
        ctype += "; charset=utf-8"
    return 200, ctype, f.read_bytes()


def handle_get(path, query):
    """返回 (status, content-type, body)；不认识的路径返回 None。"""
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    if path in ("/", "/index.html"):
        return _static("index.html")
    if path.startswith("/static/"):
        return _static(path[len("/static/"):])
    if not path.startswith("/api/") or path == "/api/data":
        return None
    conn = db.init()
    try:
        if path == "/api/meta":
            return _json({"catalog": panels.CATALOG,
                          "dashboards": panels.load_dashboards(),
                          "dates": panels.dates(conn),
                          "latest": panels.latest_date(conn),
                          "llm": llm.available()})
        if path == "/api/panel":
            pid = q.get("id", "")
            if pid not in panels.QUERIES:
                return _json({"error": f"未知面板 {pid}"}, 404)
            try:
                params = json.loads(q.get("params") or "{}")
            except ValueError:
                params = {}
            if q.get("force") and pid.startswith("ai_"):
                panels._CACHE.clear()
            return _json(panels.run(conn, pid, q.get("date") or None, params))
        if path == "/api/stock":
            return _json(stock(conn, q.get("code", ""), q.get("date")))
        if path == "/api/live":
            return _json(live.status())
        return _json({"error": "not found"}, 404)
    except Exception as exc:                          # noqa: BLE001
        return _json({"error": f"{type(exc).__name__}: {exc}"}, 500)
    finally:
        conn.close()


def handle_post(path, body):
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return _json({"error": "请求体不是 JSON"}, 400)
    try:
        if path == "/api/dashboards":
            return _json(panels.save_dashboard(data))
        if path == "/api/dashboards/reset":
            return _json({"dashboards": panels.reset_dashboards()})
        if path == "/api/ai/build":
            conn = db.init()
            try:
                d = panels.build_from_prompt(data.get("prompt", ""), conn,
                                             data.get("date"))
            finally:
                conn.close()
            how, err = d.pop("how"), d.pop("error", None)
            saved = panels.save_dashboard(d)
            return _json({**saved, "how": how, "error": err})
    except Exception as exc:                          # noqa: BLE001
        return _json({"error": f"{type(exc).__name__}: {exc}"}, 500)
    return None


def handle_delete(path, query):
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    if path == "/api/dashboards":
        panels.delete_dashboard(q.get("id", ""))
        return _json({"ok": True})
    return None


def stock(conn, code, date=None):
    date = date or panels.latest_date(conn)
    lu = conn.execute("SELECT * FROM limit_up WHERE trade_date=? AND code=?",
                      (date, code)).fetchone()
    info = conn.execute(
        "SELECT name, reason_info FROM theme_stock WHERE code=? AND trade_date<=? "
        "AND reason_info IS NOT NULL ORDER BY trade_date DESC, "
        "LENGTH(reason_info) DESC LIMIT 1", (code, date)).fetchone()
    name = (lu["name"] if lu else None) or (info["name"] if info else None)
    if not name:
        r = conn.execute("SELECT name FROM stock_basic WHERE code=?", (code,)).fetchone()
        name = r["name"] if r else code
    hist = [dict(r) for r in conn.execute(
        "SELECT trade_date, height, reason FROM limit_up WHERE code=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 10", (code, date))]
    news = [dict(r) for r in conn.execute(
        "SELECT ts, title FROM news WHERE title LIKE ? OR content LIKE ? "
        "ORDER BY ts DESC LIMIT 8", (f"%{name}%", f"%{name}%"))] if name else []
    lines = [m["name"] for m in panels.q_mainlines(conn, date, {})["lines"]
             if any(s["code"] == code for s in m["stocks"])]
    return {"code": code, "name": name, "limit_up": dict(lu) if lu else None,
            "reason_info": info["reason_info"] if info else None,
            "history": hist, "news": news, "lines": lines,
            "keywords": cat.keywords_of(lu["reason"]) if lu else []}
