#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chaogu —— A股短线情绪周期复盘工具。

模块划分：
    config      配置与阈值
    net         HTTP（重试/限速/缓存）
    db          SQLite 存储
    sources     数据源（同花顺 / 东方财富 / 新浪）
    collect     采集编排
    metrics     情绪周期指标计算
    theme       题材：延续性、新题材侦测、龙头识别
    forecast    预测：相似周期匹配
    report      报告生成
"""

__version__ = "0.1.0"
