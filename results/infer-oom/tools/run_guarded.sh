#!/bin/bash
# Guarded one-worker Infer analysis. Usage:
#   run_guarded.sh <run_name> <capture_dir(results-dir to copy)> [extra infer analyze args...]
# Env: INFER (binary), VMEM_KB (default 8388608), WALL (default 900), JOBS (default 1)
set -u
RUN=$1; CAP=$2; shift 2
INFER=${INFER:-/home/edong/VSCode/papers/mem_leak/output/oom/tools/infer-pre/bin/infer}
VMEM_KB=${VMEM_KB:-8388608}; WALL=${WALL:-900}; JOBS=${JOBS:-1}
BASE=/home/edong/VSCode/papers/mem_leak/output/oom/runs/$RUN
rm -rf "$BASE"; mkdir -p "$BASE"
OUT=$BASE/infer-out
# preconditions
avail_kb=$(awk '/MemAvailable/{print $2}' /proc/meminfo); load=$(cut -d' ' -f1 /proc/loadavg)
swap_used=$(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print t-f}' /proc/meminfo)
disk_gb=$(df -BG --output=avail "$BASE" | tail -1 | tr -dc 0-9)
{
  echo "date=$(date -Is) run=$RUN"; echo "MemAvailable_GiB=$(awk "BEGIN{printf \"%.1f\",$avail_kb/1048576}") load1=$load swap_used_MiB=$((swap_used/1024)) disk_avail_GiB=$disk_gb"
  echo "limits: ulimit -v $VMEM_KB KB, wall guard ${WALL}s, jobs=$JOBS nice=10 ionice=idle"
} | tee "$BASE/precheck.txt"
if [ "$avail_kb" -lt $((12*1048576)) ] || awk "BEGIN{exit !($load > 2)}" || [ "$disk_gb" -lt 50 ]; then
  echo "PRECHECK FAILED - refusing to start" | tee -a "$BASE/precheck.txt"; exit 99; fi
if pgrep -x infer >/dev/null; then echo "another infer running - refusing" | tee -a "$BASE/precheck.txt"; exit 98; fi
cp -r "$CAP" "$OUT"
rm -f "$OUT"/report.json "$OUT"/logs "$OUT"/report.txt; rm -rf "$OUT"/pulse "$OUT"/stats
CMD=("$INFER" analyze --results-dir "$OUT" --pulse-only --jobs "$JOBS" --max-jobs "$JOBS" --timeout 60 "$@")
printf '%q ' "${CMD[@]}" > "$BASE/command.txt"; echo >> "$BASE/command.txt"
# sample memory every 2s in the background
( while true; do echo "$(date +%s) $(awk '/MemAvailable/{print $2}' /proc/meminfo) $(cut -d' ' -f1 /proc/loadavg) $(awk '/SwapFree/{print $2}' /proc/meminfo)"; sleep 2; done ) > "$BASE/host_samples.txt" 2>/dev/null &
SAMPLER=$!
start=$(date +%s.%N)
setsid bash -c "ulimit -v $VMEM_KB; exec nice -n 10 ionice -c 3 python3 /home/edong/VSCode/papers/mem_leak/output/oom/tools/memwrap.py -o '$BASE/time.txt' $(printf '%q ' "${CMD[@]}")" > "$BASE/stdout.txt" 2> "$BASE/stderr.txt" &
PG=$!
( sleep "$WALL"; if kill -0 $PG 2>/dev/null; then echo "WALL GUARD HIT at $(date -Is)" >> "$BASE/precheck.txt"; kill -TERM -- -$PG 2>/dev/null; sleep 5; kill -KILL -- -$PG 2>/dev/null; fi ) >/dev/null 2>&1 &
GUARD=$!
wait $PG; rc=$?
pkill -P $GUARD 2>/dev/null; kill $GUARD 2>/dev/null; pkill -P $SAMPLER 2>/dev/null; kill $SAMPLER 2>/dev/null; wait $GUARD $SAMPLER 2>/dev/null
end=$(date +%s.%N)
wall=$(awk "BEGIN{printf \"%.1f\",$end-$start}")
peak=$(awk -F': ' '/Maximum resident/{print $2}' "$BASE/time.txt" 2>/dev/null)
{
  echo "exit=$rc wall_s=$wall peak_rss_KiB=$peak"
  echo "report.json: $( [ -f "$OUT/report.json" ] && echo "present, $(python3 -c "import json;print(len(json.load(open('$OUT/report.json'))))" 2>/dev/null) issues" || echo MISSING)"
  echo "fatal/oom lines: $(grep -c -i 'out of memory\|fatal error\|OOM danger' "$BASE/stderr.txt" "$OUT/logs" 2>/dev/null | tr '\n' ' ')"
  echo "compaction lines: $(grep -c 'Triggering compaction' "$OUT/logs" 2>/dev/null)"
  echo "top_heap_words(main_process_full): $(grep -A7 'GC stats for main_process_full' "$OUT/logs" 2>/dev/null | grep top_heap_words | awk '{print $NF, "=", $NF*8/1073741824, "GiB"}')"
  echo "timeouts/skips: $(grep -c 'TIMEOUT\|timeout' "$OUT/logs" 2>/dev/null) log lines mention timeout"
  echo "post: MemAvailable_GiB=$(awk '/MemAvailable/{printf "%.1f",$2/1048576}' /proc/meminfo) load1=$(cut -d' ' -f1 /proc/loadavg) du_out=$(du -sh "$OUT" | cut -f1)"
} | tee "$BASE/summary.txt"
exit $rc
