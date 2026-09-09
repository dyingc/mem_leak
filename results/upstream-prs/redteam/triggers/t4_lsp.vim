let s:job = job_start(['cat'], {'in_mode': 'lsp', 'out_mode': 'lsp', 'err_mode': 'nl'})
sleep 200m
let s:ch = job_getchannel(s:job)
silent! call ch_sendexpr(s:ch, {'method': 'test', 'params': function('tr')})
silent! call ch_sendexpr(s:ch, {'method': 'test', 'params': function('tr')})
silent! call ch_sendexpr(s:ch, {'method': 'test', 'params': function('tr')})
" sibling: non-LSP json channel with the same bad value -> json_encode_nr_expr
let s:job2 = job_start(['cat'], {'in_mode': 'json', 'out_mode': 'json', 'err_mode': 'nl'})
sleep 200m
silent! call ch_sendexpr(job_getchannel(s:job2), {'method': 'test', 'params': function('tr')})
silent! call ch_sendexpr(job_getchannel(s:job2), {'method': 'test', 'params': function('tr')})
" sibling: json_encode() itself
silent! let s:x = json_encode({'a': function('tr')})
silent! let s:x = js_encode({'a': function('tr')})
call job_stop(s:job)
call job_stop(s:job2)
sleep 200m
qall!
