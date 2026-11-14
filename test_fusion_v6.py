import unittest
from datetime import datetime, timezone
from world_server import (fuse_observations, observations_compatible,
                          deduplicate_exact_records, distance_km)

T = 1794680000

def obs(lat, lon, dt=0, source='VIIRS', scan='0.45', track='0.45'):
    return {'lat':lat,'lon':lon,'ts':T+dt,'source':source,
            'datetime':datetime.fromtimestamp(T+dt,timezone.utc).isoformat(),
            'confidence':'n','frp':'5','scan_km':scan,'track_km':track,
            'daynight':'D'}

class FusionV6Tests(unittest.TestCase):
    def test_twenty_nearby_pixels_combine(self):
        # Repeated adjacent pixels within ~1.3km at the same acquisition time.
        p=[obs(35+(i%5)*0.0025,33+(i//5)*0.0025,i%3*60) for i in range(20)]
        events=fuse_observations(p)
        self.assertLess(len(events),20)
        self.assertEqual(sum(e['count'] for e in events),20)
    def test_far_fires_remain_separate(self):
        p=[obs(35+i*0.0003,33) for i in range(10)]+[obs(35.06+i*0.0003,33) for i in range(10)]
        e=fuse_observations(p)
        self.assertEqual(len(e),2)
    def test_same_position_different_pass_groups(self):
        p=[obs(35,33), obs(35.001,33.001, 5*3600)]
        self.assertEqual(len(fuse_observations(p)),1)
    def test_over_eight_hours_not_grouped(self):
        p=[obs(35,33),obs(35.0002,33.0002,9*3600)]
        self.assertEqual(len(fuse_observations(p)),2)
    def test_no_far_chain(self):
        p=[obs(35+i*0.008,33, i*60) for i in range(8)]
        e=fuse_observations(p)
        self.assertGreater(len(e),1)
        self.assertEqual(sum(q['count'] for q in e),len(p))
    def test_exact_rows_removed(self):
        p=obs(35,33)
        self.assertEqual(deduplicate_exact_records([p,p.copy()])[1],1)
    def test_pixel_size_matters(self):
        small1=obs(35,33,scan='.35',track='.35')
        small2=obs(35.009,33,scan='.35',track='.35')
        large1=obs(35,33,source='MODIS',scan='1.3',track='1.4')
        large2=obs(35.009,33,source='MODIS',scan='1.3',track='1.4')
        self.assertFalse(observations_compatible(small1,small2))
        self.assertTrue(observations_compatible(large1,large2))
    def test_dateline_centroid(self):
        p=[obs(1,179.999),obs(1,-179.999)]
        es=fuse_observations(p)
        self.assertEqual(len(es),1)
        self.assertAlmostEqual(abs(es[0]['lon']),180,places=2)

if __name__=='__main__': unittest.main()
