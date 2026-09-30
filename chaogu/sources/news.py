#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新浪财经新闻直播流（7x24 快讯）。

用于捕捉"外围新闻 / 突发催化"，并可对题材做关键词匹配。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

from ..net import get_json

CST = timezone(timedelta(hours=8))
FEED = "https://zhibo.sina.com.cn/api/zhibo/feed"

# 关注的外围/宏观关键词（用于打标签）
GLOBAL_KEYWORDS = [
    "美联储", "降息", "加息", "鲍威尔", "非农", "CPI", "PCE", "议息",
    "美股", "纳斯达克", "道琼斯", "标普", "英伟达", "台积电", "苹果",
    "关税", "特朗普", "白宫", "制裁", "出口管制", "实体清单",
    "原油", "OPEC", "黄金", "铜", "锂", "稀土",
    "人民币", "汇率", "央行", "降准", "MLF", "LPR", "国债",
    "港股", "恒生", "北向", "外资",
    "国常会", "政治局", "发改委", "工信部", "证监会", "财政部",
]

# 政策/产业催化关键词
CATALYST_KEYWORDS = [
    "规划", "方案", "意见", "通知", "试点", "补贴", "招标", "中标",
    "量产", "投产", "扩产", "涨价", "提价", "订单", "签约", "合作",
    "突破", "研发成功", "获批", "上市", "过会", "重组", "并购",
]


def fetch_news(page_size=50, zhibo_id=152):
    """拉取快讯。zhibo_id=152 财经，6 为全球直播。"""
    url = (f"{FEED}?page=1&page_size={page_size}&zhibo_id={zhibo_id}"
           f"&tag_id=0&dire=f&dpc=1")
    j = get_json(url, referer="https://finance.sina.com.cn/")
    feed = (((j.get("result") or {}).get("data") or {}).get("feed") or {})
    out = []
    for it in feed.get("list") or []:
        raw = (it.get("rich_text") or "").strip()
        if not raw:
            continue
        title, body = _split_title(raw)
        ts = it.get("create_time") or ""
        out.append({
            "id": str(it.get("id") or ""),
            "ts": _norm_ts(ts),
            "title": title,
            "content": body,
            "source": "新浪财经",
            "url": it.get("docurl") or "",
            "tags": _tags(raw),
        })
    return out


def _split_title(raw):
    """新浪快讯常以【标题】正文 形式组织。"""
    m = re.match(r"^【(.+?)】\s*(.*)$", raw, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw[:60].strip(), raw


def _norm_ts(ts):
    if not ts:
        return ""
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ts


def _tags(text):
    hit = [k for k in GLOBAL_KEYWORDS if k in text]
    hit += [k for k in CATALYST_KEYWORDS if k in text]
    return ",".join(hit)


def score_news_sentiment(items):
    """极简情绪打分：利好词/利空词计数。仅作参考。"""
    pos = ["上涨", "大涨", "利好", "突破", "增长", "超预期", "回暖",
           "创新高", "扩产", "中标", "获批", "降准", "降息", "支持"]
    neg = ["下跌", "大跌", "利空", "暴跌", "下滑", "不及预期", "亏损",
           "违约", "退市", "处罚", "调查", "加息", "收紧", "制裁"]
    p = n = 0
    for it in items:
        t = (it.get("title") or "") + (it.get("content") or "")
        p += sum(1 for w in pos if w in t)
        n += sum(1 for w in neg if w in t)
    total = p + n
    return {
        "positive": p, "negative": n,
        "score": round((p - n) / total, 3) if total else 0.0,
        "n": len(items),
    }
