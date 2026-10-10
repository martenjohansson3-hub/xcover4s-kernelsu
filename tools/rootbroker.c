#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

/* V64: explicit choice: no per-app authorization. Any app with su access may get UID 0. */
#define SOCK_NAME "v64_auto_rootbroker"
#define MAX_CMD 131072u
#define APP_PATH "/system/bin:/system/xbin:/vendor/bin"
static void logmsg(const char *msg) { int fd=open("/dev/kmsg",O_WRONLY|O_CLOEXEC); if(fd>=0){dprintf(fd,"V64-rootbroker: %s\n",msg);close(fd);} }
static int send_all(int fd,const void *buf,size_t n){const char*p=buf;while(n){ssize_t r=write(fd,p,n);if(r<0&&errno==EINTR)continue;if(r<=0)return -1;p+=r;n-=r;}return 0;}
static int recv_all(int fd,void *buf,size_t n){char*p=buf;while(n){ssize_t r=read(fd,p,n);if(r<0&&errno==EINTR)continue;if(r<=0)return -1;p+=r;n-=r;}return 0;}
static int dial(void){int s=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(s<0)return -1;struct sockaddr_un a={0};a.sun_family=AF_UNIX;a.sun_path[0]='\0';memcpy(a.sun_path+1,SOCK_NAME,sizeof(SOCK_NAME)-1);socklen_t len=offsetof(struct sockaddr_un,sun_path)+sizeof(SOCK_NAME);if(connect(s,(void*)&a,len)){close(s);return -1;}return s;}
static int client(int argc,char **argv){
    const char *cmd=NULL;char *joined=NULL;
    if(argc>=3 && !strcmp(argv[1],"-c")){cmd=argv[2];}
    else if(argc>=2 && !strcmp(argv[1],"-v")){puts("V64 su experimental");return 0;}
    else if(argc>=2 && !strcmp(argv[1],"-V")){puts("64");return 0;}
    else if(argc>=2 && strcmp(argv[1],"-")){
        /* Some clients pass a command without -c. */
        size_t n=0;for(int i=1;i<argc;i++)n+=strlen(argv[i])+1;
        joined=malloc(n+1);if(!joined)return 1;joined[0]=0;
        for(int i=1;i<argc;i++){if(i>1)strcat(joined," ");strcat(joined,argv[i]);}
        cmd=joined;
    }
    size_t n=cmd?strlen(cmd):0;
    if(n>MAX_CMD){free(joined);return 1;}
    int s=dial(); if(s<0){dprintf(2,"V64 su: broker unavailable: %s\n",strerror(errno));free(joined);return 1;}
    uint32_t len=htonl((uint32_t)n);
    if(send_all(s,&len,4)|| (n&&send_all(s,cmd,n))){close(s);free(joined);return 1;}
    free(joined);
    pid_t pid=fork();
    if(pid==0){char buf[8192];for(;;){ssize_t r=read(0,buf,sizeof(buf));if(r<=0)break;if(send_all(s,buf,(size_t)r))break;}shutdown(s,SHUT_WR);_exit(0);}
    char buf[8192];ssize_t r;
    while((r=read(s,buf,sizeof(buf)))>0){if(send_all(1,buf,(size_t)r))break;}
    close(s);
    if(pid>0){kill(pid,SIGTERM);waitpid(pid,NULL,0);}
    return r<0?1:0;
}
static void reap(int sig){(void)sig;while(waitpid(-1,NULL,WNOHANG)>0){}}
static int mkdir_safe(const char*p){if(mkdir(p,0700)&&errno!=EEXIST)return -1;return 0;}
static int file_copy_fd(int in,const char *dst){
    if(lseek(in,0,SEEK_SET)<0)return -1;
    int out=open(dst,O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0755);if(out<0){close(in);return -1;}
    char buf[16384];ssize_t r;int ok=0;
    while((r=read(in,buf,sizeof(buf)))>0)if(send_all(out,buf,(size_t)r)){ok=-1;break;}
    if(r<0)ok=-1;
    fchmod(out,0755);close(out);return ok;
}
static int install_overlay(const char *dir,const char *tag){
    char upper[256],work[256],opt[768],client[300];
    snprintf(upper,sizeof(upper),"/data/local/v64-root/%s-up",tag);
    snprintf(work,sizeof(work),"/data/local/v64-root/%s-work",tag);
    if(mkdir_safe(upper)||mkdir_safe(work))return -1;
    snprintf(client,sizeof(client),"%s/su",upper);
    int src=open("/data/local/v64-root/broker-binary",O_RDONLY|O_CLOEXEC);
    if(src<0)return -1;
    int copied=file_copy_fd(src,client);close(src);if(copied)return -1;
    snprintf(opt,sizeof(opt),"lowerdir=%s,upperdir=%s,workdir=%s",dir,upper,work);
    return mount("overlay",dir,"overlay",0,opt);
}
static void try_install_su(int broker_fd){
    /* Wait for Android PID 1 to switch its root and for /data to be mounted. */
    int ready=0;
    for(int i=0;i<1200;i++){
        if(access("/proc/1/root/system/bin/sh",X_OK)==0 && access("/proc/1/root/data/local",F_OK)==0){ready=1;break;}
        usleep(100000);
    }
    if(!ready){logmsg("system root not accessible; su injection skipped");return;}
    int ns=open("/proc/1/ns/mnt",O_RDONLY|O_CLOEXEC);
    if(ns>=0){(void)setns(ns,CLONE_NEWNS);close(ns);}
    if(chdir("/proc/1/root")||chroot(".")||chdir("/")){logmsg("failed joining Android root; su skipped");return;}
    /* /data must be real encrypted userdata and not just an empty directory. */
    ready=0;for(int i=0;i<1200;i++){
       if(access("/data/system/packages.list",R_OK)==0){ready=1;break;}
       usleep(100000);
    }
    if(!ready){logmsg("/data not ready; su skipped");return;}
    if(mkdir_safe("/data/local/v64-root")){logmsg("failed mkdir su root");return;}
    if(broker_fd<0 || file_copy_fd(broker_fd,"/data/local/v64-root/broker-binary")){logmsg("broker binary unavailable after switchroot");return;}
    int ok=0;
    if(access("/system/xbin",F_OK)==0 && install_overlay("/system/xbin","xbin")==0)ok++;
    if(access("/system/bin",F_OK)==0 && install_overlay("/system/bin","bin")==0)ok++;
    logmsg(ok?"su overlay mounted (device test required)":"failed mounting su overlays");
}
static void session(int fd){
    if(chdir("/proc/1/root") || chroot(".") || chdir("/"))_exit(127);
    uint32_t netlen; if(recv_all(fd,&netlen,4))_exit(2);
    uint32_t n=ntohl(netlen);if(n>MAX_CMD)_exit(2);
    char *cmd=NULL;
    if(n){cmd=malloc((size_t)n+1);if(!cmd||recv_all(fd,cmd,n))_exit(2);cmd[n]=0;}
    if(dup2(fd,0)<0||dup2(fd,1)<0||dup2(fd,2)<0)_exit(2);
    if(fd>2)close(fd);
    setenv("PATH",APP_PATH,1);
    if(cmd)execl("/system/bin/sh","sh","-c",cmd,(char*)NULL);
    else execl("/system/bin/sh","sh",(char*)NULL);
    _exit(127);
}
static int daemon_run(void){
    if(geteuid()!=0){logmsg("no uid0 at startup");return 1;}
    struct sigaction sa={0};sa.sa_handler=reap;sa.sa_flags=SA_RESTART|SA_NOCLDSTOP;sigemptyset(&sa.sa_mask);sigaction(SIGCHLD,&sa,NULL);
    int s=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
    if(s<0)return 1;
    struct sockaddr_un a={0};a.sun_family=AF_UNIX;a.sun_path[0]='\0';memcpy(a.sun_path+1,SOCK_NAME,sizeof(SOCK_NAME)-1);
    if(bind(s,(void*)&a,offsetof(struct sockaddr_un,sun_path)+sizeof(SOCK_NAME))||listen(s,32)){close(s);return 1;}
    /* Fork installer separately so incoming app requests are served. */
    int binfd=open("/rootbroker-v64",O_RDONLY|O_CLOEXEC);
    pid_t p=fork();if(p==0){close(s);try_install_su(binfd);_exit(0);}
    if(binfd>=0)close(binfd);
    for(;;){int fd=accept4(s,NULL,NULL,SOCK_CLOEXEC);if(fd<0){if(errno==EINTR)continue;usleep(100000);continue;}
        struct ucred cr; socklen_t slen=sizeof(cr);
        if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&cr,&slen)){close(fd);continue;}
        pid_t child=fork();if(child==0){close(s);session(fd);}
        close(fd);
    }
}
int main(int argc,char **argv){
    const char *base=strrchr(argv[0],'/');base=base?base+1:argv[0];
    signal(SIGPIPE,SIG_IGN);
    if(!strcmp(base,"su"))return client(argc,argv);
    return daemon_run();
}
