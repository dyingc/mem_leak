#include <stdint.h>

__attribute__((noinline)) int doubling_null(int x) {
  uintptr_t raw = (uintptr_t)0 + ((uintptr_t)x - (uintptr_t)x);
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  raw = raw + raw;
  int *p = (int *)raw;
  return *p;
}
