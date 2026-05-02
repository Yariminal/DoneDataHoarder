from donedatahoarder.proposals.namer import _deduplicate_stem_words

tests = [
    ('architectural_floor_plan_floor_310', 'architectural_floor_plan_310'),
    ('design_elevations_and_plan', 'design_elevations_and_plan'),
    ('bathroom_bathroom_design', 'bathroom_design'),
    ('solar_dekathlon_sponsors_list', 'solar_dekathlon_sponsors_list'),
    ('', ''),
    ('single', 'single'),
]

all_pass = True
for inp, expected in tests:
    out = _deduplicate_stem_words(inp)
    status = 'PASS' if out == expected else 'FAIL'
    if out != expected:
        all_pass = False
    print(f'{status}: _deduplicate_stem_words("{inp}") = "{out}" (expected "{expected}")')

print(f'\nAll tests passed: {all_pass}')
