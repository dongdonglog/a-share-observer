#!/usr/bin/env python3
"""统计设施的数值自检（已知答案检验）。

为什么需要
----------
DSR / PBO / PSR 这类统计量如果实现有 bug，会给出"看起来合理"但错误的
结论。必须用**已知答案**验证：纯噪声下 PSR 应≈0.5、PBO 应≈0.5、
DSR 应不显著；有一个明显更优策略时 PBO 应→0。

用法:
    python3 tools/test_stats.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import pathlib
import random
import statistics as st
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chaogu import seat  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(name)


def _check_shell_scripts():
    """.sh 里 $VAR 后面不能紧跟非 ASCII 字符（会被当成变量名）。"""
    import pathlib
    import re
    pat = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)([^\x00-\x7f])")
    bad = []
    root = pathlib.Path(__file__).resolve().parent.parent
    scripts = list(root.glob("*.sh")) + list((root / "tools").glob("*.sh"))
    for sh in scripts:
        for i, line in enumerate(sh.read_text(encoding="utf-8").split("\n"), 1):
            for m in pat.finditer(line):
                bad.append(f"{sh.name}:{i} '{m.group(0)}'")
    # 不抛异常：走 check() 的正常失败路径，才能被 FAILED 收集、
    # 让退出码变成 1。抛 AssertionError 会直接崩栈，退出码不可靠。
    return (not bad,
            f"{len(scripts)} 个脚本"
            + ("" if not bad else "，污染：" + "; ".join(bad)))


def main():
    print("=== 1. 正态分布函数 ===")
    check("ppf(0.5)=0", abs(seat._norm_ppf(0.5)) < 1e-9,
          f"{seat._norm_ppf(0.5):+.2e}")
    check("ppf(0.975)≈1.96", abs(seat._norm_ppf(0.975) - 1.959964) < 1e-5,
          f"{seat._norm_ppf(0.975):.6f}")
    check("cdf(1.96)≈0.975", abs(seat._norm_cdf(1.96) - 0.975002) < 1e-5,
          f"{seat._norm_cdf(1.96):.6f}")
    check("cdf(ppf(p))=p 往返", all(
        abs(seat._norm_cdf(seat._norm_ppf(p)) - p) < 1e-6
        for p in (0.01, 0.1, 0.5, 0.9, 0.99)), "5 个分位点")

    print()
    print("=== 2. PSR：纯噪声应≈0.5，强信号应→1 ===")
    rng = random.Random(11)
    # 注意：真实均值为 0 时，PSR 服从 **U(0,1)**，而不是"每次≈0.5"。
    # 单次 PSR 的方差很大（2000 次重复得 5%分位 0.04、95%分位 0.95）。
    # 因此只能对**分布**做断言，不能对单次取值做断言。
    psv = []
    for seed in range(200):
        r = random.Random(seed)
        psv.append(seat.psr([r.gauss(0, 1) for _ in range(2000)]))
    mean_psr = st.mean(psv)
    check("纯噪声 PSR 分布均值≈0.5", abs(mean_psr - 0.5) < 0.06,
          f"{mean_psr:.4f} (n=200)")
    check("纯噪声 PSR 覆盖两极（近似 U(0,1)）",
          min(psv) < 0.15 and max(psv) > 0.85,
          f"min {min(psv):.3f}, max {max(psv):.3f}")
    check("纯噪声 PSR 落在 0~1", all(0 <= v <= 1 for v in psv))
    strong = [rng.gauss(0.5, 1) for _ in range(2000)]
    check("强收益 PSR>0.999", seat.psr(strong) > 0.999,
          f"{seat.psr(strong):.6f}")
    neg = [rng.gauss(-0.5, 1) for _ in range(2000)]
    check("负收益 PSR<0.001", seat.psr(neg) < 0.001, f"{seat.psr(neg):.6f}")

    print()
    print("=== 3. DSR：纯噪声应不显著（<0.95）===")
    dsrs = []
    for seed in range(10):
        r = random.Random(seed + 500)
        m = [[r.gauss(0, 1) for _ in range(300)] for _ in range(8)]
        d = seat.deflated_sharpe(m)
        if d.get("ok"):
            dsrs.append(d["dsr"])
    check("噪声 DSR 均值<0.95", st.mean(dsrs) < 0.95,
          f"均值 {st.mean(dsrs):.3f} (n={len(dsrs)})")
    check("噪声 DSR 不全>0.95", sum(1 for x in dsrs if x > 0.95) <= 2,
          f"{sum(1 for x in dsrs if x > 0.95)}/{len(dsrs)} 超过")

    print()
    print("=== 4. SR0：随试验数 N 单调上升（选择偏差惩罚）===")
    sr0s = []
    for N in (2, 5, 10, 20, 40):
        r = random.Random(99)
        m = [[r.gauss(0, 1) for _ in range(400)] for _ in range(N)]
        sr0s.append(seat.deflated_sharpe(m)["expected_max_sr"])
    check("SR0 随 N 递增", all(sr0s[i] <= sr0s[i + 1] + 1e-9
                              for i in range(len(sr0s) - 1)),
          " < ".join(f"{v:+.3f}" for v in sr0s))

    print()
    print("=== 5. PBO：纯噪声≈0.5，含真优策略→0 ===")
    pbos, rks = [], []
    for seed in range(10):
        r = random.Random(seed * 31 + 7)
        m = [[r.gauss(0, 1) for _ in range(600)] for _ in range(6)]
        res = seat.pbo_cscv(m, n_blocks=10)
        if res.get("ok"):
            pbos.append(res["pbo"])
    check("噪声 PBO 均值在 0.25~0.75", 0.25 <= st.mean(pbos) <= 0.75,
          f"均值 {st.mean(pbos):.3f} (n={len(pbos)})")

    r = random.Random(4242)
    m = [[r.gauss(0, 1) for _ in range(600)] for _ in range(5)]
    m.append([r.gauss(1.0, 1) for _ in range(600)])
    res = seat.pbo_cscv(m, n_blocks=10)
    check("含真优策略 PBO<0.05", res["pbo"] < 0.05, f"{res['pbo']:.4f}")
    check("含真优策略 样本外排名=1", res["median_oos_rank"] > 0.95,
          f"{res['median_oos_rank']:.3f}")

    print()
    print("=== 6. PBO 边界：样本过短应优雅失败 ===")
    res = seat.pbo_cscv([[1.0, 2.0], [2.0, 1.0]], n_blocks=10)
    check("过短样本返回 ok=False", res.get("ok") is False,
          res.get("reason", ""))

    print()
    print("=== 6. shell 脚本变量名污染（set -u 下的隐蔽崩溃）===")
    _ok, _detail = _check_shell_scripts()
    check("无 $VAR 紧跟非 ASCII 字符（set -u 下会崩）", _ok, _detail)

    print()
    print("=== 7. 自动采集时序：盘中数据不算当日终值 ===")
    import tempfile
    import os
    from chaogu import dashboard as _dash
    from chaogu import db as _db
    _tmp = tempfile.mktemp(suffix=".db")
    try:
        _c = _db.init(_tmp)
        # 模拟 14:58（收盘前）采到 turnover
        _c.execute("INSERT INTO collect_log (trade_date,step,status,rows,"
                   "message,ts) VALUES (?,?,?,?,?,?)",
                   ("2026-01-05", "turnover", "ok", 1, "盘中",
                    "2026-01-05T14:58:55"))
        _c.commit()
        check("盘中(14:58)记录判为未采集",
              _dash._already_collected(_c, "2026-01-05") is False,
              "会触发收盘后重采")
        # 收盘后
        _c.execute("INSERT INTO collect_log (trade_date,step,status,rows,"
                   "message,ts) VALUES (?,?,?,?,?,?)",
                   ("2026-01-06", "turnover", "ok", 1, "盘后",
                    "2026-01-06T15:30:10"))
        _c.commit()
        check("收盘后(15:30)记录判为已采集",
              _dash._already_collected(_c, "2026-01-06") is True,
              "不重复采集")
        # 龙虎榜补采：0 行必须继续重试
        check("LHB 初始需补采",
              _dash._lhb_state(_c, "2026-01-07") == (False, 0))
        _dash._mark_key(_c, _dash._LHB_KEY, "2026-01-07", "0 行",
                        status="empty")
        check("LHB 0 行不算成功（下轮重试）",
              _dash._lhb_state(_c, "2026-01-07")[0] is False,
              f"tries={_dash._lhb_state(_c, '2026-01-07')[1]}")
        _dash._mark_key(_c, _dash._LHB_KEY, "2026-01-07", "88 行",
                        status="ok")
        check("LHB 拿到行才算成功",
              _dash._lhb_state(_c, "2026-01-07")[0] is True)
        _c.close()
    finally:
        for _s in ("", "-wal", "-shm"):
            try:
                os.remove(_tmp + _s)
            except OSError:
                pass

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------- 脚本自检
# 加这一段的原因：dash.sh 里写过 `echo "...（pid $pid）"`，
# bash 在 set -u 下会把全角括号当成变量名的一部分，
# 于是报 `pid）: unbound variable`。而当时我的测试都带 >/dev/null，
# 把它盖住了 —— 直到冷启动测试才暴露。
# 所以这里静态扫一遍所有 shell 脚本，防止同类问题再溜进去。
