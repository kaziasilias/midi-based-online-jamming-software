import csv
from statistics import mean, stdev
from collections import defaultdict

filename = "latency_log.csv"

# peer_stats[(local, remote)] -> list of rtt values
peer_stats = defaultdict(list)

with open(filename, newline="") as f:
    reader = csv.reader(f)
    for row in reader:
        if len(row) != 6:
            continue
        timestamp, room, local, remote, rtt_ms, jitter_ms = row
        try:
            rtt = float(rtt_ms)
        except ValueError:
            continue
        peer_stats[(local, remote)].append(rtt)

for (local, remote), samples in peer_stats.items():
    if len(samples) < 2:
        continue
    print(f"\n=== {local} ↔ {remote} ===")
    print(f"  Samples: {len(samples)}")
    print(f"  RTT mean: {mean(samples):.1f} ms")
    print(f"  RTT min / max: {min(samples):.1f} / {max(samples):.1f} ms")
    print(f"  RTT std dev (jitter-ish): {stdev(samples):.1f} ms")
