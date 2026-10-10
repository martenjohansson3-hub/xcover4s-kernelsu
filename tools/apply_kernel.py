
#!/usr/bin/env python3
"""
XCover4s V64 - experimental CP software latch.

Patches:
  drivers/soc/samsung/cal-if/pmucal_cp.c
  drivers/misc/modem_v1/modem_ctrl_ss310ap.c

This is an experimental software latch, not a verified
physical modem power cutoff.
"""

from pathlib import Path
import re

ROOT = Path("kernel")
CP = ROOT / "drivers/soc/samsung/cal-if/pmucal_cp.c"
MODEM = ROOT / "drivers/misc/modem_v1/modem_ctrl_ss310ap.c"


def fail(message):
    raise SystemExit("V64 PATCH ERROR: " + message)


def replace_one(source, old, new, label):
    count = source.count(old)
    if count != 1:
        fail(f"{label}: expected one match, found {count}")
    return source.replace(old, new, 1)


def wrap_function(source, name, signature, static=False):
    """
    Rename the original function and install a wrapper.
    Only exact function signatures are accepted.
    """

    storage = r"static\s+" if static else ""

    pattern = re.compile(
        r"(?m)^"
        + storage
        + r"int\s+"
        + re.escape(name)
        + r"\s*\(\s*"
        + re.escape(signature)
        + r"\s*\)\s*\{"
    )

    matches = list(pattern.finditer(source))

    if len(matches) != 1:
        fail(
            f"{name}: expected one function definition, "
            f"found {len(matches)}"
        )

    match = matches[0]

    # Find the matching closing brace, ignoring braces
    # inside comments and string literals.
    start = match.end() - 1
    depth = 0
    end = None
    state = "normal"
    i = start

    while i < len(source):
        c = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""

        if state == "line_comment":
            if c == "\n":
                state = "normal"

        elif state == "block_comment":
            if c == "*" and nxt == "/":
                state = "normal"
                i += 1

        elif state == "string":
            if c == "\\":
                i += 1
            elif c == '"':
                state = "normal"

        elif state == "char":
            if c == "\\":
                i += 1
            elif c == "'":
                state = "normal"

        else:
            if c == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif c == "/" and nxt == "*":
                state = "block_comment"
                i += 1
            elif c == '"':
                state = "string"
            elif c == "'":
                state = "char"
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        i += 1

    if end is None:
        fail(f"{name}: closing brace not found")

    original = source[match.start():end]

    renamed = re.sub(
        r"\b" + re.escape(name) + r"\b",
        "v64_original_" + name,
        original,
        count=1,
    )

    args = "" if signature == "void" else "mc"
    qualifier = "static " if static else ""

    wrapper = f"""
{qualifier}int {name}({signature})
{{
    int cookie;
    int ret;

    cookie = v64_cp_read_enter();

    if (cookie < 0)
        return cookie;

    ret = v64_original_{name}({args});

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


def main():
    for path in (CP, MODEM):
        if not path.is_file():
            fail(f"missing kernel source: {path}")

    cp = CP.read_text()
    modem = MODEM.read_text()

    if "v64_cp_latched" in cp:
        fail("CP source already patched")

    if "v64_cp_read_enter" in modem:
        fail("modem source already patched")

    # Kernel headers and declarations.
    cp = """#include <linux/srcu.h>
#include <linux/mutex.h>
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/init.h>

int v64_cp_read_enter(void);
void v64_cp_read_leave(int cookie);

""" + cp

    # Wrap selected low-level CP functions.
    for name in (
        "pmucal_cp_init",
        "pmucal_cp_reset_release",
    ):
        cp = wrap_function(
            cp,
            name,
            "void",
        )

    # Modem controller declarations.
    modem = """/* V64 CP latch hooks */
extern int v64_cp_read_enter(void);
extern void v64_cp_read_leave(int cookie);

""" + modem

    # Wrap selected Samsung modem controller functions.
    for name in (
        "ss310ap_on",
        "ss310ap_reset",
        "ss310ap_boot_on",
        "ss310ap_dump_start",
    ):
        modem = wrap_function(
            modem,
            name,
            "struct modem_ctl *mc",
            static=True,
        )

    # Software latch and sysfs interface.
    # DEFINE_SRCU already expands with static storage
    # in this kernel. Do NOT prefix it with static.
    cp += r"""

/* V64 CP software latch */
DEFINE_SRCU(v64_cp_srcu);

static DEFINE_MUTEX(v64_cp_set_mutex);

static int v64_cp_latched;
static int v64_cp_result = -EAGAIN;

int v64_cp_read_enter(void)
{
    int cookie;

    cookie = srcu_read_lock(&v64_cp_srcu);

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

    if (count != 2 ||
        buf[0] != '1' ||
        buf[1] != '\n')
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

    # Validate generated declarations.
    if re.search(
        r"\bstatic\s+DEFINE_SRCU\s*\(",
        cp,
    ):
        fail("duplicate static in SRCU declaration")

    if cp.count("DEFINE_SRCU(v64_cp_srcu);") != 1:
        fail("incorrect SRCU declaration count")

    for name in (
        "pmucal_cp_init",
        "pmucal_cp_reset_release",
    ):
        if f"v64_original_{name}" not in cp:
            fail(f"missing CP wrapper: {name}")

    for name in (
        "ss310ap_on",
        "ss310ap_reset",
        "ss310ap_boot_on",
        "ss310ap_dump_start",
    ):
        if f"v64_original_{name}" not in modem:
            fail(f"missing modem wrapper: {name}")

    # Write files only after validation.
    CP.write_text(cp)
    MODEM.write_text(modem)

    print("PASS: V64 CP patch applied")
    print("PASS: SRCU declaration verified")
    print("PASS: modem wrappers generated")
    print("SELinux unchanged")
    print("CP source:", CP)
    print("Modem source:", MODEM)


if __name__ == "__main__":
    main()
