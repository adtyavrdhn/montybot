"""Per build x site x UA medians from out/results.jsonl (labels starting with 'warmup' are skipped)."""
import json
import pathlib
import statistics
from collections import defaultdict

rows = [json.loads(line) for line in (pathlib.Path(__file__).resolve().parents[1] / 'out/results.jsonl').open()]
groups = defaultdict(list)
for r in rows:
    if str(r.get('label', '')).startswith('warmup'):
        continue
    key = (r.get('build', r.get('engine')), r['site'], r.get('ua_kind', '-'), 'prefs' if r.get('prefs', True) else 'default-prefs')
    groups[key].append(r)
med = lambda xs: round(statistics.median(xs), 3) if xs else None  # noqa: E731
print('| build | site | UA | prefs | n | launch s | load s (median, range) | fp peak MiB | footprint after 2 s MiB | peak RSS MiB | challenged | JS errors in log |')
print('|---|---|---|---|---|---|---|---|---|---|---|---|')
for (b, site, ua, prefs), rs in groups.items():
    loads = [r['load_s'] for r in rs if r.get('load_s') is not None]
    ch = sum(1 for r in rs if r.get('challenge'))
    errs = sum(1 for r in rs for e in r.get('log_errors', []) if 'Error at' in e and 'ERROR servoshell' not in e)
    print(f"| {b} | {site} | {ua} | {prefs} | {len(rs)} | {med([r['launch_s'] for r in rs if 'launch_s' in r])} | "
          f"{med(loads)} ({min(loads) if loads else '-'}-{max(loads) if loads else '-'}) | {med([r['fp_peak_mib'] for r in rs if r.get('fp_peak_mib')])} | "
          f"{med([r['footprint_mib'] for r in rs if r.get('footprint_mib')])} | {med([r['peak_rss_sum_mib'] for r in rs if r.get('peak_rss_sum_mib')])} | {ch}/{len(rs)} | {errs} |")
