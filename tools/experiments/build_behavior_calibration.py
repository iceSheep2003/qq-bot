"""Build a small validation-only ranking calibration profile."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validated", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.validated / "validated.jsonl").open(encoding="utf-8") if line.strip()]
    accepted = [row for row in rows if row.get("validated_label")]
    by_family = defaultdict(Counter)
    global_counts = Counter()
    for row in accepted:
        label = row["validated_label"]
        family, strategy = label["situation_family"], label["strategy"]
        by_family[family][strategy] += 1
        global_counts[strategy] += 1
    n_strategies = max(1, len(global_counts))
    global_total = sum(global_counts.values())
    bonuses = {}
    for family, counts in by_family.items():
        family_total = sum(counts.values())
        family_bonus = {}
        for strategy in global_counts:
            local = math.log((counts[strategy] + 1) / (family_total + n_strategies))
            global_rate = math.log((global_counts[strategy] + 1) / (global_total + n_strategies))
            family_bonus[strategy] = round(max(-1.5, min(1.5, local - global_rate)), 4)
        bonuses[family] = family_bonus
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = {
        "schema_version": 1,
        "source": "validation-model-reviewed",
        "samples": len(accepted),
        "family_strategy_bonus": bonuses,
        "note": "ranking calibration only; does not change ontology nodes or permissions",
    }
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(args.output)
    print(json.dumps(profile, ensure_ascii=False))


if __name__ == "__main__":
    main()
