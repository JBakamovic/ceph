# Session export: rgw-standalone-admin link failure — investigation, fix, and evidence

Date: 2026-08-13/14 (investigation), exported 2026-09-02
Participants: Adi (jusufadis.bakamovic@clyso.com) + Claude
Repo: ceph, branch work based on main (regression window ~2026-07-22..23)

---

## 1. Problem statement

`main` fails to link `bin/rgw-standalone-admin` in the default build configuration:

```
/usr/bin/ld: radosgw-admin.cc.o: undefined reference to `vtable for rgw::sal::RadosZone'
/usr/bin/ld: radosgw-admin.cc.o: undefined reference to `vtable for rgw::sal::RadosZoneGroup'
collect2: error: ld returned 1 exit status
```

References originate from `resolve_zone_id`, `resolve_zone_id_opt`,
`resolve_zone_ids_opt`, `JSONFormatter_PrettyZone::Handler::encode_json`, and
the inline dtors `RadosZone::~RadosZone` / `RadosZoneGroup::~RadosZoneGroup`.

Observed: fails on Fedora 40 (GCC 14.2.1) release builds; "works" on CentOS 9,
Rocky 10, Ubuntu jammy/noble (see §4 — each "works" had a specific reason).

## 2. Regression point

- Target `rgw-standalone-admin` added by commit `5f01d5c058c`
  ("RGW - Standalone - Add a standalone build target and RPM"), landed on main
  via **PR #68291** merge `77f6a3b47eb` (2026-07-23) — matches the
  "fine on Jul 22" boundary. First appearance on first-parent history
  confirmed; absent in merge's parent.
- `WITH_RADOSGW_STANDALONE` is `cmake_dependent_option(... ON WITH_RADOSGW_POSIX OFF)`
  and `WITH_RADOSGW_POSIX` defaults ON ⇒ the broken target builds **by default**.
- Note: `WITH_RADOSGW_POSIX` is defined twice in top-level CMakeLists
  (option at ~585, cmake_dependent_option at ~596) — pre-existing wart, flagged.

## 3. Root cause (final, evidence-backed)

`radosgw-admin.cc` is compiled a second time for the no-RADOS standalone
target (`-UWITH_RADOSGW_RADOS`), but:

1. It contains real unguarded uses of concrete `rgw::sal::RadosStore`
   (period-push `get_remote_conn`, reshard max-shards check, 3× tier-type
   validation via `->svc()->sync_modules`).
2. It only *compiles* because `rgw_sal_rados.h` leaks in transitively:
   - `radosgw-admin.cc → rgw_sync.h → rgw_sal_rados.h`
   - `radosgw-admin.cc → rgw_data_sync.h → rgw_sal_rados.h`
   - `radosgw-admin.cc → rgw_http_client_curl.h → rgw_frontend.h → rgw_sal_rados.h`
     (`rgw_frontend.h`'s include is a pure over-include — uses no Rados type)
3. With the concrete classes visible, **GCC 14's speculative devirtualization**
   (at -O2/-O3, non-LTO) speculates virtual calls/dtors (e.g. the
   `unique_ptr<Zone>` teardown in `resolve_zone_*`) to the only visible
   derived classes and emits references to their vtables.
4. The standalone libraries exclude `rgw_sal_rados.cc` (which holds the key
   functions/vtables) ⇒ undefined reference at link.

## 4. Platform matrix (why CI never caught it) — all cells verified

Method: preprocessed the failing TU once (`g++ -E` → self-contained
`radosgw-admin.ii`), compiled identical bytes across compilers/flags,
counted `U vtable for rgw::sal::Rados*` (verdict at final link for LTO).

| Toolchain | Opt | LTO | Result |
|---|---|---|---|
| GCC 12 (jammy via toolchain-r PPA), GCC 13.2 (noble default), GCC 13.3 (centos9 gcc-toolset-13) | any | – | no speculation of this pattern → OK |
| GCC 14.2.1 / 14.3.1 | -O0 / -Og | – | pass doesn't run → OK |
| GCC 14.2.1 / 14.3.1 | -O2 / -O3 | **-flto=auto** | whole-program view sees vtable exists nowhere → speculation declined → OK (all el10/Fedora **RPM** builds; distro optflags inject LTO) |
| GCC 14.2.1 / 14.3.1 | -O2 / -O3 | none | **LINK FAILURE** (local `do_cmake`-style builds on GCC 14 distros) |

Key experimental numbers (host GCC 14.2.1, same `.ii`):
- no-LTO -O2 final link: **14** vtable refs among link errors (control)
- LTO -O2 / LTO -O3 / LTO + full el10 RPM hardening flags: **0**
- `-fno-devirtualize-speculatively` at -O3: **0** (pins the pass)
- Rocky 10 container (GCC 14.3.1) -O2/-O3 non-LTO: same 2 refs ⇒ NOT fixed in 14.3

Investigation corrections worth remembering (labels lied, compile lines didn't):
- User's local `build-ceph/debug` was actually `-O3 -DNDEBUG` (Release).
- CI job #8110 "rocky10 RelWithDebInfo" was actually `CMAKE_BUILD_TYPE=Debug`;
  its compile lines carry RPM `-O2` **followed by** Ceph's `-Og` (last -O wins).
  The rocky10 **default** cell was RelWithDebInfo `-O2` + `-flto=auto` → LTO-shielded.
- CI log #8110 is a Jenkins matrix (many distros interleaved) — same object
  "built" 14× = 7 cells × 2 targets; per-line attribution by proximity is
  unreliable, attribute by flags (`-march=x86-64-v3` ⇒ el10; `-DNDEBUG` vs
  `-Og`+`CEPH_DEBUG_MUTEX` ⇒ default vs debug flavor).

## 5. The fix (proper patch — applied & build-verified by Adi)

**Approach:** guard RADOS-only code + close the header leaks. All guards are
true under `WITH_RADOSGW_RADOS` ⇒ RADOS build preprocesses identically.
Patch: `rgw-standalone-admin-proper-fix.patch` (8 files, +76 lines, additive only).

- `src/rgw/radosgw-admin/radosgw-admin.cc`
  - guard `get_remote_conn()` overloads; `commit_period` remote lookup →
    `-ENOTSUP` + "use --url" hint in standalone
  - guard reshard max-shards check (callers already guarded)
  - 3× tier-type validation → `EINVAL` "--tier-type requires the RADOS backend"
  - guard `#include "rgw_sync.h"`, `#include "rgw_data_sync.h"`
- `src/rgw/rgw_main.h`: `rest_filter()` no-RADOS fallback returning `orig`
  (sync modules are RADOS-only; non-RADOS stores return null sync module —
  identical runtime behavior)
- `src/rgw/rgw_frontend.h`: guard over-include of `rgw_sal_rados.h`
- `src/rgw/rgw_appmain.cc`: guard unused `#include "rgw_rest_log.h"`
- `src/rgw/rgw_rest_metadata.cc`: guard include + 3 op bodies → `-ENOTSUP`
  (handlers only registered by `RadosStore::register_admin_apis`; POSIX
  registers none ⇒ unreachable, defensive)
- `src/rgw/rgw_rest_config.cc`: guard include + `RGWOp_ZoneConfig_Get::send_response`
  → `-ENOTSUP` (same registration argument)
- `src/rgw/rgw_realm_reloader.cc`: guard include + RadosLuaManager block
  (already runtime-dead behind `get_name()=="rados"`)
- `src/rgw/driver/rados/rgw_bucket.cc`: guard include (PR authors had guarded
  all 20 use sites but not the include) + add missing direct
  `#include "common/errno.h"` (`cpp_strerror` previously arrived transitively
  via `rgw_sal_rados.h` — include-what-you-use fix)

**Result: standalone + RADOS builds both green with the patch.**

Earlier attempts, kept for the record:
- CMake stopgap (`WITH_RADOSGW_STANDALONE_ADMIN=OFF` gate) — superseded, removed.
- First naive include-guard-only patch — WRONG; moved failure to compile time
  and exposed the unguarded `RadosStore` uses; withdrawn.
- `-fno-devirtualize-speculatively` as the fix — REJECTED: only disables one
  pass (plain inlining of visible inline dtors also emits refs, all GCC ≥11),
  GCC-only flag, and — decisive — it ships live UB (see §6).

## 6. Runtime UB — hypothesis CONFIRMED

The unguarded casts weren't just a link problem; they're crashes in the
shipped binary. In the standalone binary (unpatched tree, linked via the
devirt flag workaround), driver is a `POSIXDriver`, so
`static_cast<rgw::sal::RadosStore*>(driver)->svc()->sync_modules->...` reads
garbage past the real object.

Reachable sites: `zonegroup add` (~5738), `zone create` (~6408),
`zone modify` (~6678) — all with `--tier-type`.

Experiment (standalone env with posix/dbstore backend):
```bash
export D=/tmp/standalone-test && mkdir -p $D/posix
RGWA="bin/rgw-standalone-admin --no-mon-config -c /dev/null \
  --dbstore-config-uri=file:$D/config.db \
  --rgw-posix-base-path=$D/posix \
  --rgw-posix-userdb-dir=$D \
  --rgw-posix-database-root=$D \
  --rgw-data=$D --run-dir=$D"
$RGWA realm create --rgw-realm=r1 --default                       # OK ✓
$RGWA zonegroup create --rgw-zonegroup=zg1 --rgw-realm=r1 --master --default  # OK ✓
$RGWA zone create --rgw-zone=z1 --rgw-zonegroup=zg1 --master --default --tier-type=cloud
# → *** Caught signal (Segmentation fault) *** in main()          # CONFIRMED ✓
```
(`--rgw-backend-store` is ignored — standalone hardwires posix in
`DriverManager::get_config`. Config store in standalone is always
dbstore/sqlite via `dbstore_config_uri` regardless of `rgw_config_store`.)

Patched binary must instead print `ERROR: --tier-type requires the RADOS
backend` and exit cleanly — the before/after pair for the PR.

Suggested formal strengthening: rebuild target with UBSan only
(`-fsanitize=undefined -fno-sanitize-recover=vptr` — full ASan clashes with
tcmalloc; alternative `-DALLOCATOR=libc`), expect
"member call on address 0x… which does not point to an object of type
'rgw::sal::RadosStore'" at radosgw-admin.cc:6480.

## 7. Bonus bugs found along the way

1. **Standalone startup segfault on unwritable paths**: with default
   `/var/lib/ceph/radosgw` paths unavailable, `POSIXDriver::initialize` /
   `DriverManager::init_raw_storage_provider` segfaults instead of erroring
   (crash observed before DB paths were redirected). Independent, reportable.
2. `WITH_RADOSGW_POSIX` defined twice in top-level CMakeLists (585 vs 596).
3. CI observability: "debug"-labeled builds that are Release and vice versa;
   the rocky10 debug flavor only links thanks to accidental `-O2 ... -Og`
   flag ordering.

## 8. Artifacts (in ceph repo root / outputs)

- `rgw-standalone-admin-proper-fix.patch` — the fix (8 files, +76). Apply:
  `git apply rgw-standalone-admin-proper-fix.patch` (`--check` to dry-run,
  `-R` to revert, `-3` if drifted).
- `rgw-standalone-admin-fix-PR.md` — commit message + PR title/description
  (incl. root-cause section, behavior notes, follow-ups: GCC≥14-no-LTO CI job,
  optional GCC bugzilla report). Fill in Signed-off-by + tracker id.
- `rgw-standalone-admin-proper-fix.md` — earlier scoping/difficulty doc
  (pre-implementation; superseded in parts by actual patch).
- `vtable-ab-test.sh` — cross-compiler A/B: preprocess TU → compile same .ii
  in fedora:40 / quay.io/rockylinux/rockylinux:10 / fedora:39 containers +
  host control; sanity-checks that the leak is present in the .ii.
- `lto-hypothesis-test.sh` — LTO verdict via link-error grep with mandatory
  non-LTO control (earlier nm-based verdict was invalid — control caught it).
- Preprocessed reduction for a GCC bug report: `/tmp/vtable-ab.*/radosgw-admin.ii`
  (reproduces on 14.2.1 & 14.3.1; vanishes with -fno-devirtualize-speculatively;
  reduce with cvise before filing).

## 9. Key methodology lessons

- Judge builds by their compile lines, not their labels (3 mislabels found).
- A/B tests need a positive control (the nm-based LTO verdict silently
  measured nothing; the fixed method's control showed 14 refs).
- Preprocessed-source cross-compiler testing (`-E` → same .ii everywhere)
  eliminates every environment variable except the compiler itself.
- "Fixes the linker error" ≠ "fixes the bug": the flag workaround shipped a
  binary that segfaults on `zone create --tier-type` — the linker error was
  the only thing protecting users from the UB.

## 10. Open items

- [ ] Re-apply patch (tree was reverted for repro work), rebuild both configs.
- [ ] Optional: UBSan-only run for the formal vptr diagnostic (§6).
- [ ] Optional: patched-binary control transcript for the PR.
- [ ] File tracker issue; fill Signed-off-by + Fixes: in commit message; submit PR
      referencing PR #68291 authors for the -ENOTSUP behavior decisions.
- [ ] Propose CI cell: GCC ≥14, no LTO, standalone targets (the uncovered quadrant).
- [ ] Optional: GCC bugzilla report with reduced .ii.
- [ ] Report bonus bug: standalone startup segfault on unwritable default paths.
