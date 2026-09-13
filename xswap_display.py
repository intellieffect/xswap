"""Weekly-first presentation. Never substitute a short window for a week."""
from collections import Counter
from datetime import datetime
import os
import sys
import time
from xswap_usage import AUTH_FAILED_STATUS, is_ok, reset_credit_lines, usage_lines

# Every user-visible string in this module lives here, keyed by a short id.
# `ko` values are the original hard-coded strings, preserved verbatim.
STRINGS = {
    "en": {
        "no_accounts": "No accounts registered. xswap register main",
        "header": "Accounts · weekly remaining",
        "legend": "█ remaining  ░ used",
        "weekly_line": "     ├ Weekly : {value}",
        "reset_line": "     └ Resets : {value}",
        "remaining": "{left:g}% remaining",
        "sessions": "Running sessions · detected by xswap",
        "no_sessions": "  No running sessions detected",
        "session_count": "{count} sessions",
        "session_single": "1 session",
        "switch_hint": "Switch: xswap switch <number>",
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
        "no_fallback": "Warning: no fallback account in the pool has weekly quota above the {reserve:g}% reserve ({names})",
    },
    "ko": {
        "no_accounts": "등록된 계정이 없습니다. xswap register main",
        "header": "계정 · 주간 잔여량",
        "legend": "█ 남음  ░ 사용",
        "weekly_line": "     ├ 주간 잔여량 : {value}",
        "reset_line": "     └ 초기화      : {value}",
        "remaining": "{left:g}% 남음",
        "sessions": "실행 세션 · xswap 감지",
        "no_sessions": "  감지된 실행 세션 없음",
        "session_count": "{count}개 세션",
        "session_single": "1개 세션",
        "switch_hint": "전환: xswap switch <번호>",
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
        "no_fallback": "경고: 풀의 예비 계정 중 주간 잔여량이 예비율 {reserve:g}%를 넘는 계정이 없습니다 ({names})",
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


UNAVAILABLE_PREFIX = 'usage unavailable: '


def status_note(row, lang="en"):
    """One line for a row without a usable weekly gauge, naming the fix when there is one.

    `xswap list` used to collapse every failure to "unavailable"; a login that the
    usage service rejects now reads as sign-in required with the login command, both
    for the live failure and for the recorded state served to --cached/--offline.
    """
    status = row['status']
    if status == 'disabled':
        return _t(lang, 'disabled')
    if status == 'offline':
        return _t(lang, 'offline')
    if is_ok(status):
        return _t(lang, 'no_weekly_data')
    reason = status[len(UNAVAILABLE_PREFIX):] if status.startswith(UNAVAILABLE_PREFIX) else status
    if status in ('not signed in', AUTH_FAILED_STATUS) or 'sign in' in reason:
        return _t(lang, 'signin_required') + f" · xswap login {row['name']}"
    return _t(lang, 'unavailable') + (f' · {reason}' if reason else '')


def fallback_warning(rows, settings, now=None, lang="en"):
    """Say when automatic switching is on but no other pool account could take over."""
    if not settings.get('enabled'):
        return None
    reserve = settings.get('weeklyRemainingThreshold') or 0
    by_name = {r['name']: r for r in rows}
    selected = next((r['name'] for r in rows if r.get('active')), None)
    others = [n for n in settings.get('accounts') or [] if n != selected and n in by_name]
    if not others:
        return None
    for name in others:
        left = summary(by_name[name], now, lang)['remaining']
        if left is None or left > reserve:  # Unknown quota is not proof of exhaustion.
            return None
    return _t(lang, 'no_fallback', reserve=reserve, names=', '.join(others))


def summary(row, now=None, lang="en"):
    now = time.time() if now is None else now
    window = weekly(row)
    left = window['remainingPercent'] if window else None
    used = 100 - left if left is not None else None
    note = status_note(row, lang)
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


def render(rows, settings, include_spark=False, details=False, color=None, now=None, lang="en", sessions=None, hints=None):
    if not rows:
        return _t(lang, 'no_accounts')
    if color is None:
        color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    now = time.time() if now is None else now
    lines = [_t(lang, 'header'), _t(lang, 'legend'), '']
    for index, row in enumerate(rows, 1):
        item = summary(row, now, lang)
        badge = _t(lang, 'selected') if item['selected'] else ''
        if item['exhausted']: badge += (' · ' if badge else '') + _t(lang, 'exhausted')
        name = f"{row.get('slot', index)}. {item['name']}"
        if item['cached'] and item['fetchedAt'] is not None:
            name += f" (cached {max(0, int(now - item['fetchedAt']))}s ago)"
        lines.append(('▸ ' if item['selected'] else '  ') + name + ' · ' + row['identity'] + ('  [' + badge + ']' if badge else ''))
        if item['remaining'] is not None:
            filled = min(20, max(0, int(item['remaining'] / 5 + .5)))
            text = '[' + '█' * filled + '░' * (20 - filled) + ']  ' + _t(lang, 'remaining', left=item['remaining'])
        else:
            text = item['summary']
        if color:
            code = {'green': 32, 'yellow': 33, 'red': 31, 'gray': 90}[item['tone']]
            text = f'\033[{code}m{text}\033[0m'
        lines.append(_t(lang, 'weekly_line', value=text))
        reset = item['reset'] or _t(lang, 'unavailable')
        window = weekly(row)
        if window and window['resetsAt']:
            try:
                stamp = datetime.fromtimestamp(window['resetsAt']).astimezone().strftime('%m/%d %H:%M %Z')
                reset = f'{stamp} · {reset}'
            except (ValueError, OverflowError, OSError):
                pass
        lines.append(_t(lang, 'reset_line', value=reset))
        if details and is_ok(row['status']):
            lines.extend('  ' + line for line in reset_credit_lines(row.get('resetCredits')))
        if details:
            buckets = [b for b in row['buckets'] if include_spark or 'spark' not in (b['id'] + b['name']).lower()]
            lines.extend('  ' + line for line in usage_lines(buckets))
        elif include_spark:
            buckets = [b for b in row['buckets'] if 'spark' in (b['id'] + b['name']).lower()]
            if buckets: lines.extend('  ' + line for line in usage_lines(buckets))
        lines.append('')
    if sessions is not None:
        lines.append(_t(lang, 'sessions'))
        counts = Counter((session.get('surface'), session.get('account')) for session in sessions if session.get('running'))
        for (surface, account), count in counts.items():
            label = {'cli': 'CLI', 'desktop': 'Desktop'}.get(surface, 'Unknown')
            lines.append(f"  ● {label} · {account or 'unknown'} · " + _t(lang, 'session_single' if count == 1 else 'session_count', count=count))
            # A session still running an older bridge sits under its group with the
            # command that reopens it on the installed code (xswap_cli.bridge_hints).
            for hint in hints or []:
                if (hint.get('surface'), hint.get('account')) != (surface, account):
                    continue
                text = f"⚠ {hint['hint']}"
                if color:
                    text = f'\033[33m{text}\033[0m'
                lines.append('    ' + text)
        if not counts:
            lines.append(_t(lang, 'no_sessions'))
        lines.append('')
    lines.append(_t(lang, 'switch_hint'))
    lines.append(policy_label(settings, lang))
    warning = fallback_warning(rows, settings, now, lang)
    if warning:
        lines.append(warning)
    lines.append(_t(lang, 'footer'))
    return '\n'.join(lines)


def dashboard(manager, lang="en"):
    from xswap_cli import status_data
    # The menu bar polls this every five minutes: a natural background sweep.
    state = status_data(manager)
    rows = manager.account_rows()
    return {'accounts': [summary(row, lang=lang) for row in sorted(rows, key=lambda r: not r['active'])],
            'policy': policy_label(state, lang),
            'sessions': [{'account': s['account'], 'surface': s['surface']} for s in state['sessions'] if s['running']],
            'updatedAt': time.time()}
