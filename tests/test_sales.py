import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sales_import import parse_xlsx, reviewed_result
from sales_store import SalesStore
from store import Store, Conflict
from domain import ValidationError
from server import App
from test_core import sale


def fixture(rows):
    # Synthetic test workbook only. No real personal data.
    from openpyxl import Workbook
    w=Workbook();s=w.active
    headers=['']*51
    for i,v in {1:'순번',2:'대상자명',4:'대상자ID',10:'제공인력명',17:'결제일시',18:'승인번호',
                27:'결제구분',32:'시작시간(월/일/분)',33:'종료시간(월/일/분)',47:'반납구분'}.items():
        headers[i-1]=v
    s.append(headers)
    for overrides in rows:
        values=['']*51
        for i,v in {1:1,2:'테스트',4:'id1',10:'직원',17:'2026-10-01 12:14',18:'001',
                    19:'209912',27:'정상결제',32:'10-01 11:00',33:'10-01 12:14',35:'01:14',**overrides}.items():
            values[i-1]=v
        s.append(values)
    output=io.BytesIO();w.save(output);w.close();return output.getvalue()


class SalesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.store.add_user('admin','admin-test-password')
        self.store.add_user('staff','staff-test-password')
        self.sales=SalesStore(self.store)

    def test_preserves_original_rows_leading_zero_and_duplicate_warning(self):
        raw=fixture([{}, {1:2,19:'190001',32:'',33:''}])
        info=self.sales.upload('test.xlsx',raw,'staff')
        self.assertEqual(raw,self.sales.original(info['id']))
        rows=self.sales.rows(info['id'])['rows'];self.assertEqual(2,len(rows))
        item=self.sales.get(rows[0]['id'])
        self.assertEqual('001',item['summary']['approval'])
        self.assertEqual('209912',item['raw'][18]['value'])
        self.assertEqual(2,item['summary']['sameApprovalRows'])
        self.assertEqual(60,item['summary']['sourceRecognizedMinutes'])
        self.assertTrue(self.sales.upload('again.xlsx',raw,'admin')['duplicate'])
        self.assertEqual(1,len(self.sales.imports()))

    def test_return_and_bulk_rules_from_source_cannot_be_overridden(self):
        info=self.sales.upload('test.xlsx',fixture([{47:'반납'}, {18:'002',27:'일괄결제',35:'01:15'}]),'staff')
        rows=self.sales.rows(info['id'])['rows']
        returned=self.sales.get(rows[0]['id'])
        draft=sale();draft.update(returned=False,approval='forged',decision='')
        saved=self.sales.save(returned['id'],1,draft,'staff')
        self.assertTrue(saved['draft']['returned'])
        self.assertEqual('001',saved['draft']['approval'])
        self.assertEqual('not_provided',saved['result']['status'])
        bulk=self.sales.get(rows[1]['id'])
        draft=sale();draft['record']['approval']='002'
        saved=self.sales.save(bulk['id'],1,draft,'staff')
        self.assertEqual('bulk',saved['draft']['paymentKind'])
        self.assertTrue(any('소급' in f for f in saved['result']['findings']))
        draft['record']['retroactive']=True
        saved=self.sales.save(bulk['id'],2,draft,'staff')
        self.assertFalse(any('소급' in f for f in saved['result']['findings']))

    def test_review_history_conflict_and_source_immutable(self):
        info=self.sales.upload('test.xlsx',fixture([{}]),'staff')
        ident=self.sales.rows(info['id'])['rows'][0]['id'];before=self.sales.get(ident)
        draft=sale();draft['record']['approval']='001'
        self.sales.save(ident,1,draft,'staff')
        with self.assertRaises(Conflict):self.sales.save(ident,1,draft,'staff')
        after=SalesStore(Store(self.temp.name)).get(ident)
        self.assertEqual(before['raw'],after['raw']);self.assertEqual(2,after['version'])
        with self.store.connect() as db:self.assertEqual(1,db.execute('SELECT count(*) FROM sales_reviews').fetchone()[0])

    def test_target_month_no_effect_and_74_75_boundary(self):
        _,rows=parse_xlsx(fixture([{19:'190001'},{18:'002',19:'209912',35:'01:15'}]))
        self.assertEqual([60,90],[r['summary']['sourceRecognizedMinutes'] for r in rows])
        self.assertEqual('',rows[0]['draft']['intervals'][0]['start']) # no invented year
        summary=rows[0]['summary'];draft=sale()
        a=reviewed_result(summary,dict(draft,targetMonth='190001'))[1]
        b=reviewed_result(summary,dict(draft,targetMonth='209912'))[1]
        self.assertEqual(a,b)

    def test_empty_unknown_and_bad_file_rejected_or_flagged(self):
        with self.assertRaises(ValidationError):parse_xlsx(b'not-a-workbook')
        _,rows=parse_xlsx(fixture([{27:'예외결제',35:'',32:'',33:''}]))
        self.assertIsNone(rows[0]['summary']['sourceActualMinutes'])
        self.assertEqual('unknown',rows[0]['summary']['paymentKind'])
        self.assertTrue(rows[0]['summary']['flags'])

    def test_admin_only_create_list_and_password_login(self):
        with self.assertRaises(PermissionError):self.store.manage_users('staff')
        with self.assertRaises(PermissionError):self.store.manage_users('staff',{'username':'hacker','password':'long-password'})
        self.store.manage_users('admin',{'username':'new','password':'long-password'})
        self.assertIsNotNone(self.store.login('new','long-password'))
        with self.assertRaises(Conflict):self.store.manage_users('admin',{'username':'new','password':'long-password'})
        with self.assertRaises(ValidationError):self.store.manage_users('admin',{'username':'short','password':'a'})
        self.assertNotIn('digest',self.store.manage_users('admin')[0])

    def test_http_admin_permissions_and_sales_auth(self):
        app=App(self.store,'http://127.0.0.1:8877')
        def call(path,user=None,body=None):
            token=self.store.login(user,user+'-test-password') if user else ''
            data=json.dumps(body or {}).encode();status=[]
            env=dict(PATH_INFO=path,REQUEST_METHOD='POST' if body is not None else 'GET',
                     HTTP_HOST='127.0.0.1:8877',HTTP_ORIGIN='http://127.0.0.1:8877',HTTP_COOKIE='session='+token,
                     CONTENT_TYPE='application/json',CONTENT_LENGTH=str(len(data)),QUERY_STRING='')
            env['wsgi.input']=io.BytesIO(data)
            response=b''.join(app(env,lambda code,headers:status.append(code)))
            return status[0],response
        self.assertTrue(call('/api/users','staff')[0].startswith('403'))
        self.assertTrue(call('/api/users','staff',{'username':'bad','password':'long-password'})[0].startswith('403'))
        self.assertTrue(call('/api/users','admin')[0].startswith('200'))
        self.assertTrue(call('/api/sales-imports')[0].startswith('403'))
        self.assertTrue(call('/api/sales-imports','staff')[0].startswith('200'))

    def test_filters_and_pagination(self):
        info=self.sales.upload('test.xlsx',fixture([{18:str(i),47:'반납' if i==0 else '',27:'일괄결제' if i==1 else '정상결제'} for i in range(52)]),'staff')
        self.assertEqual(50,len(self.sales.rows(info['id'])['rows']))
        self.assertEqual(2,len(self.sales.rows(info['id'],2)['rows']))
        self.assertEqual(1,self.sales.rows(info['id'],category='returned')['total'])
        self.assertEqual(1,self.sales.rows(info['id'],category='bulk')['total'])


if __name__=='__main__':unittest.main()
