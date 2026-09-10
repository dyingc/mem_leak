#!/usr/bin/env python3
"""Arithmetic-heavy synthetic corpus (regime B: huge symbolic states).
Generates V variants of a compact SHA-256 (distinct symbol names) in separate TUs, plus a caller TU
that hashes arrays of lines in loops (like Vim's u_compute_hash), and a big-switch interpreter
function (like Vim's list_instructions)."""
import argparse, os
ap = argparse.ArgumentParser()
ap.add_argument('--variants', type=int, default=2)
ap.add_argument('--rounds', type=int, default=64, help='compression rounds (64 = real SHA-256)')
ap.add_argument('--switch-cases', type=int, default=200)
ap.add_argument('--callers', type=int, default=4)
ap.add_argument('--unroll', action='store_true', help='emit the 64 rounds and message schedule as straight-line code (like Vim)')
ap.add_argument('--out', required=True)
a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)

SHA = r'''
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
typedef struct { uint32_t h[8]; uint64_t len; unsigned char buf[64]; size_t buflen; } ctxN;
static const uint32_t KN[64] = {
0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
#define ROTR(x,n) (((x) >> (n)) | ((x) << (32 - (n))))
void processN(ctxN *c, const unsigned char *p) {
  uint32_t w[64], a, b, cc, d, e, f, g, h, t1, t2; int i;
  for (i = 0; i < 16; i++) w[i] = ((uint32_t)p[4*i] << 24) | ((uint32_t)p[4*i+1] << 16) | ((uint32_t)p[4*i+2] << 8) | p[4*i+3];
  for (i = 16; i < 64; i++) {
    uint32_t s0 = ROTR(w[i-15],7) ^ ROTR(w[i-15],18) ^ (w[i-15] >> 3);
    uint32_t s1 = ROTR(w[i-2],17) ^ ROTR(w[i-2],19) ^ (w[i-2] >> 10);
    w[i] = w[i-16] + s0 + w[i-7] + s1; }
  a=c->h[0]; b=c->h[1]; cc=c->h[2]; d=c->h[3]; e=c->h[4]; f=c->h[5]; g=c->h[6]; h=c->h[7];
ROUNDBODY
  c->h[0]+=a; c->h[1]+=b; c->h[2]+=cc; c->h[3]+=d; c->h[4]+=e; c->h[5]+=f; c->h[6]+=g; c->h[7]+=h;
}
void initN(ctxN *c) { static const uint32_t iv[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
  memcpy(c->h, iv, sizeof iv); c->len = 0; c->buflen = 0; }
void updateN(ctxN *c, const unsigned char *data, size_t n) {
  c->len += n;
  while (n > 0) { size_t take = 64 - c->buflen; if (take > n) take = n;
    memcpy(c->buf + c->buflen, data, take); c->buflen += take; data += take; n -= take;
    if (c->buflen == 64) { processN(c, c->buf); c->buflen = 0; } }
}
void finishN(ctxN *c, unsigned char out[32]) {
  unsigned char pad[72]; size_t i; uint64_t bits = c->len * 8; memset(pad, 0, sizeof pad); pad[0] = 0x80;
  size_t padlen = (c->buflen < 56) ? 56 - c->buflen : 120 - c->buflen;
  for (i = 0; i < 8; i++) pad[padlen + i] = (unsigned char)(bits >> (56 - 8 * i));
  updateN(c, pad, padlen + 8);
  for (i = 0; i < 8; i++) { out[4*i] = c->h[i] >> 24; out[4*i+1] = c->h[i] >> 16; out[4*i+2] = c->h[i] >> 8; out[4*i+3] = c->h[i]; }
}
char *hexN(const unsigned char *d, size_t n) { static const char *hx = "0123456789abcdef"; char *s = (char *)malloc(2*n+1); size_t i;
  if (!s) return 0; for (i = 0; i < n; i++) { s[2*i] = hx[d[i] >> 4]; s[2*i+1] = hx[d[i] & 15]; } s[2*n] = 0; return s; }
int selftestN(void) { ctxN c; unsigned char out[32]; initN(&c); updateN(&c, (const unsigned char *)"abc", 3); finishN(&c, out);
  char *h = hexN(out, 32); int ok = h && h[0] == 'b'; free(h); return ok; }
'''
hdr = ['#include <stddef.h>', '#include <stdint.h>']
for v in range(a.variants):
    if a.unroll:
        rb = []
        for i in range(a.rounds):
            rb.append(f'  {{ uint32_t S1 = ROTR(e,6) ^ ROTR(e,11) ^ ROTR(e,25); uint32_t ch = (e & f) ^ (~e & g); t1 = h + S1 + ch + KN[{i}] + w[{i}];')
            rb.append(f'    uint32_t S0 = ROTR(a,2) ^ ROTR(a,13) ^ ROTR(a,22); uint32_t mj = (a & b) ^ (a & cc) ^ (b & cc); t2 = S0 + mj; h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2; }}')
        roundbody = '\n'.join(rb)
        sched = '\n'.join(f'  w[{i}] = w[{i-16}] + (ROTR(w[{i-15}],7) ^ ROTR(w[{i-15}],18) ^ (w[{i-15}] >> 3)) + w[{i-7}] + (ROTR(w[{i-2}],17) ^ ROTR(w[{i-2}],19) ^ (w[{i-2}] >> 10));' for i in range(16, 64))
        src = SHA.replace('ROUNDBODY', roundbody)
        src = src.replace('''  for (i = 16; i < 64; i++) {
    uint32_t s0 = ROTR(w[i-15],7) ^ ROTR(w[i-15],18) ^ (w[i-15] >> 3);
    uint32_t s1 = ROTR(w[i-2],17) ^ ROTR(w[i-2],19) ^ (w[i-2] >> 10);
    w[i] = w[i-16] + s0 + w[i-7] + s1; }''', sched)
    else:
        src = SHA.replace('ROUNDBODY', '''  for (i = 0; i < ROUNDS; i++) {
    uint32_t S1 = ROTR(e,6) ^ ROTR(e,11) ^ ROTR(e,25); uint32_t ch = (e & f) ^ (~e & g);
    t1 = h + S1 + ch + KN[i] + w[i];
    uint32_t S0 = ROTR(a,2) ^ ROTR(a,13) ^ ROTR(a,22); uint32_t mj = (a & b) ^ (a & cc) ^ (b & cc);
    t2 = S0 + mj; h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2; }''').replace('ROUNDS', str(a.rounds))
    for name in ['ctxN', 'KN', 'processN', 'initN', 'updateN', 'finishN', 'hexN', 'selftestN']:
        src = src.replace(name, name[:-1] + str(v))
    open(os.path.join(a.out, f'sha{v}.c'), 'w').write(src)
    hdr += [f'typedef struct {{ uint32_t h[8]; uint64_t len; unsigned char buf[64]; size_t buflen; }} ctx{v};',
            f'void init{v}(ctx{v} *c); void update{v}(ctx{v} *c, const unsigned char *d, size_t n);',
            f'void finish{v}(ctx{v} *c, unsigned char out[32]); char *hex{v}(const unsigned char *d, size_t n); int selftest{v}(void);']
open(os.path.join(a.out, 'sha.h'), 'w').write('\n'.join(hdr) + '\n')
# callers: hash arrays of lines (like u_compute_hash) and a big switch interpreter
cs = ['#include "sha.h"', '#include <string.h>', '#include <stdlib.h>',
      'typedef struct { char **lines; int count; int flags; } bufT;', 'int g_mode;']
for k in range(a.callers):
    v = k % a.variants
    cs.append(f'''char *compute_hash{k}(bufT *buf, int from, int to) {{
  ctx{v} c; unsigned char out[32]; int i; init{v}(&c);
  if (!buf || !buf->lines) return 0;
  for (i = from; i <= to && i < buf->count; i++) {{
    const char *p = buf->lines[i]; if (!p) continue;
    update{v}(&c, (const unsigned char *)p, strlen(p) + 1);
    if (buf->flags & {1 << (k % 8)}) update{v}(&c, (const unsigned char *)"\\n", 1);
  }}
  finish{v}(&c, out);
  return hex{v}(out, 32);
}}''')
sw = ['int interp(bufT *buf, const int *isn, int n) {', '  int acc = 0, pc = 0, sp = 0; int stack[64];',
      '  while (pc < n) { int op = isn[pc++];', '    switch (op) {']
for i in range(a.switch_cases):
    body = {0: f'acc += {i}; if (sp < 64) stack[sp++] = acc;', 1: f'if (sp > 0) acc ^= stack[--sp]; else acc = {i};',
            2: f'if (buf && buf->count > {i % 7}) acc += (int)strlen(buf->lines[{i % 7}] ? buf->lines[{i % 7}] : "");',
            3: f'{{ char *h = compute_hash{i % a.callers}(buf, 0, {i % 5}); if (h) {{ acc += h[0]; free(h); }} }}',
            4: f'acc = (acc << {i % 5 + 1}) | (acc >> {i % 3 + 1}); g_mode = acc & {i};'}[i % 5]
    sw.append(f'    case {i}: {body} break;')
sw += ['    default: return -1; } if (acc > 1000000) break; }', '  return acc; }']
cs += sw
cs.append('int main_entry(bufT *buf, const int *isn, int n) { int r = interp(buf, isn, n); ' + ' '.join(f'if (!selftest{v}()) return -{v+1};' for v in range(a.variants)) + ' return r; }')
open(os.path.join(a.out, 'caller.c'), 'w').write('\n'.join(cs) + '\n')
srcs = ' '.join([f'sha{v}.c' for v in range(a.variants)] + ['caller.c'])
open(os.path.join(a.out, 'Makefile'), 'w').write(f'CC ?= gcc\nCFLAGS ?= -O0 -g\nSRCS := {srcs}\nOBJS := $(SRCS:.c=.o)\nall: $(OBJS)\n%.o: %.c sha.h\n\t$(CC) $(CFLAGS) -c $< -o $@\nclean:\n\trm -f $(OBJS)\n.PHONY: all clean\n')
print('generated', srcs)
