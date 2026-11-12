"""登录后的一次持仓分析；任务只保留内存状态，不接触登录凭证。"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_state = {"state": "idle"}
_generation = 0


def status():
    with _lock:
        return dict(_state)


def _allowed(session, login_id):
    from . import portfolio
    if not session.analysis_allowed(login_id):
        return False
    c = portfolio.connect()
    try:
        return c.execute("SELECT source FROM portfolio_source WHERE id=1").fetchone()[0] == "eastmoney"
    finally:
        c.close()


def start(session, login_id):
    """登录请求在完整同步后调用；模型请求独立执行，不阻塞刷新/退出。"""
    global _generation, _state
    with _lock:
        _generation += 1
        generation = _generation
        _state = {"state": "running", "trigger": "login", "message": "登录成功，正在结合盘面分析持仓…"}

    def run():
        from . import db, portfolio, llm
        conn = None
        result = {"state": "cancelled", "message": "会话或账户来源已变化，自动分析已取消"}
        try:
            if not _allowed(session, login_id):
                return
            conn = db.init()
            snap = portfolio.snapshot(conn)
            if not portfolio.has_analyzable_assets(snap):
                result = {"state": "empty", "message": "账户暂无可分析的股票、基金或现金理财，已跳过自动分析"}
                return
            from . import research_data
            research_data.refresh(snap)
            # 若显式更新已在运行，等待同一轮完成或失败后才生成一次报告。
            import time
            while research_data.status()["state"] == "running":
                if not _allowed(session, login_id): return
                time.sleep(.25)
            if not llm.available():
                result = {"state": "unavailable", "message": "研究资料已更新；AI 尚未配置，可查看计算结果与图表"}
                return
            analysis = portfolio.analysis(conn, generate=True,
                publish_if=lambda: _allowed(session, login_id))
            result = {"state": "complete", "message": "登录后的 AI 持仓分析已完成", "at": analysis["at"]}
        except Exception:
            # 模型错误可能包含供应商返回的请求信息；这里只给固定文案。
            if _allowed(session, login_id):
                result = {"state": "failed", "message": "自动持仓分析失败，可点击生成按钮手动重试"}
        finally:
            if conn is not None:
                conn.close()
            with _lock:
                if generation == _generation:
                    _state.clear()
                    _state.update(result, trigger="login")

    thread = threading.Thread(target=run, name="portfolio-login-analysis", daemon=True)
    thread.start()
    return thread
