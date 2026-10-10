
#!/usr/bin/env python3
"""V64 CP software latch patch. Not proof of electrical CP power-off."""
from pathlib import Path
import re

root = Path('kernel')
cp = root / 'drivers/soc/samsung/cal-if/pmucal_cp.c'
modem = root / 'drivers/misc/modem_v1/modem_ctrl_ss310ap.c'

for p in (cp, modem):
    if not p.is_file():
        raise SystemExit(f'Missing source: {p}')

# Fail before changing any file when input does not match the reviewed sources.
a = cp.read_text()
b = modem.read_text()
if 'v64_cp_latched' in a or 'v64_cp_read_enter' in b:
    raise SystemExit('V64 already present: refusing to apply twice')


def wrap_function(src, name, signature, static=False):
    # Original Samsung source: one top-level int function with a brace on next line.
    regex = re.compile(r'(?m)^' + (r'static\s+' if static else '') +
                       r'int\s+' + re.escape(name) +
                       r'\s*\(\s*' + re.escape(signature) + r'\s*\)\s*\{')
    matches = list(regex.finditer(src))
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one {name} definition, found {len(matches)}')
    m = matches[0]
    # The reviewed files use a top-level closing brace at column zero.
    close = re.search(r'(?m)^\}\s*$', src[m.end():])
    if close is None:
        raise RuntimeError(f'Cannot find closing brace for {name}')
    end = m.end() + close.end()
    original = src[m.start():end]
    renamed = re.sub(r'\b' + re.escape(name) + r'\b', 'v64_original_' + name,
                     original, count=1)
    args = '' if signature == 'void' else 'mc'
    prefix = 'static ' if static else ''
    wrapper = f'''\n{prefix}int {name}({signature})
{{
    int cookie = v64_cp_read_enter();
    int ret;
    if (cookie < 0)
        return cookie;
    ret = v64_original_{name}({args});
    v64_cp_read_leave(cookie);
    return ret;
}}
'''
    return src[:m.start()] + renamed + '\n' + wrapper + src[end:]

for fn in ('pmucal_cp_init', 'pmucal_cp_reset_release'):
    a = wrap_function(a, fn, 'void')
for panic in ('panic("cp reset assert fail");', 'panic("cp reset release fail");'):
    if a.count(panic) != 1:
        raise RuntimeError(f'Unexpected panic occurrence: {panic}')
    a = a.replace(panic, '/* V64: propagate existing error return, do not panic. */')

# Guard the reviewed high-level modem paths as well as low-level PMUCAL.
b = '''/* V64 CP software latch hooks */
extern int v64_cp_read_enter(void);
extern void v64_cp_read_leave(int cookie);
''' + b
for fn in ('ss310ap_on', 'ss310ap_reset', 'ss310ap_boot_on', 'ss310ap_dump_start'):
    b = wrap_function(b, fn, 'struct modem_ctl *mc', static=True)

# Functions are declared before their wrappers and implemented below.
a = '''#include <linux/srcu.h>
#include <linux/mutex.h>
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/init.h>

int v64_cp_read_enter(void);
void v64_cp_read_leave(int cookie);

''' + a

a += r'''
/* V64 software-only one-way latch; does not guarantee CP power isolation. */
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
    WRITE_ONCE(v64_cp_latched, 1);
    synchronize_srcu(&v64_cp_srcu);
    ret = pmucal_cp_reset_assert();
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

# Verify outputs before writing either source file.
assert 'int v64_cp_read_enter(void)' in a
assert 'static int ss310ap_on(struct modem_ctl *mc)' in b
cp.write_text(a)
modem.write_text(b)
print('V64 CP patch applied:', cp, modem)
print('NOTE: SELinux, rootbroker, boot repack and other CP paths are not handled here.')
