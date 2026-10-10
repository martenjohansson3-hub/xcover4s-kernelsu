
#!/usr/bin/env python3
"""
XCover4s V64 - experimental CP software latch.

Targets:
  kernel/drivers/soc/samsung/cal-if/pmucal_cp.c
  kernel/drivers/misc/modem_v1/modem_ctrl_ss310ap.c

No KernelSU, Magisk, or SELinux changes.

Experimental only. This does not prove physical modem
power isolation or complete protection against restart.
"""

from pathlib import Path
import re

ROOT = Path("kernel")
CP = ROOT / "drivers/soc/samsung/cal-if/pmucal_cp.c"
MODEM = ROOT / "drivers/misc/modem_v1/modem_ctrl_ss310ap.c"

MARKER = "V64_CP_LATCH_PATCH"


def fail(message):
    raise SystemExit("V64 PATCH ERROR: " + message)


def insert_at_entry(source, name, signature, insertion,
                    static=None):
    """Insert code immediately after one C function's opening brace."""

    if static is True:
        storage = r"static\s+"
    elif static is False:
        storage = r""
    else:
        storage = r"(?:static\s+)?"

    pattern = re.compile(
        r"(?m)^[ \t]*"
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
            f"{name}: expected exactly one definition; "
            f"found {len(matches)}"
        )

    position = matches[0].end()

    return (
        source[:position]
        + "\n"
        + insertion
        + "\n"
        + source[position:]
    )


def main():
    for path in (CP, MODEM):
        if not path.is_file():
            fail(f"Missing source: {path}")

    cp = CP.read_text()
    modem = MODEM.read_text()

    if MARKER in cp or MARKER in modem:
        fail("V64 patch already applied")

    # Provide declarations before the existing source.
    cp = """/* V64_CP_LATCH_PATCH */
#include <linux/atomic.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>

int v64_cp_is_locked(void);

""" + cp

    # The low-level CP start/release functions must refuse
    # activation once the software latch has been set.
    for name in (
        "pmucal_cp_init",
        "pmucal_cp_reset_release",
    ):
        cp = insert_at_entry(
            cp,
            name,
            "void",
            """
    if (v64_cp_is_locked())
        return -EPERM;
""",
            static=False,
        )

    # Make the lock state available to the modem driver.
    modem = """/* V64_CP_LATCH_PATCH */
#include <linux/errno.h>

extern int v64_cp_is_locked(void);

""" + modem

    # Samsung's modem controller functions all have the
    # signatures confirmed in modem_ctrl_ss310ap.c.
    #
    # No renaming or brace matching is required.
    for name in (
        "ss310ap_on",
        "ss310ap_reset",
        "ss310ap_boot_on",
        "ss310ap_dump_start",
    ):
        modem = insert_at_entry(
            modem,
            name,
            "struct modem_ctl *mc",
            """
    if (v64_cp_is_locked())
        return -EPERM;
""",
            static=True,
        )

    # A one-way software latch. The state can only be
    # cleared by a fresh kernel boot.
    #
    # This is not a hardware-enforced power switch.
    cp += r"""

/* V64 CP latch state */
static atomic_t v64_cp_latched = ATOMIC_INIT(0);
static int v64_cp_result = -EAGAIN;

int v64_cp_is_locked(void)
{
    return atomic_read(&v64_cp_latched) != 0;
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
        v64_cp_is_locked()
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

    if (atomic_cmpxchg(
            &v64_cp_latched, 0, 1) != 0)
        return -EPERM;

    /*
     * Attempt to assert CP reset.
     * This does not establish physical power removal.
     */
    ret = pmucal_cp_reset_assert();

    WRITE_ONCE(v64_cp_result, ret);

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

    # Verify the generated patch before writing either file.
    if "static DEFINE_SRCU" in cp:
        fail("Unexpected duplicate-static SRCU declaration")

    for name in (
        "pmucal_cp_init",
        "pmucal_cp_reset_release",
    ):
        if not re.search(
            r"\bint\s+" + name + r"\s*\(",
            cp,
        ):
            fail(f"Missing CP function: {name}")

    for name in (
        "ss310ap_on",
        "ss310ap_reset",
        "ss310ap_boot_on",
        "ss310ap_dump_start",
    ):
        if not re.search(
            r"\bstatic\s+int\s+" + name + r"\s*\(",
            modem,
        ):
            fail(f"Missing modem function: {name}")

    if cp.count(MARKER) != 1:
        fail("Invalid CP patch marker")

    if modem.count(MARKER) != 1:
        fail("Invalid modem patch marker")

    # Commit both changes only after all checks pass.
    CP.write_text(cp)
    MODEM.write_text(modem)

    print("PASS: V64 CP patch generated")
    print("PASS: modem entry guards inserted")
    print("PASS: no function-body brace parsing")
    print("PASS: no duplicate-static SRCU")
    print("SELinux unchanged")
    print("CP source:", CP)
    print("Modem source:", MODEM)


if __name__ == "__main__":
    main()
