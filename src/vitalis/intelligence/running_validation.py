"""Aggregate-only, chronological validation against independent outcomes."""

from datetime import date
from math import isfinite
from statistics import mean, median


def _percentile(values, q):
    values = sorted(values)
    p = (len(values) - 1) * q
    i = int(p)
    return values[i] + (values[min(i + 1, len(values) - 1)] - values[i]) * (p - i)


def validate_race_predictions(rows, *, minimum_samples=20):
    usable, rejected, seen, groups = [], 0, set(), {}
    for row in rows:
        try:
            key = (row['subject'], row['outcome_id'])
            actual, prediction, baseline = (float(row[k]) for k in ('actual_seconds', 'predicted_seconds', 'baseline_seconds'))
            before = date.fromisoformat(row['feature_date'])
            outcome = date.fromisoformat(row['outcome_date'])
            valid = key not in seen and before < outcome and row['outcome_id'] not in row['fit_workout_ids']
            valid &= row['distance_m'] in (5000, 10000) and all(isfinite(x) and x > 0 for x in (actual, prediction, baseline))
            if not valid:
                raise ValueError('qualification')
            seen.add(key)
            usable.append((actual, prediction, baseline))
            groups.setdefault(row['distance_m'], []).append((actual, prediction, baseline))
        except (KeyError, ValueError, TypeError):
            rejected += 1
    if len(usable) < minimum_samples:
        return {'validation_state': 'insufficient_data', 'eligible_for_publish': False,
                'sample_count': len(usable), 'rejected_count': rejected}
    errors = [abs(p / a - 1) * 100 for a, p, _ in usable]
    reference = [abs(b / a - 1) * 100 for a, _, b in usable]
    group_results = {}
    for distance, observations in groups.items():
        ge = [abs(p/a-1)*100 for a, p, _ in observations]
        gb = [abs(b/a-1)*100 for a, _, b in observations]
        group_results[str(distance)] = {'sample_count': len(observations),
            'median_absolute_percent_error': round(median(ge), 3), 'p90_absolute_percent_error': round(_percentile(ge, .9), 3),
            'passed': len(observations) >= minimum_samples and median(ge) <= 5 and _percentile(ge, .9) <= 10 and mean(ge) <= mean(gb)}
    enough = all(g['sample_count'] >= minimum_samples for g in group_results.values())
    passed = enough and all(g['passed'] for g in group_results.values())
    return {'validation_state': 'insufficient_data' if not enough else 'passed' if passed else 'failed', 'eligible_for_publish': passed,
            'by_distance_m': group_results,
            'sample_count': len(usable), 'rejected_count': rejected,
            'median_absolute_percent_error': round(median(errors), 3),
            'p90_absolute_percent_error': round(_percentile(errors, .9), 3),
            'mean_absolute_error_seconds': round(mean(abs(p-a) for a, p, _ in usable), 2),
            'mean_signed_error_seconds': round(mean(p-a for a, p, _ in usable), 2),
            'baseline_mean_absolute_percent_error': round(mean(reference), 3)}


def _calibration(pairs):
    mx, my = mean(x for x, _ in pairs), mean(y for _, y in pairs)
    variance = sum((x-mx)**2 for x, _ in pairs)
    slope = sum((x-mx)*(y-my) for x, y in pairs) / variance if variance > 0 else 0
    return my-slope*mx, slope


def validate_relative_indices(rows, *, minimum_training=30, minimum_test=20):
    """Compare equally complex calibrations at a predeclared 28±7 day horizon.

    Training outcomes must precede the first test feature date. Identifiers
    qualify independent outcomes but never appear in the aggregate result.
    """
    usable, rejected, seen = [], 0, set()
    for row in rows:
        try:
            identity = (row['subject'], row['outcome_id'])
            before, after = date.fromisoformat(row['feature_date']), date.fromisoformat(row['outcome_date'])
            rpi, cs, actual = (float(row[k]) for k in ('rpi_ratio', 'cs_ratio', 'actual_speed_ratio'))
            if identity in seen or row['outcome_id'] in row['fit_workout_ids'] or not 21 <= (after-before).days <= 35:
                raise ValueError('independence')
            if not all(isfinite(x) and x > 0 for x in (rpi, cs, actual)):
                raise ValueError('invalid ratio')
            seen.add(identity)
            usable.append((before, after, rpi-1, cs-1, actual-1))
        except (KeyError, TypeError, ValueError):
            rejected += 1
    usable.sort()
    base = {'validation_state': 'insufficient_data', 'eligible_for_publish': False, 'sample_count': len(usable), 'rejected_count': rejected}
    if len(usable) < minimum_training + minimum_test:
        return base
    split = usable[int(len(usable)*.7)][0]
    train = [r for r in usable if r[1] < split]
    test = [r for r in usable if r[0] >= split]
    base.update(training_count=len(train), test_count=len(test), purged_count=len(usable)-len(train)-len(test))
    if len(train) < minimum_training or len(test) < minimum_test:
        return base
    rpi_fit = _calibration([(r[2], r[4]) for r in train])
    cs_fit = _calibration([(r[3], r[4]) for r in train])
    rpi_error = mean(abs((rpi_fit[0]+rpi_fit[1]*r[2])-r[4]) * 100 for r in test)
    cs_error = mean(abs((cs_fit[0]+cs_fit[1]*r[3])-r[4]) * 100 for r in test)
    passed = rpi_error + .001 < cs_error
    return {**base, 'validation_state': 'passed' if passed else 'failed', 'eligible_for_publish': passed,
            'composite_mean_absolute_error_percent': round(rpi_error, 4),
            'cs_mean_absolute_error_percent': round(cs_error, 4)}
