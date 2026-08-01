#!/usr/bin/env python
import sqlite3

dbpath = r'D:\Test\Test db\hebrew_cad_fresh_20260503_120641.db'
conn = sqlite3.connect(dbpath)
c = conn.cursor()

# Get total proposals
c.execute('SELECT COUNT(*) FROM proposals')
total = c.fetchone()[0]

print("=" * 70)
print("THRESHOLD IMPACT ANALYSIS")
print("=" * 70)
print()

print("Impact of Different Review Thresholds on 317 Total Proposals:")
print("-" * 70)
print("Threshold | Approved | Pending | vs 0.8 | Status")
print("-" * 70)

thresholds = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
current_approved = 133  # The 0.8 threshold result

for threshold in thresholds:
    c.execute('SELECT COUNT(*) FROM proposals WHERE confidence >= ?', (threshold,))
    approved = c.fetchone()[0]
    pending = total - approved
    gain = approved - current_approved

    status = ""
    if threshold == 0.8:
        status = " CURRENT"
    elif threshold == 0.65:
        status = " RECOMMENDED"

    sign = "+" if gain >= 0 else ""
    print("{:.2f}      | {:>8} | {:>7} | {:>+6} | {}".format(
        threshold, approved, pending, gain, status))

print()
print("KEY INSIGHTS:")
print("-" * 70)
print("  * At 0.8 (current):    Only 133 proposals = files NOT organized")
print("  * At 0.65 (recommended): ~185 proposals = better organization")
print("  * Gain of ~50 proposals = all the project folder MOVEs")
print()
print("RECOMMENDATION:")
print("  Use 0.65 threshold for balanced organization")
print()

conn.close()
