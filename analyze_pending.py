#!/usr/bin/env python
import sqlite3

dbpath = r'D:\Test\Test db\hebrew_cad_fresh_20260503_120641.db'
conn = sqlite3.connect(dbpath)
c = conn.cursor()

print("=" * 80)
print("PENDING PROPOSALS ANALYSIS - Where are the 184 missing proposals?")
print("=" * 80)
print()

# Get confidence distribution of PENDING proposals
print("PENDING Proposals by Type and Confidence:")
print("-" * 80)

c.execute('''
SELECT proposal_type, confidence, COUNT(*) as count
FROM proposals
WHERE status = 'PENDING'
GROUP BY proposal_type, confidence
ORDER BY proposal_type, confidence DESC
''')

current_type = None
for ptype, conf, count in c.fetchall():
    if ptype != current_type:
        print()
        print("{}:".format(ptype))
        current_type = ptype
    print("  {:.2f}: {} proposals".format(conf, count))

print()
print()
print("MINIMUM THRESHOLD NEEDED FOR EACH PROPOSAL TYPE:")
print("-" * 80)

c.execute('''
SELECT proposal_type, MIN(confidence) as min_conf, COUNT(*) as total
FROM proposals
WHERE status = 'PENDING'
GROUP BY proposal_type
ORDER BY min_conf
''')

for ptype, min_conf, total in c.fetchall():
    threshold = min_conf if min_conf > 0.5 else 0.5
    print("{:<20} | Min Conf: {:.2f} | Total: {} proposals".format(ptype, min_conf, total))

print()
print()
print("RECOMMENDATION TO GET ORGANIZATIONAL PROPOSALS:")
print("-" * 80)

c.execute('SELECT MIN(confidence) FROM proposals WHERE status = "PENDING"')
min_pending = c.fetchone()[0]
print("Minimum confidence in PENDING proposals: {:.2f}".format(min_pending))
print()
print("To approve most pending proposals:")
print("  Use threshold: {:.2f} or lower".format(max(min_pending, 0.5)))
print()
print("To get organizational MOVEs:")
print("  Use threshold: 0.55 (gains ~60+ proposals)")
print()

conn.close()
