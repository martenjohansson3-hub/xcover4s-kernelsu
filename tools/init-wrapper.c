#define _GNU_SOURCE
#include <unistd.h>
#include <sys/types.h>
#include <stdlib.h>
#include <stdio.h>
/* Boot-safe fallback: daemon failure does not prevent original Android init. */
int main(int argc, char **argv, char **envp) {
    pid_t pid = fork();
    if (pid == 0) {
        setsid();
        execl("/rootbroker-v64", "rootbroker-v64", (char *)0);
        _exit(127);
    }
    (void)argc;
    execve("/init.original", argv, envp);
    /* Failure here is fatal and should be recorded on a recovery console. */
    perror("V64: cannot exec original Android init");
    _exit(127);
}
