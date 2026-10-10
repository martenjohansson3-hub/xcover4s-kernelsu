#define _GNU_SOURCE
#include <unistd.h>
#include <sys/types.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h>
#include <time.h>
/* Launch original init immediately; child waits until Android data and system exist. */
int main(int argc, char **argv, char **envp) {
    pid_t pid=fork();
    if(pid==0) {
        setsid();
        struct stat st;
        for(int i=0;i<240;i++) {
            if(stat("/data/system/packages.list",&st)==0 && stat("/system/bin/sh",&st)==0) {
                execl("/rootbroker","rootbroker",(char*)0);
                _exit(127);
            }
            sleep(1);
        }
        _exit(1);
    }
    (void)argc;
    execve("/init.original",argv,envp);
    perror("cannot exec original init");
    _exit(127);
}
