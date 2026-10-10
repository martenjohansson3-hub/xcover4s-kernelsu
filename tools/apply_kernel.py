
name: XCOVER4S V64 - Display Fix and Rootbroker

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
          test "$(git -C kernel rev-parse HEAD)" = \
            6020dfa8315134187f07ca903c9d2ed7ee0256f5

      - name: Apply V64 CP patch and verify SRCU
        shell: bash
        run: |
          set -euo pipefail
          python3 -m py_compile tools/apply_kernel.py
          python3 tools/apply_kernel.py

          python3 - <<'PY'
          from pathlib import Path
          import re

          p = Path("kernel/drivers/soc/samsung/cal-if/pmucal_cp.c")
          s = p.read_text()

          s = re.sub(
              r'\bstatic\s+DEFINE_SRCU\s*\(\s*v64_cp_srcu\s*\)\s*;',
              'DEFINE_SRCU(v64_cp_srcu);',
              s
          )

          p.write_text(s)

          if re.search(r'\bstatic\s+DEFINE_SRCU\s*\(', s):
              raise SystemExit("Invalid static DEFINE_SRCU")

          if s.count("DEFINE_SRCU(v64_cp_srcu);") != 1:
              raise SystemExit("SRCU declaration count incorrect")

          print("PASS: CP SRCU declaration")
          PY

      - name: Diagnose and correct display clock types
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
          lines = s.splitlines()

          print("=== Clock declarations ===")
          for i, line in enumerate(lines, 1):
              if (
                  "decon_clocks_table" in line
                  or "decon_reg_get_clock_ratio" in line
              ):
                  print(f"{i}: {line}")

          # Identify the actual declaration, allowing qualifiers,
          # whitespace, and multidimensional arrays.
          pattern = re.compile(
              r'\b(?P<type>float|double)\s+'
              r'(?P<name>decon_clocks_table)'
              r'(?P<dimensions>(?:\s*\[[^\]]*\])+)',
              re.S
          )

          matches = list(pattern.finditer(s))

          if len(matches) != 1:
              raise SystemExit(
                  "Expected exactly one float/double "
                  "decon_clocks_table declaration; "
                  f"found {len(matches)}. "
                  "Source inspection required."
              )

          m = matches[0]

          # Refuse a conversion if fractional literals occur in
          # the initializer. Such values require manual handling.
          initializer_start = s.find("=", m.end())
          initializer_end = s.find("};", m.end())

          if (
              initializer_start < 0
              or initializer_end < initializer_start
          ):
              raise SystemExit(
                  "Cannot safely locate clock table initializer"
              )

          initializer = s[
              initializer_start:initializer_end
          ]

          if re.search(
              r'\b\d+\.\d+(?:[fFlL])?\b',
              initializer
          ):
              raise SystemExit(
                  "Fractional clock values detected; "
                  "automatic integer conversion refused"
              )

          s = (
              s[:m.start("type")]
              + "unsigned long"
              + s[m.end("type"):]
          )

          p.write_text(s)

          print("PASS: decon_clocks_table integer conversion")
          PY

          git -C kernel diff --check

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

      - name: Fast display driver compilation test
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

          test -s \
            out/drivers/video/fbdev/exynos/dpu_7885/decon_reg.o

          echo "PASS: display driver compiled"

      - name: Compile V64 kernel
        shell: bash
        run: |
          set -uo pipefail

          export ARCH=arm64
          export CROSS_COMPILE=aarch64-linux-gnu-
          export CROSS_COMPILE_COMPAT=arm-linux-gnueabi-
          export CLANG_TRIPLE=aarch64-linux-gnu-
          export CC=clang
          export LD=aarch64-linux-gnu-ld
          export KCFLAGS=-Wno-error

          set +e
          make -C kernel \
            O="$GITHUB_WORKSPACE/out" \
            -j2 Image > V64-build.log 2>&1
          result=$?
          set -e

          python3 - <<'PY'
          from pathlib import Path
          import re

          lines = Path("V64-build.log").read_text(
              errors="replace"
          ).splitlines()

          pattern = re.compile(
              r"fatal error:|error:|undefined reference|"
              r"multiple definition|No rule to make target|"
              r"Error [1-9][0-9]*"
          )

          matches = [
              i for i, line in enumerate(lines)
              if pattern.search(line)
          ]

          output = []

          for i in matches[:20]:
              start = max(0, i - 8)
              end = min(len(lines), i + 10)

              output.append(
                  f"\n===== LOG LINES {start + 1}-{end} =====\n"
                  + "\n".join(lines[start:end])
              )

          if not output:
              output.append("\n".join(lines[-150:]))

          report = "\n".join(output)

          Path("V64-ERRORS.txt").write_text(report)
          print(report)
          PY

          echo "Kernel compiler exit code: $result"

          test "$result" -eq 0
          test -s out/arch/arm64/boot/Image

      - name: Fix rootbroker offsetof and build ARM64 binaries
        shell: bash
        run: |
          set -euo pipefail

          python3 - <<'PY'
          from pathlib import Path

          p = Path("tools/rootbroker.c")
          s = p.read_text()

          if "#include <stddef.h>" not in s:
              s = "#include <stddef.h>\n" + s
              p.write_text(s)

          print("PASS: rootbroker stddef.h")
          PY

          aarch64-linux-gnu-gcc \
            -static -Os -Wall \
            -o tools/rootbroker.arm64 \
            tools/rootbroker.c

          aarch64-linux-gnu-gcc \
            -static -Os -Wall \
            -o tools/init-wrapper.arm64 \
            tools/init-wrapper.c

          for elf in tools/rootbroker.arm64 tools/init-wrapper.arm64; do
            test -s "$elf"
            readelf -h "$elf" | grep -q 'Machine:.*AArch64'

            if readelf -l "$elf" | grep -q INTERP; then
              echo "Unexpected dynamic ELF: $elf" >&2
              exit 1
            fi
          done

          echo "PASS: ARM64 rootbroker and init wrapper"

      - name: Repack boot image and create Odin TAR
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

          ls -lah deliver/

      - name: Upload results even on failure
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: XCOVER4S-V64-RESULTS
          path: |
            V64-build.log
            V64-ERRORS.txt
            V64-display-test.log
            out/.config
            out/arch/arm64/boot/Image
            deliver/
          if-no-files-found: warn
          retention-days: 14
