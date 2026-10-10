
name: XCOVER4S V64 - Validate Sources and Build

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  build-v64:
    runs-on: ubuntu-22.04
    timeout-minutes: 100

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Validate Python patch source
        shell: bash
        run: |
          set -euo pipefail

          echo "Checking tools/apply_kernel.py"

          test -f tools/apply_kernel.py

          python3 - <<'PY'
          from pathlib import Path
          import ast

          p = Path("tools/apply_kernel.py")
          s = p.read_text()

          if s.lstrip().startswith(("name:", "on:", "jobs:")):
              raise SystemExit(
                  "ERROR: tools/apply_kernel.py contains YAML. "
                  "Restore the Python script before building."
              )

          try:
              ast.parse(s, filename=str(p))
          except SyntaxError as e:
              raise SystemExit(
                  f"ERROR: invalid Python at line {e.lineno}: "
                  f"{e.msg}"
              )

          print("PASS: valid Python syntax")
          PY

      - name: Install dependencies
        shell: bash
        run: |
          set -euo pipefail
          sudo apt-get update -qq
          sudo apt-get install -y --no-install-recommends \
            build-essential bc bison flex git make clang lld \
            gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu \
            libc6-dev-arm64-cross linux-libc-dev-arm64-cross \
            libssl-dev libelf-dev python3 cpio gzip xz-utils unzip

      - name: Verify original boot image
        shell: bash
        run: |
          set -euo pipefail
          unzip -o boot-original-for-github.zip -d .
          test -s boot-original.img
          echo 'f94927e36475b2c6ba7b2c1925c3a5969d8d42b321ffc9309aa653cddc7a99e9  boot-original.img' | sha256sum -c -

      - name: Download pinned Samsung kernel
        shell: bash
        run: |
          set -euo pipefail
          git clone --depth 1 --branch lineage-18.1 \
            https://github.com/exynos7885-dev/kernel_samsung_exynos7885.git kernel
          git -C kernel fetch --depth 1 origin \
            6020dfa8315134187f07ca903c9d2ed7ee0256f5
          git -C kernel checkout --detach \
            6020dfa8315134187f07ca903c9d2ed7ee0256f5

      - name: Apply CP patch
        shell: bash
        run: |
          set -euo pipefail
          python3 tools/apply_kernel.py

          python3 - <<'PY'
          from pathlib import Path
          import re

          p = Path(
              "kernel/drivers/soc/samsung/cal-if/pmucal_cp.c"
          )
          s = p.read_text()

          s = re.sub(
              r'\bstatic\s+DEFINE_SRCU\s*\(\s*v64_cp_srcu\s*\)\s*;',
              "DEFINE_SRCU(v64_cp_srcu);",
              s
          )

          p.write_text(s)

          if s.count("DEFINE_SRCU(v64_cp_srcu);") != 1:
              raise SystemExit("SRCU verification failed")

          print("PASS: CP patch and SRCU")
          PY

      - name: Diagnose display clock source
        shell: bash
        run: |
          set -euo pipefail

          python3 - <<'PY'
          from pathlib import Path
          import re

          p = Path(
              "kernel/drivers/video/fbdev/exynos/"
              "dpu_7885/decon_reg.c"
          )
          s = p.read_text()

          pattern = re.compile(
              r'\b(float|double)\s+'
              r'decon_clocks_table'
              r'((?:\s*\[[^\]]*\])+)',
              re.S
          )

          matches = list(pattern.finditer(s))

          if len(matches) != 1:
              raise SystemExit(
                  f"Expected one floating-point table; "
                  f"found {len(matches)}"
              )

          m = matches[0]
          start = s.find("=", m.end())
          end = s.find("};", m.end())

          if start < 0 or end < start:
              raise SystemExit("Cannot locate table initializer")

          if re.search(r'\b\d+\.\d+\b', s[start:end]):
              raise SystemExit(
                  "Fractional display clock values need review"
              )

          s = (
              s[:m.start(1)]
              + "unsigned long"
              + s[m.end(1):]
          )

          p.write_text(s)
          print("PASS: display clock table conversion")
          PY

      - name: Configure ARM64 kernel
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
            -e OVERLAY_FS

          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            olddefconfig

          grep -qx 'CONFIG_CP_PMUCAL=y' out/.config

      - name: Compile display driver first
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

          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            -j2 \
            drivers/video/fbdev/exynos/dpu_7885/decon_reg.o \
            2>&1 | tee V64-display-test.log

      - name: Compile kernel
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

          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            -j2 Image 2>&1 | tee V64-build.log

          test -s out/arch/arm64/boot/Image

      - name: Build ARM64 rootbroker
        shell: bash
        run: |
          set -euo pipefail

          python3 - <<'PY'
          from pathlib import Path

          p = Path("tools/rootbroker.c")
          s = p.read_text()

          if "#include <stddef.h>" not in s:
              p.write_text("#include <stddef.h>\n" + s)
          PY

          aarch64-linux-gnu-gcc \
            -static -Os -Wall \
            -o tools/rootbroker.arm64 \
            tools/rootbroker.c

          aarch64-linux-gnu-gcc \
            -static -Os -Wall \
            -o tools/init-wrapper.arm64 \
            tools/init-wrapper.c

      - name: Repack boot and create Odin TAR
        shell: bash
        run: |
          set -euo pipefail

          python3 tools/repack_boot.py

          test -s deliver/boot.img
          test "$(stat -c %s deliver/boot.img)" = 37748736

          tar -C deliver --format=ustar \
            -cf deliver/V64-EXPERIMENTAL-ODIN.tar boot.img

          mkdir -p restore
          cp boot-original.img restore/boot.img

          tar -C restore --format=ustar \
            -cf deliver/V64-ORIGINAL-RESTORE.tar boot.img

          sha256sum deliver/*.tar \
            > deliver/V64-checksums.txt

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: XCOVER4S-V64-RESULTS
          path: |
            V64-build.log
            V64-display-test.log
            out/.config
            out/arch/arm64/boot/Image
            deliver/
          if-no-files-found: warn
          retention-days: 14
