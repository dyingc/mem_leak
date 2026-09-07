set pagination off
printf "RESULT first_list=%p", first_list
if first_list
  printf " head.refcount=%d head.len=%ld head.next.refcount=%d", first_list->lv_refcount, first_list->lv_len, first_list->lv_used_next ? first_list->lv_used_next->lv_refcount : -1
end
printf " current_funccal=%p", current_funccal
if current_funccal
  printf " funccal.func=%s", current_funccal->fc_func->uf_name
end
printf "\n"
detach
