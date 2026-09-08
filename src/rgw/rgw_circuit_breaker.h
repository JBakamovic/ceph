// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab ft=cpp

#pragma once

#include <chrono>
#include <mutex>
#include <unordered_map>
#include <string>

#include "include/rados/librados.hpp"
#include "osd/osd_types.h"
#include "common/ceph_context.h"
#include "common/dout.h"
#include "rgw_common.h"
#include "driver/rados/rgw_sal_rados.h"

class RGWCircuitBreaker {
  static constexpr auto dout_subsys = ceph_subsys_rgw;
public:
  enum class State {
    Closed,
    Open,
    HalfOpen
  };

  struct PGStats {
    State state{State::Closed};
    uint64_t consecutive_slow{0};
    uint64_t total_shed{0};
    uint64_t total_requests{0};
    int32_t inflight{0};
    std::chrono::steady_clock::time_point trip_time{};
    std::chrono::steady_clock::time_point oldest_inflight_start{};
    bool canary_in_flight{false};
  };

private:
  CephContext* cct{nullptr};
  mutable std::mutex stats_mutex;
  std::unordered_map<pg_t, PGStats> pg_states;

  mutable std::mutex pool_mutex;
  std::unordered_map<std::string, librados::IoCtx> pool_ioctx_cache;

public:
  explicit RGWCircuitBreaker(CephContext* cct) : cct(cct) {}

  bool is_enabled() const {
    if (!cct) return false;
    return cct->_conf.get_val<bool>("rgw_circuit_breaker_enabled");
  }

  uint64_t get_failure_threshold() const {
    return cct->_conf.get_val<uint64_t>("rgw_circuit_breaker_failure_threshold");
  }

  uint64_t get_latency_threshold_ms() const {
    return cct->_conf.get_val<uint64_t>("rgw_circuit_breaker_latency_threshold_ms");
  }

  uint64_t get_open_duration_secs() const {
    return cct->_conf.get_val<uint64_t>("rgw_circuit_breaker_open_duration_secs");
  }

  uint64_t get_max_inflight_per_pg() const {
    return cct->_conf.get_val<uint64_t>("rgw_circuit_breaker_max_inflight_per_pg");
  }

  bool resolve_pg(req_state* s, pg_t* target_pg) {
    if (!s || !s->penv.driver) return false;
    auto rados_store = dynamic_cast<rgw::sal::RadosStore*>(s->penv.driver);
    if (!rados_store) return false;

    rgw_obj obj;
    if (s->object && !s->object->empty()) {
      obj = s->object->get_obj();
    }
    if (obj.empty() && s->bucket && !s->object_key.name.empty()) {
      obj.init(s->bucket->get_key(), s->object_key);
    }
    if (obj.empty() || !s->bucket) {
      return false;
    }
    if (obj.bucket.name.empty()) {
      obj.bucket = s->bucket->get_key();
    }

    rgw_raw_obj raw_obj;
    bool ok = rados_store->getRados()->obj_to_raw(s->bucket->get_info().placement_rule, obj, &raw_obj);
    if (!ok || raw_obj.empty() || raw_obj.pool.name.empty() || raw_obj.oid.empty()) {
      return false;
    }

    librados::Rados* rados = rados_store->getRados()->get_rados_handle();
    if (!rados) return false;

    std::lock_guard l(pool_mutex);
    auto it = pool_ioctx_cache.find(raw_obj.pool.name);
    if (it == pool_ioctx_cache.end()) {
      librados::IoCtx ioctx;
      int r = rados->ioctx_create(raw_obj.pool.name.c_str(), ioctx);
      if (r < 0) return false;
      it = pool_ioctx_cache.emplace(raw_obj.pool.name, std::move(ioctx)).first;
    }

    uint32_t pos = 0;
    int r = it->second.get_object_pg_hash_position2(raw_obj.oid, &pos);
    if (r == 0) {
      *target_pg = pg_t(pos, it->second.get_id());
      return true;
    }

    return false;
  }

  // Returns true if request should be shed immediately (503 SlowDown)
  bool should_shed(const pg_t& pg) {
    if (!is_enabled()) return false;

    std::lock_guard l(stats_mutex);
    auto& stats = pg_states[pg];
    auto now = std::chrono::steady_clock::now();

    stats.total_requests++;

    if (stats.state == State::Open) {
      auto open_secs = get_open_duration_secs();
      if (now - stats.trip_time >= std::chrono::seconds(open_secs)) {
        // Transition to HalfOpen for canary probe
        stats.state = State::HalfOpen;
        stats.canary_in_flight = true;
        stats.inflight++;
        ldout(cct, 1) << "Circuit breaker for PG " << pg
                      << " transitioning from OPEN -> HALF-OPEN; allowing canary probe" << dendl;
        return false;
      }
      stats.total_shed++;
      return true; // Shed
    }

    if (stats.state == State::HalfOpen) {
      if (!stats.canary_in_flight) {
        stats.canary_in_flight = true;
        stats.inflight++;
        return false; // Allow canary probe
      }
      stats.total_shed++;
      return true; // Other requests continue to shed in HalfOpen
    }

    // State is Closed
    if (stats.inflight > 0 && stats.oldest_inflight_start != std::chrono::steady_clock::time_point{}) {
      auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
          now - stats.oldest_inflight_start).count();
      if (elapsed_ms >= static_cast<int64_t>(get_latency_threshold_ms())) {
        stats.consecutive_slow++;
        ldout(cct, 1) << "Circuit breaker detected in-flight stall on PG " << pg
                      << " (in-flight duration: " << elapsed_ms << "ms >= "
                      << get_latency_threshold_ms() << "ms, consecutive: "
                      << stats.consecutive_slow << "/" << get_failure_threshold() << ")" << dendl;
        if (stats.consecutive_slow >= get_failure_threshold()) {
          stats.state = State::Open;
          stats.trip_time = now;
          ldout(cct, 0) << "CIRCUIT BREAKER TRIPPED to OPEN for PG " << pg
                        << " due to in-flight stall!" << dendl;
        }
        stats.total_shed++;
        return true;
      }
    }

    auto max_inflight = get_max_inflight_per_pg();
    if (max_inflight > 0 && stats.inflight >= static_cast<int32_t>(max_inflight)) {
      ldout(cct, 1) << "Circuit breaker shed: PG " << pg
                    << " reached max in-flight requests (" << stats.inflight
                    << " >= " << max_inflight << ")" << dendl;
      stats.total_shed++;
      return true;
    }

    if (stats.inflight == 0) {
      stats.oldest_inflight_start = now;
    }
    stats.inflight++;
    return false;
  }

  void record_outcome(const pg_t& pg, uint64_t duration_ms, int op_ret) {
    if (!is_enabled()) return;

    std::lock_guard l(stats_mutex);
    auto it = pg_states.find(pg);
    if (it == pg_states.end()) return;

    auto& stats = it->second;
    if (stats.inflight > 0) {
      stats.inflight--;
    }
    if (stats.inflight == 0) {
      stats.oldest_inflight_start = {};
    }

    auto lat_thresh = get_latency_threshold_ms();
    auto fail_thresh = get_failure_threshold();

    bool is_failure = false;
    // Slow request is considered a failure
    if (duration_ms >= lat_thresh) {
      is_failure = true;
    } else if (op_ret < 0) {
      // Negative ret codes that indicate storage / OSD failure or timeouts
      // Exclude normal application errors like -ENOENT (404), -EACCES/-EPERM (403), etc.
      if (op_ret == -ETIMEDOUT || op_ret == -EIO || op_ret == -EBUSY ||
          op_ret == -EAGAIN || op_ret == -ECANCELED || op_ret == -1) {
        is_failure = true;
      }
    }

    if (stats.state == State::HalfOpen) {
      stats.canary_in_flight = false;
      if (is_failure) {
        stats.state = State::Open;
        stats.trip_time = std::chrono::steady_clock::now();
        ldout(cct, 1) << "Circuit breaker canary probe FAILED for PG " << pg
                      << " (duration: " << duration_ms << "ms, ret: " << op_ret
                      << "); re-opening circuit breaker" << dendl;
      } else {
        stats.state = State::Closed;
        stats.consecutive_slow = 0;
        ldout(cct, 1) << "Circuit breaker canary probe SUCCEEDED for PG " << pg
                      << " (duration: " << duration_ms << "ms, ret: " << op_ret
                      << "); recovering PG to CLOSED" << dendl;
      }
      return;
    }

    if (stats.state == State::Closed) {
      if (is_failure) {
        stats.consecutive_slow++;
        ldout(cct, 1) << "Circuit breaker recorded slow/failed request for PG " << pg
                      << " (duration: " << duration_ms << "ms, ret: " << op_ret
                      << ", consecutive: " << stats.consecutive_slow
                      << "/" << fail_thresh << ")" << dendl;
        if (stats.consecutive_slow >= fail_thresh) {
          stats.state = State::Open;
          stats.trip_time = std::chrono::steady_clock::now();
          ldout(cct, 0) << "CIRCUIT BREAKER TRIPPED to OPEN for PG " << pg
                        << " after " << stats.consecutive_slow
                        << " consecutive slow/failed requests!" << dendl;
        }
      } else {
        if (stats.consecutive_slow > 0) {
          stats.consecutive_slow--;
        }
      }
    }
  }

  class Guard {
    RGWCircuitBreaker* cb{nullptr};
    pg_t pg;
    bool active{false};
    std::chrono::steady_clock::time_point start_time;

  public:
    Guard(RGWCircuitBreaker* cb, const pg_t& pg, bool active)
      : cb(cb), pg(pg), active(active), start_time(std::chrono::steady_clock::now()) {}

    ~Guard() {
      if (active && cb) {
        auto d = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - start_time).count();
        cb->record_outcome(pg, d, -1);
      }
    }

    void finish(int op_ret) {
      if (active && cb) {
        auto d = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - start_time).count();
        cb->record_outcome(pg, d, op_ret);
        active = false;
      }
    }
  };
};
