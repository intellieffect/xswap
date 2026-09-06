"""Weekly-first presentation. Never substitute a short window for a week."""
import os
import sys
import time
from xswap_usage import is_ok, reset_credit_lines, reset_label, usage_lines

# Every user-visible string in this module lives here, keyed by a short id.
# `ko` values are the original hard-coded strings, preserved verbatim.
STRINGS = {
    "en": {
        "no_accounts": "No accounts registered. xswap register main",
        "header": "xswap · weekly usage",
        "legend": "█ used  ░ left",
        "selected": "selected",
        "exhausted": "limit reached",
        "disabled": "disabled",
        "signin_required": "sign-in required",
        "offline": "offline",
        "unavailable": "unavailable",
        "no_weekly_data": "weekly usage unavailable",
        "used_left": "{used:g}% used · {left:g}% left",
        "reset_pending": "reset pending",
        "resets_in": "resets in {duration}",
        "duration_days_hours": "{days}d {hours}h",
        "duration_hours_mins": "{hours}h {mins}m",
        "duration_mins": "{mins}m",
        "autoswitch_off": "New-session auto-switch OFF",
        "autoswitch_on_threshold": "New-session auto-switch ON · switches at {pct:g}% used",
        "autoswitch_on_limit": "New-session auto-switch ON · switches at limit",
        "footer": "selected = default account for new runs · existing session accounts: xswap auto-status",
    },
    "ko": {
        "no_accounts": "등록된 계정이 없습니다. xswap register main",
        "header": "xswap · 주간 사용량",
        "legend": "█ 사용  ░ 남음",
        "selected": "선택됨",
        "exhausted": "한도 도달",
        "disabled": "비활성화 (disabled)",
        "signin_required": "로그인 필요",
        "offline": "오프라인",
        "unavailable": "조회 불가",
        "no_weekly_data": "주간 사용량 미제공",
        "used_left": "{used:g}% 사용 · {left:g}% 남음",
        "reset_pending": "초기화 대기 중",
        "resets_in": "초기화 {duration} 후",
        "duration_days_hours": "{days}일 {hours}시간",
        "duration_hours_mins": "{hours}시간 {mins}분",
        "duration_mins": "{mins}분",
        "autoswitch_off": "새 세션 자동전환 OFF",
        "autoswitch_on_threshold": "새 세션 자동전환 ON · {pct:g}% 사용 시 전환",
        "autoswitch_on_limit": "새 세션 자동전환 ON · 한도 도달 시 전환",
        "footer": "선택됨 = 새 실행의 기본 계정 · 기존 세션 계정은 xswap auto-status",
    },
}


def _t(lang, key, **kwargs):
    strings = STRINGS.get(lang) or STRINGS["en"]
    text = strings.get(key, STRINGS["en"][key])
    return text.format(**kwargs) if kwargs else text


def resolve_lang(explicit=None, environ=None):
    """"en" unless explicit=="ko", XSWAP_LANG starts with "ko", or (XSWAP_LANG unset)
    LC_ALL/LANG starts with "ko". Any other explicit value, and any unrecognized
    environment value, falls back to "en"."""
    if explicit == "ko":
        return "ko"
    if explicit is not None:
        return "en"
    environ = os.environ if environ is None else environ
    xswap_lang = environ.get("XSWAP_LANG")
    if xswap_lang:
        return "ko" if xswap_lang.lower().startswith("ko") else "en"
    for var in ("LC_ALL", "LANG"):
        value = environ.get(var)
        if value:
            return "ko" if value.lower().startswith("ko") else "en"
    return "en"


def weekly(row):
    for bucket in row['buckets']:
        if bucket['id'] == 'codex':
            for window in bucket['windows']:
                if window['windowMinutes'] == 10080:
                    return window
    return None


def summary(row, now=None, lang="en"):
    now = time.time() if now is None else now
    window = weekly(row)
    left = window['remainingPercent'] if window else None
    used = 100 - left if left is not None else None
    status = row['status']
    note = (_t(lang, 'disabled') if status == 'disabled' else _t(lang, 'signin_required') if status == 'not signed in' else
            _t(lang, 'offline') if status == 'offline' else
            _t(lang, 'unavailable') if not is_ok(status) else _t(lang, 'no_weekly_data'))
    bar = ''
    if used is not None:
        filled = min(20, max(0, int(used / 5 + .5)))
        bar = '█' * filled + '░' * (20 - filled)
        note = _t(lang, 'used_left', used=used, left=left)
    reset = ''
    if window and window['resetsAt']:
        remaining = window['resetsAt'] - now
        if remaining <= 0:
            reset = _t(lang, 'reset_pending')
        else:
            minutes = max(1, int(remaining / 60))
            days, rest = divmod(minutes, 1440)
            hours, mins = divmod(rest, 60)
            duration = (_t(lang, 'duration_days_hours', days=days, hours=hours) if days else
                        _t(lang, 'duration_hours_mins', hours=hours, mins=mins) if hours else
                        _t(lang, 'duration_mins', mins=mins))
            reset = _t(lang, 'resets_in', duration=duration)
    return {'name': row['name'], 'selected': row.get('active', False), 'used': used,
            'remaining': left, 'bar': bar, 'summary': note, 'reset': reset,
            'tone': 'red' if used is not None and used >= 90 else 'yellow' if used is not None and used >= 80 else 'green' if used is not None else 'gray',
            'exhausted': left == 0, 'cached': row.get('cached', False), 'fetchedAt': row.get('fetchedAt')}


def policy_label(settings, lang="en"):
    if not settings.get('enabled'):
        return _t(lang, 'autoswitch_off')
    threshold = settings.get('weeklyRemainingThreshold', 0)
    if threshold:
        return _t(lang, 'autoswitch_on_threshold', pct=100 - threshold)
    return _t(lang, 'autoswitch_on_limit')


def render(rows, settings, include_spark=False, details=False, color=None, now=None, lang="en"):
    if not rows:
        return _t(lang, 'no_accounts')
    if color is None:
        color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    now = time.time() if now is None else now
    lines = [_t(lang, 'header'), _t(lang, 'legend'), '']
    for row in sorted(rows, key=lambda row: not row.get('active', False)):
        item = summary(row, now, lang)
        badge = _t(lang, 'selected') if item['selected'] else ''
        if item['exhausted']: badge += (' · ' if badge else '') + _t(lang, 'exhausted')
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
    lines.append(policy_label(settings, lang))
    lines.append(_t(lang, 'footer'))
    return '\n'.join(lines)


def dashboard(manager, lang="en"):
    from xswap_cli import status_data
    state = status_data(manager, cleanup=False)
    rows = manager.account_rows()
    return {'accounts': [summary(row, lang=lang) for row in sorted(rows, key=lambda r: not r['active'])],
            'policy': policy_label(state, lang),
            'sessions': [{'account': s['account'], 'surface': s['surface']} for s in state['sessions'] if s['running']],
            'updatedAt': time.time()}
