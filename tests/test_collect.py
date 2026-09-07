import datetime as dt
import importlib.machinery
import importlib.util
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('collector', str(Path(__file__).resolve().parents[1] / 'bin/collect'))
spec = importlib.util.spec_from_loader(loader.name, loader)
c = importlib.util.module_from_spec(spec)
loader.exec_module(c)


class CollectorTests(unittest.TestCase):
    def test_management_key_only(self):
        with patch.dict(c.os.environ, {'OPENROUTER_API_KEY': 'inference'}, clear=True), patch.object(c, 'read_config', return_value={'apiKey': 'inference'}), patch.object(c, 'probe_activity') as probe:
            self.assertEqual(c.management_key(), '')
            self.assertEqual(c.collect_usage('7d', False)['usageStatusText'], 'Management key required')
            probe.assert_not_called()

    def test_daily_aggregation_and_zero_days(self):
        window = {'start': '2026-09-01T00:00:00Z', 'end': '2026-09-03T12:00:00Z'}
        rows = [{'date__day': '2026-09-01T00:00:00Z', 'total_usage': 0.1, 'request_count': 2, 'tokens_total': 10} for _ in range(40)]
        rows.append({'created_at__day': '2026-09-03', 'total_usage': 1, 'request_count': 1, 'tokens_total': 5})
        days = c.daily_usage({'data': rows}, window)
        self.assertEqual([day['cost'] for day in days], [4, 0, 1])
        self.assertEqual(days[0]['prompts'], 80)
        self.assertEqual(days[0]['messageCount'], 400)

    def test_ninety_days_are_not_truncated_locally(self):
        window = c.period_time_range('3mo')
        start = dt.date.fromisoformat(window['start'][:10])
        rows = [{'date__day': (start + dt.timedelta(days=i)).isoformat(), 'total_usage': 1} for i in range(90)]
        self.assertEqual(sum(day['cost'] for day in c.daily_usage({'data': rows}, window)), 90)
        self.assertTrue(window['start'].endswith('T00:00:00Z'))

    def test_partial_or_malformed_daily_response_rejected(self):
        window = c.period_time_range('7d')
        for result in ({}, {'data': [], 'metadata': {'truncated': True}}, {'data': [{'total_usage': 1}]}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                c.daily_usage(result, window)

    def test_account_query_has_no_key_filter_and_totals_match(self):
        calls = []
        def request(path, key, method='GET', body=None):
            calls.append((path, key, body))
            if body.get('granularity') == 'day':
                return {'data': [{'date__day': body['time_range']['start'], 'total_usage': 40, 'request_count': 80, 'tokens_total': 400}]}
            if body['metrics'] == ['cache_hit_rate']:
                return {'data': [{'cache_hit_rate': 0.5}]}
            return {'data': []}
        with patch.object(c, 'api_request', side_effect=request):
            activity = c.probe_activity('management', '7d')
        self.assertEqual(activity['spend'], 40)
        self.assertEqual(activity['requests'], 80)
        self.assertEqual(activity['tokens'], 400)
        daily = next(body for _, _, body in calls if body.get('granularity') == 'day')
        self.assertNotIn('filters', daily)
        self.assertNotIn('dimensions', daily)
        self.assertTrue(all(key == 'management' for _, key, _ in calls))

    def test_cache_isolation_force_and_auth_rejection(self):
        records = {}
        activity = {'recentDays': [], 'spend': 40}
        def read(path, age):
            return records.get(str(path))
        def write(path, record):
            records[str(path)] = record
        with patch.object(c, 'cache_root', return_value=Path('/unused')), patch.object(c, 'read_fresh_json', side_effect=read), patch.object(c, 'write_json', side_effect=write), patch.object(c, 'management_key', return_value='key-one') as key, patch.object(c, 'probe_activity', return_value=activity) as probe, patch.object(c, 'probe_account', return_value={'ok': True, 'balance': {'remaining': 10}}):
            self.assertTrue(c.collect_usage('7d', False)['ready'])
            c.collect_usage('7d', False)
            self.assertEqual(probe.call_count, 1)
            c.collect_usage('7d', True)
            self.assertEqual(probe.call_count, 2)
            probe.side_effect = urllib.error.URLError('offline')
            stale = c.collect_usage('7d', True)
            self.assertEqual(stale['activity']['spend'], 40)
            self.assertIn('Showing cached totals from', stale['authHelpText'])
            key.return_value = 'key-two'
            self.assertNotIn('activity', c.collect_usage('7d', False))
            key.return_value = 'key-one'
            probe.side_effect = urllib.error.HTTPError('url', 403, 'Forbidden', {}, None)
            rejected = c.collect_usage('7d', True)
            self.assertNotIn('activity', rejected)
            self.assertNotIn('balance', rejected)
            self.assertNotIn('activity', c.collect_usage('7d', False))

    def test_optional_details_failure_preserves_daily_usage(self):
        def request(path, key, method='GET', body=None):
            if body.get('granularity') == 'day':
                return {'data': []}
            raise TimeoutError('timeout')
        with patch.object(c, 'api_request', side_effect=request):
            activity = c.probe_activity('management', '7d')
        self.assertEqual(len(activity['recentDays']), 7)
        self.assertEqual(activity['spend'], 0)
        self.assertIsNone(activity['cacheHitRate'])
        self.assertIn('unavailable', activity['helpText'])

    def test_http_response_unwraps_analytics_envelope(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.geturl.return_value = c.API_BASE_URL + '/analytics/query'
        response.read.return_value = b'{"data":{"data":[],"metadata":{"truncated":false}}}'
        response.__enter__.return_value = response
        with patch.object(c._opener, 'open', return_value=response) as opened:
            result = c.api_request('/analytics/query', 'management', 'POST', {'granularity': 'day'})
        self.assertEqual(c.analytics_rows(result), [])
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer management')

    def test_breakdowns_include_all_rows_sorted_by_spend(self):
        def request(path, key, method='GET', body=None):
            if body.get('granularity') == 'day' or not body.get('dimensions'):
                return {'data': []}
            self.assertIn('total_usage', body['metrics'])
            self.assertEqual(body['order_by']['field'], 'total_usage')
            self.assertEqual(body['limit'], 1000)
            field = body['dimensions'][0]
            return {'data': [{field: f'Entry {i}', 'total_usage': i, 'tokens_total': 1000-i} for i in range(40)]}
        with patch.object(c, 'api_request', side_effect=request):
            activity = c.probe_activity('management', '7d')
        for field in ('topKeys', 'topModels', 'topApps'):
            self.assertEqual(len(activity[field]), 40)
            self.assertEqual(activity[field][0]['cost'], 39)
            self.assertEqual(sum(row['cost'] for row in activity[field]), 780)
        self.assertEqual(c.ranked_spend([{'app': None, 'total_usage': 2}], 'app', 1000)[0]['name'], 'Unknown')

    def test_truncated_breakdown_is_an_error_not_an_empty_success(self):
        def request(path, key, method='GET', body=None):
            if body.get('dimensions') == ['api_key_id']:
                return {'data': [], 'metadata': {'truncated': True}}
            return {'data': []}
        with patch.object(c, 'api_request', side_effect=request):
            activity = c.probe_activity('management', '7d')
        self.assertIn('keys', activity['breakdownErrors'])
        self.assertEqual(activity['topKeys'], [])
        self.assertNotIn('models', activity['breakdownErrors'])
        self.assertEqual(len(activity['recentDays']), 7)

    def test_short_period_windows(self):
        window = c.period_time_range('24h')
        start = dt.datetime.fromisoformat(window['start'])
        end = dt.datetime.fromisoformat(window['end'])
        self.assertEqual(end - start, dt.timedelta(hours=24))
        window = c.period_time_range('3d')
        days = c.daily_usage({'data': []}, window)
        self.assertEqual(len(days), 3)
        self.assertTrue(window['start'].endswith('T00:00:00Z'))

    def test_rolling_window_uses_hourly_buckets_and_shared_range(self):
        windows = []
        def request(path, key, method='GET', body=None):
            windows.append(body['time_range'])
            if body.get('granularity'):
                self.assertEqual(body['granularity'], 'hour')
                self.assertEqual(body['limit'], 25)
                return {'data': [
                    {'created_at__hour': body['time_range']['start'], 'total_usage': 1},
                    {'date__hour': body['time_range']['end'], 'total_usage': 2}
                ]}
            return {'data': []}
        with patch.object(c, 'api_request', side_effect=request):
            activity = c.probe_activity('management', '24h')
        self.assertEqual(activity['spend'], 3)
        self.assertTrue(all(window == windows[0] for window in windows))

    def test_balance_uses_management_key_without_key_endpoint(self):
        with patch.object(c, 'api_request', return_value={'total_credits': 100, 'total_usage': 40}) as request, patch.object(c, 'read_config', return_value={}):
            result = c.probe_account('management')
        request.assert_called_once_with('/credits', 'management')
        self.assertEqual(result['balance']['remaining'], 60)


if __name__ == '__main__':
    unittest.main()
