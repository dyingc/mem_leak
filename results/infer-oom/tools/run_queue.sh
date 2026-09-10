#!/bin/bash
# Run commands one after another (each line of the queue file is a full shell command line,
# typically "ENV=.. tools/run_guarded.sh <run> <capture> [args]"). Waits for a running build to finish.
Q=$1; LOG=/home/edong/VSCode/papers/mem_leak/output/oom/runs/queue.log
cd /home/edong/VSCode/papers/mem_leak/output/oom
while read -r line; do
  [ -z "$line" ] && continue; case "$line" in \#*) continue;; esac
  while pgrep -f "make -j8 opt" >/dev/null; do sleep 10; done
  # wait for load to settle (precheck needs load <= 2)
  for i in $(seq 1 60); do awk "BEGIN{exit !($(cut -d' ' -f1 /proc/loadavg) > 2)}" || break; sleep 10; done
  echo "=== $(date -Is) START $line" >> "$LOG"
  eval "$line" >> "$LOG" 2>&1
  echo "=== $(date -Is) END rc=$?" >> "$LOG"
  sleep 5
done < "$Q"
