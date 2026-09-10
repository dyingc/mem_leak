#!/usr/bin/env python3
"""Port the memory-behaviour half of notes/infer-pulse-oom.patch onto Infer v1.3.0.

Three things, all of which upstream either still gets wrong or leaves switched off on the
configuration we analyse with (--jobs 1, or the default forked ProcessPool):

1. The compaction threshold's unit bug, which upstream still has verbatim -- and now in two
   places, ProcessPool.ml (forked workers) and DomainPool.ml (multicore).  It multiplies GB by
   1024/Sys.word_size_in_bits = 16 instead of 1024/(word_size_in_bits/8) = 128, so the threshold
   is 8x too small: the documented 8 GB fires at 1 GiB.  The log line has the mirror-image bug,
   reporting heap_words * bits-per-word as bytes, i.e. gigabits labelled "GB" -- 8x too high.
   Fixed in both files.  The default is then dropped from 8 to 1 so that the *effective* threshold
   stays where it has been all along (1 GiB) rather than silently becoming 8x laxer; the number in
   --help now means what it says.

2. Upstream grew a real LRU for the summary cache (Concurrent.Cache.set_lru_mode, backed by a
   Hash_queue) and wired it into Summary.OnDisk.set_lru_limit -- but InferAnalyze.ml only calls it
   inside `else if Config.multicore then`, and --summaries-lru-max-size says "Relevant only to
   multicore mode".  On the two paths that actually run (Int.equal Config.jobs 1 -> run_sequentially,
   and the default forked ProcessPool) the cache is still unbounded, which is what let it reach
   ~1.3 GiB of a 3.06 GiB heap on the big Vim translation units.  So: call the limits on those paths
   too, and let 0 mean "unbounded" so the old behaviour is still reachable for A/B runs.

3. --gc-space-overhead.  v1.2.0 set space_overhead (and allocation_policy) in set_gc_params;
   v1.3.0 sets neither -- OCaml 5 has only one major allocator, and upstream left space_overhead at
   the runtime default.  Since heap-to-live ratio is exactly what we are measuring (heap was about
   2.2x live at the default), keep the knob.

Run from the v1.3.0 source root.  Idempotent.
"""
import sys, pathlib

root = pathlib.Path(".").resolve()
if not (root / "infer/src/base/ProcessPool.ml").exists():
    sys.exit("run me from the infer source root")


def edit(relpath, old, new):
    p = root / relpath
    s = p.read_text()
    if new.strip() in s:
        print(f"    already applied: {relpath}")
        return
    n = s.count(old)
    if n != 1:
        sys.exit(f"{relpath}: expected exactly 1 occurrence of anchor, found {n}")
    p.write_text(s.replace(old, new))
    print(f"    patched {relpath}")


# --- 1. the compaction threshold unit bug, in both pools -----------------------------------
edit("infer/src/base/ProcessPool.ml",
     """let compaction_if_heap_greater_equal_to_words =
  (* we don't try hard to avoid overflow, apart from assuming that word size
     divides 1024 perfectly, thus multiplying with a smaller factor *)
  Config.compaction_if_heap_greater_equal_to_GB * 1024 * 1024 * (1024 / Sys.word_size_in_bits)""",
     """let compaction_if_heap_greater_equal_to_words =
  (* words per GB, not bits per GB: [Sys.word_size_in_bits / 8] is the number of bytes in a word.
     We don't try hard to avoid overflow, apart from assuming that the word size in bytes divides
     1024 perfectly, thus multiplying with a smaller factor. *)
  Config.compaction_if_heap_greater_equal_to_GB * 1024 * 1024
  * (1024 / (Sys.word_size_in_bits / 8))""")

edit("infer/src/base/ProcessPool.ml",
     """    L.log_task "Triggering compaction, heap size= %d GB@\\n"
      (heap_words * Sys.word_size_in_bits / 1024 / 1024 / 1024) ;""",
     """    L.log_task "Triggering compaction, heap size= %d GB@\\n"
      (heap_words * (Sys.word_size_in_bits / 8) / 1024 / 1024 / 1024) ;""")

edit("infer/src/base/DomainPool.ml",
     """    Config.compaction_if_heap_greater_equal_to_GB_multicore * 1024 * 1024
    * (1024 / Sys.word_size_in_bits)""",
     """    Config.compaction_if_heap_greater_equal_to_GB_multicore * 1024 * 1024
    * (1024 / (Sys.word_size_in_bits / 8))""")

edit("infer/src/base/DomainPool.ml",
     """        L.debug Analysis Quiet "Triggering compaction, heap size= %d GB@\\n"
          (heap_words * Sys.word_size_in_bits / 1024 / 1024 / 1024) ;""",
     """        L.debug Analysis Quiet "Triggering compaction, heap size= %d GB@\\n"
          (heap_words * (Sys.word_size_in_bits / 8) / 1024 / 1024 / 1024) ;""")

edit("infer/src/base/Config.ml",
     '''  CLOpt.mk_int ~long:"compaction-if-heap-greater-equal-to-GB" ~default:8 ~meta:"int"
    "An analysis worker will trigger compaction if its heap size is equal or great to this value \\
     in Gigabytes. Defaults to 8"''',
     '''  (* the threshold used to be computed 8x too small (see ProcessPool.ml), so the shipped default
     of 8 actually fired at 1 GiB; keep the behaviour and make the number honest *)
  CLOpt.mk_int ~long:"compaction-if-heap-greater-equal-to-GB" ~default:1 ~meta:"int"
    "An analysis worker will trigger compaction if its heap size is equal or great to this value \\
     in Gigabytes. Defaults to 1"''')

# --- 2. bound the summary cache outside multicore too --------------------------------------
edit("infer/src/base/Config.ml",
     '''  CLOpt.mk_int ~long:"summaries-lru-max-size" ~meta:"int" ~default:2000
    "Specify size of summary LRU cache. Relevant only to multicore mode. Defaults to 2000"''',
     '''  CLOpt.mk_int ~long:"summaries-lru-max-size" ~meta:"int" ~default:2000
    "Specify size of the summary LRU cache, in summaries. 0 means unbounded, which is what infer \\
     used to do outside multicore mode and is the only way to reproduce that. Evicting a summary \\
     loses nothing: it is written to the results database before being cached, so it is re-read \\
     from disk on its next use. Defaults to 2000"''')

edit("infer/src/backend/InferAnalyze.ml",
     """let analyze replay_call_graph source_files_to_analyze =""",
     '''(** Bound the caches that grow for as long as a worker lives. Upstream does this only in multicore
    mode, but nothing about it is multicore-specific: with one worker per file and no bound, the
    summary cache alone reached ~1.3 GiB of a 3.06 GiB heap on the largest Vim translation units.
    A limit of 0 restores the old unbounded behaviour, for A/B runs. *)
let set_lru_limits () =
  let limit n = if n > 0 then Some n else None in
  Attributes.set_lru_limit ~lru_limit:(limit Config.attributes_lru_max_size) ;
  BufferOverrunUtils.set_cache_lru_limit ~lru_limit:(limit Config.inferbo_lru_max_size) ;
  Summary.OnDisk.set_lru_limit ~lru_limit:(limit Config.summaries_lru_max_size) ;
  Exe_env.set_lru_limit ~lru_limit:(limit Config.tenvs_lru_max_size)


let analyze replay_call_graph source_files_to_analyze =''')

edit("infer/src/backend/InferAnalyze.ml",
     """  RestartScheduler.setup () ;
  if Int.equal Config.jobs 1 then (""",
     """  RestartScheduler.setup () ;
  set_lru_limits () ;
  if Int.equal Config.jobs 1 then (""")

# the multicore branch set the same four limits by hand; it now shares set_lru_limits
edit("infer/src/backend/InferAnalyze.ml",
     """  else if Config.multicore then (
    Attributes.set_lru_limit ~lru_limit:(Some Config.attributes_lru_max_size) ;
    BufferOverrunUtils.set_cache_lru_limit ~lru_limit:(Some Config.inferbo_lru_max_size) ;
    Summary.OnDisk.set_lru_limit ~lru_limit:(Some Config.summaries_lru_max_size) ;
    Exe_env.set_lru_limit ~lru_limit:(Some Config.tenvs_lru_max_size) ;
    RestartScheduler.setup () ;""",
     """  else if Config.multicore then (
    RestartScheduler.setup () ;""")

# --- 3. --gc-space-overhead ----------------------------------------------------------------
# note: like minor_heap_size_mb, this one is read through its ref inside set_gc_params, so it needs
# no `and gc_space_overhead = !gc_space_overhead` deref and no Config.mli entry.
edit("infer/src/base/Config.ml",
     """and generated_classes =
  CLOpt.mk_path_opt""",
     '''and gc_space_overhead =
  CLOpt.mk_int ~long:"gc-space-overhead" ~default:120 ~meta:"int"
    "OCaml's [space_overhead] GC parameter: the percentage of live words the major heap is allowed \\
     to grow beyond before the collector works harder. Lower values trade CPU for a smaller \\
     resident set; 120 is the runtime default, which is what infer has been getting implicitly \\
     since it stopped setting this itself."


and generated_classes =
  CLOpt.mk_path_opt''')

edit("infer/src/base/Config.ml",
     """  let minor_heap_size = new_size minor_heap_size_mb in
  Gc.set {ctrl with minor_heap_size}""",
     """  let minor_heap_size = new_size minor_heap_size_mb in
  let space_overhead = !gc_space_overhead in
  Gc.set {ctrl with minor_heap_size; space_overhead}""")

print("done")

# --- 4. the summary LRU limit was inert, in every mode -------------------------------------
# Concurrent.MakeCache applies [lru_limit] only in [add].  Summary.OnDisk inserts through [update]
# instead, because it keeps a two-layer procname -> AnalysisRequest.Map -> summary structure, so
# nothing was ever dropped and --summaries-lru-max-size had no effect at all -- not even in
# multicore mode, the one place upstream switches it on.  Measured on an 800-procedure fixture:
# the summary cache hit rate is 64% at --summaries-lru-max-size 1, 100, 2000 and 0 alike, while
# the attributes cache (which inserts through [add]) correctly drops from 98% to 81% at
# --attributes-lru-max-size 1.  Trim in [update] too.
edit("infer/src/base/Concurrent.ml",
     """  let add t k v =
    in_mutex t ~f:(fun hq ->
        HQ.remove hq k |> ignore ;
        HQ.enqueue_front_exn hq k v ;
        match t.lru_limit with
        | None ->
            ()
        | Some limit ->
            let n = HQ.length hq - limit in
            if n > 0 then HQ.drop_back ~n hq )""",
     """  (* must be called under the mutex *)
  let trim_to_lru_limit t hq =
    match t.lru_limit with
    | None ->
        ()
    | Some limit ->
        let n = HQ.length hq - limit in
        if n > 0 then HQ.drop_back ~n hq


  let add t k v =
    in_mutex t ~f:(fun hq ->
        HQ.remove hq k |> ignore ;
        HQ.enqueue_front_exn hq k v ;
        trim_to_lru_limit t hq )""")

edit("infer/src/base/Concurrent.ml",
     """  let update ~f t key =
    in_mutex t ~f:(fun hq ->
        HQ.lookup_and_remove hq key |> f |> Option.iter ~f:(HQ.enqueue_front_exn hq key) )""",
     """  let update ~f t key =
    in_mutex t ~f:(fun hq ->
        HQ.lookup_and_remove hq key |> f |> Option.iter ~f:(HQ.enqueue_front_exn hq key) ;
        trim_to_lru_limit t hq )""")
