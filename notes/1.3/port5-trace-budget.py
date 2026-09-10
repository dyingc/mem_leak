#!/usr/bin/env python3
"""Port the still-needed half of notes/infer-pulse-history-oom.patch onto Infer v1.3.0.

The DAG traversal fix itself (phys_equal pruning in pop_least_timestamp, Epoch/duplicate dropping
in multiplex) is ALREADY upstream as facebook/infer@c257eb16f, which is an ancestor of v1.3.0.
What is not upstream, and is what this script adds:

  --pulse-max-trace-elements   an explicit, deterministic bound on how much of a value history is
                               materialised into one error trace, with a visible marker element
                               when it bites, and a Stats counter so truncation is never silent.

Run from the v1.3.0 source root.  Idempotent: refuses to run twice.
"""
import re, sys, pathlib

root = pathlib.Path(".").resolve()
if not (root / "infer/src/pulse/PulseValueHistory.ml").exists():
    sys.exit("run me from the infer source root")


def edit(relpath, old, new, *, once=True):
    p = root / relpath
    s = p.read_text()
    if new.strip() and new.strip() in s:
        print(f"    already applied: {relpath}")
        return
    n = s.count(old)
    if n != 1:
        sys.exit(f"{relpath}: expected exactly 1 occurrence of anchor, found {n}")
    p.write_text(s.replace(old, new))
    print(f"    patched {relpath}")


# --- 1. Config.ml: the option -------------------------------------------------------------
edit("infer/src/base/Config.ml",
     "and pulse_max_heap =\n",
     '''and pulse_max_trace_elements =
  CLOpt.mk_int ~long:"pulse-max-trace-elements" ~default:10000
    ~in_help:InferCommand.[(Analyze, manual_pulse)]
    "Stop materialising the value history of a reported issue into its error trace after $(i,int) \\
     trace elements, and mark the trace as truncated at that point. This is a last-resort bound on \\
     the memory one report can use; 0 means no bound. It never changes which issues are reported, \\
     only how much of the explanation is printed."


and pulse_max_heap =
''')

edit("infer/src/base/Config.ml",
     "and pulse_max_heap = !pulse_max_heap\n",
     "and pulse_max_trace_elements = !pulse_max_trace_elements\n\nand pulse_max_heap = !pulse_max_heap\n")

edit("infer/src/base/Config.mli",
     "val pulse_max_heap : int option\n",
     "val pulse_max_trace_elements : int\n\nval pulse_max_heap : int option\n")

# --- 2. Stats: the counter ----------------------------------------------------------------
edit("infer/src/base/Stats.ml",
     "  ; pulse_summaries_count: ",
     "  ; pulse_traces_truncated: IntCounter.t Atomic.t\n  ; pulse_summaries_count: ")

edit("infer/src/base/Stats.ml",
     "let incr_summary_file_try_load () = incr Fields.summary_file_try_load\n",
     "let incr_pulse_traces_truncated () = incr Fields.pulse_traces_truncated\n\n"
     "let incr_summary_file_try_load () = incr Fields.summary_file_try_load\n")

edit("infer/src/base/Stats.ml",
     "    ~pulse_summaries_count:(pp_pulse_summaries_count fmt)\n",
     "    ~pulse_traces_truncated:(pp_int_field fmt)\n"
     "    ~pulse_summaries_count:(pp_pulse_summaries_count fmt)\n")

edit("infer/src/base/Stats.mli",
     "val incr_summary_file_try_load : unit -> unit\n",
     "val incr_pulse_traces_truncated : unit -> unit\n\n"
     "val incr_summary_file_try_load : unit -> unit\n")

# --- 3. PulseValueHistory.add_to_errlog: the bound -----------------------------------------
OLD_ERRLOG = """let add_to_errlog ?(include_taint_events = false) ~nesting history errlog =
  let nesting = ref nesting in
  let errlog = ref errlog in
  let one_iter_event = function
    | Event event ->
        if include_taint_events || not (is_taint_event event) then
          errlog := add_event_to_errlog ~nesting:!nesting event !errlog
    | EnterCall _ ->
        decr nesting
    | ReturnFromCall (call, location) ->
        errlog := add_returned_from_call_to_errlog ~nesting:!nesting call location !errlog ;
        incr nesting
  in
  rev_iter history ~f:one_iter_event ;
  !errlog"""

NEW_ERRLOG = """exception Trace_budget_exhausted

(** Materialise [history] into an error trace, in chronological order.

    [pop_least_timestamp] traverses the history as the DAG it is (physically shared sub-histories
    are expanded once), so this is linear in the size of that DAG and the bound below is a
    last-resort backstop rather than the mechanism that keeps reporting affordable: it fires only
    for a history that is genuinely that large. When it does fire it is deterministic (the oldest
    events are the ones dropped, because [rev_iter] walks from the most recent event backwards) and
    it is stated in the trace itself, so a truncated explanation can never be mistaken for a
    complete one. Truncating a trace changes no analysis result: the issue is still reported, at the
    same place, with the same type. *)
let add_to_errlog ?(include_taint_events = false) ~nesting history errlog =
  let nesting = ref nesting in
  let errlog = ref errlog in
  let budget = Config.pulse_max_trace_elements in
  let produced = ref 0 in
  let last_location = ref None in
  let truncated = ref false in
  let spend location =
    if budget > 0 && !produced >= budget then (
      truncated := true ;
      raise_notrace Trace_budget_exhausted ) ;
    incr produced ;
    last_location := Some location
  in
  let one_iter_event = function
    | Event event ->
        if include_taint_events || not (is_taint_event event) then (
          spend (location_of_event event) ;
          errlog := add_event_to_errlog ~nesting:!nesting event !errlog )
    | EnterCall _ ->
        decr nesting
    | ReturnFromCall (call, location) ->
        spend location ;
        errlog := add_returned_from_call_to_errlog ~nesting:!nesting call location !errlog ;
        incr nesting
  in
  (try rev_iter history ~f:one_iter_event with Trace_budget_exhausted -> ()) ;
  if !truncated then (
    Stats.incr_pulse_traces_truncated () ;
    let location = Option.value !last_location ~default:Location.dummy in
    let description =
      F.asprintf
        "earlier history not shown: this value's history reached the %d-element trace limit \\
         (--pulse-max-trace-elements); the issue itself is unaffected"
        budget
    in
    errlog := Errlog.make_trace_element !nesting location description [] :: !errlog ) ;
  !errlog"""

edit("infer/src/pulse/PulseValueHistory.ml", OLD_ERRLOG, NEW_ERRLOG)

print("done")

# --- 4. unit tests: the DAG traversal is upstream, but nothing guards it -------------------
# Upstream's PulseValueHistoryTest.ml exercises branching but never physical sharing, so nothing
# there would catch a regression of c257eb16f.  Both tests below abort the test runner with
# "out of memory" if the phys_equal pruning is removed.
edit("infer/src/pulse/unit/PulseValueHistoryTest.ml",
     "let iter_print_history history =",
     '''let count_history_events history =
  let n = ref 0 in
  ValueHistory.iter history ~f:(function Event _ -> incr n | _ -> ()) ;
  F.printf "%d events" !n


let iter_print_history history =''')

edit("infer/src/pulse/unit/PulseValueHistoryTest.ml",
     '''      [%expect {|ev0;ev1;ev2;ev3;ev4;ev5;ev6;ev7;ev8;ev9;ev10|}]
  end )''',
     '''      [%expect {|ev0;ev1;ev2;ev3;ev4;ev5;ev6;ev7;ev8;ev9;ev10|}]


    let%expect_test "physically shared branches are expanded once" =
      (* [h ++ h] stores the *same* [h] on both sides, so the history is a DAG with [n] nodes but
         2^[n] root-to-leaf paths. Iterating it has to stay linear in the number of nodes: without
         the physically-equal pruning in [pop_least_timestamp] this test takes time and memory
         exponential in [n] and aborts the process with "Fatal error: out of memory". [n = 60] is
         far past the point where that happens. *)
      let rec double n hist = if n <= 0 then hist else double (n - 1) (hist ++ hist) in
      double 60 (ev 2 ^:: ev 1 ^::^ ev 0) |> iter_print_history ;
      [%expect {|ev0;ev1;ev2|}]


    let%expect_test "sharing reached through different parents is expanded once" =
      (* the same history below two *different* parents, rather than as both children of one
         [BinaryOp]; each level doubles the number of root-to-leaf paths just the same. Timestamps
         keep increasing towards the root, as the representation invariant requires. *)
      let rec nest n t hist =
        if n <= 0 then hist
        else nest (n - 1) (t + 2) ((ev (t + 1) ^:: hist) ++ (ev (t + 2) ^:: hist))
      in
      nest 30 2 (ev 2 ^:: ev 1 ^::^ ev 0) |> count_history_events ;
      [%expect {|63 events|}]
  end )''')
