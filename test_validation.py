"""Run: python -m unittest -v test_fusion test_validation"""
import csv
import tempfile
import unittest
from pathlib import Path
from evaluate_fusion import score_pairs, parse_utc

class ValidationTests(unittest.TestCase):
    def test_timezone_handling(self):
        self.assertEqual(parse_utc('2026-01-01T12:00:00Z'), parse_utc('2026-01-01T14:00:00+02:00'))

    def test_no_benchmark_not_perfect(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'cases.csv'
            with p.open('w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['lat_a','lon_a','utc_a','source_a','lat_b','lon_b','utc_b','source_b','same_fire'])
            with self.assertRaisesRegex(ValueError, 'No reviewed pairs'):
                score_pairs(p, lambda a,b: True)

    def test_true_and_false_merge_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'cases.csv'
            with p.open('w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['lat_a','lon_a','utc_a','source_a','lat_b','lon_b','utc_b','source_b','same_fire'])
                for label in ('1','0','1'):
                    writer.writerow([35,33,'2026-01-01T12:00:00Z','VIIRS',35,33,'2026-01-01T12:01:00Z','MODIS',label])
            result = score_pairs(p, lambda a,b: True)
            self.assertEqual(result['confusion_matrix'], {'TP':2,'TN':0,'FP':1,'FN':0})
            self.assertAlmostEqual(result['pair_precision'],2/3)
            self.assertEqual(result['pair_recall'],1)

    def test_reject_naive_local_timestamp(self):
        with self.assertRaises(ValueError):
            parse_utc('2026-01-01T12:00:00')

if __name__ == '__main__':unittest.main()
