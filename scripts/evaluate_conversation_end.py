"""Evaluate synthetic ending examples with live Jev; never dispatch agents or audio."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.voice_stack import conversation_end as ending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = ending._EXAMPLES
    cases = [{"group": group, "expected": group, "text": text}
             for group in ("close", "continue", "uncertain") for text in corpus[group]]
    cases += [{"group": "held_out", **case} for case in corpus["held_out"]]
    rows = []
    for case in cases:
        started = time.monotonic()
        candidate = ending.candidate(case["text"])
        decision = ending.judge(case["text"]) if candidate else ending.Decision(outcome="no_hint")
        elapsed = time.monotonic() - started
        close = decision.close and elapsed < ending.WALL_TIMEOUT
        rows.append({**case, "candidate": candidate, "choice": decision.choice,
                     "probability": decision.probability, "outcome": decision.outcome,
                     "seconds": round(elapsed, 3), "close": close,
                     "passed": close == (case["expected"] == "close")})
    report = {"cases": len(rows), "passed": sum(row["passed"] for row in rows),
              "false_closes": sum(row["close"] and row["expected"] != "close" for row in rows),
              "missed_closes": sum(not row["close"] and row["expected"] == "close" for row in rows),
              "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}))
    for row in rows:
        if not row["passed"]: print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
