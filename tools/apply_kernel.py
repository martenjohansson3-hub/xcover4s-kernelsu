name: XCOVER4S V64 - Experimental Odin boot with rootbroker and CP latch

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  build-v64:
    runs-on: ubuntu-22.04
    timeout-minutes: 100

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Extract and verify original boot image
        shell: bash
        run: |
          set -euo pipefail

          test -s boot-original-for-github.zip
          unzip -o boot-original-for-github.zip -d .

          test -s boot-original.img
          test -s tools/rootbroker.c
          test -s tools/init-wrapper.c
          test -s tools/apply_kernel.py
          test -s tools/repack_boot.py

          echo 'f94927e36475b2c6ba7b2c1925c3a5969d8d42b321ffc9309aa653cddc7a99e9  boot-original.img' | sha256sum -c -

      - name: Install build dependencies
        shell: bash
        run: |
          set -euo pipefail

          sudo apt-get update -qq

          sudo apt-get install -y --no-install-recommends \
            build-essential \
            bc \
            bison \
            flex \
            git \
            make \
            clang \
            lld \
            gcc-aarch64-linux-gnu \
            binutils-aarch64-linux-gnu \
            libssl-dev \
            libelf-dev \
            python3 \
            cpio \
            gzip \
            xz-utils

      - name: Download kernel source
        shell: bash
        run: |
          set -euo pipefail

          git clone \
            --depth 1 \
            --branch lineage-18.1 \
            https://github.com/exynos7885-dev/kernel_samsung_exynos7885.git \
            kernel

          git -C kernel fetch --depth 1 origin \
            6020dfa8315134187f07ca903c9d2ed7ee0256f5

          git -C kernel checkout --detach \
            6020dfa8315134187f07ca903c9d2ed7ee0256f5

          test "$(git -C kernel rev-parse HEAD)" = \
            6020dfa8315134187f07ca903c9d2ed7ee0256f5

      - name: Fix old SELinux patch in apply_kernel.py
        shell: bash
        run: |
          set -euo pipefail

          python3 - <<'PY'
          from pathlib import Path

          p = Path("tools/apply_kernel.py")
          s = p.read_text()

          old = """s=sel.read_text()
          pattern=re.compile(r'(?m)^([ \\t]*(?:static[ \\t]+)?int[ \\t]+selinux_enforcing[ \\t]*=[ \\t]*)1([ \\t]*;)')
          matches=list(pattern.finditer(s))
          if len(matches)!=1:raise RuntimeError('SELinux boot default ambiguous; refusing to patch')
          s=pattern.sub(r'\\g<1>0\\g<2>',s,count=1)
          sel.write_text(s)
          print('V64: changed',cp,modem,sel)"""

          if old in s:
              s = s.replace(
                  old,
                  'print("V64: CP patch applied", cp, modem)'
              )
              p.write_text(s)
              print("Removed old SELinux source patch.")
          elif "SELinux boot default ambiguous" in s:
              raise SystemExit(
                  "Unknown SELinux patch structure. "
                  "Refusing automatic modification."
              )
          else:
              print("Old SELinux patch already absent.")

          compile(p.read_text(), str(p), "exec")
          PY

      - name: Apply CP kernel patch
        shell: bash
        run: |
          set -euo pipefail

          python3 tools/apply_kernel.py

          git -C kernel diff --check

          git -C kernel diff > V64-applied-kernel.patch

          test -s V64-applied-kernel.patch

      - name: Build ARM64 kernel
        shell: bash
        run: |
          set -euo pipefail

          export ARCH=arm64
          export CROSS_COMPILE=aarch64-linux-gnu-
          export CROSS_COMPILE_COMPAT=arm-linux-gnueabi-
          export CLANG_TRIPLE=aarch64-linux-gnu-
          export CC=clang
          export LD=aarch64-linux-gnu-ld
          export KCFLAGS=-Wno-error

          mkdir -p out

          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            lineage_xcover4s_defconfig

          kernel/scripts/config --file out/.config \
            -e CP_PMUCAL \
            -e OVERLAY_FS \
            -e SECURITY_SELINUX \
            -e SECURITY_SELINUX_DEVELOP \
            -e SECURITY_SELINUX_BOOTPARAM \
            -d SECURITY_SELINUX_DISABLE

          kernel/scripts/config \
            --file out/.config \
            --set-val SECURITY_SELINUX_BOOTPARAM_VALUE 0

          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            olddefconfig

          grep -qx 'CONFIG_CP_PMUCAL=y' out/.config
          grep -qx 'CONFIG_OVERLAY_FS=y' out/.config
          grep -qx 'CONFIG_SECURITY_SELINUX_BOOTPARAM_VALUE=0' out/.config

          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            -j2 Image 2>&1 | tee V64-build.log

          test -s out/arch/arm64/boot/Image

      - name: Build custom ARM64 rootbroker and init wrapper
        shell: bash
        run: |
          set -euo pipefail

          aarch64-linux-gnu-gcc \
            -static -Os -Wall -Wextra -Werror \
            -o tools/rootbroker.arm64 \
            tools/rootbroker.c

          aarch64-linux-gnu-gcc \
            -static -Os -Wall -Wextra -Werror \
            -o tools/init-wrapper.arm64 \
            tools/init-wrapper.c

          for elf in \
            tools/rootbroker.arm64 \
            tools/init-wrapper.arm64
          do
            readelf -h "$elf" | grep -q 'Machine:.*AArch64'

            if readelf -l "$elf" | grep -q INTERP; then
              echo "ERROR: dynamically linked binary: $elf"
              exit 1
            fi
          done

      - name: Repack Android boot image
        shell: bash
        run: |
          set -euo pipefail

          python3 tools/repack_boot.py

          test -s deliver/boot.img

          test "$(stat -c %s deliver/boot.img)" = 37748736

      - name: Create Odin TAR and original restore
        shell: bash
        run: |
          set -euo pipefail

          tar -C deliver \
            --format=ustar \
            -cf deliver/V64-EXPERIMENTAL-ODIN.tar \
            boot.img

          mkdir -p restore

          cp boot-original.img restore/boot.img

          tar -C restore \
            --format=ustar \
            -cf deliver/V64-ORIGINAL-RESTORE.tar \
            boot.img

          cp V64-build.log deliver/
          cp V64-applied-kernel.patch deliver/
          cp out/.config deliver/kernel-config.txt

          sha256sum \
            deliver/*.tar \
            deliver/boot.img \
            > deliver/V64-checksums.txt

          cat > deliver/IMPORTANT-READ-FIRST.txt <<'EOF'
          XCOVER4S V64 - EXPERIMENTAL

          Device: Samsung Galaxy XCover 4s
          Android: /e/OS Android 11

          Custom rootbroker:
          - No KernelSU
          - No Magisk
          - No per-app approval prompts intended

          CP:
          - Manual CP latch intended
          - Kernel-level software protection
          - Physical modem shutdown NOT verified

          SELinux:
          - Permissive boot default requested in kernel config
          - Actual runtime state NOT verified

          WARNING:
          This build has NOT been boot-tested.
          Odin packaging does not prove device compatibility.
          A successful build does not prove modem shutdown.
          Do not flash without a verified recovery procedure.
          EOF

          ls -lah deliver/

      - name: Upload V64 Odin artifacts
        uses: actions/upload-artifact@v4
        with:
          name: XCOVER4S-V64-EXPERIMENTAL-FLASH-PACKAGE
          path: deliver/
          if-no-files-found: error
          retention-days: 14
