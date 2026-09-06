import unittest
from xswap_display import render, summary, policy_label
from xswap_usage import normalize_limits


def row(name='work', left=94, weekly=True):
    return {'name': name, 'active': True, 'identity': 'private@example.test', 'status': 'ok',
            'buckets': normalize_limits({'rateLimits': {'limitId': 'codex', 'primary': {
                'usedPercent': 100-left, 'windowDurationMins': 10080 if weekly else 300,
                'resetsAt': 100000}}})}


class DisplayTests(unittest.TestCase):
    def test_weekly_never_uses_short_window_as_fallback(self):
        value = summary(row(weekly=False))
        self.assertIsNone(value['remaining'])
        self.assertEqual(value['summary'], '주간 사용량 미제공')
        self.assertNotIn('0%', value['summary'])

    def test_percent_bar_edges_and_colors(self):
        for left,tone,filled in [(100,'green',0),(94,'green',1),(15,'yellow',17),(0,'red',20)]:
            value = summary(row(left=left))
            self.assertEqual(value['tone'],tone)
            self.assertEqual(value['bar'].count('█'),filled)
            self.assertEqual(value['used'],100-left)

    def test_default_is_private_selected_first_and_no_ansi_in_pipe(self):
        other = row('other');other['active']=False
        text = render([other,row()],{'enabled':True,'weeklyRemainingThreshold':10},color=False,now=0)
        self.assertLess(text.index('work'),text.index('other'))
        self.assertNotIn('private@example.test',text)
        self.assertNotIn('\033',text)
        self.assertIn('90% 사용 시 전환',text)
        self.assertIn('선택됨',text)
        self.assertNotIn('사용 중',text)

    def test_stale_reset_does_not_restore_quota(self):
        result = summary(row(left=0),now=200000)
        self.assertEqual(result['remaining'],0)
        self.assertEqual(result['reset'],'초기화 대기 중')

    def test_disabled_policy_and_explicit_details(self):
        self.assertIn('OFF',policy_label({}))
        self.assertIn('private@example.test',render([row()],{},details=True,color=False))

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
        item = summary(value, now=100)
        self.assertEqual(item['summary'], '6% 사용 · 94% 남음')
        text = render([value], {}, color=False, now=100)
        self.assertIn('work (cached 60s ago)', text)
        self.assertNotIn('조회 불가', text)
