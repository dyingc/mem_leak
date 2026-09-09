vim9script
silent! echo reduce("abc", (acc, c) => [][0])
source t3_inner_qall.vim
