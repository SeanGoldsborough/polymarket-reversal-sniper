"""
Fetch actual Polymarket BTC 5m market resolutions and cache them locally.

Why we need this:
  Current backtests use a heuristic: final_bid >= 0.95 → resolves $1.00,
  final_bid <= 0.05 → resolves $0.00, else use final_bid. This is wrong when:
    - Window ends with bid still oscillating (rare but possible)
    - Market resolves to opposite side from final bid
    - We hit a market that hadn't resolved yet when ticks were captured

  Using the ACTUAL resolved outcome (from Polymarket API) is correct.

Cache file: /home/ubuntu/reports/resolutions.json
  Format: {window_start: {"up_winning": bool, "resolved": bool, "skipped_reason": str|None}}

Reusable for any future strategy that needs ground-truth outcomes.
"""
import argparse
import json
import time
from pathlib import Path
import asyncio
import aiohttp


CACHE_PATH = "/home/ubuntu/reports/resolutions.json"


async def fetch_market(session, window_start, retries=3):
    """Returns dict with up_token, dn_token, resolved, up_winning, raw_outcome_prices."""
    slug = f"btc-updown-5m-{window_start}"
    # closed=true is REQUIRED for gamma-api to return resolved historical markets
    url = f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true"
    for attempt in range(retries):
        try:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    if attempt == retries - 1:
                        return {"window_start": window_start, "resolved": False,
                                "skipped_reason": f"http_{r.status}"}
                    await asyncio.sleep(0.5)
                    continue
                data = await r.json()
                if not data:
                    return {"window_start": window_start, "resolved": False,
                            "skipped_reason": "no_market"}
                m = data[0]
                outcomes_raw = m.get("outcomes", "[]")
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                up_idx = 0 if outcomes[0] == "Up" else 1
                # outcomePrices is the field that shows resolution: ["1", "0"] or ["0", "1"]
                prices_raw = m.get("outcomePrices", "[]")
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                if not prices or len(prices) < 2:
                    return {"window_start": window_start, "resolved": False,
                            "skipped_reason": "no_outcome_prices"}
                up_price = float(prices[up_idx])
                dn_price = float(prices[1 - up_idx])
                # resolved if one is 1 and the other is 0
                if (up_price == 1.0 and dn_price == 0.0):
                    return {"window_start": window_start, "resolved": True,
                            "up_winning": True, "outcome_prices": [up_price, dn_price]}
                if (up_price == 0.0 and dn_price == 1.0):
                    return {"window_start": window_start, "resolved": True,
                            "up_winning": False, "outcome_prices": [up_price, dn_price]}
                return {"window_start": window_start, "resolved": False,
                        "skipped_reason": f"unresolved_{up_price}_{dn_price}"}
        except asyncio.TimeoutError:
            if attempt == retries - 1:
                return {"window_start": window_start, "resolved": False,
                        "skipped_reason": "timeout"}
            await asyncio.sleep(0.5)
        except Exception as e:
            if attempt == retries - 1:
                return {"window_start": window_start, "resolved": False,
                        "skipped_reason": f"err_{str(e)[:30]}"}
            await asyncio.sleep(0.5)
    return {"window_start": window_start, "resolved": False, "skipped_reason": "exhausted"}


async def fetch_all(windows, concurrency=10):
    sem = asyncio.Semaphore(concurrency)
    results = {}

    async with aiohttp.ClientSession() as session:
        async def one(w):
            async with sem:
                r = await fetch_market(session, w)
                results[w] = r
                return r

        tasks = [asyncio.create_task(one(w)) for w in windows]
        for i, _ in enumerate(asyncio.as_completed(tasks), 1):
            await _
            if i % 25 == 0 or i == len(tasks):
                ok = sum(1 for v in results.values() if v.get("resolved"))
                print(f"  fetched {i}/{len(tasks)} (resolved: {ok})", flush=True)
    return results


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}


def save_cache(cache):
    Path(CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
    # Store with string keys for JSON
    with open(CACHE_PATH, "w") as f:
        json.dump({str(k): v for k, v in cache.items()}, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--refetch", action="store_true",
                    help="Re-fetch even windows already in cache")
    args = ap.parse_args()

    files = sorted(Path(args.ticks).glob("ticks_*.csv"))
    windows = []
    for f in files:
        try:
            windows.append(int(f.stem.split("_")[1]))
        except Exception:
            pass
    print(f"Found {len(windows)} tick windows")

    cache = load_cache()
    print(f"Cache has {len(cache)} entries ({sum(1 for v in cache.values() if v.get('resolved'))} resolved)")

    to_fetch = windows if args.refetch else [w for w in windows if w not in cache]
    print(f"Need to fetch {len(to_fetch)} windows")

    if to_fetch:
        new_results = asyncio.run(fetch_all(to_fetch))
        cache.update(new_results)
        save_cache(cache)
        print(f"Saved cache → {CACHE_PATH}")

    # Summary
    resolved = sum(1 for v in cache.values() if v.get("resolved"))
    up_won = sum(1 for v in cache.values() if v.get("resolved") and v.get("up_winning"))
    dn_won = resolved - up_won
    print()
    print(f"=== RESOLUTION CACHE SUMMARY ===")
    print(f"Total windows: {len(cache)}")
    print(f"Resolved: {resolved} ({100 * resolved / max(1, len(cache)):.0f}%)")
    print(f"  UP won: {up_won}  ({100 * up_won / max(1, resolved):.0f}% of resolved)")
    print(f"  DN won: {dn_won}  ({100 * dn_won / max(1, resolved):.0f}% of resolved)")
    unresolved = [v for v in cache.values() if not v.get("resolved")]
    if unresolved:
        from collections import Counter
        reasons = Counter(v.get("skipped_reason", "?") for v in unresolved)
        print(f"Unresolved breakdown:")
        for r, c in reasons.most_common():
            print(f"  {r}: {c}")


if __name__ == "__main__":
    main()
