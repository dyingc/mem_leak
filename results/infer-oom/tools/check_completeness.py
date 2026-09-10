#!/usr/bin/env python3
"""Explicit completeness check of an Infer/Pulse results directory.

For every *defined* captured procedure (optionally restricted to the scheduled source files),
verify that results.db has a non-NULL Pulse payload. Procedures without one are classified from
the run's own evidence (logs, pulse/oom-aborted-procedures-*.txt) and listed. Exit status 0 only
if the analysis is complete."""
import argparse, os, re, subprocess, sys, collections
ap = argparse.ArgumentParser()
ap.add_argument('results_dir'); ap.add_argument('--infer', required=True)
ap.add_argument('--files', help='file with one source path per line: restrict to procedures defined there')
ap.add_argument('--verbose', action='store_true')
a = ap.parse_args()
rd = a.results_dir
files = None
if a.files:
    files = set(l.strip() for l in open(a.files) if l.strip())
out = subprocess.run([a.infer, 'debug', '--results-dir', rd, '--procedures', '--procedures-name',
                      '--procedures-source-file', '--procedures-definedness'],
                     capture_output=True, text=True).stdout
procs = {}  # uid -> (source_file, defined)
cur = None
for line in out.splitlines():
    if line and not line.startswith(' '):
        cur = line.strip(); procs[cur] = [None, None]
    elif line.strip().startswith('source_file:'): procs[cur][0] = line.split(':', 1)[1].strip()
    elif line.strip().startswith('defined:'): procs[cur][1] = line.split(':', 1)[1].strip() == 'true'
def sql(q):
    return subprocess.run(['sqlite3', os.path.join(rd, 'results.db'), q], capture_output=True, text=True).stdout.splitlines()
with_pulse = set(sql("select proc_uid from specs where Pulse is not null"))
integrity = sql("pragma integrity_check")
expected = {u for u, (sf, d) in procs.items() if d and (files is None or (sf and any(sf.endswith(f) or f.endswith(sf) for f in files)))}
missing = sorted(expected - with_pulse)
# evidence
logs = open(os.path.join(rd, 'logs'), errors='replace').read() if os.path.exists(os.path.join(rd, 'logs')) else ''
too_big = set(re.findall(r'Skipped large procedure \((\S+), size:\d+\) in pulse', logs))
timeouts = set(m.group(1) for m in re.finditer(r'TIMEOUT in pulse after [\d.]+s of CPU time analyzing \S+:(\S+)', logs))
oom = set()
pdir = os.path.join(rd, 'pulse')
if os.path.isdir(pdir):
    for f in os.listdir(pdir):
        if f.startswith('oom-aborted-procedures-'):
            for l in open(os.path.join(pdir, f)): oom.add(l.split('\t')[0])
oom_danger = set(re.findall(r'Aborting the analysis of the procedure (\S+) to avoid', logs))
oom |= oom_danger
reasons = collections.Counter()
def reason(u):
    n = u.split('{')[0]
    if u in oom or n in oom: return 'pulse-max-heap abort (explicit)'
    if u in too_big or n in too_big: return 'skipped: CFG larger than --pulse-max-cfg-size'
    if u in timeouts or n in timeouts: return 'per-procedure --timeout'
    return 'UNEXPLAINED'
for u in missing: reasons[reason(u)] += 1
report = os.path.join(rd, 'report.json')
print(f'results_dir: {rd}')
print(f'report.json: {"present" if os.path.exists(report) else "MISSING"}; results.db integrity_check: {" ".join(integrity)}')
print(f'captured procedures: {len(procs)} (defined: {sum(1 for v in procs.values() if v[1])}); expected in scope: {len(expected)}; with Pulse summary: {len(expected & with_pulse)}; missing: {len(missing)}')
for r, c in reasons.items(): print(f'  {c:5d}  {r}')
if a.verbose or len(missing) <= 40:
    for u in missing: print(f'    - {u} [{procs[u][0]}] {reason(u)}')
status = 'COMPLETE' if not missing else ('INCOMPLETE (all gaps explained/explicit)' if 'UNEXPLAINED' not in reasons else 'INCOMPLETE (unexplained gaps)')
print('verdict:', status)
sys.exit(0 if not missing else 1)
