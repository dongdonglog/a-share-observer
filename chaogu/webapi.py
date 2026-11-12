#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新版看板的 HTTP 接口（挂在 dashboard._Handler 上）。

  GET  /                     新版看板页面
  GET  /static/<file>        前端静态文件（chaogu/web/）
  GET  /api/meta             面板目录、看板列表、可选日期、AI 是否可用
  GET  /api/panel?id=&date=&params=<json>   单个面板的数据
  GET  /api/stock?code=&date=               个股详情（催化原文、历史涨停、快讯）
  GET  /api/live             实时刷新状态
  GET  /api/llm/config       AI 配置视图（**只回显 key 掩码，永不返回明文**）
  POST /api/llm/config       {base,api,model,key?} 保存 AI 配置（key 留空=不改）
  POST /api/llm/test         {base?,api?,model?,key?} 跑一次最小请求自检，不写盘
  POST /api/dashboards       保存看板          DELETE /api/dashboards?id=
  POST /api/dashboards/order     {ids} 调整标签顺序
  GET  /api/dashboards/export?id=  导出看板 JSON
  POST /api/dashboards/import/preview  校验导入文件（不保存）
  POST /api/dashboards/duplicate {id} 复制看板到其右侧
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
from . import db, live, llm, panels, portfolio, eastmoney, watch

WEB = Path(__file__).resolve().parent / "web"


def _json(obj, code=200):
    return code, "application/json; charset=utf-8", json.dumps(
        obj, ensure_ascii=False, default=str)


def _llm_save(data):
    """保存 AI 配置。

    环境变量的优先级高于落盘文件，被覆盖时直接拒绝——否则用户会看到"保存成功"
    但行为没变，那种"看起来生效、实际没生效"比报错更难查。
    """
    if llm.env_override():
        return _json({"error": "检测到 CHAOGU_LLM_* 环境变量，它的优先级高于 data/llm.json。"
                               "请先 unset 这些变量再保存，否则改了也不会生效"}, 409)
    cfg, err = llm.normalize_config(data)
    if err:
        return _json({"error": err}, 400)
    try:
        llm.save_cfg(cfg)
    except OSError as exc:
        return _json({"error": f"写入 {llm.CFG_PATH} 失败：{exc}"}, 500)
    return _json({"ok": True, "config": llm.config_view()})


def _static(rel):
    f = (WEB / rel).resolve()
    if WEB not in f.parents or not f.is_file():
        return 404, "text/plain; charset=utf-8", "not found"
    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype.endswith("javascript"):
        ctype += "; charset=utf-8"
    return 200, ctype, f.read_bytes()


def handle_get(path, query, local=True):
    """返回 (status, content-type, body)；不认识的路径返回 None。

    local 表示请求是否来自本机。服务绑到 0.0.0.0 之后内网其他人也能调公开
    接口，他们拿到的看板列表要裁掉账户、盯盘和 AI 面板，见
    panels.public_dashboards。默认 True 是为了让不传参的调用方（测试、
    将来可能的内嵌用法）保持原来的全量行为。
    """
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    if path in ("/", "/index.html"):
        return _static("index.html")
    if path.startswith("/static/"):
        return _static(path[len("/static/"):])
    if not path.startswith("/api/") or path == "/api/data":
        return None
    if path == "/api/dashboards/export":
        try:
            return _json(panels.export_dashboard(q.get("id", "")))
        except ValueError as exc:
            return _json({"error": str(exc)}, 404)
    if path == "/api/eastmoney/status":
        return _json(eastmoney.session.status())
    if path == "/api/watch/status":
        return _json(watch.status())
    if path == "/api/watch/history":
        unread = q.get("unread", "").lower() in ("1", "true", "yes")
        try:
            limit = int(q.get("limit", 100))
        except ValueError:
            return _json({"error": "limit 必须是整数"}, 400)
        return _json({"items": watch.history(unread=unread, limit=limit)})
    if path in ("/api/watch", "/api/watch/dashboard"):
        try:
            data = watch.dashboard()
            data["positions"] = watch.position_targets()
            data["status"] = watch.status()
            return _json(data)
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"{type(exc).__name__}: {exc}"}, 500)
    if path == "/api/llm/config":
        return _json(llm.config_view())
    conn = db.init()
    try:
        if path == "/api/meta":
            return _json({"catalog": panels.visible_catalog(local),
                          "dashboards": panels.public_dashboards(
                              panels.load_dashboards(), local),
                          "dates": panels.dates(conn),
                          "latest": panels.latest_date(conn),
                          "llm": llm.available(),
                          # 前端据此进入只读模式：绑 0.0.0.0 后内网别人也能打开
                          # 页面，写接口对他们会 403，与其让人点了才报错，不如
                          # 直接把新建看板/编辑布局/AI 生成这些入口收起来。
                          "local": bool(local)})
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
        if path == "/api/portfolio":
            from . import workbench
            return _json(workbench.account_view(portfolio.snapshot(conn)))
        if path == "/api/portfolio/workbench":
            from . import workbench
            return _json(workbench.build(portfolio.snapshot(conn), int(q.get("days",90))))
        if path == "/api/portfolio/history":
            from . import workbench
            return _json(workbench.history(portfolio.snapshot(conn)["data_source"],int(q.get("days",90))))
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
        if not isinstance(data, dict):
            return _json({"error": "请求必须是 JSON 对象"}, 400)
        if path == "/api/watch/stocks":
            return _json(watch.add_stock(data.get("code"), data.get("name", "")))
        if path == "/api/watch/rules":
            return _json(watch.save_rule(data))
        if path == "/api/watch/read":
            return _json(watch.mark_read(data.get("ids"), all_unread=bool(data.get("all_unread"))))
        if path == "/api/watch/history/clear":
            return _json(watch.clear_history(read_only=bool(data.get("read_only", True))))
        if path == "/api/llm/config":
            return _llm_save(data)
        if path == "/api/llm/test":
            # 只把前端真的提交了的字段当草稿；没给 key 就回落到已保存的那把，
            # 这样「只改了模型名」也能直接测，不用重新粘一次 key。
            draft = {k: data[k] for k in ("base", "api", "model", "key") if k in data}
            return _json(llm.test_connection(draft, timeout=45))
        if path.startswith("/api/eastmoney/"):
            action = path.rsplit("/", 1)[-1]
            try:
                if action == "init":
                    result = eastmoney.session.initialize()
                elif action == "captcha":
                    result = eastmoney.session.captcha(data)
                elif action == "login":
                    result = eastmoney.session.login(data)
                elif action in ("sms-send", "sms-verify"):
                    result = eastmoney.session.sms(data, send=action == "sms-send")
                elif action == "product-capabilities":
                    result = eastmoney.session.product_capabilities()
                elif action == "confirm":
                    result = eastmoney.session.confirm(data.get("snapshot_version", ""))
                elif action == "reconcile":
                    result = eastmoney.session.reconcile(data.get("diagnostic_version", ""))
                elif action in ("refresh", "logout", "activity"):
                    result = getattr(eastmoney.session, action)()
                else:
                    return _json({"error": "not found"}, 404)
                if action in ("login", "refresh", "reconcile"):
                    login_id = eastmoney.session.claim_analysis()
                    if login_id:
                        from . import portfolio_jobs
                        portfolio.broker_mutate({"kind": "source", "source": "eastmoney"})
                        portfolio_jobs.start(eastmoney.session, login_id)
                        result["analysis_queued"] = True
                return _json(result)
            except eastmoney.BrokerError as exc:
                return _json({"error": str(exc)}, 400)
            except Exception:
                return _json({"error": "账户操作失败，请手动重试"}, 500)
            finally:
                data.clear()
        if path in ("/api/portfolio/profile", "/api/portfolio/simulate", "/api/portfolio/import", "/api/portfolio/research"):
            from . import workbench, research_data
            conn = db.init()
            try:
                snap = portfolio.snapshot(conn)
                if path.endswith("/profile"):
                    return _json(workbench.profile(data))
                if path.endswith("/import"):
                    return _json(workbench.import_data(data,snap))
                if path.endswith("/research"):
                    return _json(research_data.start(snap))
                return _json(workbench.scenarios(snap,workbench.validate_profile(data)))
            finally:
                conn.close()
        if path == "/api/portfolio/broker":
            return _json(portfolio.broker_mutate(data))
        if path == "/api/portfolio":
            return _json(portfolio.mutate(data))
        if path == "/api/news-impact/analyze":
            from . import news_impact
            conn = db.init()
            try:
                return _json(news_impact.start(conn, data.get("date") or panels.latest_date(conn)))
            finally:
                conn.close()
        if path == "/api/portfolio/analyze":
            conn = db.init()
            try:
                return _json(portfolio.analysis(conn, generate=True))
            finally:
                conn.close()
        if path == "/api/ai/panels":
            # 只编排，前端把结果添加到当前页，不创建新看板或更改账户。
            conn = db.init()
            try:
                return _json(panels.build_from_prompt(data.get("prompt", ""), conn,
                                                     data.get("date")))
            finally:
                conn.close()
        if path == "/api/dashboards":
            return _json(panels.save_dashboard(data))
        if path == "/api/dashboards/import/preview":
            return _json(panels.preview_import(data))
        if path == "/api/dashboards/order":
            return _json({"dashboards": panels.reorder_dashboards(data.get("ids"))})
        if path == "/api/dashboards/duplicate":
            return _json(panels.duplicate_dashboard(data.get("id", "")))
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
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:                          # noqa: BLE001
        return _json({"error": f"{type(exc).__name__}: {exc}"}, 500)
    return None


def handle_delete(path, query):
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    if path == "/api/dashboards":
        try:
            panels.delete_dashboard(q.get("id", ""))
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": True})
    if path == "/api/watch/rules":
        try:
            return _json(watch.delete_rule(q.get("id", "")))
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
    if path == "/api/watch/stocks":
        try:
            return _json(watch.remove_stock(q.get("code", "")))
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
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
        "SELECT ts, title FROM news WHERE (title LIKE ? OR content LIKE ?) "
        "AND replace(ts,' ','T')<? ORDER BY ts DESC LIMIT 8",
        (f"%{name}%", f"%{name}%", panels.news_impact.bounds(date)[1]))] if name else []
    lines = [m["name"] for m in panels.q_mainlines(conn, date, {})["lines"]
             if any(s["code"] == code for s in m["stocks"])]
    return {"code": code, "name": name, "limit_up": dict(lu) if lu else None,
            "reason_info": info["reason_info"] if info else None,
            "history": hist, "news": news, "lines": lines,
            "keywords": cat.keywords_of(lu["reason"]) if lu else []}
