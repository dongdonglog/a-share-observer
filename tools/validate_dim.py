#!/usr/bin/env python3
"""维度报告机械校验器。

校验 heavy-research 各维度报告是否符合输出契约。这些检查都是**机械的**
（不判断内容对错），目的是拦截结构性错误：叶节点缺失、编号错位、
优先级与提纲不一致、置信度非法、模板残留等。

用法:
    python3 tools/validate_dim.py web
    python3 tools/validate_dim.py source
    python3 tools/validate_dim.py all
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_FILES = list((ROOT / ".workflows").glob("*/research/_run.md"))
SESSION = (max(RUN_FILES, key=lambda path: path.stat().st_mtime).parents[1]
           if RUN_FILES else ROOT / ".workflows/session-not-found")
RESEARCH = SESSION / "research"

LEGAL_CONF = {"confirmed", "unverified", "CONFLICT"}
REQUIRED_SECTIONS = ("### 结论与证据", "### 已尝试但未覆盖", "### 未执行")


def parse_outline():
    """从 _run.md 解析提纲叶节点 -> {序号: (优先级, 标题)}。"""
    t = (RESEARCH / "_run.md").read_text(encoding="utf-8")
    m = re.search(r"(?ms)^## Research Outline\s*\n(.*)$", t)
    if not m:
        raise SystemExit("_run.md 中找不到 '## Research Outline'")
    out = {}
    for n, pri, title in re.findall(
            r"(?m)^\s*-?\s*#(\d+) \[(P\d)\] \[leaf\] (.+)$", m.group(1)):
        out[int(n)] = (pri, title.strip())
    return out


def parse_report(path):
    """解析维度报告 -> {序号: (优先级, 标题, 正文)}。"""
    t = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## 子问题 #(\d+)（(P\d)）：(.+)$", t)
    if len(parts) < 4:
        return {}, t
    out = {}
    for i in range(1, len(parts), 4):
        n = int(parts[i])
        body = parts[i + 3] if i + 3 < len(parts) else ""
        out[n] = (parts[i + 1], parts[i + 2].strip(), body)
    return out, t


def check(dim):
    path = RESEARCH / f"{dim}.md"
    errs, warns = [], []
    if not path.exists():
        return [f"{dim}.md 不存在"], []

    outline = parse_outline()
    report, raw = parse_report(path)

    # 1. 叶节点齐全且编号一致
    missing = sorted(set(outline) - set(report))
    extra = sorted(set(report) - set(outline))
    if missing:
        errs.append(f"缺失叶节点: {missing}")
    if extra:
        errs.append(f"多余叶节点: {extra}")

    # 2. 优先级与标题逐字一致
    for n in sorted(set(outline) & set(report)):
        pri_o, title_o = outline[n]
        pri_r, title_r, _ = report[n]
        if pri_o != pri_r:
            errs.append(f"#{n} 优先级不符: 提纲 {pri_o} vs 报告 {pri_r}")
        if title_o != title_r:
            errs.append(f"#{n} 标题不符:\n      提纲: {title_o}\n      报告: {title_r}")

    # 3. 三个必需小节齐全
    for n in sorted(report):
        body = report[n][2]
        for sec in REQUIRED_SECTIONS:
            if sec not in body:
                errs.append(f"#{n} 缺少小节 {sec}")

    # 4. 置信度合法
    for conf in re.findall(r"置信度[:：](\S+)", raw):
        if conf not in LEGAL_CONF:
            errs.append(f"非法置信度: {conf}（只允许 {sorted(LEGAL_CONF)}）")

    # 5. 每条非空结论必须有置信度与来源
    for n in sorted(report):
        body = report[n][2]
        # 抽 "### 结论与证据" 段
        m = re.search(r"### 结论与证据(.*?)(?=###|\Z)", body, re.S)
        if not m:
            continue
        seg = m.group(1)
        if re.search(r"(?m)^\s*-\s*无\s*$", seg):
            continue                      # 合法空结果
        concl = re.findall(r"(?m)^\s{2,}-\s+(\S.*)$", seg)
        if concl and not re.search(r"置信度[:：]", seg):
            errs.append(f"#{n} 有结论但无置信度标记")
        if concl and not re.search(r"来源[:：]", seg):
            errs.append(f"#{n} 有结论但无来源标记")

    # 6. 模板残留
    for bad, label in (("[[REPLACE", "REPLACE 占位符"),
                       ("<真实时间戳>", "时间戳占位符")):
        if bad in raw:
            errs.append(f"存在{label}残留")
    for i, line in enumerate(raw.splitlines(), 1):
        if line.strip() in ("...", "…"):
            errs.append(f"第 {i} 行是独占行省略号")
        # 斜杠枚举占位
        if re.search(r"置信度[:：]\S*/\S*", line) or \
           re.search(r"(P0/P1/P2|HIGH/MED/LOW|confirmed/unverified)", line):
            errs.append(f"第 {i} 行存在斜杠枚举占位")

    # 7. 元数据
    for key in ("run_id", "session_id", "tool call 总次数", "树形覆盖率"):
        if not re.search(rf"(?m)^- {re.escape(key)}[:：]", raw):
            errs.append(f"元数据缺少 {key}")
    m = re.search(r"(?m)^- 树形覆盖率[:：](\d+)/(\d+)$", raw)
    if m:
        cov, tot = int(m.group(1)), int(m.group(2))
        if cov != tot:
            errs.append(f"树形覆盖率不足: {cov}/{tot}")
        if tot != len(outline):
            errs.append(f"覆盖率分母 {tot} 与提纲叶节点数 {len(outline)} 不符")

    # 8. run_id 与当前 run 一致（防旧轮次残留）
    run = (RESEARCH / "_run.md").read_text(encoding="utf-8")
    cur = re.search(r"(?m)^- run_id[:：]\s*(\S+)$", run)
    got = re.search(r"(?m)^- run_id[:：]\s*(\S+)$", raw)
    if cur and got and cur.group(1) != got.group(1):
        errs.append(f"run_id 不匹配: 当前 {cur.group(1)} vs 报告 {got.group(1)}")

    # 警告：规模过小
    if len(raw) < 2000:
        warns.append(f"文件偏小（{len(raw)} 字符），可能覆盖不足")
    return errs, warns


def main():
    dims = sys.argv[1:] or ["all"]
    if dims == ["all"]:
        dims = ["web", "memory", "source"]
    total_err = 0
    for d in dims:
        errs, warns = check(d)
        total_err += len(errs)
        status = "PASS" if not errs else "FAIL"
        print(f"[{status}] {d}  (错误 {len(errs)}, 警告 {len(warns)})")
        for e in errs:
            print(f"   ❌ {e}")
        for w in warns:
            print(f"   ⚠️  {w}")
    print()
    print(f"合计错误 {total_err}")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
