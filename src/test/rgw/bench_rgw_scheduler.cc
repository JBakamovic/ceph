// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab ft=cpp

/*
 * Ceph - scalable distributed file system
 *
 * Copyright (C) 2026 Ceph contributors
 *
 * This is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License version 2.1, as published by the Free Software
 * Foundation. See file COPYING.
 */

#include <iostream>
#include <iomanip>
#include <fstream>
#include <sstream>
#include <vector>
#include <string>
#include <memory>
#include <chrono>
#include <atomic>
#include <mutex>
#include <random>
#include <algorithm>
#include <cmath>
#include <numeric>
#include <map>
#include <optional>

#include <boost/asio/io_context.hpp>
#include <boost/asio/executor_work_guard.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/spawn.hpp>
#include <boost/asio/detached.hpp>
#include <boost/program_options.hpp>

#include "json_spirit/json_spirit.h"
#include "common/ceph_context.h"
#include "common/config.h"
#include "common/async/completion.h"
#include "global/global_context.h"
#include "rgw_dmclock.h"
#include "rgw_dmclock_scheduler.h"
#include "rgw_dmclock_async_scheduler.h"
#include "rgw_dmclock_sync_scheduler.h"

using namespace std::chrono_literals;
namespace po = boost::program_options;
namespace dmc = rgw::dmclock;

// ============================================================================
// Operation Types & Cost Modeling
// ============================================================================
enum class OpType {
  ProbeHealth,   // HEAD /health - Lightweight 0-byte ping
  GetSmall,      // GET 4KB - Low-latency interactive read
  PutSmall,      // PUT 4KB - Low-latency interactive write
  GetLarge,      // GET 16MB - Bandwidth intensive read
  PutLarge,      // PUT 16MB - Bandwidth & I/O intensive write
  ListBucket     // LIST 1000 keys - CPU & Bucket Index lock intensive
};

inline const char* op_type_str(OpType op) {
  switch (op) {
    case OpType::ProbeHealth: return "ProbeHealth";
    case OpType::GetSmall:    return "GetSmall(4KB)";
    case OpType::PutSmall:    return "PutSmall(4KB)";
    case OpType::GetLarge:    return "GetLarge(16MB)";
    case OpType::PutLarge:    return "PutLarge(16MB)";
    case OpType::ListBucket:  return "ListBucket";
  }
  return "Unknown";
}

inline const char* op_type_name(OpType op) {
  switch (op) {
    case OpType::ProbeHealth: return "ProbeHealth";
    case OpType::GetSmall:    return "GetSmall";
    case OpType::PutSmall:    return "PutSmall";
    case OpType::GetLarge:    return "GetLarge";
    case OpType::PutLarge:    return "PutLarge";
    case OpType::ListBucket:  return "ListBucket";
  }
  return "Unknown";
}

inline std::optional<OpType> parse_op_type(const std::string& name) {
  if (name == "ProbeHealth") return OpType::ProbeHealth;
  if (name == "GetSmall")    return OpType::GetSmall;
  if (name == "PutSmall")    return OpType::PutSmall;
  if (name == "GetLarge")    return OpType::GetLarge;
  if (name == "PutLarge")    return OpType::PutLarge;
  if (name == "ListBucket")  return OpType::ListBucket;
  return std::nullopt;
}

inline const char* op_class_to_str(dmc::op_class c) {
  switch (c) {
    case dmc::op_class::admin:    return "admin";
    case dmc::op_class::auth:     return "auth";
    case dmc::op_class::data:     return "data";
    case dmc::op_class::metadata: return "metadata";
    default: return "data";
  }
}

inline std::optional<dmc::op_class> parse_op_class(const std::string& str) {
  if (str == "admin")    return dmc::op_class::admin;
  if (str == "auth")     return dmc::op_class::auth;
  if (str == "data")     return dmc::op_class::data;
  if (str == "metadata") return dmc::op_class::metadata;
  return std::nullopt;
}

inline dmc::op_class default_op_to_dmc_client(OpType op) {
  switch (op) {
    case OpType::ProbeHealth: return dmc::op_class::admin;
    case OpType::GetSmall:
    case OpType::PutSmall:
    case OpType::GetLarge:
    case OpType::PutLarge:    return dmc::op_class::data;
    case OpType::ListBucket:  return dmc::op_class::metadata;
  }
  return dmc::op_class::data;
}

// ============================================================================
// Pacing Distribution Models
// ============================================================================
enum class PacingDistribution {
  Constant,
  Uniform,
  Exponential // Poisson process inter-arrival times
};

inline const char* pacing_dist_to_str(PacingDistribution dist) {
  switch (dist) {
    case PacingDistribution::Constant:    return "constant";
    case PacingDistribution::Uniform:     return "uniform";
    case PacingDistribution::Exponential: return "exponential";
  }
  return "constant";
}

inline PacingDistribution parse_pacing_dist(const std::string& str) {
  if (str == "uniform")     return PacingDistribution::Uniform;
  if (str == "exponential" || str == "poisson") return PacingDistribution::Exponential;
  return PacingDistribution::Constant;
}

// ============================================================================
// Configuration Data Structures
// ============================================================================
struct OpModelConfig {
  std::map<OpType, dmc::Cost> costs = {
    {OpType::ProbeHealth, 1},
    {OpType::GetSmall,    2},
    {OpType::PutSmall,    3},
    {OpType::GetLarge,    16},
    {OpType::PutLarge,    24},
    {OpType::ListBucket,  20}
  };

  std::map<OpType, double> latency_multipliers = {
    {OpType::ProbeHealth, 0.5},
    {OpType::GetSmall,    1.0},
    {OpType::PutSmall,    1.5},
    {OpType::GetLarge,    4.0},
    {OpType::PutLarge,    6.0},
    {OpType::ListBucket,  5.0}
  };

  dmc::Cost get_cost(OpType op) const {
    auto it = costs.find(op);
    return (it != costs.end()) ? it->second : 1;
  }

  double get_latency_mult(OpType op) const {
    auto it = latency_multipliers.find(op);
    return (it != latency_multipliers.end()) ? it->second : 1.0;
  }
};

struct DmClockProfile {
  double reservation = 10.0;
  double weight = 50.0;
  double limit = 50.0;
};

struct BackendConfig {
  double cluster_base_latency_ms = 5.0;
  int cluster_capacity = 64;           // Saturation knee threshold
  double congestion_factor = 3.0;      // Exponent multiplier under saturation
  double spike_amplitude_ms = 0.0;     // Injected latency spike
  double spike_interval_s = 5.0;
  double spike_duration_s = 1.0;
  double jitter_pct = 0.10;            // +/- 10% random noise
};

struct TenantConfig {
  std::string name;
  std::string role_desc = "custom";
  int workers = 10;
  dmc::op_class client_id = dmc::op_class::data;
  double pacing_ms = 0.0;
  PacingDistribution pacing_dist = PacingDistribution::Constant;
  std::vector<std::pair<OpType, int>> op_weights; // OpType -> weight
  DmClockProfile dmclock_profile{10.0, 50.0, 50.0};
};

struct BenchConfig {
  std::string scheduler_type = "throttler"; // "throttler", "dmclock", "dmclock_coarse", "dmclock_fine", "none"
  int64_t max_concurrent_requests = 128;
  int runtime_seconds = 10;
  int thread_count = 8;
  bool allow_retries = false;
  int max_retries = 3;

  // Adaptive closed-loop capacity tuning options
  bool adaptive_tuning = false;
  double adaptive_target_latency_ms = 5.0;
  int adaptive_sample_interval_ms = 100;

  BackendConfig backend;
  std::map<dmc::op_class, DmClockProfile> dmclock_profiles = {
    {dmc::op_class::admin,    {10.0, 100.0, 50.0}},
    {dmc::op_class::auth,     { 5.0,  50.0, 25.0}},
    {dmc::op_class::data,     {20.0, 100.0, 100.0}},
    {dmc::op_class::metadata, {10.0,  50.0, 50.0}}
  };
  OpModelConfig op_model;
  std::vector<TenantConfig> tenants;

  // Output export options
  std::string export_json_path;
  std::string export_csv_path;
};

// Create the default baseline multi-tenant scenario
inline std::vector<TenantConfig> default_tenants(int bully_workers = 60,
                                                 int interactive_workers = 15,
                                                 int probe_interval_ms = 250) {
  std::vector<TenantConfig> tenants;

  // Tenant A: Bully Aggressor
  TenantConfig bully;
  bully.name = "Tenant A (Bully)";
  bully.role_desc = "Bulk Aggressor";
  bully.workers = bully_workers;
  bully.client_id = dmc::op_class::data;
  bully.pacing_ms = 0.0;
  bully.pacing_dist = PacingDistribution::Constant;
  bully.op_weights = {
    {OpType::PutLarge, 66},
    {OpType::ListBucket, 34}
  };
  bully.dmclock_profile = {20.0, 100.0, 100.0};
  tenants.push_back(bully);

  // Tenant B: Interactive Victim
  TenantConfig interactive;
  interactive.name = "Tenant B (Interactive)";
  interactive.role_desc = "Latency Sensitive";
  interactive.workers = interactive_workers;
  interactive.client_id = dmc::op_class::data;
  interactive.pacing_ms = 2.0;
  interactive.pacing_dist = PacingDistribution::Constant;
  interactive.op_weights = {
    {OpType::GetSmall, 50},
    {OpType::PutSmall, 50}
  };
  interactive.dmclock_profile = {50.0, 100.0, 100.0};
  tenants.push_back(interactive);

  // Tenant C: Health Probe
  TenantConfig probe;
  probe.name = "Tenant C (Health Probe)";
  probe.role_desc = "Health Monitor";
  probe.workers = 1;
  probe.client_id = dmc::op_class::admin;
  probe.pacing_ms = static_cast<double>(probe_interval_ms);
  probe.pacing_dist = PacingDistribution::Constant;
  probe.op_weights = {
    {OpType::ProbeHealth, 100}
  };
  probe.dmclock_profile = {10.0, 100.0, 50.0};
  tenants.push_back(probe);

  return tenants;
}

// ============================================================================
// JSON Serialization & Parsing (via json_spirit)
// ============================================================================
inline std::string dump_config_to_json(const BenchConfig& config) {
  json_spirit::mObject root;

  // Benchmark global
  json_spirit::mObject bench_obj;
  bench_obj["scheduler"] = config.scheduler_type;
  bench_obj["max_concurrent_requests"] = static_cast<int64_t>(config.max_concurrent_requests);
  bench_obj["runtime_seconds"] = config.runtime_seconds;
  bench_obj["thread_count"] = config.thread_count;
  bench_obj["allow_retries"] = config.allow_retries;
  bench_obj["max_retries"] = config.max_retries;
  bench_obj["adaptive_tuning"] = config.adaptive_tuning;
  bench_obj["adaptive_target_latency_ms"] = config.adaptive_target_latency_ms;
  bench_obj["adaptive_sample_interval_ms"] = config.adaptive_sample_interval_ms;
  root["benchmark"] = bench_obj;

  // Backend
  json_spirit::mObject backend_obj;
  backend_obj["cluster_base_latency_ms"] = config.backend.cluster_base_latency_ms;
  backend_obj["cluster_capacity"] = config.backend.cluster_capacity;
  backend_obj["congestion_factor"] = config.backend.congestion_factor;
  backend_obj["spike_amplitude_ms"] = config.backend.spike_amplitude_ms;
  backend_obj["spike_interval_s"] = config.backend.spike_interval_s;
  backend_obj["spike_duration_s"] = config.backend.spike_duration_s;
  backend_obj["jitter_pct"] = config.backend.jitter_pct;
  root["backend"] = backend_obj;

  // dmClock profiles
  json_spirit::mObject dmclock_obj;
  for (const auto& [cid, prof] : config.dmclock_profiles) {
    json_spirit::mObject p;
    p["res"] = prof.reservation;
    p["wgt"] = prof.weight;
    p["lim"] = prof.limit;
    dmclock_obj[op_class_to_str(cid)] = p;
  }
  root["dmclock"] = dmclock_obj;

  // Op costs
  json_spirit::mObject costs_obj;
  for (const auto& [op, cost] : config.op_model.costs) {
    costs_obj[op_type_name(op)] = static_cast<int>(cost);
  }
  root["op_costs"] = costs_obj;

  // Op latency multipliers
  json_spirit::mObject lat_obj;
  for (const auto& [op, mult] : config.op_model.latency_multipliers) {
    lat_obj[op_type_name(op)] = mult;
  }
  root["op_latency_multipliers"] = lat_obj;

  // Tenants array
  json_spirit::mArray tenants_arr;
  for (const auto& t : config.tenants) {
    json_spirit::mObject t_obj;
    t_obj["name"] = t.name;
    t_obj["role"] = t.role_desc;
    t_obj["workers"] = t.workers;
    t_obj["client_id"] = op_class_to_str(t.client_id);
    t_obj["pacing_ms"] = t.pacing_ms;
    t_obj["pacing_distribution"] = pacing_dist_to_str(t.pacing_dist);

    json_spirit::mObject weights_obj;
    for (const auto& [op, w] : t.op_weights) {
      weights_obj[op_type_name(op)] = w;
    }
    t_obj["op_weights"] = weights_obj;

    json_spirit::mObject dmclock_obj;
    dmclock_obj["res"] = t.dmclock_profile.reservation;
    dmclock_obj["wgt"] = t.dmclock_profile.weight;
    dmclock_obj["lim"] = t.dmclock_profile.limit;
    t_obj["dmclock"] = dmclock_obj;

    tenants_arr.push_back(t_obj);
  }
  root["tenants"] = tenants_arr;

  return json_spirit::write_formatted(root);
}

inline bool load_config_from_json(const std::string& json_str, BenchConfig& config) {
  json_spirit::mValue root_val;
  if (!json_spirit::read(json_str, root_val) || root_val.type() != json_spirit::obj_type) {
    std::cerr << "[-] Error: Failed to parse JSON configuration.\n";
    return false;
  }

  const auto& root = root_val.get_obj();

  // 1. Benchmark global
  if (auto it = root.find("benchmark"); it != root.end() && it->second.type() == json_spirit::obj_type) {
    const auto& b = it->second.get_obj();
    if (auto v = b.find("scheduler"); v != b.end() && v->second.type() == json_spirit::str_type) {
      config.scheduler_type = v->second.get_str();
    }
    if (auto v = b.find("max_concurrent_requests"); v != b.end() && v->second.type() == json_spirit::int_type) {
      config.max_concurrent_requests = v->second.get_int64();
    }
    if (auto v = b.find("runtime_seconds"); v != b.end() && v->second.type() == json_spirit::int_type) {
      config.runtime_seconds = v->second.get_int();
    }
    if (auto v = b.find("thread_count"); v != b.end() && v->second.type() == json_spirit::int_type) {
      config.thread_count = v->second.get_int();
    }
    if (auto v = b.find("allow_retries"); v != b.end() && v->second.type() == json_spirit::bool_type) {
      config.allow_retries = v->second.get_bool();
    }
    if (auto v = b.find("max_retries"); v != b.end() && v->second.type() == json_spirit::int_type) {
      config.max_retries = v->second.get_int();
    }
    if (auto v = b.find("adaptive_tuning"); v != b.end() && v->second.type() == json_spirit::bool_type) {
      config.adaptive_tuning = v->second.get_bool();
    }
    if (auto v = b.find("adaptive_target_latency_ms"); v != b.end()) {
      config.adaptive_target_latency_ms = v->second.get_real();
    }
    if (auto v = b.find("adaptive_sample_interval_ms"); v != b.end() && v->second.type() == json_spirit::int_type) {
      config.adaptive_sample_interval_ms = v->second.get_int();
    }
  }

  // 2. Backend
  if (auto it = root.find("backend"); it != root.end() && it->second.type() == json_spirit::obj_type) {
    const auto& b = it->second.get_obj();
    if (auto v = b.find("cluster_base_latency_ms"); v != b.end()) {
      config.backend.cluster_base_latency_ms = v->second.get_real();
    }
    if (auto v = b.find("cluster_capacity"); v != b.end()) {
      config.backend.cluster_capacity = v->second.get_int();
    }
    if (auto v = b.find("congestion_factor"); v != b.end()) {
      config.backend.congestion_factor = v->second.get_real();
    }
    if (auto v = b.find("spike_amplitude_ms"); v != b.end()) {
      config.backend.spike_amplitude_ms = v->second.get_real();
    }
    if (auto v = b.find("spike_interval_s"); v != b.end()) {
      config.backend.spike_interval_s = v->second.get_real();
    }
    if (auto v = b.find("spike_duration_s"); v != b.end()) {
      config.backend.spike_duration_s = v->second.get_real();
    }
    if (auto v = b.find("jitter_pct"); v != b.end()) {
      config.backend.jitter_pct = v->second.get_real();
    }
  }

  // 3. dmClock profiles
  if (auto it = root.find("dmclock"); it != root.end() && it->second.type() == json_spirit::obj_type) {
    for (const auto& [client_name, prof_val] : it->second.get_obj()) {
      if (auto cid = parse_op_class(client_name); cid && prof_val.type() == json_spirit::obj_type) {
        const auto& p = prof_val.get_obj();
        DmClockProfile prof;
        if (auto v = p.find("res"); v != p.end()) prof.reservation = v->second.get_real();
        if (auto v = p.find("wgt"); v != p.end()) prof.weight = v->second.get_real();
        if (auto v = p.find("lim"); v != p.end()) prof.limit = v->second.get_real();
        config.dmclock_profiles[*cid] = prof;
      }
    }
  }

  // 4. Op costs
  if (auto it = root.find("op_costs"); it != root.end() && it->second.type() == json_spirit::obj_type) {
    for (const auto& [op_name, cost_val] : it->second.get_obj()) {
      if (auto op = parse_op_type(op_name); op && cost_val.type() == json_spirit::int_type) {
        config.op_model.costs[*op] = static_cast<dmc::Cost>(cost_val.get_int());
      }
    }
  }

  // 5. Op latency multipliers
  if (auto it = root.find("op_latency_multipliers"); it != root.end() && it->second.type() == json_spirit::obj_type) {
    for (const auto& [op_name, mult_val] : it->second.get_obj()) {
      if (auto op = parse_op_type(op_name)) {
        config.op_model.latency_multipliers[*op] = mult_val.get_real();
      }
    }
  }

  // 6. Tenants
  if (auto it = root.find("tenants"); it != root.end() && it->second.type() == json_spirit::array_type) {
    config.tenants.clear();
    for (const auto& item : it->second.get_array()) {
      if (item.type() != json_spirit::obj_type) continue;
      const auto& t_obj = item.get_obj();

      TenantConfig t;
      if (auto v = t_obj.find("name"); v != t_obj.end() && v->second.type() == json_spirit::str_type) {
        t.name = v->second.get_str();
      }
      if (auto v = t_obj.find("role"); v != t_obj.end() && v->second.type() == json_spirit::str_type) {
        t.role_desc = v->second.get_str();
      }
      if (auto v = t_obj.find("workers"); v != t_obj.end() && v->second.type() == json_spirit::int_type) {
        t.workers = v->second.get_int();
      }
      if (auto v = t_obj.find("client_id"); v != t_obj.end() && v->second.type() == json_spirit::str_type) {
        if (auto cid = parse_op_class(v->second.get_str())) {
          t.client_id = *cid;
        }
      }
      if (auto v = t_obj.find("pacing_ms"); v != t_obj.end()) {
        t.pacing_ms = v->second.get_real();
      }
      if (auto v = t_obj.find("pacing_distribution"); v != t_obj.end() && v->second.type() == json_spirit::str_type) {
        t.pacing_dist = parse_pacing_dist(v->second.get_str());
      }
      if (auto v = t_obj.find("op_weights"); v != t_obj.end() && v->second.type() == json_spirit::obj_type) {
        for (const auto& [op_name, weight_val] : v->second.get_obj()) {
          if (auto op = parse_op_type(op_name); op && weight_val.type() == json_spirit::int_type) {
            t.op_weights.push_back({*op, weight_val.get_int()});
          }
        }
      }
      if (auto v = t_obj.find("dmclock"); v != t_obj.end() && v->second.type() == json_spirit::obj_type) {
        const auto& p = v->second.get_obj();
        if (auto r = p.find("res"); r != p.end()) t.dmclock_profile.reservation = r->second.get_real();
        if (auto w = p.find("wgt"); w != p.end()) t.dmclock_profile.weight = w->second.get_real();
        if (auto l = p.find("lim"); l != p.end()) t.dmclock_profile.limit = l->second.get_real();
      }
      if (t.op_weights.empty()) {
        t.op_weights.push_back({OpType::GetSmall, 100});
      }
      config.tenants.push_back(t);
    }
  }

  return true;
}

inline bool load_config_from_file(const std::string& filepath, BenchConfig& config) {
  std::ifstream fin(filepath);
  if (!fin.is_open()) {
    std::cerr << "[-] Error: Unable to open config file: " << filepath << "\n";
    return false;
  }
  std::stringstream ss;
  ss << fin.rdbuf();
  return load_config_from_json(ss.str(), config);
}

// ============================================================================
// Metrics & Statistics Tracking
// ============================================================================
struct RequestSample {
  double total_lat_ms;
  double queue_lat_ms;
  double backend_lat_ms;
  bool success;
  int error_code; // 0 = success, -EAGAIN = 503 throttled
  OpType op;
};

struct TenantStats {
  std::string name;
  std::string role;
  std::atomic<uint64_t> total_attempted{0};
  std::atomic<uint64_t> total_accepted{0};
  std::atomic<uint64_t> total_rejected_503{0};
  std::atomic<uint64_t> total_retries{0};
  std::atomic<uint64_t> total_bytes{0};

  std::mutex samples_mutex;
  std::vector<RequestSample> samples;

  void record_sample(const RequestSample& sample) {
    std::lock_guard<std::mutex> lock(samples_mutex);
    samples.push_back(sample);
  }
};

// Compute percentile (0.0 - 100.0) from sorted vector
inline double compute_percentile(const std::vector<double>& sorted_data, double p) {
  if (sorted_data.empty()) return 0.0;
  if (sorted_data.size() == 1) return sorted_data[0];
  double rank = (p / 100.0) * (sorted_data.size() - 1);
  size_t low = static_cast<size_t>(std::floor(rank));
  size_t high = static_cast<size_t>(std::ceil(rank));
  double weight = rank - low;
  return sorted_data[low] * (1.0 - weight) + sorted_data[high] * weight;
}

// ============================================================================
// Simulated RADOS / Storage Backend Congestion Engine
// ============================================================================
class SimulatedBackend {
public:
  SimulatedBackend(const BackendConfig& cfg, const OpModelConfig& op_model)
    : config(cfg), op_model(op_model),
      recent_ema_latency_ms(cfg.cluster_base_latency_ms) {}

  void simulate_storage_io(OpType op, boost::asio::yield_context& yield,
                           boost::asio::io_context& ioc,
                           double& out_backend_lat_ms) {
    int inflight = ++active_storage_ops;
    double base_ms = config.cluster_base_latency_ms * op_model.get_latency_mult(op);

    // Non-linear congestion delay when active storage ops exceed cluster capacity
    double load_mult = 1.0;
    if (inflight > config.cluster_capacity) {
      double overload = static_cast<double>(inflight - config.cluster_capacity) / config.cluster_capacity;
      load_mult = 1.0 + config.congestion_factor * std::pow(overload, 1.5);
    }

    // Check for periodic background fault / latency spike injection (e.g. OSD scrub)
    double injected_spike_ms = 0.0;
    if (config.spike_amplitude_ms > 0.0 && config.spike_interval_s > 0.0) {
      auto now_s = std::chrono::duration<double>(
          std::chrono::steady_clock::now().time_since_epoch()).count();
      double cycle = std::fmod(now_s, config.spike_interval_s);
      if (cycle < config.spike_duration_s) {
        injected_spike_ms = config.spike_amplitude_ms;
      }
    }

    double total_ms = (base_ms * load_mult) + injected_spike_ms;

    // Add configurable random jitter
    if (config.jitter_pct > 0.0) {
      thread_local std::mt19937 gen(std::random_device{}());
      std::uniform_real_distribution<double> jitter_dist(1.0 - config.jitter_pct, 1.0 + config.jitter_pct);
      total_ms *= jitter_dist(gen);
    }

    out_backend_lat_ms = total_ms;

    // Update real-time Exponential Moving Average (EMA) for telemetry feedback
    double old_ema = recent_ema_latency_ms.load();
    recent_ema_latency_ms.store(0.85 * old_ema + 0.15 * total_ms);

    boost::asio::steady_timer timer(ioc);
    timer.expires_after(std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::duration<double, std::milli>(total_ms)));
    boost::system::error_code ec;
    timer.async_wait(yield[ec]);

    --active_storage_ops;
  }

  int get_active_ops() const { return active_storage_ops.load(); }
  double get_ema_latency_ms() const { return recent_ema_latency_ms.load(); }

private:
  BackendConfig config;
  OpModelConfig op_model;
  std::atomic<int> active_storage_ops{0};
  std::atomic<double> recent_ema_latency_ms{5.0};
};

// ============================================================================
// Tenant-Aware Scheduler Interfaces & Implementations
// ============================================================================
struct TenantRequest {
  uint32_t tenant_idx{0};
  dmc::Time started{0.0};
  dmc::Cost cost{0};

  TenantRequest() = default;
  TenantRequest(uint32_t idx, dmc::Time s, dmc::Cost c)
    : tenant_idx(idx), started(s), cost(c) {}
  virtual ~TenantRequest() = default;
};

class TenantScheduler {
public:
  virtual ~TenantScheduler() = default;
  virtual void cancel() {}
  virtual void update_capacity(double /*scale_factor*/) {}
  virtual std::pair<int, dmc::SchedulerCompleter> schedule_request(
      uint32_t tenant_idx,
      dmc::op_class op_class,
      const dmc::ReqParams& params,
      const dmc::Time& time,
      dmc::Cost cost,
      boost::asio::yield_context yield) = 0;
};

// 1. Throttler (Ceph RGW default global FIFO semaphore)
class ThrottlerTenantScheduler : public TenantScheduler {
public:
  ThrottlerTenantScheduler(CephContext *cct, int64_t max_concurrency = 128)
    : cct(cct), throttler(std::make_shared<dmc::SimpleThrottler>(cct)),
      base_max(max_concurrency) {}

  void update_capacity(double scale_factor) override {
    int64_t new_max = std::max<int64_t>(8, static_cast<int64_t>(base_max * scale_factor));
    cct->_conf.set_val("rgw_max_concurrent_requests", std::to_string(new_max));
  }

  std::pair<int, dmc::SchedulerCompleter> schedule_request(
      uint32_t,
      dmc::op_class op_class,
      const dmc::ReqParams& params,
      const dmc::Time& time,
      dmc::Cost cost,
      boost::asio::yield_context yield) override
  {
    return throttler->schedule_request(op_class, params, time, cost, yield);
  }

private:
  CephContext *cct;
  std::shared_ptr<dmc::SimpleThrottler> throttler;
  int64_t base_max;
};

// 2. Coarse 4-Class dmClock (Upstream Ceph RGW dmClock)
class CoarseDmClockTenantScheduler : public TenantScheduler {
public:
  CoarseDmClockTenantScheduler(
      CephContext *cct,
      boost::asio::io_context& context,
      dmc::ClientCounters& counters,
      const std::map<dmc::op_class, DmClockProfile>& profiles,
      int64_t max_concurrency = 128)
    : cct(cct), profiles(profiles), base_max(max_concurrency),
      scheduler(std::make_shared<dmc::AsyncScheduler>(
          cct, context, std::ref(counters), nullptr,
          // the queue is keyed by client_id; upstream's coarse mode leaves
          // tenant_id at 0, so only the op class distinguishes queues here
          [this](const dmc::client_id& client) -> dmc::ClientInfo* {
            static dmc::ClientInfo client_infos[4] = {
              {10.0, 50.0, 50.0},
              {10.0, 50.0, 50.0},
              {10.0, 50.0, 50.0},
              {10.0, 50.0, 50.0}
            };
            auto it = this->profiles.find(client.op);
            size_t idx = static_cast<size_t>(client.op);
            if (it != this->profiles.end() && idx < 4) {
              client_infos[idx] = dmc::ClientInfo{it->second.reservation, it->second.weight, it->second.limit};
            }
            return &client_infos[idx];
          },
          crimson::dmclock::AtLimitParam(crimson::dmclock::RejectThreshold{1.0}))) {}

  void cancel() override {
    scheduler->cancel();
  }

  void update_capacity(double scale_factor) override {
    int64_t new_max = std::max<int64_t>(8, static_cast<int64_t>(base_max * scale_factor));
    cct->_conf.set_val("rgw_max_concurrent_requests", std::to_string(new_max));
  }

  std::pair<int, dmc::SchedulerCompleter> schedule_request(
      uint32_t,
      dmc::op_class op_class,
      const dmc::ReqParams& params,
      const dmc::Time& time,
      dmc::Cost cost,
      boost::asio::yield_context yield) override
  {
    return scheduler->schedule_request(op_class, params, time, cost, yield);
  }

private:
  CephContext *cct;
  std::map<dmc::op_class, DmClockProfile> profiles;
  int64_t base_max;
  std::shared_ptr<dmc::AsyncScheduler> scheduler;
};

// 3. Fine-Grained Multi-Tenant dmClock (Per-Tenant Isolated dmClock Queues)
class FineGrainedDmClockTenantScheduler : public TenantScheduler {
public:
  using Signature = void(boost::system::error_code, dmc::PhaseType);
  using Completion = ceph::async::Completion<Signature, ceph::async::AsBase<TenantRequest>>;
  using Queue = crimson::dmclock::PullPriorityQueue<uint32_t, TenantRequest, false>;
  using RequestRef = typename Queue::RequestRef;

  FineGrainedDmClockTenantScheduler(
      CephContext *cct,
      boost::asio::io_context& context,
      const std::vector<TenantConfig>& tenant_configs,
      int64_t max_reqs)
    : cct(cct),
      strand(boost::asio::make_strand(context)),
      timer(strand),
      base_max_requests(max_reqs > 0 ? max_reqs : std::numeric_limits<int64_t>::max()),
      max_requests(base_max_requests),
      queue(
        [this](uint32_t tenant_idx) -> dmc::ClientInfo* {
          if (tenant_idx < client_infos.size()) {
            return &client_infos[tenant_idx];
          }
          static dmc::ClientInfo default_info{10.0, 50.0, 50.0};
          return &default_info;
        },
        crimson::dmclock::AtLimitParam(crimson::dmclock::RejectThreshold{1.0}))
  {
    client_infos.reserve(tenant_configs.size());
    base_profiles.reserve(tenant_configs.size());
    for (size_t i = 0; i < tenant_configs.size(); ++i) {
      base_profiles.push_back(tenant_configs[i].dmclock_profile);
      client_infos.emplace_back(
        tenant_configs[i].dmclock_profile.reservation,
        tenant_configs[i].dmclock_profile.weight,
        tenant_configs[i].dmclock_profile.limit
      );
    }
  }

  ~FineGrainedDmClockTenantScheduler() override {
    cancel();
  }

  void cancel() override {
    is_cancelled.store(true);
    timer.cancel();
    std::vector<std::unique_ptr<Completion>> to_cancel;
    {
      std::lock_guard<std::mutex> lock(queue_mutex);
      for (size_t i = 0; i < client_infos.size(); ++i) {
        queue.remove_by_client(i, false, [&to_cancel](RequestRef&& req) {
          auto c = static_cast<Completion*>(req.release());
          to_cancel.emplace_back(c);
        });
      }
    }
    for (auto& c : to_cancel) {
      Completion::post(std::move(c),
                       boost::asio::error::operation_aborted,
                       dmc::PhaseType::priority);
    }
  }

  void update_capacity(double scale_factor) override {
    if (is_cancelled.load()) return;
    boost::asio::post(strand, [this, scale_factor]() {
      if (is_cancelled.load()) return;
      {
        std::lock_guard<std::mutex> lock(queue_mutex);
        for (size_t i = 0; i < base_profiles.size(); ++i) {
          double r = base_profiles[i].reservation * scale_factor;
          double w = std::max(1.0, base_profiles[i].weight * scale_factor);
          double l = std::max(1.0, base_profiles[i].limit * scale_factor);
          // Priority Protection: VIP and Health reservations (>= 10.0) are shielded from excessive reduction
          if (base_profiles[i].reservation >= 10.0) {
            r = std::max(r, base_profiles[i].reservation * 0.70);
          }
          client_infos[i].update(r, w, l);
        }
        max_requests = std::max<int64_t>(8, static_cast<int64_t>(base_max_requests * scale_factor));
      }
      process_internal(dmc::get_time());
    });
  }

  void schedule_timer(const dmc::Time& time) {
    if (is_cancelled.load()) return;
    auto new_expiry = ceph::coarse_real_clock::from_double(time);
    if (timer_active && timer.expiry() <= new_expiry) {
      return;
    }
    timer.expires_at(new_expiry);
    timer_active = true;
    timer.async_wait(boost::asio::bind_executor(strand, [this](boost::system::error_code ec) {
      timer_active = false;
      if (!ec && !is_cancelled.load()) {
        process_internal(dmc::get_time());
      }
    }));
  }

  void process_internal(const dmc::Time& now) {
    if (is_cancelled.load()) return;
    while (outstanding_requests < max_requests && !is_cancelled.load()) {
      typename Queue::PullReq pull;
      {
        std::lock_guard<std::mutex> lock(queue_mutex);
        pull = queue.pull_request(now);
      }
      if (pull.is_none()) {
        break;
      }
      if (pull.is_future()) {
        schedule_timer(pull.getTime());
        break;
      }
      ++outstanding_requests;
      auto& r = pull.get_retn();
      auto c = static_cast<Completion*>(r.request.release());
      Completion::post(std::unique_ptr<Completion>{c},
                       boost::system::error_code{}, r.phase);
    }
  }

  void request_complete() {
    --outstanding_requests;
    if (!is_cancelled.load()) {
      boost::asio::post(strand, [this]() {
        if (!is_cancelled.load()) {
          process_internal(dmc::get_time());
        }
      });
    }
  }

  template <typename CompletionToken>
  auto async_request(uint32_t tenant_idx, const dmc::ReqParams& params,
                     const dmc::Time& time, dmc::Cost cost, CompletionToken&& token) {
    return boost::asio::async_initiate<CompletionToken, Signature>(
        [this](auto handler, auto ex, uint32_t tenant_idx,
               const dmc::ReqParams& params, const dmc::Time& time, dmc::Cost cost) {
          auto completion = Completion::create(ex, std::move(handler),
                                               TenantRequest{tenant_idx, time, cost});
          auto req = RequestRef{std::move(completion)};
          int r = 0;
          {
            std::lock_guard<std::mutex> lock(queue_mutex);
            r = queue.add_request(std::move(req), tenant_idx, params, time, cost);
          }
          if (r == 0) {
            boost::asio::post(strand, [this]() {
              process_internal(dmc::get_time());
            });
          } else {
            boost::system::error_code ec(r, boost::system::system_category());
            auto completion = static_cast<Completion*>(req.release());
            Completion::post(std::unique_ptr<Completion>{completion},
                             ec, dmc::PhaseType::priority);
          }
        }, token, strand, tenant_idx, params, time, cost);
  }

  std::pair<int, dmc::SchedulerCompleter> schedule_request(
      uint32_t tenant_idx,
      dmc::op_class,
      const dmc::ReqParams& params,
      const dmc::Time& time,
      dmc::Cost cost,
      boost::asio::yield_context yield) override
  {
    boost::system::error_code ec;
    async_request(tenant_idx, params, time, cost, yield[ec]);

    if (ec) {
      if (ec == boost::system::errc::resource_unavailable_try_again) {
        return std::make_pair(-EAGAIN, dmc::SchedulerCompleter{});
      }
      return std::make_pair(-ec.value(), dmc::SchedulerCompleter{});
    }

    return std::make_pair(0, dmc::SchedulerCompleter([this]() {
      request_complete();
    }));
  }

private:
  CephContext *cct;
  using Strand = boost::asio::strand<boost::asio::io_context::executor_type>;
  Strand strand;
  std::mutex queue_mutex;
  using Clock = ceph::coarse_real_clock;
  using Timer = boost::asio::basic_waitable_timer<Clock, boost::asio::wait_traits<Clock>, Strand>;
  Timer timer;
  bool timer_active{false};
  std::atomic<bool> is_cancelled{false};
  int64_t base_max_requests;
  int64_t max_requests;
  std::atomic<int64_t> outstanding_requests{0};
  std::vector<DmClockProfile> base_profiles;
  std::vector<dmc::ClientInfo> client_infos;
  Queue queue;
};

// 4. No-Op (Unbounded)
class NoOpTenantScheduler : public TenantScheduler {
public:
  std::pair<int, dmc::SchedulerCompleter> schedule_request(
      uint32_t,
      dmc::op_class,
      const dmc::ReqParams&,
      const dmc::Time&,
      dmc::Cost,
      boost::asio::yield_context) override
  {
    return std::make_pair(0, dmc::SchedulerCompleter{});
  }
};

// ============================================================================
// Adaptive Closed-Loop Capacity Controller (Telemetry Monitor Thread)
// ============================================================================
inline void run_adaptive_controller_thread(
    std::shared_ptr<TenantScheduler> scheduler,
    std::shared_ptr<SimulatedBackend> backend,
    const std::atomic<bool>& running,
    double target_lat_ms = 5.0,
    int sample_interval_ms = 50)
{
  double current_scale = 1.0;
  while (running.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(sample_interval_ms));
    if (!running.load()) break;

    double measured_lat = backend->get_ema_latency_ms();
    if (measured_lat <= target_lat_ms * 1.10) {
      // Cluster healthy / recovery: Additive Increase up to 1.5x nominal capacity
      current_scale = std::min(1.5, current_scale + 0.05);
    } else if (measured_lat > target_lat_ms * 1.25) {
      // Congestion / OSD scrub spike: Multiplicative Decrease based on latency overshoot
      double ratio = target_lat_ms / measured_lat;
      double dec = std::clamp(ratio, 0.35, 0.85);
      current_scale = std::max(0.15, current_scale * dec);
    }

    scheduler->update_capacity(current_scale);
  }
}

// ============================================================================
// Dynamic Op Selector & Pacing Helpers
// ============================================================================
class OpSelector {
public:
  explicit OpSelector(const std::vector<std::pair<OpType, int>>& op_weights) {
    std::vector<double> weights;
    for (const auto& [op, w] : op_weights) {
      ops.push_back(op);
      weights.push_back(static_cast<double>(w));
    }
    if (ops.empty()) {
      ops.push_back(OpType::GetSmall);
      weights.push_back(1.0);
    }
    dist = std::discrete_distribution<size_t>(weights.begin(), weights.end());
  }

  template <typename Generator>
  OpType select_op(Generator& gen) {
    return ops[dist(gen)];
  }

private:
  std::vector<OpType> ops;
  std::discrete_distribution<size_t> dist;
};

inline std::chrono::microseconds compute_pacing_delay(double mean_pacing_ms,
                                                      PacingDistribution dist_type,
                                                      std::mt19937& rng) {
  if (mean_pacing_ms <= 0.0) return 0us;

  double delay_ms = mean_pacing_ms;
  switch (dist_type) {
    case PacingDistribution::Constant:
      delay_ms = mean_pacing_ms;
      break;
    case PacingDistribution::Uniform: {
      std::uniform_real_distribution<double> u_dist(0.0, 2.0 * mean_pacing_ms);
      delay_ms = u_dist(rng);
      break;
    }
    case PacingDistribution::Exponential: {
      // Exponential distribution with lambda = 1.0 / mean_pacing_ms
      std::exponential_distribution<double> exp_dist(1.0 / mean_pacing_ms);
      delay_ms = exp_dist(rng);
      break;
    }
  }
  return std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::duration<double, std::milli>(delay_ms));
}

// ============================================================================
// Dynamic Worker Coroutine
// ============================================================================
void run_tenant_worker(boost::asio::io_context& ioc,
                       std::shared_ptr<TenantScheduler> scheduler,
                       std::shared_ptr<SimulatedBackend> backend,
                       std::shared_ptr<TenantStats> stats,
                       uint32_t tenant_idx,
                       TenantConfig tenant_cfg,
                       const std::atomic<bool>& running,
                       const BenchConfig& config) {
  boost::asio::spawn(boost::asio::make_strand(ioc),
                     [&ioc, scheduler, backend, stats, tenant_idx, tenant_cfg, &running, config]
                     (boost::asio::yield_context yield) {
    thread_local std::mt19937 rng(std::random_device{}());
    OpSelector selector(tenant_cfg.op_weights);

    while (running.load()) {
      auto start_time = std::chrono::steady_clock::now();
      stats->total_attempted++;

      OpType op = selector.select_op(rng);
      dmc::op_class cid = tenant_cfg.client_id;
      dmc::Cost cost = config.op_model.get_cost(op);
      auto dmc_time = dmc::get_time();

      int ret = 0;
      dmc::SchedulerCompleter completer;

      // 1. Admission Control / Scheduling
      std::tie(ret, completer) = scheduler->schedule_request(tenant_idx, cid, {}, dmc_time, cost, yield);

      auto scheduled_time = std::chrono::steady_clock::now();
      double queue_lat_ms = std::chrono::duration<double, std::milli>(
          scheduled_time - start_time).count();

      if (ret < 0) {
        // Rejected (e.g. -EAGAIN -> 503 SlowDown)
        stats->total_rejected_503++;
        RequestSample s{queue_lat_ms, queue_lat_ms, 0.0, false, ret, op};
        stats->record_sample(s);

        if (!running.load()) {
          break;
        }

        if (config.allow_retries) {
          // Exponential backoff retry
          int retry_count = 0;
          while (retry_count < config.max_retries && running.load()) {
            stats->total_retries++;
            int backoff_ms = 10 * (1 << retry_count);
            boost::asio::steady_timer retry_timer(ioc);
            retry_timer.expires_after(std::chrono::milliseconds(backoff_ms));
            boost::system::error_code ec;
            retry_timer.async_wait(yield[ec]);
            if (!running.load()) break;

            std::tie(ret, completer) = scheduler->schedule_request(tenant_idx, cid, {}, dmc::get_time(), cost, yield);
            if (ret == 0) break;
            retry_count++;
          }
          if (ret < 0 || !running.load()) {
            continue;
          }
        } else {
          // Yield briefly to avoid spin loop on rejection
          boost::asio::steady_timer sleep_timer(ioc);
          sleep_timer.expires_after(5ms);
          boost::system::error_code ec;
          sleep_timer.async_wait(yield[ec]);
          continue;
        }
      }

      if (!running.load()) {
        break;
      }

      // 2. Storage Backend Execution
      double backend_lat_ms = 0.0;
      backend->simulate_storage_io(op, yield, ioc, backend_lat_ms);

      // Completer destructor releases the scheduler throttle slot
      // Release by DESTROYING the completer. Assigning an empty one does not
      // work: Completer's move-assignment is defaulted, so it overwrites the
      // stored callback without ever invoking it, and the slot is leaked.
      { dmc::SchedulerCompleter release = std::move(completer); }

      auto end_time = std::chrono::steady_clock::now();
      double total_lat_ms = std::chrono::duration<double, std::milli>(
          end_time - start_time).count();

      stats->total_accepted++;
      RequestSample s{total_lat_ms, queue_lat_ms, backend_lat_ms, true, 0, op};
      stats->record_sample(s);

      // 3. Pacing / Think Time Delay
      if (tenant_cfg.pacing_ms > 0.0 && running.load()) {
        auto pacing_delay = compute_pacing_delay(tenant_cfg.pacing_ms, tenant_cfg.pacing_dist, rng);
        if (pacing_delay.count() > 0 && running.load()) {
          boost::asio::steady_timer pacing_timer(ioc);
          pacing_timer.expires_after(pacing_delay);
          boost::system::error_code ec;
          pacing_timer.async_wait(yield[ec]);
        }
      }
    }
  }, boost::asio::detached);
}

// ============================================================================
// Metrics Exporters (JSON & CSV)
// ============================================================================
inline void export_results_to_json(const std::string& filepath,
                                   const std::vector<std::shared_ptr<TenantStats>>& all_stats,
                                   const BenchConfig& config,
                                   double elapsed_s) {
  json_spirit::mObject root;
  root["scheduler"] = config.scheduler_type;
  root["elapsed_time_s"] = elapsed_s;
  root["max_concurrency"] = static_cast<int64_t>(config.max_concurrent_requests);
  root["cluster_capacity"] = config.backend.cluster_capacity;
  root["server_threads"] = config.thread_count;

  json_spirit::mArray tenants_arr;
  double sum_tps = 0.0;
  double sum_sq_tps = 0.0;

  for (const auto& t : all_stats) {
    json_spirit::mObject t_obj;
    t_obj["name"] = t->name;
    t_obj["role"] = t->role;
    uint64_t attempted = t->total_attempted.load();
    uint64_t accepted = t->total_accepted.load();
    uint64_t drops = t->total_rejected_503.load();
    uint64_t retries = t->total_retries.load();
    double drop_rate = (attempted > 0) ? (100.0 * drops / attempted) : 0.0;
    double tps = (elapsed_s > 0) ? (static_cast<double>(accepted) / elapsed_s) : 0.0;

    sum_tps += tps;
    sum_sq_tps += (tps * tps);

    t_obj["attempted"] = static_cast<int64_t>(attempted);
    t_obj["accepted"] = static_cast<int64_t>(accepted);
    t_obj["drops_503"] = static_cast<int64_t>(drops);
    t_obj["retries"] = static_cast<int64_t>(retries);
    t_obj["drop_rate_pct"] = drop_rate;
    t_obj["throughput_tps"] = tps;

    std::vector<double> latencies;
    {
      std::lock_guard<std::mutex> lock(t->samples_mutex);
      for (const auto& s : t->samples) {
        if (s.success) latencies.push_back(s.total_lat_ms);
      }
    }
    std::sort(latencies.begin(), latencies.end());

    t_obj["p50_ms"] = compute_percentile(latencies, 50.0);
    t_obj["p90_ms"] = compute_percentile(latencies, 90.0);
    t_obj["p95_ms"] = compute_percentile(latencies, 95.0);
    t_obj["p99_ms"] = compute_percentile(latencies, 99.0);
    t_obj["max_ms"] = latencies.empty() ? 0.0 : latencies.back();

    tenants_arr.push_back(t_obj);
  }

  root["tenants"] = tenants_arr;
  root["total_throughput_tps"] = sum_tps;
  size_t n = all_stats.size();
  double jains = (sum_sq_tps > 0.0 && n > 0) ? ((sum_tps * sum_tps) / (n * sum_sq_tps)) : 1.0;
  root["jains_fairness_index"] = jains;

  std::ofstream fout(filepath);
  if (fout.is_open()) {
    fout << json_spirit::write_formatted(root) << "\n";
    std::cout << "[+] Benchmark results exported to JSON: " << filepath << "\n";
  } else {
    std::cerr << "[-] Error: Failed to write JSON results to: " << filepath << "\n";
  }
}

inline void export_results_to_csv(const std::string& filepath,
                                  const std::vector<std::shared_ptr<TenantStats>>& all_stats) {
  std::ofstream fout(filepath);
  if (!fout.is_open()) {
    std::cerr << "[-] Error: Failed to write CSV results to: " << filepath << "\n";
    return;
  }

  fout << "tenant,op,success,error_code,total_lat_ms,queue_lat_ms,backend_lat_ms\n";
  for (const auto& t : all_stats) {
    std::lock_guard<std::mutex> lock(t->samples_mutex);
    for (const auto& s : t->samples) {
      fout << "\"" << t->name << "\","
           << "\"" << op_type_name(s.op) << "\","
           << (s.success ? 1 : 0) << ","
           << s.error_code << ","
           << std::fixed << std::setprecision(3) << s.total_lat_ms << ","
           << std::fixed << std::setprecision(3) << s.queue_lat_ms << ","
           << std::fixed << std::setprecision(3) << s.backend_lat_ms << "\n";
    }
  }
  std::cout << "[+] Benchmark raw samples exported to CSV: " << filepath << "\n";
}

// ============================================================================
// Report & Analysis Printer
// ============================================================================
void print_report(const std::vector<std::shared_ptr<TenantStats>>& all_stats,
                  const BenchConfig& config,
                  double elapsed_s) {
  std::cout << "\n"
            << "====================================================================================================\n"
            << "                         RGW WORKLOAD SCHEDULING BENCHMARK REPORT                                   \n"
            << "====================================================================================================\n";
  std::cout << " Scheduler: " << config.scheduler_type
            << " | Max Concurrency Ceiling: " << config.max_concurrent_requests
            << " | Cluster Knee: " << config.backend.cluster_capacity << " ops\n"
            << " Elapsed Time: " << std::fixed << std::setprecision(2) << elapsed_s << "s"
            << " | Server Threads: " << config.thread_count << "\n";
  std::cout << "----------------------------------------------------------------------------------------------------\n";

  std::cout << std::left
            << std::setw(26) << "Tenant Persona"
            << std::setw(11) << "Attempted"
            << std::setw(11) << "Accepted"
            << std::setw(11) << "503 Drops"
            << std::setw(10) << "Drop Rate"
            << std::setw(10) << "Req/sec"
            << std::setw(9)  << "p50 (ms)"
            << std::setw(9)  << "p95 (ms)"
            << std::setw(9)  << "p99 (ms)"
            << std::setw(9)  << "Max (ms)"
            << "\n";
  std::cout << "----------------------------------------------------------------------------------------------------\n";

  std::vector<double> tenant_throughputs;

  for (const auto& t : all_stats) {
    uint64_t attempted = t->total_attempted.load();
    uint64_t accepted = t->total_accepted.load();
    uint64_t drops = t->total_rejected_503.load();
    double drop_rate = (attempted > 0) ? (100.0 * drops / attempted) : 0.0;
    double tps = (elapsed_s > 0) ? (static_cast<double>(accepted) / elapsed_s) : 0.0;

    tenant_throughputs.push_back(tps);

    // Extract latency percentiles for accepted requests
    std::vector<double> latencies;
    {
      std::lock_guard<std::mutex> lock(t->samples_mutex);
      for (const auto& s : t->samples) {
        if (s.success) {
          latencies.push_back(s.total_lat_ms);
        }
      }
    }
    std::sort(latencies.begin(), latencies.end());

    double p50 = compute_percentile(latencies, 50.0);
    double p95 = compute_percentile(latencies, 95.0);
    double p99 = compute_percentile(latencies, 99.0);
    double max_lat = latencies.empty() ? 0.0 : latencies.back();

    std::cout << std::left
              << std::setw(26) << t->name
              << std::setw(11) << attempted
              << std::setw(11) << accepted
              << std::setw(11) << drops
              << std::fixed << std::setprecision(1) << std::setw(9) << drop_rate << "%"
              << std::fixed << std::setprecision(1) << std::setw(10) << tps
              << std::fixed << std::setprecision(1) << std::setw(9) << p50
              << std::fixed << std::setprecision(1) << std::setw(9) << p95
              << std::fixed << std::setprecision(1) << std::setw(9) << p99
              << std::fixed << std::setprecision(1) << std::setw(9) << max_lat
              << "\n";
  }

  // Jain's Fairness Index computation: J = (sum x_i)^2 / (n * sum x_i^2)
  double sum_tps = 0.0;
  double sum_sq_tps = 0.0;
  size_t n_tenants = tenant_throughputs.size();
  for (double tps : tenant_throughputs) {
    sum_tps += tps;
    sum_sq_tps += (tps * tps);
  }
  double jains_fairness = (sum_sq_tps > 0.0 && n_tenants > 0)
                          ? ((sum_tps * sum_tps) / (n_tenants * sum_sq_tps))
                          : 1.0;

  std::cout << "----------------------------------------------------------------------------------------------------\n";
  std::cout << " Overall Throughput: " << std::fixed << std::setprecision(1) << sum_tps << " req/sec"
            << " | Jain's Fairness Index: " << std::fixed << std::setprecision(3) << jains_fairness << " (1.0 = ideal)\n";
  std::cout << "====================================================================================================\n\n";

  if (!config.export_json_path.empty()) {
    export_results_to_json(config.export_json_path, all_stats, config, elapsed_s);
  }
  if (!config.export_csv_path.empty()) {
    export_results_to_csv(config.export_csv_path, all_stats);
  }
}

// ============================================================================
// Main Entry Point
// ============================================================================
int main(int argc, char* argv[]) {
  BenchConfig config;
  std::string config_file;
  bool dump_config_flag = false;

  // CLI variables for quick overrides
  int cli_bully_workers = 60;
  int cli_interactive_workers = 15;
  int cli_probe_interval_ms = 250;

  try {
    po::options_description desc{"RGW Workload Scheduler Benchmark Options"};
    desc.add_options()
      ("help,h", "Produce help message")
      ("config,c", po::value<std::string>(&config_file),
       "Path to declarative JSON scenario configuration file")
      ("dump_config", po::bool_switch(&dump_config_flag),
       "Dump default/active scenario JSON configuration to stdout and exit")
      ("export_json", po::value<std::string>(&config.export_json_path),
       "Path to export benchmark summary and percentiles in JSON format")
      ("export_csv", po::value<std::string>(&config.export_csv_path),
       "Path to export raw request latency samples in CSV format")
      ("scheduler", po::value<std::string>(&config.scheduler_type)->default_value("throttler"),
       "Scheduler to test: throttler | dmclock_coarse (or dmclock) | dmclock_fine (per-tenant) | none")
      ("max_concurrency", po::value<int64_t>(&config.max_concurrent_requests)->default_value(128),
       "Ceiling on concurrent requests (rgw_max_concurrent_requests)")
      ("runtime", po::value<int>(&config.runtime_seconds)->default_value(10),
       "Benchmark runtime duration in seconds")
      ("threads", po::value<int>(&config.thread_count)->default_value(8),
       "Number of server I/O threads")
      ("cluster_capacity", po::value<int>(&config.backend.cluster_capacity)->default_value(64),
       "Simulated backend cluster knee capacity (concurrent ops before latency climbs)")
      ("cluster_base_latency_ms", po::value<double>(&config.backend.cluster_base_latency_ms)->default_value(5.0),
       "Simulated backend base latency in ms")
      ("congestion_factor", po::value<double>(&config.backend.congestion_factor)->default_value(3.0),
       "Backend latency penalty exponent multiplier under overload")
      ("spike_amplitude_ms", po::value<double>(&config.backend.spike_amplitude_ms)->default_value(0.0),
       "Injected background latency spike amplitude in ms (simulates OSD scrub/hiccup)")
      ("spike_interval_s", po::value<double>(&config.backend.spike_interval_s)->default_value(5.0),
       "Interval in seconds for periodic latency spike")
      ("spike_duration_s", po::value<double>(&config.backend.spike_duration_s)->default_value(1.0),
       "Duration in seconds of each periodic latency spike")
      ("bully_workers", po::value<int>(&cli_bully_workers)->default_value(60),
       "Number of concurrent workers for Tenant 1 (Bully bulk uploader)")
      ("interactive_workers", po::value<int>(&cli_interactive_workers)->default_value(15),
       "Number of concurrent workers for Tenant 2 (Interactive small ops)")
      ("probe_interval_ms", po::value<int>(&cli_probe_interval_ms)->default_value(250),
       "Probe request interval for Tenant 3 (Health monitor)")
      ("retries", po::bool_switch(&config.allow_retries),
       "Enable client exponential backoff retries upon 503")
      ("adaptive", po::bool_switch(&config.adaptive_tuning),
       "Enable adaptive closed-loop capacity tuning based on backend latency feedback")
      ("target_latency_ms", po::value<double>(&config.adaptive_target_latency_ms),
       "Target backend latency in ms for adaptive capacity controller")
      ("sample_interval_ms", po::value<int>(&config.adaptive_sample_interval_ms),
       "Sampling interval in ms for adaptive capacity controller");

    po::variables_map vm;
    po::store(po::parse_command_line(argc, argv, desc), vm);

    if (vm.count("help")) {
      std::cout << desc << "\n";
      return 0;
    }
    po::notify(vm);

    // Initialize default tenants if not loaded from config
    config.tenants = default_tenants(cli_bully_workers, cli_interactive_workers, cli_probe_interval_ms);

    // If config file provided, load it
    if (!config_file.empty()) {
      if (!load_config_from_file(config_file, config)) {
        return 1;
      }
      // Re-apply CLI overrides if explicitly passed on command line
      if (vm.count("scheduler") && !vm["scheduler"].defaulted()) config.scheduler_type = vm["scheduler"].as<std::string>();
      if (vm.count("max_concurrency") && !vm["max_concurrency"].defaulted()) config.max_concurrent_requests = vm["max_concurrency"].as<int64_t>();
      if (vm.count("runtime") && !vm["runtime"].defaulted()) config.runtime_seconds = vm["runtime"].as<int>();
      if (vm.count("threads") && !vm["threads"].defaulted()) config.thread_count = vm["threads"].as<int>();
      if (vm.count("cluster_capacity") && !vm["cluster_capacity"].defaulted()) config.backend.cluster_capacity = vm["cluster_capacity"].as<int>();
      if (vm.count("cluster_base_latency_ms") && !vm["cluster_base_latency_ms"].defaulted()) config.backend.cluster_base_latency_ms = vm["cluster_base_latency_ms"].as<double>();
      if (vm.count("congestion_factor") && !vm["congestion_factor"].defaulted()) config.backend.congestion_factor = vm["congestion_factor"].as<double>();
      if (vm.count("spike_amplitude_ms") && !vm["spike_amplitude_ms"].defaulted()) config.backend.spike_amplitude_ms = vm["spike_amplitude_ms"].as<double>();
      if (vm.count("adaptive") && !vm["adaptive"].defaulted()) config.adaptive_tuning = vm["adaptive"].as<bool>();
      if (vm.count("target_latency_ms") && !vm["target_latency_ms"].defaulted()) config.adaptive_target_latency_ms = vm["target_latency_ms"].as<double>();
      if (vm.count("sample_interval_ms") && !vm["sample_interval_ms"].defaulted()) config.adaptive_sample_interval_ms = vm["sample_interval_ms"].as<int>();
    }

    if (dump_config_flag) {
      std::cout << dump_config_to_json(config) << "\n";
      return 0;
    }

  } catch (const std::exception& ex) {
    std::cerr << "Configuration Error: " << ex.what() << "\n";
    return 1;
  }

  // Initialize Ceph Context
  std::unique_ptr<CephContext> cct = std::make_unique<CephContext>(CEPH_ENTITY_TYPE_ANY);
  if (!g_ceph_context) {
    g_ceph_context = cct.get();
  }
  g_ceph_context->_conf.set_val("rgw_max_concurrent_requests",
                                std::to_string(config.max_concurrent_requests));

  boost::asio::io_context context;
  using Executor = boost::asio::io_context::executor_type;
  std::optional<boost::asio::executor_work_guard<Executor>> work;
  work.emplace(boost::asio::make_work_guard(context));

  // Instantiate Scheduler
  std::shared_ptr<TenantScheduler> scheduler;
  dmc::ClientCounters counters(g_ceph_context);

  if (config.scheduler_type == "throttler") {
    scheduler = std::make_shared<ThrottlerTenantScheduler>(g_ceph_context, config.max_concurrent_requests);
  } else if (config.scheduler_type == "dmclock" || config.scheduler_type == "dmclock_coarse") {
    scheduler = std::make_shared<CoarseDmClockTenantScheduler>(
        g_ceph_context, context, counters, config.dmclock_profiles, config.max_concurrent_requests);
  } else if (config.scheduler_type == "dmclock_fine" || config.scheduler_type == "dmclock_per_tenant") {
    scheduler = std::make_shared<FineGrainedDmClockTenantScheduler>(
        g_ceph_context, context, config.tenants, config.max_concurrent_requests);
  } else if (config.scheduler_type == "none") {
    scheduler = std::make_shared<NoOpTenantScheduler>();
  } else {
    std::cerr << "Unknown scheduler type: " << config.scheduler_type << "\n";
    return 1;
  }

  auto backend = std::make_shared<SimulatedBackend>(config.backend, config.op_model);

  // Setup Tenants & Metrics
  std::vector<std::shared_ptr<TenantStats>> all_stats;
  std::atomic<bool> running{true};

  std::cout << "[*] Starting workload benchmark with " << config.scheduler_type
            << " scheduler for " << config.runtime_seconds << "s...\n"
            << "    Configured Tenants: " << config.tenants.size()
            << " | Capacity Knee: " << config.backend.cluster_capacity << "\n";

  std::thread controller_thread;
  if (config.adaptive_tuning) {
    std::cout << "    [+] Adaptive Closed-Loop Capacity Tuning: ACTIVE\n"
              << "        Target Latency: " << config.adaptive_target_latency_ms << "ms"
              << " | Sample Interval: " << config.adaptive_sample_interval_ms << "ms\n";
    controller_thread = std::thread([scheduler, backend, &running, &config]() {
      run_adaptive_controller_thread(scheduler, backend, running,
                                     config.adaptive_target_latency_ms,
                                     config.adaptive_sample_interval_ms);
    });
  }

  for (uint32_t t_idx = 0; t_idx < config.tenants.size(); ++t_idx) {
    const auto& t_cfg = config.tenants[t_idx];
    auto t_stats = std::make_shared<TenantStats>();
    t_stats->name = t_cfg.name;
    t_stats->role = t_cfg.role_desc;
    all_stats.push_back(t_stats);

    std::cout << "    -> Tenant [" << t_idx << "] '" << t_cfg.name << "': " << t_cfg.workers
              << " workers, client_id=" << op_class_to_str(t_cfg.client_id)
              << ", pacing=" << t_cfg.pacing_ms << "ms (" << pacing_dist_to_str(t_cfg.pacing_dist)
              << "), dmClock(R=" << t_cfg.dmclock_profile.reservation
              << ", W=" << t_cfg.dmclock_profile.weight
              << ", L=" << t_cfg.dmclock_profile.limit << ")\n";

    for (int w = 0; w < t_cfg.workers; w++) {
      run_tenant_worker(context, scheduler, backend, t_stats, t_idx, t_cfg, running, config);
    }
  }

  // Spawn I/O Thread Pool
  std::vector<std::thread> threads;
  threads.reserve(config.thread_count);
  for (int i = 0; i < config.thread_count; i++) {
    threads.emplace_back([&context]() noexcept {
      context.run();
    });
  }

  auto start_time = std::chrono::steady_clock::now();

  // Run for specified duration
  std::this_thread::sleep_for(std::chrono::seconds(config.runtime_seconds));

  running.store(false);
  if (controller_thread.joinable()) {
    controller_thread.join();
  }
  if (scheduler) {
    scheduler->cancel();
  }
  work.reset();

  for (auto& t : threads) {
    if (t.joinable()) {
      t.join();
    }
  }

  auto end_time = std::chrono::steady_clock::now();
  double elapsed_s = std::chrono::duration<double>(end_time - start_time).count();

  print_report(all_stats, config, elapsed_s);

  return 0;
}
