
#!/usr/bin/env python3
"""V64 CP software latch patch for Samsung Exynos 7885.

Experimental: does not establish physical CP power-off or complete
coverage of modem restart paths.
"""

from pathlib import Path
import re

ROOT = Path("kernel")
CP = ROOT / "drivers/soc/samsung/cal-if/pmucal_cp.c"
MODEM = ROOT / "drivers/misc/modem_v1/modem_ctrl_ss310ap.c"

for path in (CP, MODEM):
    if not path.is_file():
        raise SystemExit(f"Missing source: {path}")

cp_source = CP.read_text()
modem_source = MODEM.read_text()

if "v64_cp_latched" in cp_source:
    raise SystemExit("V64 CP patch already applied")

if "v64_cp_read_enter" in modem_source:
    raise SystemExit("V64 modem patch already applied")


def wrap_function(source, name, signature, is_static=False):
    prefix = r"static\s+" if is_static else ""

    pattern = re.compile(
        r"(?m)^"
        + prefix
        + r"int\s+"
        + re.escape(name)
        + r"\s*\(\s*"
        + re.escape(signature)
        + r"\s*\)\s*\{"
    )

    matches = list(pattern.finditer(source))

    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one definition of {name}, found {len(matches)}"
        )

    match = matches[0]

    closing = re.search(
        r"(?m)^\}\s*$",
        source[match.end():]
    )

    if closing is None:
        raise RuntimeError(
            f"Cannot locate end of {name}"
        )

    end = match.end() + closing.end()

    original = source[match.start():end]

    renamed = re.sub(
        r"\b" + re.escape(name) + r"\b",
        "v64_original_" + name,
        original,
        count=1
    )

    arguments = "" if signature == "void" else "mc"
    storage = "static " if is_static else ""

    wrapper = f"""
{storage}int {name}({signature})
{{
    int cookie;
    int ret;

    cookie = v64_cp_read_enter();

    if (cookie < 0)
        return cookie;

    ret = v64_original_{name}({arguments});

    v64_cp_read_leave(cookie);

    return ret;
}}
"""

    return (
        source[:match.start()]
        + renamed
        + "\n"
        + wrapper
        + source[end:]
    )


# Add kernel headers and forward declarations.
cp_source = """#include <linux/srcu.h>
#include <linux/mutex.h>
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/init.h>

int v64_cp_read_enter(void);
void v64_cp_read_leave(int cookie);

""" + cp_source


# Protect selected low-level CP initialization/release paths.
for function in (
    "pmucal_cp_init",
    "pmucal_cp_reset_release",
):
    cp_source = wrap_function(
        cp_source,
        function,
        "void"
    )


# Avoid kernel panic at the reviewed CP reset failure sites.
for statement in (
    'panic("cp reset assert fail");',
    'panic("cp reset release fail");',
):
    if cp_source.count(statement) != 1:
        raise RuntimeError(
            f"Unexpected panic site: {statement}"
        )

    cp_source = cp_source.replace(
        statement,
        "/* V64: panic removed; inspect error propagation. */",
        1
    )


# Protect selected Samsung modem controller paths.
modem_source = """/* V64 CP software latch hooks */
extern int v64_cp_read_enter(void);
extern void v64_cp_read_leave(int cookie);

""" + modem_source

for function in (
    "ss310ap_on",
    "ss310ap_reset",
    "ss310ap_boot_on",
    "ss310ap_dump_start",
):
    modem_source = wrap_function(
        modem_source,
        function,
        "struct modem_ctl *mc",
        is_static=True
    )


# The latch is created in the CP source.
# IMPORTANT: DEFINE_SRCU already supplies static in this kernel.
# Never write "static DEFINE_SRCU".
cp_source += r"""

/* V64 one-way software CP latch. */
DEFINE_SRCU(v64_cp_srcu);
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

static ssize_t v64_hard_off_lock_show(
    struct kobject *kobj,
    struct kobj_attribute *attr,
    char *buf)
{
    return scnprintf(
        buf,
        PAGE_SIZE,
        "%d\n",
        READ_ONCE(v64_cp_latched)
    );
}

static ssize_t v64_hard_off_result_show(
    struct kobject *kobj,
    struct kobj_attribute *attr,
    char *buf)
{
    return scnprintf(
        buf,
        PAGE_SIZE,
        "%d\n",
        READ_ONCE(v64_cp_result)
    );
}

static ssize_t v64_hard_off_lock_store(
    struct kobject *kobj,
    struct kobj_attribute *attr,
    const char *buf,
    size_t count)
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
    __ATTR(
        hard_off_lock,
        0600,
        v64_hard_off_lock_show,
        v64_hard_off_lock_store
    );

static struct kobj_attribute v64_result_attr =
    __ATTR(
        hard_off_result,
        0400,
        v64_hard_off_result_show,
        NULL
    );

static int __init v64_cp_sysfs_init(void)
{
    struct kobject *kobj;
    int ret;

    kobj = kobject_create_and_add(
        "cp_control",
        kernel_kobj
    );

    if (!kobj)
        return -ENOMEM;

    ret = sysfs_create_file(
        kobj,
        &v64_lock_attr.attr
    );

    if (!ret)
        ret = sysfs_create_file(
            kobj,
            &v64_result_attr.attr
        );

    if (ret)
        kobject_put(kobj);

    return ret;
}

late_initcall(v64_cp_sysfs_init);
"""


# Check the generated output before modifying source files.
if "static DEFINE_SRCU(v64_cp_srcu)" in cp_source:
    raise RuntimeError("Duplicate-static SRCU declaration")

if cp_source.count("DEFINE_SRCU(v64_cp_srcu);") != 1:
    raise RuntimeError("Invalid SRCU declaration count")

for function in (
    "pmucal_cp_init",
    "pmucal_cp_reset_release",
):
    if f"v64_original_{function}" not in cp_source:
        raise RuntimeError(f"Missing wrapper for {function}")

for function in (
    "ss310ap_on",
    "ss310ap_reset",
    "ss310ap_boot_on",
    "ss310ap_dump_start",
):
    if f"v64_original_{function}" not in modem_source:
        raise RuntimeError(f"Missing wrapper for {function}")


CP.write_text(cp_source)
MODEM.write_text(modem_source)

print("V64 CP patch applied")
print("PASS: DEFINE_SRCU without duplicate static")
print("CP:", CP)
print("MODEM:", MODEM)
print("SELinux unchanged")
print("Rootbroker and boot repacking handled by workflow")
