# Does the out-parameter allocator model change anything end to end?

2026-09-10. First end-to-end exercise of `--pulse-model-alloc-arg-pattern`, which had been dead
code since it was written on 09-08.

## Why it had never run

`hints.json` (493 summaries) has **340 allocators, every one `target=return`, zero `argN`**. I had
read that as "Vim has no out-parameter allocators". It is not: the stage-1 prompt that produced
those hints predates the text that teaches the model to emit them. `INSTRUCTIONS` grew 2208 -> 3010
chars in 75913b0 on 09-08, three hours after the hints were written, and the added text is exactly

    Takes a `T **out` argument and stores freshly allocated memory in `*out`
    Allocator: "return" ... or "argN" if the N-th argument is a pointer-to-pointer out parameter

Re-running stage 1 with the current instructions and nothing else changed (`output/vim_9_2_0015_s1b`,
$1.84, 800 s, same codebase.json, no rules file):

|             | old prompt (2208 ch) | current prompt (3010 ch) |
|---|---|---|
| summaries   | 493 | 599 |
| allocators  | 340, all `return` | 442 = 345 `return` + **97 `argN`** |
| deallocators| 153 | 157 |

The drift is confined to the allocator side, which is what the added text addresses. Several
functions appear at more than one index -- `unix_build_argv` at arg1/arg2/arg3, `open_pty` at
arg2/arg3, `spell_read_tree` at arg1/arg3 -- i.e. one call handing back several allocations, which
the old schema could not express at all.

## The cap the experiment hit

The first run died in 1.2 s:

    --pulse-model-alloc-arg-pattern: argument index 8 not supported (max 7)

Infer's model DSL needs each index spelled out (`any_arg $+` N times, then `capt_arg`), so the
patch hard-codes a ceiling, and 7 was arbitrary. Vim has two arg8 out-parameter allocators
(`find_file_in_path_option`, `parse_member`). Extended to arg11 and rebuilt (87 s). Smoke test with
a nine-parameter function and `8:^f$` reports `MEMORY_LEAK_C`, which it cannot do unless the capture
lands on the right argument. `notes/1.3/port1-arg-models.py` and `infer-arg-models.patch` updated
and verified to match the built source verbatim.

## Result

Same v1.3.0 patched binary, same capture, `--jobs 1`, same stage-3 settings, `--adjacent-findings`
on in both. Only the hints differ.

|                      | control (493, 0 alloc-arg) | experiment (599, 97 alloc-arg) |
|---|---|---|
| analyze wall         | 1267 s | 1291 s |
| analyze peak RSS     | 3.93 GB | 3.89 GB |
| raw issues           | 564 | 609 |
| leak warnings        | 444 | **485** |
| Z3 feasible          | 404 | 433 |
| functions to the LLM | 259 | 282 |
| LLM-confirmed        | 53 | 55 |
| bugs.json            | 96 (69 analyzer + 27 adjacent) | 101 (75 + 26) |
| upstream-PR hits     | 4/9 | 4/9 |

Cost of 97 extra models: +2% wall clock, no extra memory.

**The models demonstrably fire.** Of the 96 warnings unique to the experiment arm, 46 name an
alloc-arg function as the allocation source -- `mch_expand_wildcards` 8, `echo_string_core` 7,
`find_type_by_id` 6, `get_spec_reg` 5, and so on.

**Almost none of it survives the filters.** Of 41 extra leak warnings, three bugs in the final
report trace back to the model: `ex_retab` via `tabstop_set` (arg1) and `get_reg_contents` twice
via `get_spec_reg` (arg1).

Diffed at both granularities, because the function-level view flatters the loss and hides the gain:

    bug entries  96 -> 101   shared 87   control-only 4   experiment-only 9
      analyzer       64 -> 70   shared 63   control-only 1   experiment-only 7
      llm-adjacent   27 -> 26   shared 24   control-only 3   experiment-only 2
    functions    78 ->  79   shared 74   control-only 4   experiment-only 5

## The one regression, and it is caused by the model

The single analyzer entry lost is `serverSendToVim` at `if_xcmdsrv.c:427`:

    Memory dynamically allocated by `vim_strsave (custom malloc)`, indirectly via call to `LookupName`

`LookupName` is now modelled as an arg3 out-parameter allocator, so Pulse stops descending into it
and uses the model summary instead -- and the chain through `vim_strsave` that produced the report
disappears. `if_xcmdsrv.c` is PR9's file, so the experiment arm has nothing at all there.

The three lost adjacent findings are all `list_alloc()` cases, i.e. the family Vim's garbage
collector refutes (see full-pipeline-v13.md). Losing those is right.

## Not adopted yet

The hints differ by more than the 97 alloc-arg specs: `return` allocators moved 340 -> 345 and
deallocators 153 -> 157. Small, but until a third arm runs the 599 hints with every alloc-arg spec
stripped, the 7 gains and 1 loss cannot be attributed to the out-parameter models rather than to
those 9 other summaries.
