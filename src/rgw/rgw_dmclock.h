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

#include <atomic>
#include <chrono>
#include <cmath>
#include <algorithm>
#include <memory>
#include <functional>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/io_context.hpp>

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

/// Lock-free telemetry tracker for storage backend I/O completion latencies.
class LatencyTelemetry {
  std::atomic<double> recent_ema_ms{5.0};
  std::atomic<uint64_t> sample_count{0};
public:
  void record_latency(double latency_ms) noexcept {
    double old_ema = recent_ema_ms.load(std::memory_order_relaxed);
    while (!recent_ema_ms.compare_exchange_weak(
        old_ema, 0.85 * old_ema + 0.15 * latency_ms,
        std::memory_order_relaxed, std::memory_order_relaxed)) {
    }
    sample_count.fetch_add(1, std::memory_order_relaxed);
  }

  double get_ema_latency_ms() const noexcept {
    return recent_ema_ms.load(std::memory_order_relaxed);
  }

  uint64_t get_sample_count() const noexcept {
    return sample_count.load(std::memory_order_relaxed);
  }
};

/// Asynchronous AIMD feedback capacity controller running on the frontend io_context.
class AdaptiveCapacityController {
public:
  using update_callback_t = std::function<void(double)>;

  AdaptiveCapacityController(
      boost::asio::io_context& ioc,
      std::shared_ptr<LatencyTelemetry> telemetry,
      update_callback_t update_cb,
      double target_latency_ms = 5.0,
      uint32_t sample_interval_ms = 50)
    : timer(ioc),
      telemetry(std::move(telemetry)),
      update_cb(std::move(update_cb)),
      target_lat_ms(target_latency_ms),
      sample_interval_ms(sample_interval_ms) {}

  void start() {
    running = true;
    schedule_next();
  }

  void stop() {
    running = false;
    timer.cancel();
  }

private:
  void schedule_next() {
    if (!running) return;
    timer.expires_after(std::chrono::milliseconds(sample_interval_ms));
    timer.async_wait([this](const boost::system::error_code& ec) {
      if (ec || !running) return;
      step();
      schedule_next();
    });
  }

  void step() {
    if (!telemetry || !update_cb) return;
    double measured_lat = telemetry->get_ema_latency_ms();
    if (measured_lat <= target_lat_ms * 1.10) {
      // Additive Increase up to 1.5x nominal
      current_scale = std::min(1.5, current_scale + 0.05);
    } else if (measured_lat > target_lat_ms * 1.25) {
      // Multiplicative Decrease based on overshoot ratio
      double ratio = target_lat_ms / measured_lat;
      double dec = std::clamp(ratio, 0.35, 0.85);
      current_scale = std::max(0.15, current_scale * dec);
    }
    update_cb(current_scale);
  }

  boost::asio::steady_timer timer;
  std::shared_ptr<LatencyTelemetry> telemetry;
  update_callback_t update_cb;
  double target_lat_ms;
  uint32_t sample_interval_ms;
  double current_scale = 1.0;
  bool running = false;
};

} // namespace rgw::dmclock
