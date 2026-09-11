#!/usr/bin/env python3
"""Port notes/infer-arg-models.patch onto Infer v1.3.0.

Adds --pulse-model-free-arg-pattern N:regex and --pulse-model-alloc-arg-pattern N:regex, which
upstream still has no equivalent of (it only frees argument 0 and only allocates the return value).

The Config half is a pure rebase.  PulseModelsC.ml is not: the model DSL was renamed between
v1.2.0 and v1.3.0, so alloc_out_arg is rewritten here rather than patched:

    mk_fresh ~model_desc ~more ()   ->  fresh ?more ()
    write_deref ~ref ~obj           ->  store ~ref
    start_model (fun () -> ...)     ->  start_named_model desc (fun () -> ...)

Run from the v1.3.0 source root.  Idempotent.
"""
import sys, pathlib

root = pathlib.Path(".").resolve()
if not (root / "infer/src/pulse/PulseModelsC.ml").exists():
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


# --- Config.ml: the two options ------------------------------------------------------------
edit("infer/src/base/Config.ml", "and pulse_model_alloc_pattern =\n", '''and pulse_model_alloc_arg_pattern =
  CLOpt.mk_string_list ~long:"pulse-model-alloc-arg-pattern" ~meta:"N:regex"
    ~in_help:InferCommand.[(Analyze, manual_pulse)]
    "Like $(b,--pulse-model-alloc-pattern) but the acquired resource is written by the matched \\
     function to the pointer-to-pointer out parameter number N (0-based) instead of being \\
     returned, e.g. $(b,1:^\\\\\\\\(make_thing\\\\\\\\)$) for $(i,int make_thing(int flags, struct \\
     thing **out)). May be given several times, once per argument position. The effect is applied \\
     only if argument N really has a pointer-to-pointer type."


and pulse_model_alloc_pattern =
''')

edit("infer/src/base/Config.ml", "and pulse_model_malloc_pattern =\n", '''and pulse_model_free_arg_pattern =
  CLOpt.mk_string_list ~long:"pulse-model-free-arg-pattern" ~meta:"N:regex"
    ~in_help:InferCommand.[(Analyze, manual_pulse)]
    "Like $(b,--pulse-model-free-pattern) but the pointer to be freed is argument number N \\
     (0-based) of the matched function, e.g. $(b,1:^\\\\\\\\(my_free\\\\\\\\|other_free\\\\\\\\)$). May be \\
     given several times, once per argument position."


and pulse_model_malloc_pattern =
''')

edit("infer/src/base/Config.ml", "let command =\n", '''(** parse the [N:regex] specifications shared by --pulse-model-{free,alloc}-arg-pattern *)
let parse_arg_index_patterns option_name specs =
  List.map specs ~f:(fun spec ->
      match String.lsplit2 spec ~on:':' with
      | Some (n, re) -> (
        match int_of_string_opt n with
        | Some n when n >= 0 ->
            (n, Str.regexp re)
        | _ ->
            L.die UserError "%s: bad argument index in '%s'" option_name spec )
      | None ->
          L.die UserError "%s expects N:regex, got '%s'" option_name spec )


let command =
''')

edit("infer/src/base/Config.ml",
     "and pulse_model_alloc_pattern = Option.map ~f:Str.regexp !pulse_model_alloc_pattern\n",
     '''and pulse_model_alloc_arg_pattern =
  parse_arg_index_patterns "--pulse-model-alloc-arg-pattern"
    (RevList.to_list !pulse_model_alloc_arg_pattern)


and pulse_model_alloc_pattern = Option.map ~f:Str.regexp !pulse_model_alloc_pattern
''')

edit("infer/src/base/Config.ml",
     "and pulse_model_free_pattern = Option.map ~f:Str.regexp !pulse_model_free_pattern\n",
     '''and pulse_model_free_pattern = Option.map ~f:Str.regexp !pulse_model_free_pattern

and pulse_model_free_arg_pattern =
  parse_arg_index_patterns "--pulse-model-free-arg-pattern"
    (RevList.to_list !pulse_model_free_arg_pattern)
''')

edit("infer/src/base/Config.mli",
     "val pulse_model_alloc_pattern : Str.regexp option\n",
     "val pulse_model_alloc_arg_pattern : (int * Str.regexp) list\n\n"
     "val pulse_model_alloc_pattern : Str.regexp option\n")

edit("infer/src/base/Config.mli",
     "val pulse_model_free_pattern : Str.regexp option\n",
     "val pulse_model_free_pattern : Str.regexp option\n\n"
     "val pulse_model_free_arg_pattern : (int * Str.regexp) list\n")

# --- PulseModelsC.ml: the out-parameter allocation model ------------------------------------
edit("infer/src/pulse/PulseModelsC.ml", "let realloc_common ~null_case ~desc allocator pointer size : model =\n", '''(* --pulse-model-alloc-arg-pattern N:regex -- the matched function acquires a resource and stores it
   into [*argN] instead of returning it. Only applied when argument N really is a pointer to a
   pointer, so that configuring the wrong index cannot mark an unrelated value as allocated. *)
let is_out_param_typ (typ : Typ.t) =
  match typ.desc with Typ.Tptr (pointee, _) -> Typ.is_pointer pointee | _ -> false


let alloc_out_arg (out_arg : DSL.aval FuncArg.t) : model =
  let open DSL.Syntax in
  start_named_model "custom alloc out parameter"
  @@ fun () ->
  let* {callee_procname} = get_data in
  if not (is_out_param_typ out_arg.FuncArg.typ) then ret ()
  else
    let* acquired = fresh ~more:"(out parameter)" () in
    let* () = allocation (CustomMalloc callee_procname) acquired in
    let* () = and_positive acquired in
    store ~ref:out_arg.FuncArg.arg_payload acquired


let realloc_common ~null_case ~desc allocator pointer size : model =
''')

MATCHERS_HEAD = "  let map_context_tenv f (x, _) = f x in\n  [ +BuiltinDecl.(match_builtin free) <>$ capt_arg $--> free\n"
NEW_HEAD = '''  let map_context_tenv f (x, _) = f x in
  let match_regexp r (_tenv, proc_name) _ = Str.string_match r (Procname.to_string proc_name) 0 in
  (* --pulse-model-free-arg-pattern N:regex -- free argument number N of matching functions *)
  let arg_n_matchers option_name model =
    List.map ~f:(fun (n, r) ->
        match n with
        | 0 ->
            +match_regexp r <>$ capt_arg $+...$--> model
        | 1 ->
            +match_regexp r <>$ any_arg $+ capt_arg $+...$--> model
        | 2 ->
            +match_regexp r <>$ any_arg $+ any_arg $+ capt_arg $+...$--> model
        | 3 ->
            +match_regexp r <>$ any_arg $+ any_arg $+ any_arg $+ capt_arg $+...$--> model
        | 4 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ capt_arg
            $+...$--> model
        | 5 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ capt_arg
            $+...$--> model
        | 6 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ capt_arg
            $+...$--> model
        | 7 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg
            $+ capt_arg $+...$--> model
        | 8 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg
            $+ capt_arg $+...$--> model
        | 9 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg
            $+ capt_arg $+...$--> model
        | 10 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg
            $+ capt_arg $+...$--> model
        | 11 ->
            +match_regexp r
            <>$ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg $+ any_arg
            $+ capt_arg $+...$--> model
        | _ ->
            Logging.die UserError "%s: argument index %d not supported (max 11)" option_name n )
  in
  let free_arg_matchers =
    arg_n_matchers "--pulse-model-free-arg-pattern" free Config.pulse_model_free_arg_pattern
  in
  (* --pulse-model-alloc-arg-pattern N:regex -- acquire a resource through out parameter N *)
  let alloc_arg_matchers =
    arg_n_matchers "--pulse-model-alloc-arg-pattern" alloc_out_arg
      Config.pulse_model_alloc_arg_pattern
  in
  free_arg_matchers
  @ [ +BuiltinDecl.(match_builtin free) <>$ capt_arg $--> free
'''
edit("infer/src/pulse/PulseModelsC.ml", MATCHERS_HEAD, NEW_HEAD)

edit("infer/src/pulse/PulseModelsC.ml",
     '''      ; +match_regexp_opt Config.pulse_model_alloc_pattern &--> custom_alloc_not_null "custom alloc"
      ]
    |> List.map ~f:(ProcnameDispatcher.Call.contramap_arg_payload ~f:ValueOrigin.addr_hist) )''',
     '''      ; +match_regexp_opt Config.pulse_model_alloc_pattern &--> custom_alloc_not_null "custom alloc"
      ]
    @ alloc_arg_matchers
    |> List.map ~f:(ProcnameDispatcher.Call.contramap_arg_payload ~f:ValueOrigin.addr_hist) )''')

print("done")
