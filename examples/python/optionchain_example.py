import time

from openalgo import api

# Initialize client
client = api(
    api_key="fb77b2df614f43f607c3cd7543200a3d0b7f8e133701ed40bebeceb901b4d440",
    host="http://127.0.0.1:3300",
)

UNDERLYING = "NIFTY"
EXCHANGE = "NSE_INDEX"
EXPIRY = "17MAR26"
STRIKE_COUNT = 15
WARMUP_WAIT = 5  # seconds — adjust if needed

# Optional side filter: None = all strikes, "otm" = ATM + OTM only, "itm" = ATM + ITM only
SIDE = "otm"


# -------------------------------------------------------
# Get available expiry dates for NIFTY
# -------------------------------------------------------
t0 = time.perf_counter()
expiry_result = client.expiry(
    symbol="NIFTY", exchange="NFO", instrumenttype="options"
)
expiry_elapsed = time.perf_counter() - t0

if expiry_result["status"] == "success":
    print(f"Available NIFTY Expiries: (fetched in {expiry_elapsed:.3f}s)")
    for exp in expiry_result["data"]:
        print(f"  {exp}")
else:
    print("Failed to fetch expiries :", expiry_result.get("message"))

print()


def fetch_chain(label: str) -> float:
    """Fetch option chain, print summary, and return elapsed seconds."""
    kwargs = {}
    if SIDE is not None:
        kwargs["side"] = SIDE  # Handled server-side; not a native SDK param

    t = time.perf_counter()
    chain = client.optionchain(
        underlying=UNDERLYING,
        exchange=EXCHANGE,
        expiry_date=EXPIRY,
        strike_count=STRIKE_COUNT,
        **kwargs,
    )
    elapsed = time.perf_counter() - t

    side_label = f" [{SIDE.upper()} only]" if SIDE else ""
    print(f"[{label}]{side_label} NIFTY Option Chain fetched in {elapsed:.3f}s")
    print("-" * 50)

    if chain["status"] == "success":
        print(f"  Underlying LTP : {chain['underlying_ltp']}")
        print(f"  ATM Strike     : {chain['atm_strike']}")

        print("\n  Strike  | CE LTP (Label) | PE LTP (Label)")
        print("  " + "-" * 46)
        for item in chain["chain"]:
            ce = item.get("ce") or {}
            pe = item.get("pe") or {}
            print(
                f"  {item['strike']:>7} | "
                f"{ce.get('ltp', '-'):>6} ({ce.get('label', '-'):>4}) | "
                f"{pe.get('ltp', '-'):>6} ({pe.get('label', '-'):>4})"
            )
    else:
        print(f"  Failed: {chain.get('message')}")

    print()
    return elapsed


# -------------------------------------------------------
# Call 1 — cold (no WebSocket cache yet)
# -------------------------------------------------------
elapsed_1 = fetch_chain("Call 1 - Cold")

# Give the background WebSocket subscriptions a moment to register
# before the second call so the cache has time to receive ticks.
print(f"Waiting {WARMUP_WAIT}s for WebSocket cache to warm up...")
time.sleep(WARMUP_WAIT)
print()

# -------------------------------------------------------
# Call 2 — warm (should hit WebSocket cache)
# -------------------------------------------------------
elapsed_2 = fetch_chain("Call 2 - Warm")

# -------------------------------------------------------
# Summary
# -------------------------------------------------------
speedup = elapsed_1 / elapsed_2 if elapsed_2 > 0 else float("inf")
saved = elapsed_1 - elapsed_2

print("=" * 50)
print(f"  Expiry fetch : {expiry_elapsed:.3f}s")
print(f"  Call 1 (cold): {elapsed_1:.3f}s")
print(f"  Call 2 (warm): {elapsed_2:.3f}s")
print(f"  Time saved   : {saved:.3f}s  ({speedup:.1f}x faster)")
print("=" * 50)
