"""Spawn servo-spike under /usr/bin/time -l and time process spawn -> key log lines.

usage: python3 scripts/measure.py <name> <url> [extra spike args...]
"""
import re, subprocess, sys, time, pathlib

root = pathlib.Path(__file__).resolve().parent.parent
name, url, *extra = sys.argv[1:]
binary = root / "servo-spike/target/release/servo-spike"
out_png = root / "out" / f"{name}.png"
log = root / "logs" / f"run-{name}.log"
cmd = ["/usr/bin/time", "-l", str(binary), url, "--out", str(out_png), *extra]
t0 = time.monotonic()
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
marks = {}
lines = []
for line in p.stdout:
    now = time.monotonic() - t0
    lines.append(line)
    m = re.match(r"\[\s*([\d.]+)s\] (\w+)", line)
    if m and "main_start" not in marks:
        # internal clock starts at main(); first log line gives main start offset
        marks["main_start"] = now - float(m.group(1))
p.wait()
total = time.monotonic() - t0
text = "".join(lines)
log.write_text(text)
ff = re.search(r"first_frame=Some\(([\d.]+)(ms|s)\)", text)
lc = re.search(r"load_complete=Some\(([\d.]+)(ms|s)\)", text)
def secs(m):
    return None if not m else float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)
rss = re.search(r"(\d+)\s+maximum resident set size", text)
peak = re.search(r"(\d+)\s+peak memory footprint", text)
ms = marks.get("main_start", 0)
print(f"{name}: spawn->main {ms:.3f}s; spawn->first_frame "
      f"{(ms + secs(ff)) if ff else None}; spawn->load_complete {(ms + secs(lc)) if lc else None}; "
      f"total {total:.2f}s; maxRSS {int(rss.group(1))/2**20:.0f} MiB; "
      f"peak footprint {int(peak.group(1))/2**20:.0f} MiB" if rss and peak else f"{name}: no rusage; total {total:.2f}s")
