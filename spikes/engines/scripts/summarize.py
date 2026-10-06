"""Print markdown tables from out/results.jsonl (medians and ranges per engine x site)."""

import collections
import json
import pathlib
import statistics

OUT = pathlib.Path(__file__).resolve().parent.parent / 'out'
rows = [json.loads(l) for l in open(OUT / 'results.jsonl')]
by = collections.defaultdict(list)
for r in rows:
    by[(r['engine'], r['site'])].append(r)


def rng(vals, fmt='{:.2f}'):
    vals = [v for v in vals if v is not None]
    if not vals:
        return 'n/a'
    lo, hi, med = min(vals), max(vals), statistics.median(vals)
    return fmt.format(med) + (f' ({fmt.format(lo)}-{fmt.format(hi)})' if len(vals) > 1 else '')


print('| engine | site | n | launch s | load s | peak RSS sum MiB | peak footprint MiB | challenge | errors |')
print('|---|---|---|---|---|---|---|---|---|')
for (eng, site), rs in by.items():
    errs = [r['error'] for r in rs if 'error' in r]
    chal = sum(1 for r in rs if r.get('challenge'))
    print(
        f"| {eng} | {site} | {len(rs)} | {rng([r.get('launch_s') for r in rs], '{:.3f}')} | "
        f"{rng([r.get('load_s') for r in rs])} | {rng([r.get('peak_rss_mib') for r in rs], '{:.0f}')} | "
        f"{rng([r.get('sum_phys_footprint_peak_mib') for r in rs], '{:.0f}')} | "
        f"{chal}/{len(rs)} | {'; '.join(sorted(set(errs)))[:200]} |"
    )
