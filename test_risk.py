import unittest
from unittest import mock
import world_server as ws

HOT_DRY = {'temp_c': 38, 'humidity_pct': 12, 'wind_kmh': 30, 'rain_3d_mm': 0}
WET_COOL = {'temp_c': 14, 'humidity_pct': 80, 'wind_kmh': 5, 'rain_3d_mm': 25}

class RiskTests(unittest.TestCase):
    def test_hot_dry_windy_is_very_high(self):
        self.assertEqual(ws.risk_level(6, HOT_DRY)[0], 'very_high')
    def test_wet_cool_not_flagged(self):
        self.assertIsNone(ws.risk_level(3, WET_COOL)[0])
    def test_no_weather_needs_heavy_activity(self):
        self.assertIsNone(ws.risk_level(3, None)[0])
        self.assertEqual(ws.risk_level(12, None)[0], 'elevated')
    def test_build_risk_uses_weather(self):
        ev = [{'lat': 35.0, 'lon': 33.0, 'count': 6}, {'lat': 35.1, 'lon': 33.1, 'count': 2},
              {'lat': -20.0, 'lon': 140.0, 'count': 3}]
        with mock.patch.object(ws, 'weather_points', return_value=[HOT_DRY, WET_COOL]):
            areas = ws.build_risk(ev)
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0]['level'], 'very_high')
        self.assertEqual(areas[0]['observations'], 8)

if __name__ == '__main__': unittest.main()
