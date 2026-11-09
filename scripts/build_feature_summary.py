"""Rebuild models/feature_summary.json from the datasets in data/.

Run after changing a dataset (train_model.py also runs it):

    python scripts/build_feature_summary.py

The screening form reads this small file instead of parsing the datasets, which is what made the
first screening after a restart slow on the free Render server.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ahead.resources import SUMMARY_FILE, build_summary_file  # noqa: E402

if __name__ == "__main__":
    saved = build_summary_file()
    print(f"Saved {SUMMARY_FILE} ({', '.join(f'{d}: {len(e['fields'])} fields' for d, e in saved.items())})")
