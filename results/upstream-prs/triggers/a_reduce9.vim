vim9script
silent! echo reduce("abc", (acc, c) => [][0])
sleep 30
qall!
