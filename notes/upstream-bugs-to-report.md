# Upstream Infer bugs found on the way, to be reported separately

Both are self-contained defects in `facebook/infer` at **v1.3.0 (`410a6d939`)**, with no dependency
on anything in this repo.  They are fixed locally in `notes/1.3/infer-pulse-memory.patch`, but that
patch also carries our own changes, so each needs extracting into its own PR.

Status: **not yet reported.**  Our own work comes first.

---

## 1. Compaction threshold is 8x too small; its log line is 8x too large

**Where:** `infer/src/base/ProcessPool.ml:321-334` (forked workers) and
`infer/src/base/DomainPool.ml:170-192` (multicore).  Identical bug in both.

```ocaml
(** do a compaction if heap size over this value *)
let compaction_if_heap_greater_equal_to_words =
  (* we don't try hard to avoid overflow, apart from assuming that word size
     divides 1024 perfectly, thus multiplying with a smaller factor *)
  Config.compaction_if_heap_greater_equal_to_GB * 1024 * 1024 * (1024 / Sys.word_size_in_bits)
```

`Sys.word_size_in_bits` is the word size in **bits** (64), not bytes.  Words per GB is
`1024 * 1024 * 1024 / (word_size_in_bits / 8)` = `1024 * 1024 * (1024 / 8)`, so the last factor
should be `1024 / (Sys.word_size_in_bits / 8)` = 128, not `1024 / 64` = 16.  The threshold is
therefore **8x too small**: the documented and shipped default of 8 GB actually fires at 1 GiB.

The log line immediately below has the mirror-image mistake:

```ocaml
    L.log_task "Triggering compaction, heap size= %d GB@\n"
      (heap_words * Sys.word_size_in_bits / 1024 / 1024 / 1024) ;
```

`heap_words * bits_per_word` is a bit count, printed as if it were bytes, so the reported figure is
**8x too high** — it is really gigabits.

**Reproduction** (800-procedure C file, `--jobs 2 --compaction-if-heap-greater-equal-to-GB 1`,
peak RSS ~300 MB so the OCaml heap never reaches 1 GB):

| build | compactions triggered | log line for the same heap |
| --- | --- | --- |
| stock v1.3.0 | 1 | `Triggering compaction, heap size= 1 GB` |
| threshold+log fixed | 0 | `Triggering compaction, heap size= 0 GB` |

**Fix:** `1024 / (Sys.word_size_in_bits / 8)` in the threshold, `Sys.word_size_in_bits / 8` in the
log line, in both files.  Note that fixing the threshold makes the shipped default 8x laxer than the
behaviour everyone has actually been getting, so the PR should say so explicitly and probably change
the default in the same commit.

---

## 2. `--summaries-lru-max-size` has never had any effect, in any mode

**Where:** `infer/src/base/Concurrent.ml`, `MakeCache`.

`add` trims to the LRU limit:

```ocaml
  let add t k v =
    in_mutex t ~f:(fun hq ->
        HQ.remove hq k |> ignore ;
        HQ.enqueue_front_exn hq k v ;
        match t.lru_limit with
        | None -> ()
        | Some limit -> let n = HQ.length hq - limit in if n > 0 then HQ.drop_back ~n hq )
```

`update` does not:

```ocaml
  let update ~f t key =
    in_mutex t ~f:(fun hq ->
        HQ.lookup_and_remove hq key |> f |> Option.iter ~f:(HQ.enqueue_front_exn hq key) )
```

`Summary.OnDisk` (`infer/src/backend/Summary.ml:163-177`) inserts through `update`, because it keeps
a two-layer `procname -> AnalysisRequest.Map -> summary` structure rather than one entry per
summary.  So nothing is ever dropped from the summary cache and `--summaries-lru-max-size` is inert
— including in multicore mode, the one configuration where `InferAnalyze.ml:162-166` switches the
limit on at all.  The same applies to any other cache that inserts through `update`.

**Reproduction** (800-procedure C file, `--jobs 1`, summary-cache hit rate from
`stats/stats.jsonl`, `count.backend_stats.cache.summaries.hit_rate`):

| `--summaries-lru-max-size` | stock v1.3.0 | with trimming in `update` |
| --- | --- | --- |
| 0 (unbounded) | 64% | 64% |
| 2000 (default) | 64% | 64% |
| 100 | 64% | **41%** |
| 8 | 64% | **41%** |

Control showing the mechanism itself is fine: `--attributes-lru-max-size 1` already moves the
attributes hit rate 98% -> 81% on stock, because `Attributes` inserts through `add`.

**Fix:** factor the trimming into a `trim_to_lru_limit` helper called from both `add` and `update`.

**Worth mentioning in the same PR:** `--summaries-lru-max-size`'s help text says "Relevant only to
multicore mode", and `InferAnalyze.ml` only calls the four `set_lru_limit` functions inside
`else if Config.multicore then`.  Nothing about them is multicore-specific, and with one worker per
file and no bound the summary cache is exactly what grows without limit.  Whether upstream wants the
limits on by default outside multicore is their call, but the current state is that the option
exists, is documented, and does nothing.
