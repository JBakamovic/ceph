# PR draft: rgw: fix rgw-standalone-admin build (no-RADOS guards)

## Commit message

```
rgw: guard RADOS-only code paths for the no-RADOS standalone build

Since the rgw-standalone-admin target landed (pr#68291), the default
build (WITH_RADOSGW_POSIX=ON implies WITH_RADOSGW_STANDALONE=ON) fails
to link:

  undefined reference to `vtable for rgw::sal::RadosZone'
  undefined reference to `vtable for rgw::sal::RadosZoneGroup'

radosgw-admin.cc (and several sources in rgw_a_standalone) use the
concrete rgw::sal::RadosStore outside WITH_RADOSGW_RADOS guards. These
uses only compile in the -UWITH_RADOSGW_RADOS configuration because
rgw_sal_rados.h leaks in transitively (rgw_sync.h, rgw_data_sync.h,
rgw_frontend.h). The link then fails because the RADOS driver objects
that emit the vtables and out-of-line key functions for RadosZone /
RadosZoneGroup are deliberately not part of the standalone libraries.

Fix both sides of the problem:

* guard the concrete-RADOS uses in radosgw-admin.cc: get_remote_conn(),
  the remote period commit lookup (which now returns -ENOTSUP with a
  hint to use --url), the reshard max-shards check (its callers were
  already guarded) and the sync-module tier-type validation (--tier-type
  now returns EINVAL without the RADOS backend)
* guard the transitive routes to rgw_sal_rados.h: the rgw_sync.h /
  rgw_data_sync.h includes in radosgw-admin.cc, the rgw_sal_rados.h
  include in rgw_frontend.h (an over-include: the header uses no RADOS
  type), and the rgw_rest_log.h include in rgw_appmain.cc (unused)
* provide a no-RADOS fallback for rest_filter() in rgw_main.h: sync
  modules (and the complete RGWSyncModuleInstance type) are RADOS-only,
  and the non-RADOS stores return a null sync module anyway
* guard the RADOS-only bodies of the metadata and zone-config admin REST
  ops (-ENOTSUP). These handlers are only registered by
  RadosStore::register_admin_apis(), so they are unreachable in the
  standalone daemon; the guards are defensive
* guard the RadosLuaManager reload block in rgw_realm_reloader.cc; it
  was already unreachable at runtime behind get_name() == "rados"
* include common/errno.h directly in driver/rados/rgw_bucket.cc, which
  previously received cpp_strerror() transitively via rgw_sal_rados.h

All guards evaluate true when WITH_RADOSGW_RADOS is defined, so the
regular RADOS build preprocesses to identical code.

Note that before this change, the unguarded static_cast<RadosStore*>
paths would have been undefined behavior at runtime if reached with a
non-RADOS driver; they now fail cleanly with an error message.

Fixes: https://tracker.ceph.com/issues/XXXXX
Signed-off-by: YOUR NAME <jusufadis.bakamovic@clyso.com>
```

(Adjust the name in Signed-off-by; create the tracker issue and fill in
the number. If preferred, this splits naturally into two commits:
"guard RADOS-only code in radosgw-admin.cc" and "guard RADOS-only code
in rgw frontend/REST sources".)

## PR title

`rgw: fix rgw-standalone-admin link failure in the default build`

## PR description

```
main fails to link rgw-standalone-admin in the default configuration
(WITH_RADOSGW_POSIX=ON -> WITH_RADOSGW_STANDALONE=ON) since #68291:

  /usr/bin/ld: radosgw-admin.cc.o: undefined reference to
      `vtable for rgw::sal::RadosZone'
  /usr/bin/ld: radosgw-admin.cc.o: undefined reference to
      `vtable for rgw::sal::RadosZoneGroup'

Root cause: radosgw-admin.cc and several rgw_a_standalone sources use
the concrete RadosStore outside WITH_RADOSGW_RADOS guards. They compile
in the no-RADOS configuration only because rgw_sal_rados.h leaks in
through transitive includes; the link then fails because the RADOS
driver objects that define those classes' vtables are not in the
standalone libraries.

This PR guards the RADOS-only code paths and closes the transitive
include routes so the standalone TUs no longer see the RADOS SAL types.
All new guards evaluate true under WITH_RADOSGW_RADOS, so the regular
build is unchanged (preprocesses identically).

Behavior notes for the standalone binaries (reviewers please confirm
these match the intent of #68291):
- radosgw-admin period commit with --remote (or implicit master-zone
  remote) returns -ENOTSUP suggesting --url; the URL path still works
- --tier-type on zone/zonegroup create/modify returns EINVAL (sync
  modules are RADOS-only)
- the /admin/metadata, /admin/log and /admin/config REST APIs are only
  registered by the RADOS driver, so nothing changes for the standalone
  daemon; their op bodies additionally return -ENOTSUP defensively
- rest_filter() returns the unfiltered manager without RADOS, matching
  the previous runtime behavior (non-RADOS stores return a null sync
  module)

Runtime safety: previously, the unguarded static_cast<RadosStore*>(driver)
call sites would have been undefined behavior if ever executed with a
posix/dbstore driver. They now fail with clean error messages instead.

Why CI never caught this (root-cause analysis, empirically verified):

The failing references are produced by GCC's *speculative devirtualization*
pass. In the no-RADOS TU the leaked rgw_sal_rados.h makes RadosZone /
RadosZoneGroup the only visible derived classes of the abstract SAL zone
types, so when e.g. resolve_zone_id()'s unique_ptr<Zone> is destroyed, GCC
speculates the virtual destructor target and emits a comparison against
their vtables — which no object in the standalone link defines. Verified by
preprocessing the failing TU to a self-contained .ii and A/B-compiling the
identical bytes:

  * GCC 12/13 (jammy, noble, centos9 gcc-toolset-13): never speculates this
    pattern -> builds pass at any -O level
  * GCC 14.2.1 and 14.3.1, -O0/-Og: pass does not run -> builds pass
    (true Debug builds; note the "debug" CI flavor compiles with an
    RPM-injected -O2 followed by Ceph's -Og, and the last -O wins)
  * GCC 14.2.1 and 14.3.1, -O2/-O3 with -flto=auto (all el10/Fedora RPM
    builds — distro optflags inject LTO): at LTO link time the compiler has
    whole-program visibility, sees the vtables exist nowhere in the link,
    and declines the speculation -> builds pass
  * GCC 14.2.1 and 14.3.1, -O2/-O3 without LTO (any local developer build
    on a GCC 14 distro, e.g. do_cmake.sh on Fedora 40+ / EL10): speculative
    devirtualization emits the vtable references per-TU -> LINK FAILURE

So every optimized upstream CI build is either on GCC <= 13 or LTO-shielded;
only non-LTO developer builds on GCC 14 platforms expose the latent header
leak. The include guards fix it at the source: with the concrete types not
visible, there is nothing to speculate to, at any optimization level, with
or without LTO, on any compiler.

Follow-up suggestions:
* add a CI build of the standalone configuration with GCC >= 14 and no LTO
  (i.e. a plain do_cmake.sh-style build) so this class of failure cannot
  land silently again
* consider filing a GCC report: speculative devirtualization emitting a
  reference to the vtable of a class never instantiated in the program is
  a known-gray-zone behavior; the reduced .ii reproduces it on 14.2.1 and
  14.3.1 and it disappears with -fno-devirtualize-speculatively
```

---

## Runtime-safety audit (why the no-RADOS paths shouldn't crash)

Every `#else` branch introduced by this change, with reachability and
prior behavior:

**commit_period --remote (radosgw-admin.cc).** Reachable in standalone via
`period commit`/`period update --commit` when a remote must be contacted by
zone id. Previously this would static_cast a posix/dbstore driver to
RadosStore and dereference — undefined behavior. Now returns -ENOTSUP with
a message pointing at --url. Callers (`update_period`, the OPT::PERIOD_COMMIT
handler) treat any negative return as a normal error: print + exit. No crash.

**--tier-type validation (3 sites).** Reachable via zone/zonegroup
create/modify with --tier-type. Previously UB (same bad cast). Now EINVAL +
log message; handlers return normally.

**check_reshard_bucket_params max-shards check.** Both callers
(OPT::BUCKET_RESHARD, OPT::RESHARD_ADD) are inside existing
WITH_RADOSGW_RADOS guards, as are the reshard commands themselves — the
function is dead code in standalone. Skipping the RADOS-specific
max-shards validation has no reachable effect.

**rest_filter() fallback.** The non-RADOS stores return a null
RGWSyncModuleInstanceRef (posix: `return sync_module;`, default-constructed),
so the old code always took the `return orig` branch at runtime. The
fallback returns orig unconditionally — identical observable behavior.

**metadata/zone-config REST op bodies.** RGWRESTMgr_Metadata/_Log/_Config
are registered in exactly one place: RadosStore::register_admin_apis()
(rgw_sal_rados.cc:2576-2579). POSIXDriver::register_admin_apis() is `{}`.
The standalone daemon therefore never routes requests to these ops; a
client hitting /admin/metadata gets the same not-found handling as before.
The -ENOTSUP bodies are unreachable, defensive.

**RadosLuaManager reload block.** Was already behind
`if (env.driver->get_name() == "rados")` — never true in standalone. Same
dead code, now also not compiled.

**Summary:** no new-guard path removes behavior that standalone users
could previously exercise successfully; the paths either match prior
runtime behavior exactly, were unreachable, or convert undefined behavior
into clean errors. The change is strictly safer at runtime than the
pre-patch code.

## Suggested verification steps (to run locally)

1. RADOS build unchanged: build the regular config; optionally verify at
   the object level that guarded files are identical, e.g.
   `objdump -d .../radosgw-admin.cc.o | md5sum` before/after the patch in
   a WITH_RADOSGW_RADOS build (all guards are true there).
2. Standalone build (done): default cmake options; `ninja
   rgw-standalone-admin rgw-standalone` — compiles and links. Also try a
   RelWithDebInfo/-O0 build to cover both codegen modes.
3. Standalone admin smoke test against the posix backend, e.g.:
   `bin/rgw-standalone-admin user create --uid=test --display-name=Test`,
   `user info --uid=test`, `bucket list`. Expect normal operation.
4. Negative-path checks (the new clean errors):
   `bin/rgw-standalone-admin period commit --remote=zid ...` → the
   -ENOTSUP message; `zonegroup create ... --tier-type=cloud` → EINVAL
   message. Expect error text, exit code, no crash.
5. Standalone daemon: start `rgw-standalone` with the posix backend, then
   `curl .../admin/metadata` with admin creds → expect the same not-found
   behavior as before the patch (handler not registered).
6. RADOS regression: vstart cluster + `radosgw-admin` sanity (user/bucket
   CRUD, `period update --commit`, a reshard) and/or the qa rgw suites.
