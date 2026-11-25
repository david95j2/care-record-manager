"""Read-only XLSX ingestion. Original bytes/cells and review inputs stay separate."""
import io
import json
import sys
import re
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from domain import ValidationError, compare, recognized_minutes

# Optional app-local dependency; never import from the OCR/training environment.
sys.path.insert(0, str(Path(__file__).parent / '.deps'))


def text(value):
    return '' if value is None else str(value).strip()


def raw_cell(cell):
    value = cell.value
    if isinstance(value, (datetime, date, time)):
        value = value.isoformat()
    return {'value': value, 'type': cell.data_type, 'format': cell.number_format}


def parse_xlsx(data):
    if not data or len(data) > 20 * 1024 * 1024:
        raise ValidationError('엑셀 파일은 20MB 이하여야 합니다.')
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 2000 or sum(i.file_size for i in archive.infolist()) > 100*1024*1024:
                raise ValidationError('압축 해제 크기가 너무 큰 엑셀입니다.')
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except ValidationError:
        raise
    except (ImportError, AttributeError) as exc:
        raise ValidationError('엑셀 읽기 라이브러리를 설치한 뒤 서버를 다시 시작하세요.') from exc
    except Exception as exc:
        raise ValidationError('읽을 수 없는 XLSX 파일입니다.') from exc
    rows, headers = [], {}
    required = {1:'순번', 2:'대상자명', 4:'대상자ID', 10:'제공인력명', 17:'결제일시',
                18:'승인번호', 27:'결제구분', 32:'시작시간(월/일/분)',
                33:'종료시간(월/일/분)', 47:'반납구분'}
    try:
        for sheet in workbook:
            if (sheet.max_column or 0) > 100 or (sheet.max_row or 0) > 50001:
                raise ValidationError('지원 크기는 시트당 50,000행, 100열까지입니다.')
            iterator = sheet.iter_rows()
            first = next(iterator, ())
            if not first:
                continue
            labels = [text(c.value) for c in first]
            if any(len(labels) < i or labels[i-1] != label for i,label in required.items()):
                raise ValidationError('서비스이용내역 열 구조가 다릅니다. 순번~반납구분 열을 확인하세요.')
            headers[sheet.title] = labels
            for number, cells in enumerate(iterator, 2):
                if number > 50001 or len(rows) >= 50000 or len(cells) > 100:
                    raise ValidationError('한 파일은 50,000행, 100열까지 지원합니다.')
                raw = [raw_cell(c) for c in cells]
                if not any(c['value'] is not None for c in raw):
                    continue
                def at(i):
                    return text(raw[i-1]['value']) if len(raw) >= i else ''
                flags = []
                approval = at(18)
                if not approval:
                    flags.append('승인번호 없음')
                if len(raw) >= 18 and raw[17]['type'] != 's':
                    flags.append('승인번호 숫자 셀: 원본의 앞자리 0 확인 필요')
                if any(c['type'] == 'f' for c in raw):
                    flags.append('수식 포함: 수식은 실행하지 않음')
                kind = 'bulk' if at(27) == '일괄결제' else 'normal' if at(27) == '정상결제' else 'unknown'
                returned = at(47) == '반납'
                if at(47) not in ('','반납'):
                    flags.append('알 수 없는 반납구분: 미실시 여부 확인')
                if not at(32) or not at(33):
                    flags.append('서비스 시작/종료 누락 또는 금액 분할 행 확인')
                if kind == 'unknown':
                    flags.append('결제구분 확인 필요')
                summary = dict(beneficiary=at(2), beneficiaryId=at(4), worker=at(10),
                               paymentAt=at(17), approval=approval, paymentKind=kind,
                               returned=returned, returnType=at(47), start=at(32), end=at(33),
                               reportedHours=at(34), amount=at(20), flags=flags)
                durations = [at(i) for i in range(35,45) if at(i)]
                if durations and all(re.fullmatch(r'\d{1,4}:[0-5]\d', v) for v in durations):
                    total = sum(int(v.split(':')[0])*60+int(v.split(':')[1]) for v in durations)
                    summary['sourceActualMinutes'] = total
                    summary['sourceRecognizedMinutes'] = recognized_minutes(total) if not returned else None
                else:
                    summary['sourceActualMinutes'] = None
                    summary['sourceRecognizedMinutes'] = None
                draft = dict(approval=approval, paymentKind=kind, returned=returned, planMinutes=None,
                             intervals=[{'start':'','end':''}],
                             record={'approval':'','start':'','end':'','minutes':None,'retroactive':None},
                             decision='')
                rows.append(dict(sheet=sheet.title, row=number, raw=raw, summary=summary, draft=draft))
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError('엑셀 행을 읽지 못했습니다. 파일을 확인하세요.') from exc
    finally:
        workbook.close()
    if not rows:
        raise ValidationError('가져올 원본 행이 없습니다.')
    counts = Counter(row['summary']['approval'] for row in rows if row['summary']['approval'])
    for row in rows:
        summary = row['summary']
        summary['sameApprovalRows'] = counts[summary['approval']]
        if summary['sameApprovalRows'] > 1:
            summary['flags'].append('동일 승인번호 여러 행: 별도 결제인지 금액 분할인지 확인 (자동 합산 안 함)')
    return headers, rows


def reviewed_result(summary, draft):
    # Source classification is immutable. No target-month, reason-text or name-based matching.
    sale = dict(draft, approval=summary['approval'], returned=summary['returned'],
                paymentKind=summary['paymentKind'])
    result = compare(sale)
    result['sourceWarnings'] = summary['flags']
    if summary['flags'] and result['status'] == 'numeric_match':
        result['status'] = 'review_required'
        result['findings'].append('원본 행 확인 사항이 남아 있습니다. 분할·중복 행을 별도 결제로 확정하지 마세요.')
    return sale, result
