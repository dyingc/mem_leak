#!/usr/bin/env python3
"""Run a command, record wall time, exit status and peak RSS (ru_maxrss of children,
plus 1s samples of VmRSS/VmHWM over the process tree) into the file given by -o."""
import os, subprocess, sys, time, resource, signal
out = sys.argv[sys.argv.index('-o') + 1]; cmd = sys.argv[sys.argv.index('-o') + 2:]
def tree_rss(root):
    tot = 0; hwm = 0; stack=[root]
    while stack:
        p = stack.pop()
        try:
            for line in open(f'/proc/{p}/status'):
                if line.startswith('VmRSS:'): tot += int(line.split()[1])
                elif line.startswith('VmHWM:'): hwm = max(hwm, int(line.split()[1]))
            stack += [int(c) for c in open(f'/proc/{p}/task/{p}/children').read().split()]
        except Exception: pass
    return tot, hwm
t0 = time.time(); proc = subprocess.Popen(cmd)
samples = []; peak_tree = 0
while proc.poll() is None:
    tot, hwm = tree_rss(proc.pid); peak_tree = max(peak_tree, tot)
    samples.append((round(time.time()-t0,1), tot, hwm)); time.sleep(1)
rc = proc.returncode; ru = resource.getrusage(resource.RUSAGE_CHILDREN)
sig = f' (signal {-rc} {signal.Signals(-rc).name})' if rc < 0 else ''
with open(out, 'w') as f:
    f.write(f'Command: {" ".join(cmd)}\nExit status: {rc}{sig}\nElapsed (wall clock) seconds: {time.time()-t0:.1f}\n')
    f.write(f'User time (seconds): {ru.ru_utime:.1f}\nSystem time (seconds): {ru.ru_stime:.1f}\n')
    f.write(f'Maximum resident set size (kbytes): {ru.ru_maxrss}\nPeak tree RSS sampled (kbytes): {peak_tree}\n')
    f.write('samples (t_s, tree_rss_kb, max_vmhwm_kb):\n' + '\n'.join(f'{a} {b} {c}' for a,b,c in samples) + '\n')
sys.exit(rc if rc >= 0 else 128 - rc)
