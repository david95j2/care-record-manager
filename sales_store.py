"""Immutable uploaded sales evidence with optimistic, versioned human review."""
import hashlib
import json
import time
from uuid import uuid4
from domain import ValidationError, validate_draft
from sales_import import parse_xlsx, reviewed_result
from store import Conflict


class SalesStore:
    def __init__(self, store):
        self.store = store

    def upload(self, name, data, user):
        if not isinstance(name, str) or not name.lower().endswith('.xlsx'):
            raise ValidationError('XLSX 파일을 선택하세요.')
        digest = hashlib.sha256(data).hexdigest()
        with self.store.connect() as db:
            found = db.execute('SELECT id FROM sales_imports WHERE sha256=?', (digest,)).fetchone()
            if found:
                return {'id': found[0], 'duplicate': True}
        headers, rows = parse_xlsx(data)
        ident = str(uuid4())
        with self.store.connect() as db:
            db.execute('INSERT INTO sales_imports VALUES(?,?,?,?,?,?,?)',
                       (ident, name[:240], digest, data, json.dumps(headers, ensure_ascii=False), time.time(), user))
            for row in rows:
                key = str(uuid4())
                row['draft']['id'] = key
                db.execute('INSERT INTO sales_rows(id,import_id,sheet,row_number,raw,summary,draft) VALUES(?,?,?,?,?,?,?)',
                           (key, ident, row['sheet'], row['row'], json.dumps(row['raw'],ensure_ascii=False),
                            json.dumps(row['summary'],ensure_ascii=False), json.dumps(row['draft'],ensure_ascii=False)))
            self.store.audit(db, user, 'sales-import', ident, 1)
        return {'id': ident, 'rowCount': len(rows), 'duplicate': False}

    def imports(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT i.id,i.name,i.created,count(r.id) AS rowCount
                FROM sales_imports i JOIN sales_rows r ON r.import_id=i.id GROUP BY i.id ORDER BY i.created DESC''')]

    def rows(self, ident, page=1, query='', category='all'):
        if page < 1 or page > 10000 or len(query) > 120 or category not in ('all','returned','bulk','duplicate'):
            raise ValidationError('조회 조건을 확인하세요.')
        clauses, args = ['import_id=?'], [ident]
        if query:
            clauses.append("(instr(json_extract(summary,'$.approval'),?)>0 OR instr(json_extract(summary,'$.beneficiary'),?)>0 OR instr(json_extract(summary,'$.worker'),?)>0)")
            args.extend([query]*3)
        if category == 'returned':
            clauses.append("json_extract(summary,'$.returned')=1")
        if category == 'bulk':
            clauses.append("json_extract(summary,'$.paymentKind')='bulk'")
        if category == 'duplicate':
            clauses.append("json_extract(summary,'$.sameApprovalRows')>1")
        where = ' AND '.join(clauses)
        with self.store.connect() as db:
            total = db.execute('SELECT count(*) FROM sales_rows WHERE '+where,args).fetchone()[0]
            found = db.execute('SELECT id,sheet,row_number,summary,version FROM sales_rows WHERE '+where+' ORDER BY sheet,row_number LIMIT 50 OFFSET ?',args+[(page-1)*50])
            return {'total':total,'page':page,'rows':[dict(r,summary=json.loads(r['summary'])) for r in found]}

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute('''SELECT r.*, i.headers FROM sales_rows r JOIN sales_imports i ON i.id=r.import_id
                              WHERE r.id=?''',(ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            value = dict(row)
            for field in ('raw','summary','draft','headers'):
                value[field] = json.loads(value[field])
            value['result'] = reviewed_result(value['summary'],value['draft'])[1]
            return value

    def save(self, ident, version, draft, user):
        if type(version) is not int or not isinstance(draft,dict):
            raise ValidationError('검토 입력 형식을 확인하세요.')
        source = self.get(ident)
        sale,result = reviewed_result(source['summary'],dict(draft,id=ident))
        validate_draft({'sales':[sale]})
        if sale.get('decision') not in ('','accepted','rejected','not_provided'):
            raise ValidationError('최종 판정을 확인하세요.')
        if sale['returned'] and sale.get('decision') not in ('','not_provided'):
            raise ValidationError('반납은 서비스 미실시로만 판정할 수 있습니다.')
        payload = json.dumps(sale,ensure_ascii=False)
        with self.store.connect() as db:
            changed = db.execute('UPDATE sales_rows SET draft=?,version=version+1 WHERE id=? AND version=?',
                                 (payload,ident,version))
            if changed.rowcount != 1:
                raise Conflict('다른 화면에서 수정되었습니다. 현재 입력을 보관하고 다시 불러오세요.')
            db.execute('INSERT INTO sales_reviews(row_id,version,draft,result,username,created) VALUES(?,?,?,?,?,?)',
                       (ident,version+1,payload,json.dumps(result,ensure_ascii=False),user,time.time()))
            self.store.audit(db,user,'sales-review',ident,version+1)
        return self.get(ident)

    def original(self, ident):
        with self.store.connect() as db:
            row=db.execute('SELECT original FROM sales_imports WHERE id=?',(ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            return row[0]
