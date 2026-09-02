#!/usr/bin/env bash
# vtable-ab-test.sh
#
# Cross-compiler A/B test for the rgw-standalone-admin "undefined reference to
# vtable for rgw::sal::RadosZone" link failure.
#
# Strategy: preprocess the failing TU (radosgw-admin.cc) ONCE on this machine
# (-E -> self-contained .ii, all headers baked in), then compile the *same
# bytes* with different compilers in containers:
#
#   fedora:40      -> GCC 14.2.1  (the failing toolchain)
#   rockylinux:10  -> GCC 14.3.1  (suspected fixed)
#   fedora:39      -> GCC 13.x    (known-good generation)
#
# Because the input is identical, any difference in the emitted object is
# attributable to the compiler alone — no headers, glibc, or distro flags
# involved. Also tests -O2 and -fno-devirtualize-speculatively on 14.2.1 to
# pin the responsible pass/level.
#
# Usage:  ./vtable-ab-test.sh <ceph-build-dir>
#         (a configured build dir where the link failure reproduces, on the
#          UNPATCHED tree — check out the commit before the include guards)
#
# Requires: podman (or docker), network access for container pulls.

set -euo pipefail

BUILD_DIR=${1:?usage: $0 <ceph-build-dir (e.g. ~/development/build-ceph/release)>}
ENGINE=$(command -v podman || command -v docker) || { echo "need podman or docker"; exit 1; }

OBJ=src/rgw/CMakeFiles/rgw-standalone-admin.dir/radosgw-admin/radosgw-admin.cc.o
WORK=$(mktemp -d /tmp/vtable-ab.XXXXXX)
echo ">>> workdir: $WORK"

cd "$BUILD_DIR"

# --- 1. grab the exact compile command for the failing object -----------------
CMD=$(ninja -t commands bin/rgw-standalone-admin | grep -F -- "-o $OBJ" | head -1)
[ -n "$CMD" ] || { echo "ERROR: compile command for $OBJ not found (is the target enabled?)"; exit 1; }
echo "$CMD" > "$WORK/original-command.txt"

# --- 2. rebuild the command with python (token-safe), emit .ii + flag list ----
python3 - "$WORK" <<'EOF'
import shlex, sys, subprocess, os
work = sys.argv[1]
cmd = shlex.split(open(os.path.join(work, "original-command.txt")).read())

# drop ccache prefix if present
while "ccache" in os.path.basename(cmd[0]):
    cmd = cmd[1:]

# strip dep-file bookkeeping
out = []
skip = False
for i, tok in enumerate(cmd):
    if skip: skip = False; continue
    if tok in ("-MF", "-MT"): skip = True; continue
    if tok in ("-MD", "-MMD"): continue
    out.append(tok)
cmd = out

# locate source, -c, -o
src = next(t for t in cmd if t.endswith("radosgw-admin.cc"))
oidx = cmd.index("-o")

# 2a. preprocess: -c -> -E, -o -> work/.ii
pre = list(cmd)
pre[pre.index("-c")] = "-E"
pre[oidx + 1] = os.path.join(work, "radosgw-admin.ii")
print(">>> preprocessing ...")
subprocess.run(pre, check=True)

# 2b. distil compiler-only flags for recompilation of the .ii
#     (drop -I/-isystem/-D/-U/source/-o/-c — meaningless for preprocessed input)
flags, skip = [], False
for tok in cmd[1:]:
    if skip: skip = False; continue
    if tok in ("-isystem", "-I", "-o", "-c"): skip = True; continue
    if tok.startswith(("-I", "-D", "-U")) and tok not in ("-U",): continue
    if tok == src or tok.endswith(".o"): continue
    flags.append(tok)
open(os.path.join(work, "flags.txt"), "w").write(" ".join(flags))
print(">>> flags:", " ".join(flags))
EOF

FLAGS=$(cat "$WORK/flags.txt")

# --- 2c. sanity: the header leak must be PRESENT in the .ii, else the test is vacuous
LEAK=$(grep -c "class RadosZoneGroup" "$WORK/radosgw-admin.ii" || true)
echo ">>> sanity: 'class RadosZoneGroup' occurrences in .ii = $LEAK"
if [ "$LEAK" -eq 0 ]; then
    echo "ERROR: the concrete RadosZone/RadosZoneGroup definitions are NOT in the"
    echo "preprocessed TU — the source tree this build dir points at has the"
    echo "include-guard fix applied (fully or partially). Check out the unpatched"
    echo "commit (before the WITH_RADOSGW_RADOS guards), re-run cmake/ninja until"
    echo "the link failure reproduces, then re-run this script."
    exit 1
fi

# --- 3a. control: compile the .ii on the HOST (the toolchain that fails) ------
echo
echo "=== HOST control ($(g++ --version | head -1)) ==="
if g++ $FLAGS -c "$WORK/radosgw-admin.ii" -o "$WORK/out-host.o" 2>"$WORK/err-host.log"; then
    refs=$(nm -C "$WORK/out-host.o" | grep -c 'U vtable for rgw::sal::Rados' || true)
    echo "RESULT[host]: undefined Rados vtable refs = $refs"
    nm -C "$WORK/out-host.o" | grep 'U vtable for rgw::sal::Rados' || true
    if [ "$refs" -eq 0 ]; then
        echo "NOTE: host is CLEAN on this .ii -> the failing build's objects differ from"
        echo "this input. Suspect stale ccache entries ('ccache -C', rebuild) or that the"
        echo "build dir's sources/flags changed since the failure. Container results below"
        echo "then only confirm compiler equivalence, not the original failure."
    fi
else
    echo "RESULT[host]: DID NOT COMPILE"; tail -5 "$WORK/err-host.log"
fi

# --- 3b. compile the same .ii in each container --------------------------------
run_case () {
    local image=$1 label=$2 extra=$3
    echo
    echo "=== $label ($image) extra-flags: [${extra:-none}] ==="
    # '|| true' so one unavailable image doesn't abort the whole matrix (set -e)
    $ENGINE run --rm -v "$WORK":/w:Z "$image" bash -c "
        dnf -y -q install gcc-c++ binutils >/dev/null 2>&1 || dnf -y install gcc-c++ binutils
        g++ --version | head -1
        if g++ $FLAGS $extra -c /w/radosgw-admin.ii -o /w/out-\$\$.o 2>/w/err-\$\$.log; then
            refs=\$(nm -C /w/out-\$\$.o | grep -c 'U vtable for rgw::sal::Rados' || true)
            echo \"RESULT[$label]: undefined Rados vtable refs = \$refs\"
            nm -C /w/out-\$\$.o | grep 'U vtable for rgw::sal::Rados' || true
        else
            echo \"RESULT[$label]: DID NOT COMPILE (inconclusive — see below)\"
            tail -5 /w/err-\$\$.log
        fi"
}

run_case fedora:40                            "gcc-14.2.1 -O3"                 ""                                || true
run_case fedora:40                            "gcc-14.2.1 -O2"                 "-O2"                             || true
run_case fedora:40                            "gcc-14.2.1 -O3 no-spec-devirt"  "-fno-devirtualize-speculatively" || true
run_case quay.io/rockylinux/rockylinux:10     "gcc-14.3.1 -O3"                 ""                                || true
run_case fedora:39                            "gcc-13.x  -O3"                  ""                                || true

echo
echo ">>> interpretation:"
echo "  refs>0 on 14.2.1/-O3 and refs=0 on 14.3.1/-O3  -> compiler regression fixed in 14.3 (file GCC/RH bug with the .ii)"
echo "  refs=0 on 14.2.1/-O2                            -> also explains packaged (-O2) builds passing"
echo "  refs=0 with -fno-devirtualize-speculatively     -> pins the speculative-devirt pass"
echo "  gcc-13 case may fail to compile F40-preprocessed source; that's inconclusive, not a data point"
echo ">>> preprocessed TU kept at: $WORK/radosgw-admin.ii (attach to a bug report if filing)"
