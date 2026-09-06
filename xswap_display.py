"""Weekly-first presentation. Never substitute a short window for a week."""
import os
import sys
import time
from xswap_usage import is_ok, reset_credit_lines, reset_label, usage_lines


def weekly(row):
    for bucket in row['buckets']:
        if bucket['id'] == 'codex':
            for window in bucket['windows']:
                if window['windowMinutes'] == 10080:
                    return window
    return None


def summary(row, now=None):
    now = time.time() if now is None else now
    window = weekly(row)
    left = window['remainingPercent'] if window else None
    used = 100 - left if left is not None else None
    status = row['status']
    note = ('비활성화 (disabled)' if status == 'disabled' else '로그인 필요' if status == 'not signed in' else
            '오프라인' if status == 'offline' else
            '조회 불가' if not is_ok(status) else '주간 사용량 미제공')
    bar = ''
    if used is not None:
        filled = min(20, max(0, int(used / 5 + .5)))
        bar = '█' * filled + '░' * (20 - filled)
        note = f'{used:g}% 사용 · {left:g}% 남음'
    reset = ''
    if window and window['resetsAt']:
        remaining = window['resetsAt'] - now
        if remaining <= 0:
            reset = '초기화 대기 중'
        else:
            minutes = max(1, int(remaining / 60))
            days, rest = divmod(minutes, 1440)
            hours, mins = divmod(rest, 60)
            duration = f'{days}일 {hours}시간' if days else f'{hours}시간 {mins}분' if hours else f'{mins}분'
            reset = f'초기화 {duration} 후'
    return {'name': row['name'], 'selected': row.get('active', False), 'used': used,
            'remaining': left, 'bar': bar, 'summary': note, 'reset': reset,
            'tone': 'red' if used is not None and used >= 90 else 'yellow' if used is not None and used >= 80 else 'green' if used is not None else 'gray',
            'exhausted': left == 0, 'cached': row.get('cached', False), 'fetchedAt': row.get('fetchedAt')}


def policy_label(settings):
    if not settings.get('enabled'):
        return '새 세션 자동전환 OFF'
    threshold = settings.get('weeklyRemainingThreshold', 0)
    trigger = f'{100-threshold:g}% 사용 시 전환' if threshold else '한도 도달 시 전환'
    return '새 세션 자동전환 ON · ' + trigger


def render(rows, settings, include_spark=False, details=False, color=None, now=None):
    if not rows:
        return '등록된 계정이 없습니다. xswap register main'
    if color is None:
        color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    now = time.time() if now is None else now
    lines = ['xswap · 주간 사용량', '█ 사용  ░ 남음', '']
    for row in sorted(rows, key=lambda row: not row.get('active', False)):
        item = summary(row, now)
        badge = '선택됨' if item['selected'] else ''
        if item['exhausted']: badge += (' · ' if badge else '') + '한도 도달'
        name = item['name']
        if item['cached'] and item['fetchedAt'] is not None:
            name += f" (cached {max(0, int(now - item['fetchedAt']))}s ago)"
        lines.append(('▸ ' if item['selected'] else '  ') + name + ('  [' + badge + ']' if badge else ''))
        text = (item['bar'] + '  ' if item['bar'] else '') + item['summary']
        if color:
            code = {'green': 32, 'yellow': 33, 'red': 31, 'gray': 90}[item['tone']]
            text = f'\033[{code}m{text}\033[0m'
        lines.append('  ' + text)
        if item['reset']: lines.append('  ' + item['reset'])
        if is_ok(row['status']):
            lines.extend('  ' + line for line in reset_credit_lines(row.get('resetCredits')))
        if details:
            lines.append('  ' + row['identity'])
            buckets = [b for b in row['buckets'] if include_spark or 'spark' not in (b['id'] + b['name']).lower()]
            lines.extend('  ' + line for line in usage_lines(buckets))
        elif include_spark:
            buckets = [b for b in row['buckets'] if 'spark' in (b['id'] + b['name']).lower()]
            if buckets: lines.extend('  ' + line for line in usage_lines(buckets))
        lines.append('')
    lines.append(policy_label(settings))
    lines.append('선택됨 = 새 실행의 기본 계정 · 기존 세션 계정은 xswap auto-status')
    return '\n'.join(lines)


def dashboard(manager):
    from xswap_cli import status_data
    state = status_data(manager, cleanup=False)
    rows = manager.account_rows()
    return {'accounts': [summary(row) for row in sorted(rows, key=lambda r: not r['active'])],
            'policy': policy_label(state),
            'sessions': [{'account': s['account'], 'surface': s['surface']} for s in state['sessions'] if s['running']],
            'updatedAt': time.time()}
