"""Weather-only overlay: synthetic tests; NOT real-world fire prediction validation."""
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import world_server as ws

HOT = {'temp_c': 38, 'humidity_pct': 12, 'wind_kmh': 35, 'rain_3d_mm': 0}
WET = {'temp_c': 12, 'humidity_pct': 80, 'wind_kmh': 5, 'rain_3d_mm': 30}

class WeatherHeatTests(unittest.TestCase):
    def setUp(self):
        ws._weather_heat_cache.clear()

    def test_hot_dry_windy_weather_highlights(self):
        self.assertEqual(ws.weather_conditions_score(HOT)[0], 'very_high')

    def test_wet_cool_conditions_not_highlighted(self):
        self.assertEqual(ws.weather_conditions_score(WET)[0], 'low')

    def test_missing_weather_no_guess(self):
        self.assertEqual(ws.weather_conditions_score(None), (None, None))
        self.assertEqual(ws.weather_conditions_score({'temp_c': 38}), (None, None))

    def test_heatmap_not_based_on_fire_detections(self):
        with mock.patch.object(ws, 'weather_points', side_effect=lambda pts:[HOT]*len(pts)) as api:
            data=ws.weather_grid(32, 34, 34, 36, 7)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['weather_available_points'], 42)
        self.assertEqual(len(data['areas']), 42)
        self.assertEqual(len(api.call_args.args[0]), 42)

    def test_no_weather_no_highlight_or_false_zero_risk(self):
        with mock.patch.object(ws, 'weather_points', side_effect=lambda pts:[None]*len(pts)):
            data=ws.weather_grid(32, 34, 34, 36, 7)
        self.assertEqual(data['status'], 'weather_unavailable')
        self.assertEqual(data['areas'], [])

    def test_zoom_out_skips_remote_weather_calls(self):
        with mock.patch.object(ws, 'weather_points') as api:
            data=ws.weather_grid(-180, -80, 180, 80, 2)
        self.assertEqual(data['status'], 'zoom_in')
        api.assert_not_called()

    def test_endpoint_cache_reuses_weather(self):
        with mock.patch.object(ws, 'weather_points', side_effect=lambda pts:[WET]*len(pts)) as api:
            a=ws.world_weather_heat(32,34,34,36,7)
            b=ws.world_weather_heat(32,34,34,36,7)
        self.assertEqual(a,b)
        self.assertEqual(api.call_count, 1)

    def test_validates_bad_query_bounds(self):
        client=TestClient(ws.app)
        result=client.get('/api/world/weather-heat',params={
            'west':'-400','east':'34','south':'34','north':'36','zoom':'7'})
        self.assertEqual(result.status_code, 422)

if __name__=='__main__': unittest.main()
