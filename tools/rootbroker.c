#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <fcntl.h>

#define REQUEST_SOCK "/data/local/rootmanager/request.sock"
#define CONTROL_SOCK "/data/local/rootmanager/control.sock"
#define ALLOWLIST "/data/local/rootmanager/allowlist"
#define DENYLIST  "/data/local/rootmanager/denylist"
#define PENDING   "/data/local/rootmanager/pending"
#define PACKAGES  "/data/system/packages.list"

static int uid_in_file(const char *path, uid_t uid) {
    FILE *f=fopen(path,"r"); unsigned int x;
    if(!f) return 0;
    while(fscanf(f,"%u",&x)==1) if((uid_t)x==uid){fclose(f);return 1;}
    fclose(f); return 0;
}
static int add_uid(const char *path, uid_t uid) {
    if(uid_in_file(path,uid)) return 0;
    FILE *f=fopen(path,"a"); if(!f) return -1;
    fprintf(f,"%u\n",(unsigned)uid); fclose(f); chmod(path,0600); return 0;
}
static int remove_uid(const char *path, uid_t uid) {
    char tmp[256]; snprintf(tmp,sizeof(tmp),"%s.tmp",path);
    FILE *in=fopen(path,"r"); if(!in) return 0;
    FILE *out=fopen(tmp,"w"); if(!out){fclose(in);return -1;}
    unsigned int x;
    while(fscanf(in,"%u",&x)==1) if((uid_t)x!=uid) fprintf(out,"%u\n",x);
    fclose(in); fclose(out); chmod(tmp,0600);
    if(rename(tmp,path)!=0) return -1;
    chmod(path,0600); return 0;
}
static uid_t manager_uid(void) {
    FILE *f=fopen(PACKAGES,"r"); if(!f) return (uid_t)-1;
    char line[4096], pkg[256]; unsigned int uid;
    while(fgets(line,sizeof(line),f)) {
        if(sscanf(line,"%255s %u",pkg,&uid)==2 && !strcmp(pkg,"com.xcover.rootmanager")) {
            fclose(f); return (uid_t)uid;
        }
    }
    fclose(f); return (uid_t)-1;
}
static void list_file(int fd,const char *path,const char *begin,const char *end) {
    dprintf(fd,"%s\n",begin);
    FILE *f=fopen(path,"r"); char line[64];
    if(f){while(fgets(line,sizeof(line),f)) dprintf(fd,"%s",line); fclose(f);}
    dprintf(fd,"%s\n",end);
}
static void launch_popup(uid_t uid) {
    pid_t p=fork();
    if(p==0){
        char u[32]; snprintf(u,sizeof(u),"%u",(unsigned)uid);
        execl("/system/bin/am","am","start","-n","com.xcover.rootmanager/.MainActivity",
              "--ei","uid",u,(char*)NULL);
        _exit(127);
    }
}
static int make_server(const char *path) {
    int s=socket(AF_UNIX,SOCK_STREAM,0); if(s<0) return -1;
    unlink(path);
    struct sockaddr_un a; memset(&a,0,sizeof(a)); a.sun_family=AF_UNIX;
    strncpy(a.sun_path,path,sizeof(a.sun_path)-1);
    if(bind(s,(struct sockaddr*)&a,sizeof(a))<0){close(s);return -1;}
    /* Peer credentials are verified on the control socket. */
    chmod(path,0666);
    if(listen(s,16)<0){close(s);return -1;}
    return s;
}
static void root_session(int fd) {
    pid_t p=fork();
    if(p==0){
        dup2(fd,STDIN_FILENO); dup2(fd,STDOUT_FILENO); dup2(fd,STDERR_FILENO);
        close(fd);
        execl("/system/bin/sh","sh",(char*)NULL);
        _exit(127);
    }
}
static void handle_request(int fd, uid_t uid) {
    if(uid<10000){dprintf(fd,"DENY protected_uid=%u\n",(unsigned)uid);return;}
    if(uid_in_file(DENYLIST,uid)){dprintf(fd,"DENY uid=%u\n",(unsigned)uid);return;}
    if(uid_in_file(ALLOWLIST,uid)){dprintf(fd,"ALLOW uid=%u\n",(unsigned)uid);root_session(fd);return;}

    add_uid(PENDING,uid);
    launch_popup(uid);
    dprintf(fd,"PENDING uid=%u\n",(unsigned)uid);

    for(int i=0;i<120;i++){ /* 60 seconds */
        usleep(500000);
        if(uid_in_file(ALLOWLIST,uid)){
            remove_uid(PENDING,uid);
            dprintf(fd,"ALLOW uid=%u\n",(unsigned)uid);
            root_session(fd); return;
        }
        if(uid_in_file(DENYLIST,uid)){
            remove_uid(PENDING,uid);
            dprintf(fd,"DENY uid=%u\n",(unsigned)uid); return;
        }
    }
    remove_uid(PENDING,uid);
    dprintf(fd,"DENY timeout uid=%u\n",(unsigned)uid);
}
static void handle_control(int fd, uid_t peer) {
    uid_t mu=manager_uid();
    if(mu==(uid_t)-1 || peer!=mu){
        dprintf(fd,"DENY control_uid=%u manager_uid=%u\n",(unsigned)peer,(unsigned)mu); return;
    }
    char c[256]; ssize_t n=read(fd,c,sizeof(c)-1); if(n<=0)return; c[n]=0;
    unsigned int uid;
    if(!strncmp(c,"LIST",4)) list_file(fd,ALLOWLIST,"LIST_BEGIN","LIST_END");
    else if(!strncmp(c,"PENDING",7)) list_file(fd,PENDING,"PENDING_BEGIN","PENDING_END");
    else if(!strncmp(c,"DENIED",6)) list_file(fd,DENYLIST,"DENIED_BEGIN","DENIED_END");
    else if(sscanf(c,"ALLOW %u",&uid)==1 && uid>=10000 && uid<100000){
        remove_uid(DENYLIST,uid); remove_uid(PENDING,uid); add_uid(ALLOWLIST,uid);
        dprintf(fd,"ALLOW_OK uid=%u\n",uid);
    } else if(sscanf(c,"DENY %u",&uid)==1 && uid>=10000 && uid<100000){
        remove_uid(ALLOWLIST,uid); remove_uid(PENDING,uid); add_uid(DENYLIST,uid);
        dprintf(fd,"DENY_OK uid=%u\n",uid);
    } else if(sscanf(c,"ASK %u",&uid)==1 && uid>=10000 && uid<100000){
        remove_uid(ALLOWLIST,uid); remove_uid(DENYLIST,uid); remove_uid(PENDING,uid);
        dprintf(fd,"ASK_OK uid=%u\n",uid);
    } else dprintf(fd,"ERROR unknown_command\n");
}
int main(void) {
    mkdir("/data/local/rootmanager",0711);
    const char *files[]={ALLOWLIST,DENYLIST,PENDING};
    for(int i=0;i<3;i++){int f=open(files[i],O_CREAT|O_APPEND,0600);if(f>=0)close(f);}
    int rs=make_server(REQUEST_SOCK), cs=make_server(CONTROL_SOCK);
    if(rs<0||cs<0){perror("broker socket");return 1;}
    fprintf(stderr,"rootbroker-final-test ready\n");
    for(;;){
        fd_set set; FD_ZERO(&set); FD_SET(rs,&set); FD_SET(cs,&set);
        int mx=rs>cs?rs:cs;
        if(select(mx+1,&set,NULL,NULL,NULL)<0) continue;
        int which=FD_ISSET(rs,&set)?rs:cs;
        int fd=accept(which,NULL,NULL); if(fd<0) continue;
        struct ucred cr; socklen_t l=sizeof(cr);
        if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&cr,&l)!=0){close(fd);continue;}
        /* Serve requests concurrently: a pending root request must not block
           the manager from sending ALLOW or DENY on the control socket. */
        pid_t worker=fork();
        if(worker==0){
            close(rs); close(cs);
            if(which==rs) handle_request(fd,cr.uid);
            else handle_control(fd,cr.uid);
            close(fd);
            _exit(0);
        }
        close(fd);
        while(waitpid(-1,NULL,WNOHANG)>0){}
    }
}
