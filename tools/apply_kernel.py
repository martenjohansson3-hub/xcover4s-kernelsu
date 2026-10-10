#!/usr/bin/env python3
"""V64 proof-of-build CP latch and SELinux boot default patch.
Physical RF power loss and all firmware paths cannot be validated offline.
"""
from pathlib import Path
import re

ROOT = Path('kernel')
cp = ROOT/'drivers/soc/samsung/cal-if/pmucal_cp.c'
modem = ROOT/'drivers/misc/modem_v1/modem_ctrl_ss310ap.c'
sel = ROOT/'security/selinux/hooks.c'

CP_PREAMBLE = r'''#include <linux/srcu.h>
#include <linux/mutex.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>

/* V64: software latch protects listed Linux CP initialization/release paths.
 * This is NOT a physical RF kill switch or a guarantee against CP firmware. */
static DEFINE_SRCU(v64_cp_srcu);
static DEFINE_MUTEX(v64_cp_set_mutex);
static int v64_cp_latched;
static int v64_cp_result = -EAGAIN;

int v64_cp_read_enter(void)
{
    int cookie = srcu_read_lock(&v64_cp_srcu);
    if (READ_ONCE(v64_cp_latched)) {
        srcu_read_unlock(&v64_cp_srcu, cookie);
        return -EPERM;
    }
    return cookie;
}

void v64_cp_read_leave(int cookie)
{
    srcu_read_unlock(&v64_cp_srcu, cookie);
}

static ssize_t v64_hard_off_lock_show(struct kobject *kobj,
    struct kobj_attribute *attr, char *buf)
{
    return scnprintf(buf, PAGE_SIZE, "%d\n", READ_ONCE(v64_cp_latched));
}

static ssize_t v64_hard_off_result_show(struct kobject *kobj,
    struct kobj_attribute *attr, char *buf)
{
    return scnprintf(buf, PAGE_SIZE, "%d\n", READ_ONCE(v64_cp_result));
}

static ssize_t v64_hard_off_lock_store(struct kobject *kobj,
    struct kobj_attribute *attr, const char *buf, size_t count)
{
    int ret;
    if (count != 2 || buf[0] != '1' || buf[1] != '\n')
        return -EINVAL;
    mutex_lock(&v64_cp_set_mutex);
    if (READ_ONCE(v64_cp_latched)) {
        mutex_unlock(&v64_cp_set_mutex);
        return -EPERM;
    }
    /* Latch first: reject new CP bring-up attempts and wait for in-flight calls. */
    WRITE_ONCE(v64_cp_latched, 1);
    synchronize_srcu(&v64_cp_srcu);
    ret = pmucal_cp_reset_assert();
    /* Never silently re-arm CP if reset assertion fails. */
    WRITE_ONCE(v64_cp_result, ret);
    mutex_unlock(&v64_cp_set_mutex);
    return ret ? ret : count;
}

static struct kobj_attribute v64_lock_attr =
    __ATTR(hard_off_lock, 0600, v64_hard_off_lock_show, v64_hard_off_lock_store);
static struct kobj_attribute v64_result_attr =
    __ATTR(hard_off_result, 0400, v64_hard_off_result_show, NULL);

static int __init v64_cp_sysfs_init(void)
{
    struct kobject *kobj;
    int ret;
    kobj = kobject_create_and_add("cp_control", kernel_kobj);
    if (!kobj)
        return -ENOMEM;
    ret = sysfs_create_file(kobj, &v64_lock_attr.attr);
    if (!ret)
        ret = sysfs_create_file(kobj, &v64_result_attr.attr);
    if (ret)
        kobject_put(kobj);
    return ret;
}
late_initcall(v64_cp_sysfs_init);

'''

def insert_after_function(src, func_name, wrapper):
    rgx = re.compile(r'(?m)^((?:static\s+)?int\s+)'+re.escape(func_name)+r'(\s*\([^;]*?\)\s*\{)',re.S)
    m = rgx.search(src)
    if not m or len(list(rgx.finditer(src))) != 1:
        raise RuntimeError(f'expected one {func_name} definition')
    # Rename only the definition, preserving all nested body operations.
    defn = src[m.start():m.end()].replace(func_name,'v64_original_'+func_name,1)
    src=src[:m.start()]+defn+src[m.end():]
    start=m.start()+len(defn)
    # The Samsung source has preprocessor alternative branches whose C braces
    # cannot be counted literally. Top-level closing braces begin in column 0.
    end=re.search(r'(?m)^\}\s*$',src[start:])
    if not end:
        raise RuntimeError('could not find end of function '+func_name)
    i=start+end.end()
    return src[:i]+'\n\n'+wrapper+'\n'+src[i:]

def wrapped(name, param='void'):
    args = '' if param == 'void' else 'mc'
    return f'''int {name}({param})
{{
    int cookie;
    int ret;
    cookie = v64_cp_read_enter();
    if (cookie < 0)
        return cookie;
    ret = v64_original_{name}({args});
    v64_cp_read_leave(cookie);
    return ret;
}}'''

s=cp.read_text()
assert 'v64_cp_srcu' not in s
# Original cp function exists before any sysfs registration; forward declare.
preamble=CP_PREAMBLE.replace('/* V64:', 'extern int pmucal_cp_reset_assert(void);\n\n/* V64:',1)
s=preamble+s
for name in ('pmucal_cp_init','pmucal_cp_reset_release'):
    s=insert_after_function(s,name,wrapped(name))
for line in ('panic("cp reset assert fail");','panic("cp reset release fail");'):
    if s.count(line)!=1:raise RuntimeError('unexpected reset panic site '+line)
    s=s.replace(line,'/* V64: propagate CP failure to caller rather than panic. */')
cp.write_text(s)

s=modem.read_text()
assert 'v64_cp_read_enter' not in s
s='''/* CP action guard for CONFIG_CP_PMUCAL=y (verified V62 config). */
extern int v64_cp_read_enter(void);
extern void v64_cp_read_leave(int cookie);
''' + s
for name in ('ss310ap_on','ss310ap_reset','ss310ap_boot_on','ss310ap_dump_start'):
    s=insert_after_function(s,name,wrapped(name,'struct modem_ctl *mc').replace('int '+name+'(', 'static int '+name+'(',1))
modem.write_text(s)

s=sel.read_text()
pattern=re.compile(r'(?m)^([ \t]*(?:static[ \t]+)?int[ \t]+selinux_enforcing[ \t]*=[ \t]*)1([ \t]*;)')
matches=list(pattern.finditer(s))
if len(matches)!=1:raise RuntimeError('SELinux boot default ambiguous; refusing to patch')
s=pattern.sub(r'\g<1>0\g<2>',s,count=1)
sel.write_text(s)
print('V64: changed',cp,modem,sel)
