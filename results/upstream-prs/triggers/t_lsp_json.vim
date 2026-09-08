" Trigger json_encode_lsp_msg() failure: in LSP mode the message must be a
" Dict, but a Funcref nested inside it makes json_encode_gap() fail.  The
" empty string it leaves in the growarray is then never released.
let s:job = job_start(['cat'], {'in_mode': 'lsp', 'out_mode': 'lsp', 'err_mode': 'nl'})
sleep 200m
let s:ch = job_getchannel(s:job)
silent! call ch_sendexpr(s:ch, {'method': 'test', 'params': function('tr')})
silent! call ch_sendexpr(s:ch, {'method': 'test', 'params': function('tr')})
silent! call ch_sendexpr(s:ch, {'method': 'test', 'params': function('tr')})
call job_stop(s:job)
sleep 200m
qall!
