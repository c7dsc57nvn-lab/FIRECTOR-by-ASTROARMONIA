import json, unittest
from unittest import mock
import world_server as ws

def ev(i, lat, lon, n=2):
    return {'id': i, 'lat': lat, 'lon': lon, 'count': n, 'sources': ['VIIRS'], 'latest': 1794680000,
            'oldest': 1794680000, 'examples': [], 'snapshot_group_id': f'g{i}', 'ambiguous': False}

def feat(x, y, frp=12.5):
    return json.dumps([{'data': [{'x': x, 'y': y, 'value': frp}]}])

class MeteosatTests(unittest.TestCase):
    OK = {'reachable': True, 'wms_layer': 'FRP'}
    def run_merge(self, events, responses):
        calls = iter(responses)
        with mock.patch.object(ws, 'meteosat_wms_status', return_value=self.OK), \
             mock.patch.object(ws, '_msg_feature', side_effect=lambda *a: next(calls)):
            return ws.meteosat_merge(events)
    def test_same_pixel_merges(self):
        out, st = self.run_merge([ev(1, 35, 33), ev(2, 35.02, 33)], [feat(1, 2), feat(1, 2)])
        self.assertEqual(len(out), 1); self.assertEqual(out[0]['count'], 4); self.assertEqual(st['groups_merged'], 1)
    def test_different_pixel_stays_separate(self):
        out, _ = self.run_merge([ev(1, 35, 33), ev(2, 35.02, 33)], [feat(1, 2), feat(5, 2)])
        self.assertEqual(len(out), 2)
    def test_no_fire_pixel_no_merge(self):
        out, _ = self.run_merge([ev(1, 35, 33), ev(2, 35.02, 33)], ['[]', '[]'])
        self.assertEqual(len(out), 2)
    def test_far_groups_never_queried(self):
        out, st = self.run_merge([ev(1, 35, 33), ev(2, 35.5, 33)], [])
        self.assertEqual((len(out), st['checked']), (2, 0))
    def test_unavailable_leaves_events(self):
        with mock.patch.object(ws, 'meteosat_wms_status', return_value={'reachable': False}):
            out, st = ws.meteosat_merge([ev(1, 35, 33)])
        self.assertEqual((len(out), st['status']), (1, 'meteosat_unavailable'))
    def test_missing_pixel_id_is_conservative(self):
        no_id = json.dumps([{'value': 9.0}])
        out, st = self.run_merge([ev(1, 35, 33), ev(2, 35.02, 33)], [no_id, no_id])
        self.assertEqual(len(out), 2); self.assertEqual(st['status'], 'pixel_id_not_in_response')
if __name__ == '__main__': unittest.main()
