#!/usr/bin/env python3
"""看板存储离线测试：标签顺序、改名、复制、删除、旧文件迁移。

只使用临时目录里的 dashboards.json，不读写用户真实的 data/。

用法:
    python3 tools/test_dashboards.py
退出码 0 表示全部通过。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from chaogu import panels  # noqa: E402

FAILED = []
WORKSPACES = ["information", "learning", "portfolio", "archive"]


def check(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(name)


def use_path(tmp):
    """把模块的存储路径切到临时目录。"""
    path = pathlib.Path(tmp) / "dashboards.json"
    panels.DASH_PATH = path
    return path


def ids(ds):
    return [d["id"] for d in ds]


def legacy_file(path):
    """模拟旧版文件：没有 tab_order，review 排在最前，缺少部分迁移面板。"""
    def p(pid):
        return {"uid": pid[:4], "id": pid, "title": panels.BY_ID[pid]["title"],
                "w": 6, "h": 4, "params": {}}
    data = {"dashboards": [
        {"id": "review", "title": "盘后复盘 · 明日作战", "builtin": True,
         "panels": [p("verdict"), p("outlook")]},
        {"id": "emotion", "title": "情绪周期", "builtin": True, "panels": [p("market_stats")]},
        {"id": "information", "title": "信息中心", "builtin": True,
         "panels": [p("market_stats"), p("news")]},
        {"id": "learning", "title": "短线学习", "builtin": True, "panels": [p("verdict")]},
        {"id": "archive", "title": "复盘档案", "builtin": True, "panels": [p("emotion_trend")]},
        {"id": "portfolio", "title": "长期理财", "builtin": True,
         "panels": [p("portfolio_account"), p("market_stats")]},
        {"id": "live", "title": "盘中实时", "builtin": True, "panels": [p("news")]},
    ]}
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def test_fresh_install(tmp):
    path = use_path(tmp)
    ds = panels.load_dashboards()
    check("首次运行生成默认看板", path.exists())
    check("默认顺序：四个工作区在前", ids(ds)[:4] == WORKSPACES, str(ids(ds)))
    saved = json.loads(path.read_text("utf-8"))
    check("写入 tab_order 版本号", saved.get("tab_order") == panels.TAB_ORDER_VERSION)


def test_legacy_migration(tmp):
    path = use_path(tmp)
    legacy_file(path)
    ds = panels.load_dashboards()
    check("旧文件迁移后工作区仍在前", ids(ds)[:4] == WORKSPACES, str(ids(ds)))
    check("旧文件的非工作区看板保留", ids(ds)[4:] == ["review", "emotion", "live"], str(ids(ds)))
    by = {d["id"]: d for d in ds}
    check("迁移补入 watch_center", any(p["id"] == "watch_center" for p in by["learning"]["panels"]))
    check("迁移补入 news_impact", any(p["id"] == "news_impact" for p in by["review"]["panels"]))
    check("迁移移除理财页的市场面板",
          [p["id"] for p in by["portfolio"]["panels"]] == ["portfolio_account"])
    check("迁移后写回版本号",
          json.loads(path.read_text("utf-8")).get("tab_order") == panels.TAB_ORDER_VERSION)

    # 用户删除的面板不应在之后的读取中被自动补回。
    learning = by["learning"]
    learning["panels"] = [p for p in learning["panels"] if p["id"] != "watch_center"]
    panels.save_dashboard(learning)
    ds2 = panels.load_dashboards()
    again = next(d for d in ds2 if d["id"] == "learning")
    check("用户删除的面板不会被自动补回",
          not any(p["id"] == "watch_center" for p in again["panels"]))


def test_rename_persists(tmp):
    path = use_path(tmp)
    ds = panels.load_dashboards()
    info = next(d for d in ds if d["id"] == "information")
    info["title"] = "我的信息页"
    panels.save_dashboard(info)
    ds2 = panels.load_dashboards()
    check("工作区改名后保持新名称", next(d for d in ds2 if d["id"] == "information")["title"] == "我的信息页")
    check("改名不改变标签位置", ids(ds2)[0] == "information")


def test_save_keeps_position(tmp):
    path = use_path(tmp)
    ds = panels.load_dashboards()
    before = ids(ds)
    learning = next(d for d in ds if d["id"] == "learning")
    learning["panels"] = learning["panels"][:2]
    panels.save_dashboard(learning)
    check("保存中间看板不会把它挪到末尾", ids(panels.load_dashboards()) == before)


def test_reorder(tmp):
    path = use_path(tmp)
    ds = panels.load_dashboards()
    target = list(reversed(ids(ds)))
    panels.reorder_dashboards(target)
    check("按给定顺序排列", ids(panels.load_dashboards()) == target)

    panels.reorder_dashboards(["archive", "no_such_board", "information"])
    got = ids(panels.load_dashboards())
    check("未知 id 被忽略", "no_such_board" not in got)
    check("提及的看板排在最前", got[:2] == ["archive", "information"], str(got))
    check("未提及的看板保留且不丢失", sorted(got) == sorted(target))

    try:
        panels.reorder_dashboards("not-a-list")
        check("非法 ids 报错", False)
    except ValueError:
        check("非法 ids 报错", True)


def test_duplicate(tmp):
    path = use_path(tmp)
    ds = panels.load_dashboards()
    src = next(d for d in ds if d["id"] == "learning")
    src_uids = {p["uid"] for p in src["panels"]}
    copy = panels.duplicate_dashboard("learning")
    after = ids(panels.load_dashboards())
    check("副本出现在源看板右侧", after[after.index("learning") + 1] == copy["id"])
    check("副本是自定义看板", copy["builtin"] is False)
    check("副本标题带 副本 后缀", copy["title"].endswith(" 副本"), copy["title"])
    check("副本面板 uid 重新生成",
          not ({p["uid"] for p in copy["panels"]} & src_uids))
    check("副本面板数量一致", len(copy["panels"]) == len(src["panels"]))
    try:
        panels.duplicate_dashboard("missing")
        check("复制不存在的看板报错", False)
    except ValueError:
        check("复制不存在的看板报错", True)


def test_delete_rules(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    try:
        panels.delete_dashboard("information")
        check("工作区看板不能删除", False)
    except ValueError:
        check("工作区看板不能删除", True)

    custom = panels.duplicate_dashboard("learning")
    panels.delete_dashboard(custom["id"])
    check("自定义看板可以删除", custom["id"] not in ids(panels.load_dashboards()))

    legacy_file(path)
    panels.load_dashboards()
    panels.delete_dashboard("review")
    check("旧版遗留看板可以删除", "review" not in ids(panels.load_dashboards()))


def test_client_cannot_mark_builtin(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    saved = panels.save_dashboard({"id": "mine", "title": "我的看板", "builtin": True,
                                   "panels": []})
    check("客户端不能把新看板标记为内置", saved["builtin"] is False)


def test_corrupt_file_is_backed_up(tmp):
    path = use_path(tmp)
    path.write_text("{ 这不是 json", "utf-8")
    ds = panels.load_dashboards()
    backups = list(pathlib.Path(tmp).glob("dashboards.json.broken-*"))
    check("损坏文件先备份", len(backups) == 1)
    check("损坏后恢复默认看板", ids(ds)[:4] == WORKSPACES)
    if backups:
        check("备份内容与原文件一致", backups[0].read_text("utf-8") == "{ 这不是 json")


def test_reset(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    panels.duplicate_dashboard("learning")
    ds = panels.reset_dashboards()
    check("恢复默认会移除自定义看板", ids(ds) == WORKSPACES,
          str(ids(ds)))


def test_panel_metadata_complete(tmp):
    use_path(tmp)
    check("目录与查询函数一一对应", set(panels.QUERIES) == set(panels.BY_ID))
    missing = [p["id"] for p in panels.CATALOG if not p.get("basis")]
    check("每个面板都有口径说明", not missing, str(missing))
    bad = [p["id"] for p in panels.CATALOG if p.get("trust") not in panels.TRUST]
    check("每个面板的可信度都在 TRUST 中", not bad, str(bad))
    check("已验证的只有席位跟随",
          [p["id"] for p in panels.CATALOG if p["trust"] == "verified"] == ["seats"])
    check("题材/龙头类面板不标为已验证",
          panels.BY_ID["ladder"]["trust"] != "verified" and panels.BY_ID["mainlines"]["trust"] != "verified")
    undeclared = [pid for pid, m in panels.META.items()
                  if set(m.get("param_types", {})) - set(panels.BY_ID[pid].get("params") or {})]
    check("参数规格只针对已声明的参数", not undeclared, str(undeclared))


def test_param_cleaning(tmp):
    use_path(tmp)
    got, dropped = panels.clean_params("line_trend", {"keywords": "机器人, 固态电池,,", "days": "999", "latest": True})
    check("列表参数按逗号拆分并去空", got.get("keywords") == ["机器人", "固态电池"], str(got))
    check("整数参数被限制在范围内", got.get("days") == 120, str(got))
    check("运行时参数 latest 被丢弃", dropped == ["latest"], str(dropped))
    got, dropped = panels.clean_params("news", {"limit": "abc", "keyword": "  "})
    check("无法转换的参数被丢弃", got == {} and set(dropped) == {"limit", "keyword"}, str(got))
    got, _ = panels.clean_params("outlook", {"top": 0})
    check("整数下限生效", got == {"top": 1}, str(got))
    got, dropped = panels.clean_params("verdict", {"anything": 1})
    check("未声明参数的面板全部丢弃", got == {} and dropped == ["anything"])
    got, _ = panels.clean_params("outlook", {"top": True})
    check("布尔值不被当作整数", "top" not in got)


def test_saved_panels_are_cleaned(tmp):
    use_path(tmp)
    panels.load_dashboards()
    saved = panels.save_dashboard({"id": "clean", "title": "清洗测试", "panels": [
        {"id": "news", "params": {"latest": True, "limit": 500, "keyword": "机器人"}},
        {"id": "no_such_panel", "params": {}},
    ]})
    check("保存时丢弃未知面板", len(saved["panels"]) == 1)
    params = saved["panels"][0]["params"]
    check("保存时参数被清洗", params == {"limit": 200, "keyword": "机器人"}, str(params))
    stored = next(d for d in panels.load_dashboards() if d["id"] == "clean")
    check("清洗结果已写入文件", stored["panels"][0]["params"] == params)


def test_export_is_clean(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    before = path.read_text("utf-8")
    doc = panels.export_dashboard("learning")
    check("导出格式标记正确", doc["format"] == "chaogu-dashboard" and doc["schema_version"] == panels.EXPORT_VERSION)
    check("导出不改写看板文件", path.read_text("utf-8") == before)
    blob = json.dumps(doc, ensure_ascii=False)
    check("导出不含 uid 与内置标记", '"uid"' not in blob and '"builtin"' not in blob)
    check("导出保留面板参数", all("params" in p for p in doc["dashboard"]["panels"]))
    try:
        panels.export_dashboard("missing")
        check("导出不存在的看板报错", False)
    except ValueError:
        check("导出不存在的看板报错", True)


def test_import_preview_reports_problems(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    before = path.read_text("utf-8")
    doc = panels.export_dashboard("learning")
    doc["dashboard"]["panels"].append({"id": "ghost", "title": "旧面板", "params": {}})
    doc["dashboard"]["panels"].append({"id": "outlook", "params": {"top": 99, "latest": True}})
    res = panels.preview_import(doc)
    text = "\n".join(res["warnings"])
    check("未知面板被报告并跳过", "ghost" in text and res["skipped"] == 1, text[:80])
    check("不支持的参数被报告", "latest" in text)
    check("越界整数被报告并调整", "已调整为 10" in text)
    check("预览不写入文件", path.read_text("utf-8") == before)
    d = res["dashboard"]
    check("导入结果分配新 id", d["id"] != "learning" and d["id"].startswith("d"))
    check("导入结果不是内置看板", d["builtin"] is False)
    outlook = [p for p in d["panels"] if p["id"] == "outlook"][-1]   # 追加的那个排在最后
    check("导入参数已清洗", outlook["params"] == {"top": 10}, str(outlook["params"]))


def test_import_rejects_bad_input(tmp):
    use_path(tmp)
    good = panels.export_dashboard("learning") if False else {
        "format": "chaogu-dashboard", "schema_version": 1,
        "dashboard": {"title": "测试", "panels": [{"id": "verdict"}]}}
    cases = {
        "非对象": ["x"],
        "格式不符": {**good, "format": "other"},
        "缺少版本": {k: v for k, v in good.items() if k != "schema_version"},
        "版本过高": {**good, "schema_version": 99},
        "缺少看板内容": {**good, "dashboard": None},
        "面板不是列表": {**good, "dashboard": {"title": "x", "panels": "bad"}},
        "名称为空": {**good, "dashboard": {"title": "  ", "panels": []}},
        "面板超量": {**good, "dashboard": {"title": "x", "panels": [{"id": "verdict"}] * (panels.MAX_IMPORT_PANELS + 1)}},
    }
    for name, payload in cases.items():
        try:
            panels.preview_import(payload)
            check(f"拒绝：{name}", False)
        except ValueError:
            check(f"拒绝：{name}", True)
    res = panels.preview_import(good)
    check("合法的最小文件可以通过", res["dashboard"]["title"] == "测试" and len(res["dashboard"]["panels"]) == 1)


def test_roundtrip(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    doc = panels.export_dashboard("learning")
    res = panels.preview_import(doc)
    saved = panels.save_dashboard(res["dashboard"])
    loaded = next(d for d in panels.load_dashboards() if d["id"] == saved["id"])
    check("往返导出导入面板一致",
          [(p["id"], p["params"], p["x"], p["y"], p["w"], p["h"]) for p in loaded["panels"]] ==
          [(p["id"], p["params"], p["x"], p["y"], p["w"], p["h"]) for p in doc["dashboard"]["panels"]])
    check("往返导入不产生警告", res["warnings"] == [])


def _overlap(ps):
    """返回第一对相互重叠的面板 id；没有重叠时返回 None。"""
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            a, b = ps[i], ps[j]
            if (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
                    a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]):
                return (a["id"], b["id"])
    return None


def test_layout_flow_matches_reading_order(tmp):
    use_path(tmp)
    ds = panels.load_dashboards()
    info = next(d for d in ds if d["id"] == "information")
    ps = info["panels"]
    check("每块面板都有整数坐标",
          all(isinstance(p.get("x"), int) and isinstance(p.get("y"), int) for p in ps))
    check("首块从第 0 列第 0 行开始", (ps[0]["x"], ps[0]["y"]) == (0, 0), str((ps[0]["x"], ps[0]["y"])))
    news = next(p for p in ps if p["id"] == "news")
    mainlines = next(p for p in ps if p["id"] == "mainlines")
    check("宽 10 与宽 14 的两块面板落在同一行", news["y"] == mainlines["y"],
          str((news["y"], mainlines["y"])))
    check("同行按顺序紧邻排布", news["x"] + news["w"] == mainlines["x"],
          str((news["x"], news["w"], mainlines["x"])))
    check("坐标不超出行宽", all(p["x"] + p["w"] <= panels.GRID_COLUMNS for p in ps))
    check("默认看板没有重叠", _overlap(ps) is None, str(_overlap(ps)))


def test_layout_compact_pulls_panels_up(tmp):
    use_path(tmp)
    panels.load_dashboards()
    saved = panels.save_dashboard({"id": "ov", "title": "重叠测试", "builtin": False, "panels": [
        {"id": "verdict", "w": 24, "h": 6, "x": 0, "y": 0, "params": {}},
        {"id": "ladder", "w": 12, "h": 8, "x": 0, "y": 0, "params": {}},
        {"id": "seats", "w": 12, "h": 8, "x": 0, "y": 2, "params": {}},
    ]})
    ps = {p["id"]: p for p in saved["panels"]}
    check("重叠被消除", _overlap(saved["panels"]) is None, str(_overlap(saved["panels"])))
    check("先声明的面板留在顶部", ps["verdict"]["y"] == 0)
    check("被压住的面板下移到整行面板下方",
          ps["ladder"]["y"] >= ps["verdict"]["y"] + ps["verdict"]["h"], str(ps["ladder"]["y"]))
    check("第三块继续往下让位", ps["seats"]["y"] >= ps["ladder"]["y"], str(ps["seats"]["y"]))


def test_layout_clamps_out_of_range(tmp):
    use_path(tmp)
    panels.load_dashboards()
    saved = panels.save_dashboard({"id": "cl", "title": "越界测试", "builtin": False, "panels": [
        {"id": "verdict", "w": 24, "h": 99, "x": 20, "y": 0, "params": {}},
        {"id": "ladder", "w": 1, "h": 4, "x": 0, "y": 0, "params": {}},
    ]})
    ps = {p["id"]: p for p in saved["panels"]}
    check("高度上限被夹取", ps["verdict"]["h"] == panels.MAX_PANEL_H, str(ps["verdict"]["h"]))
    check("整行宽度的列起点归零", ps["verdict"]["x"] == 0, str(ps["verdict"]["x"]))
    check("宽度下限被夹取", ps["ladder"]["w"] == panels.MIN_PANEL_W, str(ps["ladder"]["w"]))
    check("夹取后仍不溢出栅格",
          all(p["x"] + p["w"] <= panels.GRID_COLUMNS for p in saved["panels"]))


def _downgrade_to_v2(path):
    """把 v3 文件降级成"有版本号但面板没有坐标"，模拟从上一版升级上来。"""
    raw = json.loads(path.read_text("utf-8"))
    raw["tab_order"] = 2
    for d in raw["dashboards"]:
        for p in d["panels"]:
            p.pop("x", None)
            p.pop("y", None)
    path.write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    return raw


def test_layout_migration_backs_up_once_and_preserves_panels(tmp):
    path = use_path(tmp)
    panels.load_dashboards()
    raw = _downgrade_to_v2(path)
    before = {d["id"]: [(p["uid"], p["id"], p["w"], p["h"], p["params"]) for p in d["panels"]]
              for d in raw["dashboards"]}

    ds = panels.load_dashboards()
    backups = sorted(path.parent.glob("dashboards.json.before-layout-*.bak"))
    check("迁移前留下了一份备份", len(backups) == 1, str([b.name for b in backups]))
    check("备份保存的是迁移前的原文件",
          json.loads(backups[0].read_text("utf-8"))["tab_order"] == 2)
    check("版本号已写回",
          json.loads(path.read_text("utf-8")).get("tab_order") == panels.TAB_ORDER_VERSION)
    check("迁移后每块面板都有坐标",
          all(isinstance(p.get("x"), int) and isinstance(p.get("y"), int)
              for d in ds for p in d["panels"]))
    after = {d["id"]: [(p["uid"], p["id"], p["w"], p["h"], p["params"]) for p in d["panels"]]
             for d in ds}
    check("面板集合、顺序、尺寸与参数一个都没变",
          all(before[k] == after[k] for k in before if k in after),
          str([k for k in before if k in after and before[k] != after[k]]))
    panels.load_dashboards()
    check("再次读取不重复备份",
          len(sorted(path.parent.glob("dashboards.json.before-layout-*.bak"))) == 1)


def test_layout_migration_does_not_readd_deleted_panel(tmp):
    path = use_path(tmp)
    ds = panels.load_dashboards()
    learning = next(d for d in ds if d["id"] == "learning")
    learning["panels"] = [p for p in learning["panels"] if p["id"] != "watch_center"]
    panels.save_dashboard(learning)
    _downgrade_to_v2(path)
    after = panels.load_dashboards()
    got = next(d for d in after if d["id"] == "learning")
    check("带版本号的旧文件不会把用户删掉的面板补回来",
          not any(p["id"] == "watch_center" for p in got["panels"]))


def test_save_preserves_explicit_coordinates(tmp):
    use_path(tmp)
    panels.load_dashboards()
    panels.save_dashboard({"id": "free", "title": "自由布局", "builtin": False, "panels": [
        {"id": "ladder", "w": 12, "h": 8, "x": 12, "y": 0, "params": {}},
        {"id": "seats", "w": 12, "h": 8, "x": 0, "y": 0, "params": {}},
    ]})
    reloaded = next(d for d in panels.load_dashboards() if d["id"] == "free")
    check("显式坐标原样保留",
          {p["id"]: (p["x"], p["y"]) for p in reloaded["panels"]} ==
          {"ladder": (12, 0), "seats": (0, 0)},
          str({p["id"]: (p["x"], p["y"]) for p in reloaded["panels"]}))
    check("并排摆放不被下压", _overlap(reloaded["panels"]) is None)


# ------------------------------------------------------------------ 前后端布局一致性
# app.js 里 mirror 了一份和 panels.py 同名的 compactPanels / clampPanel（拖动时要用它
# 做即时反馈，不能等一个来回的网络请求）。两套实现一旦漂移，用户松手看到的位置和后端
# 保存下来的位置就会不一样。这里把 app.js 里那段源码原样抽出来在 node 里跑一遍对答案。
# node 缺失时整个用例跳过，不让纯 Python 环境跑不了测试。

APP_JS = pathlib.Path(__file__).resolve().parent.parent / "chaogu" / "web" / "app.js"
# 真正的布局算法住在 layout.js（纯函数、不碰 DOM），app.js 的布局段落只是接线。
LAYOUT_JS = pathlib.Path(__file__).resolve().parent.parent / "chaogu" / "web" / "layout.js"
JS_LAYOUT_START = "// ------------------------------------------------------------------ 栅格布局"
JS_LAYOUT_END = "// ------------------------------------------------------------------ 拖拽 & 缩放"

JS_PROBE = """
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const [start, end, layoutPath] = process.argv.slice(3);
const body = src.split(start)[1].split(end)[0];
// 按浏览器里的样子注入 ChaoguLayout：这样既跑到 app.js 真实的接线
// （compactPanels / placeNewPanel / GRID），又跑到真正会发布的算法本身。
const layout = require(layoutPath);
const S = {cur: {panels: []}};
const api = new Function("S", "window", body + "\\nreturn {GRID, clampPanel, compactPanels, placeNewPanel};")(S, {ChaoguLayout: layout});
const input = JSON.parse(fs.readFileSync(0, "utf8"));
S.cur.panels = input.panels;
if (input.op === "place") input.panels.forEach(p => { if (p.x === undefined || p.y === undefined) api.placeNewPanel(p); });
else api.compactPanels(input.active || null);
process.stdout.write(JSON.stringify({GRID: api.GRID, panels: S.cur.panels}));
"""


def _js_layout(panels, op="compact", active=None):
    """在 node 里执行 app.js 的布局接线（算法来自 layout.js）。node 不可用时返回 None。"""
    node = shutil.which("node")
    if not node:
        return None
    src = APP_JS.read_text("utf-8")
    if JS_LAYOUT_START not in src or JS_LAYOUT_END not in src or not LAYOUT_JS.is_file():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        probe = pathlib.Path(tmp) / "probe.js"
        probe.write_text(JS_PROBE, "utf-8")
        r = subprocess.run([node, str(probe), str(APP_JS), JS_LAYOUT_START, JS_LAYOUT_END,
                            str(LAYOUT_JS)],
                           input=json.dumps({"panels": panels, "op": op, "active": active}),
                           capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError("前端布局函数执行失败：" + r.stderr.strip()[:300])
    return json.loads(r.stdout)


def _coords(ps):
    return [(p["id"], p["x"], p["y"], p["w"], p["h"]) for p in ps]


def test_frontend_layout_matches_backend(tmp):
    use_path(tmp)
    js = _js_layout([])
    if js is None:
        check("跳过前后端一致性测试（未找到 node 或缺抽取标记）", True)
        return
    check("栅格尺寸常量与后端一致", js["GRID"] ==
          {"cols": panels.GRID_COLUMNS, "minW": panels.MIN_PANEL_W, "maxW": panels.MAX_PANEL_W,
           "minH": panels.MIN_PANEL_H, "maxH": panels.MAX_PANEL_H}, str(js["GRID"]))

    cases = [
        ("重力把悬空的面板吸到顶部", [
            {"id": "verdict", "w": 24, "h": 6, "x": 0, "y": 0},
            {"id": "ladder", "w": 12, "h": 8, "x": 0, "y": 12},
            {"id": "seats", "w": 12, "h": 8, "x": 12, "y": 12},
        ]),
        ("宽窄混排时各自找第一个空位", [
            {"id": "verdict", "w": 10, "h": 6, "x": 0, "y": 0},
            {"id": "ladder", "w": 14, "h": 4, "x": 10, "y": 0},
            {"id": "seats", "w": 8, "h": 6, "x": 16, "y": 0},
        ]),
        ("横向越界的列起点夹回栅格内", [
            {"id": "verdict", "w": 8, "h": 6, "x": 30, "y": 3},
            {"id": "ladder", "w": 40, "h": 30, "x": -5, "y": -2},
        ]),
        ("大面板把下面的一整排顶下去", [
            {"id": "verdict", "w": 24, "h": 10, "x": 0, "y": 0},
            {"id": "ladder", "w": 6, "h": 3, "x": 0, "y": 0},
            {"id": "seats", "w": 6, "h": 3, "x": 6, "y": 0},
            {"id": "news", "w": 6, "h": 3, "x": 12, "y": 0},
        ]),
    ]
    for name, ps in cases:
        got = _js_layout([dict(p) for p in ps])
        want = panels._solve_layout([dict(p) for p in ps])
        ok = _coords(got["panels"]) == _coords(want)
        check(f"前端与后端布局一致 · {name}", ok,
              "" if ok else f"前端 {_coords(got['panels'])} ≠ 后端 {_coords(want)}")

    # 新面板的落点：必须拿到坐标，否则后端会判定整表缺坐标而重排。
    placed = _js_layout([
        {"id": "verdict", "w": 24, "h": 6, "x": 0, "y": 0},
        {"id": "ladder", "w": 12, "h": 8, "x": 0, "y": 6},
        {"id": "seats", "w": 12, "h": 8},
    ], op="place")
    newbie = placed["panels"][-1]
    check("原有面板的坐标没被新面板挤动",
          (placed["panels"][0]["x"], placed["panels"][0]["y"]) == (0, 0) and
          (placed["panels"][1]["x"], placed["panels"][1]["y"]) == (0, 6))
    check("新面板补上了坐标", isinstance(newbie["x"], int) and isinstance(newbie["y"], int),
          str((newbie["x"], newbie["y"])))
    check("新面板落在已有面板最底边之下", newbie["y"] == 14, str(newbie["y"]))
    check("补完坐标后仍不重叠", _overlap(placed["panels"]) is None, str(_overlap(placed["panels"])))


def test_private_panel_prefix_matching(tmp):
    """私有面板靠前缀判定，前缀写宽了会误伤、写窄了会漏。"""
    for pid in ("portfolio_account", "portfolio_analysis", "watch_center",
                "ai_brief", "ai_answer"):
        check(f"{pid} 判定为私密", panels.is_private_panel(pid))
    # ai_ 带下划线就是为了不误伤 aircraft / aim 这类未来可能出现的名字
    for pid in ("market_stats", "verdict", "news", "ai", "aircraft", "portfolio"):
        check(f"{pid} 判定为公开", not panels.is_private_panel(pid))
    check("PRIVATE_PANEL_PREFIXES 与 dashboard 模块共用同一份定义",
          __import__("chaogu.dashboard", fromlist=["x"]).PRIVATE_PANEL_PREFIXES
          is panels.PRIVATE_PANEL_PREFIXES)


def test_external_visibility(tmp):
    """绑 0.0.0.0 后，内网其他人拿到的看板列表不能含账户、盯盘和 AI 面板。"""
    use_path(tmp)
    ds = panels.load_dashboards()
    # 造一块三类私密面板都占上的看板，确保裁剪是真的在起作用，
    # 而不是因为默认看板里恰好没有私密项而"看起来通过"。
    ds = ds + [{"id": "mixed", "title": "混合", "builtin": False, "panels": [
        {"uid": "m1", "id": "market_stats", "title": "温度", "w": 24, "h": 3,
         "x": 0, "y": 0, "params": {}},
        {"uid": "m2", "id": "portfolio_account", "title": "账户", "w": 24, "h": 10,
         "x": 0, "y": 3, "params": {}},
        {"uid": "m3", "id": "watch_center", "title": "盯盘", "w": 12, "h": 10,
         "x": 0, "y": 13, "params": {}},
        {"uid": "m4", "id": "ai_brief", "title": "AI", "w": 12, "h": 10,
         "x": 12, "y": 13, "params": {}},
    ]}]

    pub = panels.public_dashboards(ds, is_local=False)
    loc = panels.public_dashboards(ds, is_local=True)

    check("本机拿到全部看板", len(loc) == len(ds), f"{len(loc)}/{len(ds)}")
    check("本机看板内容原样保留",
          any(p["id"] == "portfolio_account"
              for d in loc if d["id"] == "mixed" for p in d["panels"]))

    flat = [p["id"] for d in pub for p in d["panels"]]
    leaked = [x for x in flat if panels.is_private_panel(x)]
    check("对外列表不含任何私密面板", not leaked, str(leaked))
    check("公开面板仍然在", "market_stats" in flat, str(len(flat)) + " 块")
    check("混合看板只剩公开面板",
          [p["id"] for d in pub if d["id"] == "mixed" for p in d["panels"]] == ["market_stats"])
    check("整块私密的看板不出现", "portfolio" not in ids(pub), str(ids(pub)))

    # 裁剪用的是 deepcopy：绝不能就地改到真实数据
    check("原数据没被改坏（必须 deepcopy）",
          any(p["id"] == "portfolio_account"
              for d in ds if d["id"] == "mixed" for p in d["panels"]))
    check("原数据的面板数没变",
          [len(d["panels"]) for d in ds if d["id"] == "mixed"] == [4])

    for d in pub:
        ov = _overlap(d["panels"])
        check(f"{d['id']} 裁剪后无重叠", ov is None, str(ov))
        over = [p["id"] for p in d["panels"]
                if p["x"] + p["w"] > panels.GRID_COLUMNS or p["x"] < 0]
        check(f"{d['id']} 裁剪后不越界", not over, str(over))
        check(f"{d['id']} 裁剪后留下的面板都有坐标",
              all(isinstance(p.get("x"), int) and isinstance(p.get("y"), int)
                  for p in d["panels"]))

    cat_loc = panels.visible_catalog(True)
    cat_pub = panels.visible_catalog(False)
    check("本机目录是完整的", cat_loc == panels.CATALOG)
    check("对外目录不含私密项",
          not [c for c in cat_pub if panels.is_private_panel(c["id"])])
    check("对外目录确实变短", len(cat_pub) < len(cat_loc),
          f"{len(cat_pub)} < {len(cat_loc)}")


def main():
    cases = [test_fresh_install, test_legacy_migration, test_rename_persists,
             test_save_keeps_position, test_reorder, test_duplicate, test_delete_rules,
             test_client_cannot_mark_builtin, test_corrupt_file_is_backed_up, test_reset,
             test_panel_metadata_complete, test_param_cleaning, test_saved_panels_are_cleaned,
             test_export_is_clean, test_import_preview_reports_problems, test_import_rejects_bad_input,
             test_roundtrip, test_layout_flow_matches_reading_order, test_layout_compact_pulls_panels_up,
             test_layout_clamps_out_of_range, test_layout_migration_backs_up_once_and_preserves_panels,
             test_layout_migration_does_not_readd_deleted_panel, test_save_preserves_explicit_coordinates,
             test_frontend_layout_matches_backend,
             test_private_panel_prefix_matching, test_external_visibility]
    for fn in cases:
        print(fn.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            fn(tmp)
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
