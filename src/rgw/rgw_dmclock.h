// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab ft=cpp

/*
 * Ceph - scalable distributed file system
 *
 * Copyright (C) 2019 Red Hat, Inc.
 * Copyright (C) 2019 SUSE LLC
 *
 * This is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License version 2.1, as published by the Free Software
 * Foundation.  See file COPYING.
 *
 */

#pragma once

#include <cstdint>
#include <string_view>

#include "dmclock/src/dmclock_server.h"

namespace rgw::dmclock {
// TODO: implement read vs write
enum class op_class {
                      admin, //< /admin apis
                      auth, //< swift auth, sts
                      data, //< PutObj, GetObj
                      metadata, //< bucket operations, object metadata
                      count
};

/// Derive a dmClock tenant key from a stable identity string, such as an S3
/// user or account id.  A collision only puts two tenants in the same queue,
/// which is exactly the behaviour we have without per-tenant scheduling, so a
/// 64-bit hash is enough and no coordination is needed to assign these.
constexpr uint64_t tenant_id_from(std::string_view identity)
{
  // FNV-1a, spelled out so the mapping stays stable across toolchains.
  uint64_t h = 0xcbf29ce484222325ull;
  for (unsigned char c : identity) {
    h = (h ^ c) * 0x100000001b3ull;
  }
  // Reserve 0 for "no particular tenant" (see client_id below).
  return h ? h : 1;
}

/// Identifies a dmClock queue.  Historically this was just the op class, which
/// meant every S3 tenant shared one `data` queue and a single aggressive client
/// could starve all the others.  Carrying a tenant id alongside the op class
/// gives each tenant its own queue and its own reservation.
///
/// A zero tenant_id means "daemon-level", reproducing the old behaviour
/// exactly; that is what is used when per-tenant scheduling is disabled.
struct client_id {
  uint64_t tenant_id = 0;
  op_class op = op_class::metadata;

  client_id() = default;

  /// implicit, so existing daemon-level call sites read unchanged
  constexpr client_id(op_class op) noexcept : tenant_id(0), op(op) {}

  constexpr client_id(uint64_t tenant_id, op_class op) noexcept
    : tenant_id(tenant_id), op(op) {}

  /// ordering only: the dmClock queue keys its client map with std::map
  auto operator<=>(const client_id&) const = default;
};

// TODO move these to dmclock/types or so in submodule
using crimson::dmclock::Cost;
using crimson::dmclock::ClientInfo;

enum class scheduler_t {
                        none,
                        throttler,
                        dmclock
};

inline scheduler_t get_scheduler_t(CephContext* const cct)
{
  const auto scheduler_type = cct->_conf.get_val<std::string>("rgw_scheduler_type");
  if (scheduler_type == "dmclock")
    return scheduler_t::dmclock;
  else if (scheduler_type == "throttler")
    return scheduler_t::throttler;
  else
    return scheduler_t::none;
}

} // namespace rgw::dmclock
