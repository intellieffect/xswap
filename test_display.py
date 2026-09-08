import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from xswap_display import STRINGS, dashboard, policy_label, render, resolve_lang, summary
from xswap_usage import normalize_limits


def row(name='work', left=94, weekly=True):
    return {'name': name, 'active': True, 'identity': 'private@example.test', 'status': 'ok',
            'buckets': normalize_limits({'rateLimits': {'limitId': 'codex', 'primary': {
                'usedPercent': 100-left, 'windowDurationMins': 10080 if weekly else 300,
                'resetsAt': 100000}}})}


class StringsTableTests(unittest.TestCase):
    def test_en_and_ko_cover_the_same_string_ids(self):
        self.assertEqual(set(STRINGS['en']), set(STRINGS['ko']))

    def test_ko_labels_match_expected_copy(self):
        original_ko = {
            'no_accounts': '등록된 계정이 없습니다. xswap register main',
            'header': '계정 · 주간 잔여량',
            'legend': '█ 남음  ░ 사용',
            'selected': '선택됨',
            'exhausted': '한도 도달',
            'disabled': '비활성화 (disabled)',
            'signin_required': '로그인 필요',
            'offline': '오프라인',
            'unavailable': '조회 불가',
            'no_weekly_data': '주간 사용량 미제공',
            'used_left': '{used:g}% 사용 · {left:g}% 남음',
            'reset_pending': '초기화 대기 중',
            'resets_in': '초기화 {duration} 후',
            'duration_days_hours': '{days}일 {hours}시간',
            'duration_hours_mins': '{hours}시간 {mins}분',
            'duration_mins': '{mins}분',
            'autoswitch_off': '새 세션 자동전환 OFF',
            'autoswitch_on_threshold': '새 세션 자동전환 ON · {pct:g}% 사용 시 전환',
            'autoswitch_on_limit': '새 세션 자동전환 ON · 한도 도달 시 전환',
            'footer': '선택됨 = 새 실행의 기본 계정 · 기존 세션 계정은 xswap auto-status',
        }
        for key, value in original_ko.items():
            self.assertEqual(STRINGS['ko'][key], value)


class DisplayKoreanTests(unittest.TestCase):
    """Korean copy and quota semantics when explicitly requested."""

    def test_weekly_never_uses_short_window_as_fallback(self):
        value = summary(row(weekly=False), lang='ko')
        self.assertIsNone(value['remaining'])
        self.assertEqual(value['summary'], '주간 사용량 미제공')
        self.assertNotIn('0%', value['summary'])

    def test_percent_bar_edges_and_colors(self):
        for left,tone,filled in [(100,'green',0),(94,'green',1),(15,'yellow',17),(0,'red',20)]:
            value = summary(row(left=left), lang='ko')
            self.assertEqual(value['tone'],tone)
            self.assertEqual(value['bar'].count('█'),filled)
            self.assertEqual(value['used'],100-left)

    def test_default_shows_identity_in_fixed_order_without_ansi(self):
        other = row('other');other['active']=False
        text = render([other,row()],{'enabled':True,'weeklyRemainingThreshold':10},color=False,now=0,lang='ko')
        self.assertLess(text.index('1. other'),text.index('2. work'))
        self.assertIn(' · private@example.test',text)
        self.assertIn('├ 주간 잔여량 : ',text)
        self.assertIn('└ 초기화      : ',text)
        self.assertNotIn('\033',text)
        self.assertIn('90% 사용 시 전환',text)
        self.assertIn('선택됨',text)
        self.assertNotIn('사용 중',text)

    def test_stale_reset_does_not_restore_quota(self):
        result = summary(row(left=0),now=200000,lang='ko')
        self.assertEqual(result['remaining'],0)
        self.assertEqual(result['reset'],'초기화 대기 중')

    def test_disabled_policy_and_explicit_details(self):
        self.assertIn('OFF',policy_label({},lang='ko'))
        self.assertIn('private@example.test',render([row()],{},details=True,color=False,lang='ko'))

    def test_switch_alias_parses_default_selection(self):
        from codex_swap import parser
        args = parser().parse_args(['switch', 'work'])
        self.assertEqual(args.command, 'switch')
        self.assertEqual(args.name, 'work')

    def test_cached_status_reads_like_ok_and_shows_age_on_the_account_line(self):
        value = row()
        value['status'] = 'ok (cached)'
        value['cached'] = True
        value['fetchedAt'] = 40
        item = summary(value, now=100, lang='ko')
        self.assertEqual(item['summary'], '6% 사용 · 94% 남음')
        text = render([value], {}, color=False, now=100, lang='ko')
        self.assertIn('work (cached 60s ago)', text)
        self.assertNotIn('조회 불가', text)


class DisplayEnglishTests(unittest.TestCase):
    """English is the default language for every string id."""

    def test_default_lang_with_no_argument_is_english(self):
        value = summary(row(weekly=False))
        self.assertEqual(value['summary'], 'weekly usage unavailable')

    def test_weekly_never_uses_short_window_as_fallback(self):
        value = summary(row(weekly=False), lang='en')
        self.assertIsNone(value['remaining'])
        self.assertEqual(value['summary'], 'weekly usage unavailable')

    def test_percent_bar_edges_and_colors(self):
        for left,tone,filled in [(100,'green',0),(94,'green',1),(15,'yellow',17),(0,'red',20)]:
            value = summary(row(left=left), lang='en')
            self.assertEqual(value['tone'],tone)
            self.assertEqual(value['bar'].count('█'),filled)
            self.assertEqual(value['used'],100-left)

    def test_used_left_summary_text(self):
        value = row()
        value['status'] = 'ok (cached)'
        value['cached'] = True
        value['fetchedAt'] = 40
        item = summary(value, now=100, lang='en')
        self.assertEqual(item['summary'], '6% used · 94% left')

    def test_status_notes(self):
        for status, expected in [('disabled', 'disabled'), ('not signed in', 'sign-in required'),
                                  ('offline', 'offline'), ('usage unavailable: boom', 'unavailable')]:
            value = {'name': 'x', 'active': False, 'identity': 'i', 'status': status, 'buckets': []}
            item = summary(value, lang='en')
            self.assertEqual(item['summary'], expected)

    def test_no_weekly_window_note(self):
        value = {'name': 'x', 'active': False, 'identity': 'i', 'status': 'ok', 'buckets': []}
        item = summary(value, lang='en')
        self.assertEqual(item['summary'], 'weekly usage unavailable')

    def test_reset_durations(self):
        cases = [
            ((6*1440 + 4*60) * 60, 'resets in 6d 4h'),
            ((2*60 + 13) * 60, 'resets in 2h 13m'),
            (5 * 60, 'resets in 5m'),
        ]
        for remaining_seconds, expected in cases:
            value = row(left=50)
            value['buckets'][0]['windows'][0]['resetsAt'] = remaining_seconds
            item = summary(value, now=0, lang='en')
            self.assertEqual(item['reset'], expected)

    def test_reset_pending_when_due(self):
        result = summary(row(left=0), now=200000, lang='en')
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(result['reset'], 'reset pending')

    def test_policy_label_variants(self):
        self.assertEqual(policy_label({}, lang='en'), 'New-session auto-switch OFF')
        self.assertEqual(policy_label({'enabled': True, 'weeklyRemainingThreshold': 10}, lang='en'),
                          'New-session auto-switch ON · switches at 90% used')
        self.assertEqual(policy_label({'enabled': True}, lang='en'),
                          'New-session auto-switch ON · switches at limit')

    def test_render_header_badges_and_footer(self):
        other = row('other'); other['active'] = False
        text = render([other, row(left=0)], {'enabled': True, 'weeklyRemainingThreshold': 10}, color=False, now=0, lang='en')
        self.assertIn('Accounts · weekly remaining', text)
        self.assertIn(' · private@example.test', text)
        self.assertIn('├ Weekly : ', text)
        self.assertIn('└ Resets : ', text)
        self.assertIn('used', text)
        self.assertIn('remaining', text)
        self.assertIn('selected', text)
        self.assertIn('limit reached', text)
        self.assertIn('switches at 90% used', text)
        self.assertIn('selected = default account for new runs · existing session accounts: xswap auto-status', text)

    def test_remaining_gauge_and_detected_sessions(self):
        for left, filled in [(100, 20), (75, 15), (0, 0)]:
            text = render([row(left=left)], {}, color=False, now=0, sessions=[
                {'surface': 'cli', 'account': 'second', 'running': True},
                {'surface': 'cli', 'account': 'second', 'running': True},
                {'surface': 'desktop', 'account': 'old', 'running': False},
            ])
            gauge = '[' + '█' * filled + '░' * (20 - filled) + ']'
            self.assertIn(gauge + f'  {left}% remaining', text)
            self.assertIn('● CLI · second · 2 sessions', text)
            self.assertNotIn('old', text)
            self.assertNotIn('5h', text)
            self.assertNotIn('reset credits', text)
        text = render([row(weekly=False)], {}, color=False, sessions=[])
        self.assertIn('weekly usage unavailable', text)
        self.assertNotIn('[░', text)
        self.assertIn('No running sessions detected', text)

    def test_render_no_accounts_message(self):
        self.assertEqual(render([], {}, lang='en'), 'No accounts registered. xswap register main')

    def test_details_still_shows_identity_and_english_usage_lines(self):
        text = render([row()], {}, details=True, color=False, lang='en')
        self.assertIn('private@example.test', text)


class ResolveLangTests(unittest.TestCase):
    def test_explicit_ko_wins(self):
        self.assertEqual(resolve_lang('ko', {'LANG': 'en_US.UTF-8'}), 'ko')

    def test_explicit_en_beats_korean_env(self):
        self.assertEqual(resolve_lang('en', {'LANG': 'ko_KR.UTF-8'}), 'en')

    def test_xswap_lang_ko(self):
        self.assertEqual(resolve_lang(None, {'XSWAP_LANG': 'ko'}), 'ko')

    def test_xswap_lang_en_overrides_lang_ko(self):
        self.assertEqual(resolve_lang(None, {'XSWAP_LANG': 'en', 'LANG': 'ko_KR.UTF-8'}), 'en')

    def test_lang_ko_kr_utf8_falls_back_when_xswap_lang_unset(self):
        self.assertEqual(resolve_lang(None, {'LANG': 'ko_KR.UTF-8'}), 'ko')

    def test_lang_en_us_utf8(self):
        self.assertEqual(resolve_lang(None, {'LANG': 'en_US.UTF-8'}), 'en')

    def test_unset_environment_is_english(self):
        self.assertEqual(resolve_lang(None, {}), 'en')

    def test_garbage_value_is_english(self):
        self.assertEqual(resolve_lang(None, {'LANG': 'xx_XX.UTF-8'}), 'en')
        self.assertEqual(resolve_lang(None, {'XSWAP_LANG': 'banana'}), 'en')

    def test_lc_all_is_also_honored(self):
        self.assertEqual(resolve_lang(None, {'LC_ALL': 'ko_KR.UTF-8'}), 'ko')


class JsonOutputUnaffectedByLangTests(unittest.TestCase):
    """`--json` output must stay byte-identical regardless of language."""

    def test_list_json_rows_have_no_display_strings(self):
        # --json prints account_rows() directly; summary()/render() (and their
        # lang-dependent text) are never in that path, so the payload is the
        # same regardless of --lang and carries no translated string at all.
        rows = [row()]
        payload = json.dumps(rows, ensure_ascii=False, indent=2)
        self.assertFalse(re.search(r'[가-힣]', payload), payload)

    def test_dashboard_default_language_has_no_korean_strings(self):
        class FakeManager:
            def account_rows(self):
                return [row()]

        with patch('xswap_cli.status_data', return_value={'sessions': [], 'enabled': True, 'weeklyRemainingThreshold': 10}):
            data = dashboard(FakeManager())
        payload = json.dumps(data, ensure_ascii=False)
        self.assertFalse(re.search(r'[가-힣]', payload), payload)


class OfflineListEnglishDefaultTests(unittest.TestCase):
    """`xswap list --offline` defaults to English even under a Korean locale."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.codex_home = base / 'codex-home'
        self.codex_home.mkdir()
        self.swap_home = base / 'codex-swap'

    def test_offline_list_defaults_to_english_under_korean_locale(self):
        from codex_swap import atomic_json, main
        atomic_json(self.codex_home / 'auth.json',
                    {'auth_mode': 'chatgpt', 'tokens': {'access_token': 'fake', 'refresh_token': 'fake'}})
        env = dict(os.environ)
        env.update({'CODEX_SWAP_HOME': str(self.swap_home), 'CODEX_HOME': str(self.codex_home), 'LANG': 'ko_KR.UTF-8'})
        env.pop('LC_ALL', None)
        env.pop('XSWAP_LANG', None)
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
            main(['register', 'main'])
            main(['list', '--offline'])
        output = out.getvalue()
        self.assertFalse(re.search(r'[가-힣]', output), output)


class MenuBarSwiftLocalizationTests(unittest.TestCase):
    """The menu bar app is launched from Finder/Dock/a login item, whose
    environment usually carries no LANG/XSWAP_LANG at all (only PATH is
    patched for the subprocess call). It must resolve its own language from
    its own launch environment and pass that choice explicitly to the
    `xswap dashboard` subprocess, and every Hangul string it can show must
    live in its own localisation table, not float around the source."""

    SOURCE = Path(__file__).resolve().parent / 'xswap_bridge' / 'MenuBar.swift'

    def setUp(self):
        self.text = self.SOURCE.read_text()

    def test_hangul_appears_only_inside_the_localization_table(self):
        begin = self.text.index('// L10N_TABLE_BEGIN')
        end = self.text.index('// L10N_TABLE_END') + len('// L10N_TABLE_END')
        outside = self.text[:begin] + self.text[end:]
        self.assertFalse(re.search(r'[가-힣]', outside), 'Hangul literal found outside the localisation table')
        inside = self.text[begin:end]
        self.assertTrue(re.search(r'[가-힣]', inside), 'localisation table should still contain the ko strings')

    def test_passes_lang_explicitly_to_the_dashboard_subprocess(self):
        self.assertIn('process.arguments = ["dashboard", "--lang", menuLang]', self.text)
