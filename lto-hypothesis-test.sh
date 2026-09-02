#!/usr/bin/env bash
# lto-hypothesis-test.sh — does LTO suppress the speculative-devirt vtable refs?
#
# Reuses the preprocessed TU from a previous vtable-ab-test.sh run.
# Usage: ./lto-hypothesis-test.sh /tmp/vtable-ab.XXXXXX   (your existing workdir)
set -euo pipefail
WORK=${1:?usage: $0 <vtable-ab workdir containing radosgw-admin.ii + flags.txt>}
FLAGS=$(cat "$WORK/flags.txt")
II="$WORK/radosgw-admin.ii"
cd "$WORK"

check () { # $1=file $2=label ; count undefined Rados vtable refs in the object
    refs=$(nm -C "$1" 2>/dev/null | grep -c 'U vtable for rgw::sal::Rados' || true)
    echo "RESULT[$2]: undefined Rados vtable refs = $refs"
}

# Verdict method: attempt a real link (which for LTO objects runs LTO codegen)
# and count the Rados vtable symbols among the undefined-reference ERRORS.
# Sensitive by construction: the non-LTO control MUST report >0.
# (An earlier version linked with --unresolved-symbols=ignore-all and read nm
#  on the output; that scrubs the symbols and reports 0 even for the control.)
link_verdict () { # $1=obj $2=label $3=extra link flags
    refs=$(g++ $3 "$1" -o /dev/null -pie 2>&1 | grep -c 'vtable for rgw::sal::Rados' || true)
    echo "RESULT[$2 FINAL LINK]: Rados vtable refs among link errors = $refs"
}

echo "== compiler: $(g++ --version | head -1)"

# --- control: non-LTO at -O2 (mimics CI opt level, no LTO) --------------------
g++ $FLAGS -O2 -c "$II" -o nolto-O2.o
check nolto-O2.o "no-LTO -O2 object"
link_verdict nolto-O2.o "no-LTO -O2" ""             # expect >0 (bug reproduces)

# --- experiment 1: LTO exactly like RPM optflags ------------------------------
g++ $FLAGS -O2 -flto=auto -ffat-lto-objects -c "$II" -o lto-O2.o
check lto-O2.o "LTO -O2 object (fat section; refs here are EXPECTED, not verdict)"
link_verdict lto-O2.o "LTO -O2" "-flto=auto -O2"    # <-- the verdict

# --- experiment 2: LTO at -O3 (your local level + LTO) ------------------------
g++ $FLAGS -flto=auto -ffat-lto-objects -c "$II" -o lto-O3.o
link_verdict lto-O3.o "LTO -O3" "-flto=auto -O3"

# --- experiment 3 (optional rigor): full el10-style RPM hardening flags -------
RPMFLAGS="-O2 -flto=auto -ffat-lto-objects -fexceptions -g -grecord-gcc-switches -pipe \
 -Wp,-U_FORTIFY_SOURCE,-D_FORTIFY_SOURCE=3 -Wp,-D_GLIBCXX_ASSERTIONS \
 -fstack-protector-strong -march=x86-64-v3 -mtune=generic \
 -fasynchronous-unwind-tables -fstack-clash-protection -fcf-protection"
if g++ $FLAGS $RPMFLAGS -c "$II" -o lto-rpm.o 2>rpm-err.log; then
    link_verdict lto-rpm.o "LTO + full RPM flags" "-flto=auto -O2"
else
    echo "RESULT[LTO + full RPM flags]: did not compile (see rpm-err.log)"
fi

echo
echo "interpretation (control MUST be >0 for the run to be valid):"
echo "  control >0, all LTO links =0  -> LTO hypothesis CONFIRMED"
echo "  control >0, any LTO link >0   -> LTO does NOT rescue; CI success needs another explanation"
echo "  control =0                    -> method broken, do not interpret"
