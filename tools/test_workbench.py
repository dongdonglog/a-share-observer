#!/usr/bin/env python3
"""资金核对、真实历史、研究口径、报告完整性。隔离数据库，无网络。"""
import copy
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent.parent))
from chaogu import portfolio as pf,workbench as wb,research_data as rd,db,config,llm,portfolio_jobs
from portfolio_fixtures import complete_report

RAW={'cash':1000,'equity':10000,'market_value':7000,'pnl':100,
 'positions':[{'code':'000001','name':'测试股','quantity':100,'sellable':100,'cost':19,'price':20,'value':2000,'pnl':100},
 {'code':'159001','name':'基金简称','quantity':1000,'sellable':1000,'cost':1,'price':1,'value':1000,'pnl':0}],
 'extra_assets':[{'kind':'cash_management','value':4000,'source':'user_confirmed_difference'}]}
PROFILE={'reserve':500,'horizon_months':36,'max_drawdown':15,'long_term_pct':70,'new_money':0,'batches':3}
NOW=datetime(2026,10,2,10,0,tzinfo=pf.TZ)
class WorkbenchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
  self.patches=[patch.object(pf,'PATH',self.root/'portfolio.db'),patch.object(config,'DB_PATH',self.root/'market.db'),
    patch.object(pf,'now',return_value=NOW),patch.object(pf,'_broker_status',return_value={'connected':True,'snapshot_current_session':True,'query_error':''})]
  for p in self.patches:p.start()
  self.market=db.init();pf.save_broker_snapshot(RAW);pf.broker_mutate({'kind':'source','source':'eastmoney'})
 def tearDown(self):
  self.market.close()
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def snap(self):return pf.snapshot(self.market)
 def import_product(self,**kwargs):
  data={'kind':'products','scope':'cash_management','as_of':'2026-10-02','csv':'基金代码,基金名称,产品类型,市值,份额,净值日期\n000777,测试基金,股票基金,4000,3000,2026-09-30'}
  data.update(kwargs);preview=wb.import_data(data,self.snap());wb.import_data(dict(data,commit=True,fingerprint=preview['fingerprint']),self.snap())
 def test_migration_backs_up_once_preserves_records(self):
  p=self.root/'legacy.db';c=sqlite3.connect(p);c.executescript(pf.SCHEMA+pf.BROKER_SCHEMA);c.execute('UPDATE account SET cash=123 WHERE id=1');c.commit();c.close()
  c=pf.connect(p);self.assertEqual(c.execute('SELECT cash FROM account').fetchone()[0],123);c.close()
  backups=list(self.root.glob('legacy.db.before-workbench-*.bak'));self.assertEqual(len(backups),1)
  pf.connect(p).close();self.assertEqual(len(list(self.root.glob('legacy.db.before-workbench-*.bak'))),1)
  c=sqlite3.connect(backups[0]);self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='wb_history'").fetchone());c.close()
 def test_snapshot_retains_five_minute_buckets_and_quantity_changes(self):
  c=pf.connect()
  with c:
   for minute in range(1,16):wb.record_snapshot(c,RAW,(NOW+timedelta(minutes=minute)).isoformat())
   changed=copy.deepcopy(RAW);changed['positions'][0]['quantity']=200
   wb.record_snapshot(c,changed,(NOW+timedelta(minutes=15,seconds=1)).isoformat())
  count=c.execute('SELECT COUNT(*) FROM wb_history').fetchone()[0];c.close();self.assertEqual(count,5)
 def test_cashflows_never_imply_intraday_profit(self):
  data={'kind':'cashflows','csv':'日期,净流入\n2026-10-02,500','start':'2026-10-01','end':'2026-10-02','complete':True}
  r=wb.import_data(data,self.snap());wb.import_data(dict(data,commit=True,fingerprint=r['fingerprint']),self.snap())
  h=wb.history();self.assertFalse(h['cashflows_complete']);self.assertIsNone(h['points'][0]['profit'])
 def test_end_of_day_assets_minus_complete_flows(self):
  c=pf.connect();c.execute('DELETE FROM wb_history');c.commit();c.close()
  for data in [{'kind':'history','csv':'日期,总资产\n2026-09-29,10000\n2026-09-30,11100'},
               {'kind':'cashflows','csv':'日期,净流入\n2026-09-30,1000','start':'2026-09-29','end':'2026-09-30','complete':True}]:
   r=wb.import_data(data,self.snap());wb.import_data(dict(data,commit=True,fingerprint=r['fingerprint']),self.snap())
  h=wb.history();self.assertTrue(h['cashflows_complete']);self.assertEqual(h['points'][-1]['profit'],100)
 def test_import_reclassifies_without_double_count(self):
  self.import_product();w=wb.build(self.snap());parts={x['kind']:x['value'] for x in w['allocation']['items']}
  self.assertEqual(parts['funds'],5000);self.assertEqual(parts['cash_management'],0);self.assertEqual(sum(parts.values()),10000)
  self.assertEqual(w['liquidity']['available'],1000)
 def test_import_rejects_mismatch_duplicate_changed_date_and_secrets(self):
  for csv in ['基金代码,基金名称,市值\n000777,测试,3999','基金代码,基金名称,市值\n159001,重复证券,4000',
              '基金代码,基金名称,市值,净值日期\n000777,未来,4000,2030-01-01']:
   with self.assertRaises(ValueError):wb.import_data({'kind':'products','as_of':'2026-10-02','csv':csv},self.snap())
  self.import_product(csv='基金代码,基金名称,产品类型,市值,password\n000777,基金,股票基金,4000,SECRET_PASSWORD')
  for f in self.root.glob('portfolio.db*'):self.assertNotIn(b'SECRET_PASSWORD',f.read_bytes())
  self.assertEqual(len(wb.build(self.snap())['products']),3)
 def test_stale_import_does_not_replace_summary(self):
  self.import_product();s=self.snap();s['last_successful_sync']='2026-10-03T10:00:00+08:00'
  self.assertFalse(wb.imported_products(s)[0]['valid'])
  self.assertEqual(len(wb.build(s)['products']),2)
 def test_budget_shared_reserve_unknown_funds_and_new_money(self):
  s=self.snap();p=dict(PROFILE,new_money=90000)
  result=wb.scenarios(s,p);self.assertEqual(result['immediate_budget'],0)
  for item in result['items']:self.assertAlmostEqual(sum(item['target'].values()),100000)
  s['equity']=4000;s['market_value']=3000;s['extra_assets']=[];s['account']['max_total']=100
  result=wb.scenarios(s,PROFILE);self.assertEqual(result['immediate_budget'],500)
  self.assertEqual(sum(result['items'][2]['batch_amounts']),500)
  self.assertEqual([x['id'] for x in result['items']], ['maintain','concentration','staged','defer'])
  self.assertEqual(result['items'][3]['funding']['available_now'],0)
  self.assertEqual(result['items'][3]['delta']['long_term'],0)
 def test_short_horizon_expired_or_disconnected_disables_budget(self):
  s=self.snap();s['account']['max_total']=100
  for p in [dict(PROFILE,horizon_months=3),PROFILE]:
   if p is PROFILE:s['broker']['connected']=False
   self.assertEqual(wb.scenarios(s,p)['immediate_budget'],0)
 def test_profile_rejects_invalid_numbers(self):
  for k,v in [('reserve',float('nan')),('batches',1.5),('long_term_pct',101),('new_money',-1)]:
   with self.assertRaises(ValueError):wb.profile(dict(PROFILE,**{k:v}))
 def test_no_metrics_for_insufficient_data(self):
  self.assertNotIn('return_20d_pct',wb.metrics([{'close':10}]*20))
  self.assertEqual(wb.metrics([{'close':10}]*21)['return_20d_pct'],0)
 def test_nav_real_dates_and_code_validation(self):
  text='var fS_code="159001";var Data_netWorthTrend=[{"x":1759161600000,"y":1.2}];'
  p=rd.parse_nav('159001',text);self.assertEqual(p['date'],'2025-09-30')
  with self.assertRaises(ValueError):rd.parse_nav('159002',text)
 def test_partial_constituents_unknown_weights(self):
  text="截止至：<font>2026-06-30</font><tbody><tr><td>1</td><td>000001</td><td>公司</td><td></td><td></td><td></td><td>8.5%</td></tr></tbody>"
  c=rd.parse_constituents(text);self.assertEqual(c['known_pct'],8.5);self.assertEqual(c['unknown_pct'],91.5)
 def test_truncated_missing_duplicate_report_preserves_complete(self):
  wb.profile(PROFILE)
  with patch.object(llm,'available',return_value=True),patch.object(llm,'chat',side_effect=complete_report):r=pf.analysis(self.market,True)
  self.assertTrue(r['report']['complete'])
  raw=r['report'];original=json.dumps(raw,sort_keys=True)
  candidates=['{"complete":true',json.dumps(dict(raw,complete=False)),json.dumps(dict(raw,holdings=raw['holdings'][:1])),json.dumps(dict(raw,holdings=[raw['holdings'][0]]*2))]
  for candidate in candidates:
   with patch.object(llm,'available',return_value=True),patch.object(llm,'chat',return_value=candidate):
    with self.assertRaises(ValueError):pf.analysis(self.market,True)
   self.assertEqual(json.dumps(pf.analysis(self.market)['report'],sort_keys=True),original)
 def test_refresh_is_passive_and_profile_change_marks_report_stale(self):
  wb.profile(PROFILE)
  with patch.object(llm,'available',return_value=True),patch.object(llm,'chat',side_effect=complete_report):pf.analysis(self.market,True)
  with patch.object(llm,'chat') as model,patch.object(rd,'fetch') as net:
   pf.analysis(self.market);wb.build(self.snap());model.assert_not_called();net.assert_not_called()
  wb.profile(dict(PROFILE,reserve=2000));self.assertTrue(pf.analysis(self.market)['report_stale'])
 def test_research_failure_preserves_cache_and_dates(self):
  c=pf.connect();payload={'prices':{'rows':[{'date':'2026-09-29','close':10}], 'date':'2026-09-29','fetched_at':'2026-09-29T10:00:00+08:00'}}
  c.execute('INSERT INTO wb_research VALUES(?,?,?,?)',('000001',json.dumps(payload),'2026-09-29T10:00:00+08:00',None));c.commit();c.close()
  with patch.object(rd,'fetch',side_effect=ValueError('failed')):rd.refresh(self.snap())
  p=rd.cached(['000001'])['000001'];self.assertEqual(p['prices']['date'],'2026-09-29');self.assertIsNotNone(p['error'])
 def test_overlap_uses_disclosed_fraction_only(self):
  c=pf.connect();payload={'constituents':{'date':'2026-06-30','known_pct':10,'rows':[{'code':'000001','name':'测试股','weight':10}]}}
  c.execute('INSERT INTO wb_research VALUES(?,?,?,?)',('159001',json.dumps(payload),NOW.isoformat(),None));c.commit();c.close()
  w=wb.build(self.snap());self.assertEqual(w['lookthrough']['overlaps'][0]['value'],100);self.assertEqual(w['lookthrough']['unknown_value'],900)

 def test_industry_weights_and_unknown_residual(self):
  data={'Data':{'FundCode':'159001','QuarterInfos':[{'JZRQ':'2026-06-30','HYPZInfo':[
   {'BZDM':'159001','FSRQ':'2026-06-30','HYMC':'制造业','ZJZBL':'67.3'}]}]}}
  result=rd.parse_industries('159001',data);self.assertEqual(result['unknown_pct'],32.7)
  with self.assertRaises(ValueError):rd.parse_industries('159002',data)
  data['Data']['QuarterInfos'][0]['HYPZInfo'][0]['ZJZBL']='101'
  with self.assertRaises(ValueError):rd.parse_industries('159001',data)
 def test_model_transport_rejects_truncation_before_publication(self):
  import io
  for api,reason in [('openai','length'),('anthropic','max_tokens')]:
   response={'choices':[{'finish_reason':reason,'message':{'content':'{"complete":true}'}}]} if api=='openai' else {'stop_reason':reason,'content':[{'type':'text','text':'{"complete":true}'}]}
   with patch.object(llm,'load_cfg',return_value={'api':api,'base':'https://example.invalid','key':'TEST','model':'test'}),patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(response).encode())):
    self.assertIsNone(llm.chat('test','test',strict_complete=True))

if __name__=='__main__':unittest.main(verbosity=2)
