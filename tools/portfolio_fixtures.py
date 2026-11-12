"""Structured model fixture; financial values are supplied by application calculations."""
import json

def complete_report(system, user, **kwargs):
    payload=json.loads(user)
    section=dict(judgment='待研究证据确认',evidence='引用已有日期资料',trigger='完成资金与行情核对',invalid='资料失效或边界变化',gaps='交易费用及规则待核实')
    return json.dumps(dict(complete=True,account=section,
        holdings=[dict(section,code=p['code']) for p in payload['workbench']['products']],
        methods=['按期且现金充足时比较；资金需求变化停止','仅共享预算内分批；条件失效停止',
                 '偏离目标且费用可核对时比较；流动性不足停止','证据不足时暂缓；资料完整后复核']),ensure_ascii=False)
