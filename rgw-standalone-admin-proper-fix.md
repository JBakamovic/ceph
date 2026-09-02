# Proper fix for `rgw-standalone-admin` — scope & draft

## Goal

Make `radosgw-admin.cc` (and the headers it pulls) genuinely buildable with
`-UWITH_RADOSGW_RADOS`, so the `rgw-standalone-admin` target compiles **and**
links without the RADOS driver, instead of the current situation where it only
compiles because `rgw_sal_rados.h` leaks in through headers and then fails to
link on `vtable for rgw::sal::RadosZone` / `RadosZoneGroup`.

This is the change that the temporary `WITH_RADOSGW_STANDALONE_ADMIN=OFF`
unblock is standing in for.

## Root cause (recap)

`radosgw-admin.cc` is intrinsically a RADOS tool. It uses the concrete
`rgw::sal::RadosStore` (and, via `-O2`+ devirtualization, `RadosZone` /
`RadosZoneGroup`) **outside** `#ifdef WITH_RADOSGW_RADOS`. Those uses compile in
the no-RADOS build only because the concrete class definitions leak in through
`rgw_sync.h` / `rgw_data_sync.h` / `rgw_frontend.h` → `rgw_sal_rados.h`. The link
then fails because the RADOS driver objects that emit those classes' vtables and
out-of-line key functions (`rgw_sal_rados.cc` …) are not part of the standalone
libraries.

So the fix has to do two things at once:
1. **Guard every concrete-RADOS use** in `radosgw-admin.cc` so the no-RADOS
   build doesn't reference RADOS symbols.
2. **Stop `rgw_sal_rados.h` from being visible** in no-RADOS translation units,
   which (a) removes the leak the guarded code used to rely on and (b) removes
   the `-O2`+ devirtualization path that turns abstract zone calls into
   `RadosZone`/`RadosZoneGroup` references.

Step 2 is the expensive one — see "The cascade" below.

---

## Part A — guard the concrete-RADOS sites in `radosgw-admin.cc`

All of these are RADOS/multisite/reshard functionality that is meaningless in a
dbstore/posix standalone build, so guarding them out is semantically correct
(the corresponding commands simply won't exist in the standalone tool).

| Lines | What it is | Command family |
|------|------------|----------------|
| 2023–2057 | `get_remote_conn()` (two overloads, take `RadosStore*`) | period push (multisite) |
| ~2168–2185 | remote-push block in `commit_period()` (`static_cast<RadosStore*>` + `get_remote_conn`) | period commit |
| 3405–3406 | `static_cast<RadosStore*>(driver)->getRados()->get_max_bucket_shards()` in `check_reshard_bucket_params()` | bucket reshard |
| 5803, 6476, 6756 | `static_cast<RadosStore*>(driver)->svc()->sync_modules->get_manager()` (tier-type validation) | zonegroup/zone create+modify |
| 2746 | `static_cast<const RadosZoneGroup&>(...)` in `sync_status()` | already guarded ✅ |

Two viable styles:

**A1 — line/block-level guards** (smallest diff, but scatters `#ifdef`s). Example
for the reshard check:

```diff
--- a/src/rgw/radosgw-admin/radosgw-admin.cc
+++ b/src/rgw/radosgw-admin/radosgw-admin.cc
@@ int check_reshard_bucket_params(...)
+#ifdef WITH_RADOSGW_RADOS
   if (num_shards > (int)static_cast<rgw::sal::RadosStore*>(driver)->getRados()->get_max_bucket_shards()) {
     cerr << "ERROR: num_shards too high, max value: "
          << static_cast<rgw::sal::RadosStore*>(driver)->getRados()->get_max_bucket_shards() << std::endl;
     return -EINVAL;
   }
+#endif
```

Helper functions get guarded whole:

```diff
+#ifdef WITH_RADOSGW_RADOS
 /// search for a matching zone/zonegroup id and return a connection if found
 static boost::optional<RGWRESTConn> get_remote_conn(rgw::sal::RadosStore* driver, ...) { ... }
 /// search each zonegroup for a connection
 static boost::optional<RGWRESTConn> get_remote_conn(rgw::sal::RadosStore* driver, ...) { ... }
+#endif
```

…and their only caller (the `if (!remote.empty())` block inside `commit_period`)
must be guarded to match, otherwise the no-RADOS build has a dangling reference.

The tier-type validation blocks (5803/6476/6756) are already inside `if
(ptier_type) { ... }` scopes, so each becomes:

```diff
       const string *ptier_type = (tier_type_specified ? &tier_type : nullptr);
+#ifdef WITH_RADOSGW_RADOS
       if (ptier_type) {
         auto sync_mgr = static_cast<rgw::sal::RadosStore*>(driver)->svc()->sync_modules->get_manager();
         ...
       }
+#endif
```

**A2 — command-group guards (recommended).** Because period, zonegroup, zone,
and reshard are *entire* RADOS/multisite command families, it is cleaner and
arguably less error-prone to guard at the command-dispatch level (the
`OPT_PERIOD_*`, `OPT_ZONEGROUP_*`, `OPT_ZONE_*`, `OPT_BUCKET_RESHARD` handlers)
than to sprinkle `#ifdef`s through shared helpers. This is also how much of the
file is already structured (200+ existing `WITH_RADOSGW_RADOS` guards). The
trade-off is deciding, per command, whether it belongs in the standalone tool at
all — a product decision, not a mechanical one.

Also note `resolve_zone_id` / `resolve_zone_id_opt` / `resolve_zone_ids_opt` and
`JSONFormatter_PrettyZone` use only the **abstract** SAL interface, so they do
**not** need guarding *provided* Part C removes `rgw_sal_rados.h` visibility (no
visible concrete type ⇒ nothing for the optimizer to devirtualize to). If the
header leak were left in place, these would reintroduce the link error at `-O2`+.

---

## Part B — header-layering fixes

These are the changes that make it *possible* to remove the leak. They are small
in line count but have outsized blast radius (Part D).

### B1 — `rgw_main.h::rest_filter` (RADOS-coupled inline in a generic header)

`rest_filter()` calls `sync_module->get_rest_filter(dialect, orig)`, which needs
the **complete** `RGWSyncModuleInstance`. That class is fully defined only in the
RADOS-only `driver/rados/rgw_sync_module.h`; `rgw_sal.h` merely forward-declares
it (line 45) and defines the `shared_ptr` typedef (line 46). Sync modules are a
RADOS/multisite feature, and the posix/dbstore stores return a null
`get_sync_module()`, so a no-op fallback is correct:

```diff
--- a/src/rgw/rgw_main.h
+++ b/src/rgw/rgw_main.h
+#ifdef WITH_RADOSGW_RADOS
 static inline RGWRESTMgr *rest_filter(rgw::sal::Driver* driver, int dialect, RGWRESTMgr* orig)
 {
   RGWSyncModuleInstanceRef sync_module = driver->get_sync_module();
   if (sync_module) {
     return sync_module->get_rest_filter(dialect, orig);
   } else {
     return orig;
   }
 }
+#else
+static inline RGWRESTMgr *rest_filter(rgw::sal::Driver*, int, RGWRESTMgr* orig)
+{
+  return orig;   // sync modules are RADOS-only; no filtering in standalone
+}
+#endif
```

*Cleaner-but-larger alternative:* promote the small `get_rest_filter` interface
(and enough of `RGWSyncModuleInstance`) from `driver/rados/rgw_sync_module.h` into
the generic SAL layer so generic code never depends on a RADOS header. Correct
architecturally, but touches every sync-module user — a separate, larger PR.

### B2 — `rgw_frontend.h` over-include

`rgw_frontend.h:18` includes `rgw_sal_rados.h` but uses no `Rados*` type (verified
by grep). It is a pure over-include that today happens to be load-bearing for the
leak. Guard it so the RADOS build is byte-for-byte unchanged and the no-RADOS
build stops seeing the concrete types:

```diff
--- a/src/rgw/rgw_frontend.h
+++ b/src/rgw/rgw_frontend.h
 #include "rgw_auth_registry.h"
+#ifdef WITH_RADOSGW_RADOS
 #include "rgw_sal_rados.h"
+#endif
```

---

## Part C — kill the leak / devirtualization path in `radosgw-admin.cc`

Once the sites in Part A are guarded, the RADOS sync headers are only needed by
guarded code, so their includes can be guarded too (this is what removes
`rgw_sal_rados.h` from the no-RADOS TU and defeats the `-O2`+ devirtualization
into `RadosZone`/`RadosZoneGroup`):

```diff
--- a/src/rgw/radosgw-admin/radosgw-admin.cc
+++ b/src/rgw/radosgw-admin/radosgw-admin.cc
 #include "rgw_usage.h"
+#ifdef WITH_RADOSGW_RADOS
 #include "rgw_sync.h"
+#endif
 #include "rgw_trim_bilog.h"
 #include "rgw_trim_datalog.h"
 #include "rgw_trim_mdlog.h"
+#ifdef WITH_RADOSGW_RADOS
 #include "rgw_data_sync.h"
+#endif
 #include "rgw_rest_conn.h"
```

(`rgw_trim_*` and `rgw_reshard.h` are also RADOS-driver headers and should get the
same treatment if their users are all guarded.)

---

## Part D — the cascade (this is where the real cost is)

Parts A–C are a bounded, few-dozen-line change. The expensive part is that
**removing the leak exposes every other place where generic code silently
depends on a RADOS-only definition through the same transitive include** — and
those are invisible until you build.

We already saw this from a single one-line change to `rgw_frontend.h`: it broke
`rgw_signal.cc`, `rgw_appmain.cc`, and `rgw_main.cc` (all via
`RGWSyncModuleInstance` in `rgw_main.h`). `rgw_a_standalone` compiles ~40 source
files; each one that currently reaches a RADOS type through the leak will surface
as a fresh "incomplete type" error and require either another guard or another
type promoted to the generic layer.

Consequently this cannot be finished or verified by static inspection. It is an
**iterate-to-convergence** task: rebuild the standalone config, fix the next
incomplete-type error, repeat, until both configurations build and link.

---

## Difficulty assessment

| Piece | Size | Risk / notes |
|-------|------|--------------|
| A. Guard `RadosStore`/reshard/tier sites in `radosgw-admin.cc` | Small (~6 sites) | Low, mechanical. A2 (command-group guards) needs a product call on which commands the standalone tool should expose |
| B1. Guard `rest_filter` + fallback | Small (1 site) | Low |
| B2. Guard `rgw_frontend.h` over-include | Trivial (1 line) | **Triggers the cascade** |
| C. Guard RADOS sync-header includes | Small | Low once A is done |
| D. Resolve the cascade across `rgw_a_standalone` | **Unknown, likely Medium–Large** | Each hidden generic→RADOS dependency = one more guard or a header refactor; only found by building |
| Verify RADOS build unchanged + standalone builds & links | Medium | Must build **both** configs; static analysis is insufficient |

**Overall:** the "visible" fix is roughly a **half-day** change; the cascade in
Part D realistically makes this a **multi-day** effort with build cycles, and it
may reasonably grow into a small header-hygiene refactor (moving the sync-module
public interface, and possibly other multisite types, out of the RADOS driver
into the generic SAL layer). If the answer to "which commands should the
standalone admin even expose?" is "only user/bucket basics," a **cheaper
alternative** to fixing the full file is to build `rgw-standalone-admin` from a
**trimmed source / thin command set** rather than the whole `radosgw-admin.cc`.

## Suggested sequencing

1. Land the `WITH_RADOSGW_STANDALONE_ADMIN=OFF` unblock (done) + open a tracker.
2. Decide scope: full `radosgw-admin.cc` support vs. trimmed standalone command set.
3. If full: apply A + B + C, then iterate D against a standalone build to
   convergence.
4. Add a CI config that actually builds `WITH_RADOSGW_POSIX=ON` /
   `WITH_RADOSGW_STANDALONE=ON` so this can't regress silently again.

## Verification checklist (for whoever implements it)

- [ ] `WITH_RADOSGW_RADOS=ON` build: unchanged, compiles + links (all guards true).
- [ ] Standalone config (`WITH_RADOSGW_POSIX=ON`, `WITH_RADOSGW_STANDALONE=ON`,
      `WITH_RADOSGW_STANDALONE_ADMIN=ON`): `radosgw-admin.cc`, `rgw_a_standalone`,
      `rgw-standalone`, and `rgw-standalone-admin` all compile.
- [ ] `rgw-standalone-admin` **links** (no `RadosZone`/`RadosZoneGroup`/`RadosStore`
      undefined symbols) at both `-O0` and `-O3`.
- [ ] `nm -C bin/rgw-standalone-admin | grep -i rados` is clean.
- [ ] Smoke-test the standalone tool's supported commands against a dbstore/posix backend.
