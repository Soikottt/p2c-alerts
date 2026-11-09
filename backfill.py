"""Load history from Ringba.   Usage:  python backfill.py 90
Safe to run again. Never sends alerts."""
import sys

import ingest


def show(i, n, day, total):
    print(f"[{i}/{n}] up to {day}  -  {total['fetched']} calls read so far", flush=True)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    print(f"Loading the last {days} days from Ringba ...")
    res = ingest.backfill_days(days, show)
    print(f"Finished: {res['inserted']} new calls, {res['updated']} refreshed.")
    if res["failures"]:
        print("These days failed. Just run it again to retry them:")
        for f in res["failures"]:
            print("  -", f)
        sys.exit(1)
