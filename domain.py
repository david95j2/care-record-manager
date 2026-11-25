"""Deterministic, per-sale comparison. No OCR guesses or final approval."""
from datetime import datetime

RULE_VERSION = '2026-11-25.1'


class ValidationError(ValueError):
    pass


def minutes(value):
    if type(value) is not int or value < 0:
        raise ValidationError('시간은 0 이상의 정수 분이어야 합니다.')
    return value


def recognized_minutes(total):
    hours, rest = divmod(minutes(total), 60)
    return hours * 60 + (0 if rest < 15 else 30 if rest < 45 else 60)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError('서비스 날짜와 시작·종료 시각을 확인하세요.') from None
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise ValidationError('현재 버전은 한국 현지 시각의 분 단위 입력만 지원합니다. 초 단위는 확인 필요입니다.')
    return parsed


def service_total(intervals):
    if not isinstance(intervals, list) or not intervals:
        raise ValidationError('실제 서비스 구간이 필요합니다. 휴게 구간은 입력하지 마세요.')
    parsed = sorted((timestamp(x['start']), timestamp(x['end'])) for x in intervals)
    previous = None
    total = 0
    for start, end in parsed:
        if end <= start or (previous is not None and start < previous):
            raise ValidationError('종료가 시작보다 늦어야 하며 서비스 구간이 중복되면 안 됩니다.')
        total += int((end - start).total_seconds() // 60)
        previous = end
    return total, parsed[0][0], parsed[-1][1]


def compare(sale):
    """Input is one human-linked sale, never a daily aggregation."""
    findings = []
    result = dict(ruleVersion=RULE_VERSION, status='review_required', findings=findings,
                  actualMinutes=None, recognizedMinutes=None, finalDecision=None)
    if sale.get('returned') is True:
        result['status'] = 'not_provided'
        findings.append('반납: 서비스 미실시. 기록지 미기재는 누락으로 처리하지 않습니다.')
        return result
    if sale.get('paymentKind') not in ('normal', 'bulk'):
        findings.append('예외 또는 미확정 결제 구분: 자동 정상 판정하지 않습니다.')
        return result
    try:
        plan = minutes(sale.get('planMinutes'))
        if plan == 0:
            raise ValidationError('계획 시간은 0보다 커야 합니다.')
        total, start, end = service_total(sale.get('intervals'))
        recognized = recognized_minutes(total)
    except (ValidationError, KeyError, TypeError) as exc:
        findings.append(str(exc) if isinstance(exc, ValidationError) else '서비스 구간 입력 구조를 확인하세요.')
        return result
    result.update(actualMinutes=total, recognizedMinutes=recognized)
    if recognized != plan:
        findings.append(f'계획 {plan}분 / 인정 {recognized}분 불일치')
    record = sale.get('record')
    if not isinstance(record, dict):
        findings.append('기록지 상단 값이 필요합니다.')
        return result
    approval = sale.get('approval')
    if not isinstance(approval, str) or not approval.strip() or record.get('approval') != approval:
        findings.append('승인번호 누락 또는 불일치: 선행 0을 유지하여 확인하세요.')
    try:
        if timestamp(record.get('start')) != start:
            findings.append('기록지 시작시간 불일치')
        if timestamp(record.get('end')) != end:
            findings.append('기록지 종료시간 불일치')
        if minutes(record.get('minutes')) != total:
            findings.append('기록지 제공시간과 실제 구간 합계 불일치')
    except ValidationError as exc:
        findings.append(str(exc))
    if sale['paymentKind'] == 'bulk' and record.get('retroactive') is not True:
        findings.append('일괄결제: 소급 체크가 없거나 확인되지 않았습니다.')
    # targetMonth, cancellation/reapproval, bottom notes deliberately not inspected.
    result['status'] = 'review_required' if findings else 'numeric_match'
    return result


def validate_draft(value):
    if not isinstance(value, dict) or not isinstance(value.get('sales'), list):
        raise ValidationError('sales 목록이 필요합니다.')
    if len(value['sales']) > 200:
        raise ValidationError('문서당 매출 입력은 200건까지 지원합니다.')
    ids = set()
    for sale in value['sales']:
        if not isinstance(sale, dict) or not isinstance(sale.get('id'), str) or not sale['id']:
            raise ValidationError('각 매출에 독립 ID가 필요합니다.')
        if sale['id'] in ids:
            raise ValidationError('매출 ID가 중복되었습니다.')
        ids.add(sale['id'])
        if not isinstance(sale.get('intervals'), list) or not isinstance(sale.get('record'), dict):
            raise ValidationError('서비스 구간 목록과 기록지 입력 객체가 필요합니다.')
        if any(not isinstance(x, dict) for x in sale['intervals']):
            raise ValidationError('서비스 구간 형식이 잘못되었습니다.')
        if type(sale.get('returned')) is not bool:
            raise ValidationError('반납 여부를 확인하세요.')
    return value
