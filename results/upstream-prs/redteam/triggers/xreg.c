/* Fabricate a VimRegistry root-window property listing "<base>" and
 * "<base>1".."<base>N" as registered on a live (unmapped) window, then keep the
 * window alive so DoRegisterName()'s XGetGeometry() check succeeds and every
 * candidate name is reported as "in use" (res == -1). */
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv)
{
    const char *base = argc > 1 ? argv[1] : "FOO";
    int n = argc > 2 ? atoi(argv[2]) : 999;
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) { fprintf(stderr, "no display\n"); return 1; }
    Window root = RootWindow(dpy, 0);
    Window w = XCreateSimpleWindow(dpy, root, 0, 0, 1, 1, 0, 0, 0);
    Atom reg = XInternAtom(dpy, "VimRegistry", False);
    size_t cap = (size_t)(n + 1) * 64, len = 0;
    char *buf = malloc(cap);
    len += sprintf(buf + len, "%x %s", (unsigned)w, base) + 1;   /* include NUL */
    for (int i = 1; i <= n; i++)
        len += sprintf(buf + len, "%x %s%d", (unsigned)w, base, i) + 1;
    XChangeProperty(dpy, root, reg, XA_STRING, 8, PropModeReplace,
                    (unsigned char *)buf, (int)len);
    XSync(dpy, False);
    printf("registered %d names on window 0x%x, %zu bytes\n", n + 1, (unsigned)w, len);
    fflush(stdout);
    pause();
    return 0;
}
