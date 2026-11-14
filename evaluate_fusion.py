"""Evaluate candidate grouping against independently reviewed pairs.

CSV columns: lat_a,lon_a,utc_a,source_a,lat_b,lon_b,utc_b,source_b,same_fire
same_fire: 1 for confirmed same incident, 0 for confirmed separate incidents.
The dataset must be curated independently; do not label pairs using Fire Fusion output.
Run: python evaluate_fusion.py review_pairs.csv
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_utc(value):
    value = value.strip().replace('Z', '+00:00')
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError('utc timestamps must have explicit UTC timezone')
    return int(date.astimezone(timezone.utc).timestamp())


def parse_pair(row, suffix):
    source = row[f'source_{suffix}'].strip().upper()
    if source not in ('VIIRS', 'MODIS'):
        raise ValueError('source must be VIIRS or MODIS')
    lat, lon = float(row[f'lat_{suffix}']), float(row[f'lon_{suffix}'])
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('coordinate out of range')
    return {'lat': lat, 'lon': lon, 'ts': parse_utc(row[f'utc_{suffix}']), 'source': source}


def score_pairs(filename, matcher):
    counts = {'TP': 0, 'TN': 0, 'FP': 0, 'FN': 0}
    issues = []
    with open(filename, newline='', encoding='utf-8-sig') as f:
        for number, row in enumerate(csv.DictReader(f), start=2):
            try:
                label = row['same_fire'].strip()
                if label not in ('0', '1'):
                    raise ValueError('same_fire must be 0 or 1')
                predicted = bool(matcher(parse_pair(row, 'a'), parse_pair(row, 'b')))
                actual = label == '1'
                category = ('TP' if actual else 'FP') if predicted else ('FN' if actual else 'TN')
                counts[category] += 1
                if predicted != actual:
                    issues.append({'row': number, 'error': category})
            except (KeyError, ValueError, TypeError) as err:
                raise ValueError(f'CSV row {number}: {err}') from err
    total = sum(counts.values())
    if not total:
        raise ValueError('No reviewed pairs in dataset; accuracy cannot be calculated')
    tp, fp, fn = counts['TP'], counts['FP'], counts['FN']
    # Guard undefined metrics: report null rather than misleading perfect scores.
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {'reviewed_pairs': total, 'confusion_matrix': counts,
            'pair_precision': precision, 'pair_recall': recall,
            'false_merges': fp, 'missed_matches': fn,
            'misclassified_rows': issues,
            'scope': 'pairwise matching only; NOT end-to-end fire event detection accuracy',
            'warning': 'Meaningful only with independent, representative ground truth'}


if __name__ == '__main__':
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        sys.exit('Usage: python evaluate_fusion.py path/to/independently_reviewed_pairs.csv')
    # Import from existing FIRECTOR world server, preserving its connections.
    from world_server import observations_compatible
    try:
        print(json.dumps(score_pairs(sys.argv[1], observations_compatible), indent=2))
    except ValueError as error:
        sys.exit(str(error))
