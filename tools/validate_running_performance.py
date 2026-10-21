"""Validate precomputed, independently held-out 5/10 km predictions privately.

Input rows never appear in stdout. This evidence report does not enable any
model or modify live settings; releasing a model still requires reviewed data.
"""

import argparse
import json
import hashlib
from pathlib import Path

from vitalis.intelligence.running_validation import validate_race_predictions, validate_relative_indices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--mode', choices=('race', 'relative'), default='race')
    args = parser.parse_args()
    content = args.input.read_bytes()
    rows = json.loads(content)
    if not isinstance(rows, list):
        parser.error('input must be an array of held-out predictions')
    validate = validate_race_predictions if args.mode == 'race' else validate_relative_indices
    print(json.dumps({**validate(rows), 'input_digest': hashlib.sha256(content).hexdigest()}, sort_keys=True))


if __name__ == '__main__':
    main()
