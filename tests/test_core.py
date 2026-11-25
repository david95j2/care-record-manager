import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import threading
from urllib.request import urlopen
from wsgiref.simple_server import make_server
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from domain import compare, recognized_minutes, service_total, ValidationError
from store import Store, Conflict
from server import App


def sale():
    return dict(id='sale-1', approval='00123', planMinutes=60, paymentKind='normal', returned=False,
                intervals=[dict(start='2026-11-25T13:00',end='2026-11-25T14:00')],
                record=dict(start='2026-11-25T13:00',end='2026-11-25T14:00',minutes=60,approval='00123',retroactive=None),decision='')


class RulesTests(unittest.TestCase):
    def test_boundaries(self):
        for value, expected in [(0,0),(14,0),(15,30),(44,30),(45,60),(59,60),(60,60),(74,60),(75,90),(104,90),(105,120)]:
            with self.subTest(value=value):self.assertEqual(expected,recognized_minutes(value))

    def test_invalid_minutes(self):
        for value in [-1,1.5,True,None,float('nan'),float('inf')]:
            with self.subTest(value=value),self.assertRaises(ValidationError):recognized_minutes(value)

    def test_normal(self):
        self.assertEqual('numeric_match',compare(sale())['status'])
        self.assertIsNone(compare(sale())['finalDecision'])

    def test_74_vs_75(self):
        for end,total,status in [('14:14',74,'numeric_match'),('14:15',75,'review_required')]:
            s=sale();s['intervals'][0]['end']=s['record']['end']='2026-11-25T'+end;s['record']['minutes']=total
            self.assertEqual(status,compare(s)['status'])

    def test_early10_late5(self):
        s=sale();s['intervals'][0]={'start':'2026-11-25T12:50','end':'2026-11-25T14:05'}
        self.assertEqual(90,compare(s)['recognizedMinutes'])
        self.assertEqual('review_required',compare(s)['status'])

    def test_break_sum_once_not_round_each(self):
        s=sale();s['intervals']=[dict(start='2026-11-25T13:00',end='2026-11-25T13:14'),dict(start='2026-11-25T14:00',end='2026-11-25T14:14')]
        self.assertEqual(28,compare(s)['actualMinutes']);self.assertEqual(30,compare(s)['recognizedMinutes'])

    def test_morning_afternoon_independent(self):
        morning=sale();afternoon=sale();afternoon['intervals'][0]['end']='2026-11-25T14:15'
        self.assertEqual(['numeric_match','review_required'],[compare(x)['status'] for x in [morning,afternoon]])

    def test_ignored_target_month_and_bottom_notes(self):
        s=sale();before=compare(s);s.update(targetMonth='190001',notes='변경 사유',cancellation=True,reapproval=True)
        self.assertEqual(before,compare(s))

    def test_returned_missing_record(self):
        s=dict(returned=True);self.assertEqual('not_provided',compare(s)['status'])
        self.assertIsNone(compare(s)['actualMinutes'])

    def test_bulk_retroactive(self):
        for checked,expected in [(True,'numeric_match'),(False,'review_required'),(None,'review_required')]:
            s=sale();s['paymentKind']='bulk';s['record']['retroactive']=checked
            self.assertEqual(expected,compare(s)['status'])

    def test_approval_zero_preserved(self):
        s=sale();s['record']['approval']='123';self.assertEqual('review_required',compare(s)['status'])

    def test_overlap_rejected(self):
        s=sale();s['intervals']*=2;self.assertIsNone(compare(s)['actualMinutes'])

    def test_missing_not_zero(self):
        s=sale();s['planMinutes']=None;self.assertEqual('review_required',compare(s)['status'])

    def test_seconds_not_silently_truncated(self):
        s=sale();s['intervals'][0]['end']='2026-11-25T14:14:59';self.assertIsNone(compare(s)['actualMinutes'])

    def test_unknown_kind_not_normal(self):
        s=sale();s['paymentKind']='exception';self.assertEqual('review_required',compare(s)['status'])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);self.store.add_user('tester','test-only-password')
        self.ident=self.store.upload('sample.pdf',b'%PDF-1.7 test fixture','tester')

    def test_reopen_and_original_preserved(self):
        s=sale();saved=self.store.save(self.ident,1,{'sales':[s]},'tester')
        reopened=Store(self.temp.name);self.assertEqual(s,reopened.get(self.ident)['draft']['sales'][0])
        self.assertEqual(2,saved['version']);self.assertEqual(b'%PDF-1.7 test fixture',reopened.get(self.ident,True)['original'])

    def test_conflict_does_not_overwrite(self):
        s=sale();self.store.save(self.ident,1,{'sales':[s]},'tester')
        with self.assertRaises(Conflict):self.store.save(self.ident,1,{'sales':[]},'tester')
        self.assertEqual(1,len(self.store.get(self.ident)['draft']['sales']))

    def test_confirm_requires_human_decision(self):
        self.store.save(self.ident,1,{'sales':[sale()]},'tester')
        with self.assertRaises(ValidationError):self.store.confirm(self.ident,2,'tester')

    def test_incomplete_cannot_confirm_even_with_decision(self):
        s=sale();s['record']['start']='';s['decision']='accepted'
        self.store.save(self.ident,1,{'sales':[s]},'tester')
        with self.assertRaises(ValidationError):self.store.confirm(self.ident,2,'tester')

    def test_malformed_draft_not_persisted(self):
        s=sale();s['intervals']=[None]
        with self.assertRaises(ValidationError):self.store.save(self.ident,1,{'sales':[s]},'tester')
        self.assertEqual(1,self.store.get(self.ident)['version'])

    def test_actual_http_static_and_health(self):
        httpd=make_server('127.0.0.1',0,lambda env,start:[])
        origin=f'http://127.0.0.1:{httpd.server_port}'
        httpd.set_app(App(self.store,origin))
        worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
        try:
            for path in ['/','/app.js','/autosave.js','/theme.js','/style.css','/api/health']:
                with urlopen(origin+path,timeout=5) as response:
                    self.assertEqual(200,response.status)
                    self.assertGreater(len(response.read()),0)
        finally:
            httpd.shutdown();worker.join(timeout=5);httpd.server_close()

    def test_confirmation_snapshot_not_overwritten(self):
        s=sale();s['decision']='accepted';self.store.save(self.ident,1,{'sales':[s]},'tester')
        self.store.confirm(self.ident,2,'tester');self.store.save(self.ident,2,{'sales':[]},'tester')
        with self.store.connect() as db:
            saved=json.loads(db.execute('SELECT payload FROM revisions').fetchone()[0])
        self.assertEqual('accepted',saved['sales'][0]['decision'])
        self.assertEqual([2],self.store.get(self.ident)['confirmedVersions'])

    def test_returned_cannot_confirm_as_performed(self):
        s=sale();s.update(returned=True,decision='accepted');self.store.save(self.ident,1,{'sales':[s]},'tester')
        with self.assertRaises(ValidationError):self.store.confirm(self.ident,2,'tester')

    def test_duplicate_id_blocked(self):
        with self.assertRaises(ValidationError):self.store.save(self.ident,1,{'sales':[sale(),sale()]},'tester')

    def test_auth_session_logout(self):
        self.assertIsNone(self.store.login('tester','wrong'));token=self.store.login('tester','test-only-password')
        self.assertEqual('tester',self.store.user(token));self.store.logout(token);self.assertIsNone(self.store.user(token))

    def test_upload_type_restricted(self):
        with self.assertRaises(ValidationError):self.store.upload('evil.html',b'<script>bad</script>','tester')

    def test_api_auth_origin_and_root(self):
        app=App(self.store,'http://127.0.0.1:8877')
        def call(path,method='GET',body=None,origin=None,cookie=''):
            data=json.dumps(body or {}).encode();status=[]
            env=dict(PATH_INFO=path,REQUEST_METHOD=method,HTTP_HOST='127.0.0.1:8877',HTTP_COOKIE=cookie,CONTENT_LENGTH=str(len(data)),CONTENT_TYPE='application/json',HTTP_ORIGIN=origin)
            env['wsgi.input']=io.BytesIO(data)
            result=b''.join(app(env,lambda code,headers:status.extend([code,headers])))
            return status,result
        self.assertTrue(call('/')[0][0].startswith('200'))
        self.assertTrue(call('/api/documents')[0][0].startswith('403'))
        self.assertTrue(call('/api/login','POST',{'username':'tester','password':'test-only-password'},'http://evil')[0][0].startswith('403'))
        status,_=call('/api/login','POST',{'username':'tester','password':'test-only-password'},'http://127.0.0.1:8877')
        cookie=next(v for k,v in status[1] if k=='Set-Cookie').split(';')[0]
        self.assertTrue(call('/api/documents',cookie=cookie)[0][0].startswith('200'))


if __name__ == '__main__':unittest.main()
