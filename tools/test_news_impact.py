"""Offline tests: dates, evidence scope, completeness, cache and failed reports."""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chaogu import db,news_impact as n,panels


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.c=db.init(Path(self.tmp.name)/'market.db')
        self.root=patch.object(n,'ROOT',Path(self.tmp.name)/'reports');self.root.start()
        self.c.execute("INSERT INTO stock_basic(code,name) VALUES('600001','测试公司')")
        for id,ts,title,url in [('a','2026-09-29 10:00:00','测试公司澄清市场传闻','https://example.com/a'),
                                 ('b','2026-09-29T11:00:00','测试公司澄清市场传闻','https://example.com/b'),
                                 ('c','2026-09-30T09:00:00','测试公司获大单','javascript:alert(1)')]:
            self.c.execute('INSERT INTO news(id,ts,title,content,source,url) VALUES(?,?,?,?,?,?)',(id,ts,title,title,'测试来源',url))
        self.c.commit()
        n._JOBS.clear()
    def tearDown(self):
        self.c.close();self.root.stop();self.tmp.cleanup()
    def report(self,f):
        fields=dict(direction='暂无法判断',judgment='传闻被否认',mechanism='缺订单原始证据',confirm='核对公告',invalidate='澄清否认',horizon='尚不确定',gaps='缺公告')
        return {'complete':True,'events':[dict(fields,id=e['id'],stocks=[dict(fields,code=c['code']) for c in e['candidates']]) for e in f['events']]}
    def test_date_dedup_rumor(self):
        f=n.facts(self.c,'2026-09-29');self.assertEqual(len(f['events']),1)
        self.assertTrue(f['events'][0]['rumor']);self.assertEqual(f['events'][0]['candidates'][0]['code'],'600001')
        self.assertTrue(f['events'][0]['id'].startswith('ev-'))
        self.assertNotEqual(f['events'][0]['id'],'b')
        self.assertEqual(f['events'][0]['primary_news_id'],'b')
        self.assertEqual(f['events'][0]['source_count'],2)
        self.assertEqual(len(f['events'][0]['sources']),2)
        self.assertEqual(f['events'][0]['evidence_level'],'传闻/辟谣')
        self.assertEqual(f['events'][0]['status'],'待验证')
        self.assertIn('暂无事件后的交易日',f['events'][0]['verification']['summary'])
    def test_verification_marks_following_signal(self):
        self.c.execute("INSERT INTO quote_daily(trade_date,code,name,pct,turnover) VALUES('2026-09-30','600001','测试公司',6.2,3.1)")
        self.c.execute("INSERT INTO theme_stock(trade_date,tag,code,name,reason,reason_info,continue_num,height_text) VALUES('2026-09-30','测试题材','600001','测试公司','测试题材','后续题材再出现',2,'2板')")
        f=n.facts(self.c,'2026-09-29')
        e=f['events'][0]
        self.assertEqual(e['status'],'已验证')
        self.assertEqual(e['verification']['status'],'已验证')
        self.assertTrue(e['verification']['checks'][0]['signal'])
        self.assertEqual(e['verification']['checks'][0]['horizon'],'T+1')
        self.assertEqual(e['verification']['checks'][0]['theme_count'],1)
    def test_duplicate_sources_do_not_change_signature(self):
        f=n.facts(self.c,'2026-09-29')
        self.c.execute('INSERT INTO news(id,ts,title,content,source,url) VALUES(?,?,?,?,?,?)',
                       ('d','2026-09-29T12:00:00','测试公司澄清市场传闻','测试公司澄清市场传闻','测试来源','https://example.com/d'))
        later=n.facts(self.c,'2026-09-29')
        self.assertEqual(later['events'][0]['id'],f['events'][0]['id'])
        self.assertEqual(later['events'][0]['source_count'],3)
        self.assertEqual(later['signature'],f['signature'])
    def test_direct_entities_do_not_cross_merge(self):
        self.c.execute("INSERT INTO stock_basic(code,name) VALUES('600002','对照公司')")
        self.c.execute('INSERT INTO news(id,ts,title,content,source,url) VALUES(?,?,?,?,?,?)',
                       ('d','2026-09-29T12:00:00','测试公司发布公告','测试公司发布公告','测试来源','https://example.com/d'))
        self.c.execute('INSERT INTO news(id,ts,title,content,source,url) VALUES(?,?,?,?,?,?)',
                       ('e','2026-09-29T12:01:00','对照公司发布公告','对照公司发布公告','测试来源','https://example.com/e'))
        f=n.facts(self.c,'2026-09-29')
        for event in f['events']:
            codes={c['code'] for c in event['candidates']}
            self.assertFalse({'600001','600002'} <= codes)
    def test_no_invented_candidates(self):
        self.c.execute('DELETE FROM stock_basic');f=n.facts(self.c,'2026-09-29')
        self.assertEqual(f['events'][0]['candidates'],[])
    def test_future_theme_does_not_create_candidate(self):
        self.c.execute("INSERT INTO theme_stock(trade_date,tag,code,name,reason,reason_info) VALUES('2026-09-30','x','000002','未来公司','人工智能','未来披露')")
        self.c.execute("UPDATE news SET title='人工智能行业消息',content='人工智能行业消息' WHERE id='b'")
        f=n.facts(self.c,'2026-09-29')
        self.assertFalse(any(c['code']=='000002' for e in f['events'] for c in e['candidates']))
    def test_facts_contain_public_fields_only(self):
        f=n.facts(self.c,'2026-09-29')
        text=json.dumps(f)
        for key in ('password','cookie','account','balance','token'):
            self.assertNotIn(key,text.lower())
    def test_bad_url(self):
        self.assertEqual(n.safe_url('javascript:alert(1)'),'')
        self.assertEqual(n.safe_url('https://user:password@example.com'),'')
    def test_reject_partial_and_invented(self):
        f=n.facts(self.c,'2026-09-29');r=self.report(f);n.validate(r,f)
        for mutation in ('missing_event','missing_stock','unknown_stock','incomplete','missing_condition'):
            bad=copy.deepcopy(r)
            if mutation=='missing_event':bad['events']=[]
            if mutation=='missing_stock':bad['events'][0]['stocks']=[]
            if mutation=='unknown_stock':bad['events'][0]['stocks'][0]['code']='000002'
            if mutation=='incomplete':bad['complete']=False
            if mutation=='missing_condition':bad['events'][0]['invalidate']=''
            with self.assertRaises(ValueError):n.validate(bad,f)
    def test_cached_and_failure_preserve(self):
        f=n.facts(self.c,'2026-09-29')
        with patch.object(n.llm,'chat',return_value=json.dumps(self.report(f))):n._generate(f)
        before=n._read('2026-09-29')
        with patch.object(n.llm,'chat',return_value=None):n._generate(f)
        self.assertEqual(n._read('2026-09-29'),before)
        with patch.object(n.llm,'available',return_value=True),patch.object(n.llm,'chat') as call:
            self.assertEqual(n.start(self.c,'2026-09-29')['status'],'cached')
            n.view(self.c,'2026-09-29');call.assert_not_called()
    def test_archive_keeps_old_ai_and_status(self):
        self.c.execute("INSERT INTO limit_up(trade_date,code,name,height,reason) VALUES('2026-09-29','600001','测试公司',1,'测试')")
        f=n.facts(self.c,'2026-09-29')
        with patch.object(n.llm,'chat',return_value=json.dumps(self.report(f))):n._generate(f)
        a=n.archive(self.c,'2026-09-29')
        self.assertEqual(a['items'][0]['id'],f['events'][0]['id'])
        self.assertEqual(a['items'][0]['status'],f['events'][0]['status'])
        self.assertEqual(a['items'][0]['ai_direction'],'暂无法判断')
        self.assertIn('传闻被否认',a['items'][0]['ai_judgment'])
    def test_legacy_news_date(self):
        with patch.object(panels,'q_mainlines',return_value={'lines':[]}):
            result=panels.q_news(self.c,'2026-09-29',{})
        self.assertTrue(all(x['ts'][:10]=='2026-09-29' for x in result['items']))
    def test_latest_news_and_events_ignore_market_date(self):
        ts=(datetime.now()-timedelta(minutes=1)).isoformat(timespec='seconds')
        self.c.execute('INSERT INTO news(id,ts,title,content,source,url) VALUES(?,?,?,?,?,?)',
                       ('weekend',ts,'测试公司周末公告','测试公司周末公告','测试来源','https://example.com/weekend'))
        self.c.commit()
        with patch.object(panels,'q_mainlines',return_value={'lines':[]}):
            latest=panels.run(self.c,'news','2026-09-29',{'latest':True})['data']
            historical=panels.run(self.c,'news','2026-09-29',{})['data']
        self.assertEqual(latest['items'][0]['title'],'测试公司周末公告')
        self.assertNotIn('测试公司周末公告',[x['title'] for x in historical['items']])
        self.assertIn('最新 7×24',latest['scope'])
        current=panels.run(self.c,'news_impact','2026-09-29',{'latest':True})['data']
        self.assertTrue(current['latest'])
        self.assertEqual(current['date'],datetime.now().strftime('%Y-%m-%d'))
        self.assertTrue(any(e['title']=='测试公司周末公告' for e in current['events']))
        archived=panels.run(self.c,'news_impact','2026-09-29',{})['data']
        self.assertFalse(archived['latest'])
        self.assertFalse(any(e['title']=='测试公司周末公告' for e in archived['events']))
    def test_portfolio_workspace_removes_market_panels(self):
        portfolio=panels.portfolio_dashboard()
        portfolio['panels'].extend([panels._p('market_stats'),panels._p('mainlines'),panels._p('news')])
        custom={'id':'custom','title':'自定义','builtin':False,'panels':[panels._p('market_stats')]}
        # 旧版文件迁移时才移除市场面板；之后用户可以自行把面板加回来。
        restored=panels._migrate_legacy_panels(panels._ensure_builtin_dashboards([portfolio,custom]))
        by_id={d['id']:d for d in restored}
        self.assertEqual([p['id'] for p in by_id['portfolio']['panels']],
                         ['portfolio_account','portfolio_analysis'])
        self.assertEqual(by_id['custom']['panels'][0]['id'],'market_stats')


if __name__=='__main__':unittest.main()
