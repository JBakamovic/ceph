// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab

#include <gtest/gtest.h>
#include <atomic>
#include <chrono>
#include <memory>
#include <thread>
#include <boost/asio/io_context.hpp>

#include "common/ceph_context.h"
#include "common/config.h"
#include "common/shunique_lock.h"
#include "global/global_init.h"
#include "osdc/Objecter.h"
#include "osd/osd_types.h"

using namespace std::chrono_literals;

class TestObjecter : public Objecter {
public:
  TestObjecter(CephContext *cct, boost::asio::io_context& io)
    : Objecter(cct, nullptr, nullptr, io, "test_objecter") {
    init();
    set_balanced_budget();
  }

  ~TestObjecter() override {
    shutdown();
  }

  int take_op_budget_test(Op *op) {
    ceph::shunique_lock sul(rwlock, ceph::acquire_unique);
    return _take_op_budget(op, sul);
  }

  void put_op_budget_test(int budget, int64_t pool_id) {
    put_op_budget_bytes(budget, pool_id);
  }

  void prune_pool_throttles_test(const mempool::osdmap::map<int64_t, pg_pool_t>& pools) {
    prune_pool_throttles(pools);
  }

  bool has_pool_throttle_test(int64_t pool_id) const {
    std::lock_guard l(pool_throttle_lock);
    return pool_throttles.count(pool_id) > 0;
  }

  size_t pool_throttle_count_test() const {
    std::lock_guard l(pool_throttle_lock);
    return pool_throttles.size();
  }

  std::shared_ptr<PoolThrottle> get_pool_throttle_test(int64_t pool_id) {
    return _get_pool_throttle(pool_id);
  }

  Throttle& get_global_ops_throttle() {
    return op_throttle_ops;
  }

  Throttle& get_global_bytes_throttle() {
    return op_throttle_bytes;
  }
};

class ObjecterPoolThrottleTest : public ::testing::Test {
protected:
  boost::asio::io_context io;
  std::unique_ptr<TestObjecter> objecter;

  void SetUp() override {
    g_ceph_context->_conf.set_val("objecter_pool_throttle_enable", "true");
    g_ceph_context->_conf.set_val("objecter_inflight_ops", "4");
    g_ceph_context->_conf.set_val("objecter_pool_inflight_ops_ratio", "0.5");
    g_ceph_context->_conf.apply_changes(nullptr);

    objecter = std::make_unique<TestObjecter>(g_ceph_context, io);
  }

  void TearDown() override {
    objecter.reset();
  }

  Objecter::Op* make_test_op(int64_t pool_id, const std::string& name = "test_obj") {
    object_locator_t oloc(pool_id);
    return new Objecter::Op(object_t(name), oloc, {}, 0, (Context*)nullptr, nullptr);
  }
};

TEST_F(ObjecterPoolThrottleTest, BasicPoolIsolation) {
  // Config: global inflight ops = 4, ratio = 0.5 -> max 2 ops per pool.
  auto pt1 = objecter->get_pool_throttle_test(1);
  auto pt2 = objecter->get_pool_throttle_test(2);
  ASSERT_NE(pt1, nullptr);
  ASSERT_NE(pt2, nullptr);
  EXPECT_EQ(pt1->ops.get_max(), 2);
  EXPECT_EQ(pt2->ops.get_max(), 2);

  // Take 2 ops for pool 1 (reaches pool 1 limit)
  auto op1_1 = make_test_op(1, "p1_obj1");
  auto op1_2 = make_test_op(1, "p1_obj2");
  int b1 = objecter->take_op_budget_test(op1_1);
  int b2 = objecter->take_op_budget_test(op1_2);

  EXPECT_EQ(pt1->ops.get_current(), 2);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 2);
  EXPECT_FALSE(pt1->ops.get_or_fail(1)); // Pool 1 is saturated

  // Submitting to pool 2 succeeds immediately without waiting
  auto op2_1 = make_test_op(2, "p2_obj1");
  int b2_1 = objecter->take_op_budget_test(op2_1);
  EXPECT_EQ(pt2->ops.get_current(), 1);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 3);

  // Return budgets
  objecter->put_op_budget_test(b1, 1);
  EXPECT_EQ(pt1->ops.get_current(), 1);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 2);

  objecter->put_op_budget_test(b2, 1);
  EXPECT_EQ(pt1->ops.get_current(), 0);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 1);

  objecter->put_op_budget_test(b2_1, 2);
  EXPECT_EQ(pt2->ops.get_current(), 0);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 0);

  op1_1->put();
  op1_2->put();
  op2_1->put();
}

TEST_F(ObjecterPoolThrottleTest, BlockingOnSaturatedPoolDoesNotBlockOtherPool) {
  // Scribe 2 ops to pool 1
  auto op1_1 = make_test_op(1, "p1_obj1");
  auto op1_2 = make_test_op(1, "p1_obj2");
  int b1 = objecter->take_op_budget_test(op1_1);
  int b2 = objecter->take_op_budget_test(op1_2);

  std::atomic<bool> thread_started{false};
  std::atomic<bool> thread_completed{false};
  auto op1_3 = make_test_op(1, "p1_obj3");

  // Thread 1 attempts to submit a 3rd op to pool 1 and blocks
  std::thread blocked_thread([&]() {
    thread_started = true;
    objecter->take_op_budget_test(op1_3);
    thread_completed = true;
  });

  while (!thread_started) {
    std::this_thread::yield();
  }
  std::this_thread::sleep_for(50ms);
  EXPECT_FALSE(thread_completed.load()); // Thread must be blocked waiting on pool 1 throttle

  // Main thread submits to pool 2 without blocking
  auto op2_1 = make_test_op(2, "p2_obj1");
  int b2_1 = objecter->take_op_budget_test(op2_1);
  EXPECT_EQ(objecter->get_pool_throttle_test(2)->ops.get_current(), 1);
  EXPECT_FALSE(thread_completed.load()); // Pool 1 thread is still blocked

  // Unblock pool 1 by returning an op budget
  objecter->put_op_budget_test(b1, 1);

  blocked_thread.join();
  EXPECT_TRUE(thread_completed.load());
  EXPECT_EQ(objecter->get_pool_throttle_test(1)->ops.get_current(), 2);

  // Clean up
  objecter->put_op_budget_test(b2, 1);
  objecter->put_op_budget_test(0, 1); // for op1_3
  objecter->put_op_budget_test(b2_1, 2);

  op1_1->put();
  op1_2->put();
  op1_3->put();
  op2_1->put();
}

TEST_F(ObjecterPoolThrottleTest, GlobalThrottleCap) {
  // Global cap = 4. Pool 1 takes 2, Pool 2 takes 2 -> Global is saturated at 4.
  auto op1_1 = make_test_op(1, "p1_1");
  auto op1_2 = make_test_op(1, "p1_2");
  auto op2_1 = make_test_op(2, "p2_1");
  auto op2_2 = make_test_op(2, "p2_2");

  int b1_1 = objecter->take_op_budget_test(op1_1);
  int b1_2 = objecter->take_op_budget_test(op1_2);
  int b2_1 = objecter->take_op_budget_test(op2_1);
  int b2_2 = objecter->take_op_budget_test(op2_2);

  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 4);
  EXPECT_FALSE(objecter->get_global_ops_throttle().get_or_fail(1));

  // Pool 3 has 0 ops, but global is saturated
  EXPECT_FALSE(objecter->get_global_ops_throttle().get_or_fail(1));

  // Release 1 op on Pool 1
  objecter->put_op_budget_test(b1_1, 1);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 3);

  // Now Pool 3 can submit an op
  auto op3_1 = make_test_op(3, "p3_1");
  int b3_1 = objecter->take_op_budget_test(op3_1);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 4);

  // Clean up
  objecter->put_op_budget_test(b1_2, 1);
  objecter->put_op_budget_test(b2_1, 2);
  objecter->put_op_budget_test(b2_2, 2);
  objecter->put_op_budget_test(b3_1, 3);

  op1_1->put();
  op1_2->put();
  op2_1->put();
  op2_2->put();
  op3_1->put();
}

TEST_F(ObjecterPoolThrottleTest, DynamicRatioReconfiguration) {
  auto pt = objecter->get_pool_throttle_test(1);
  EXPECT_EQ(pt->ops.get_max(), 2); // 4 * 0.5 = 2

  // Change ratio to 0.75 dynamically -> 4 * 0.75 = 3 ops
  g_ceph_context->_conf.set_val("objecter_pool_inflight_ops_ratio", "0.75");
  g_ceph_context->_conf.apply_changes(nullptr);

  // Re-queried or updated throttle reflects new ceiling
  auto pt_updated = objecter->get_pool_throttle_test(1);
  EXPECT_EQ(pt_updated->ops.get_max(), 3);

  auto pt_new = objecter->get_pool_throttle_test(2);
  EXPECT_EQ(pt_new->ops.get_max(), 3);
}

TEST_F(ObjecterPoolThrottleTest, PruneDeletedPoolThrottles) {
  // Populate throttles for Pool 10 and Pool 20
  auto pt10 = objecter->get_pool_throttle_test(10);
  auto pt20 = objecter->get_pool_throttle_test(20);
  EXPECT_TRUE(objecter->has_pool_throttle_test(10));
  EXPECT_TRUE(objecter->has_pool_throttle_test(20));

  // Simulate OSDMap where Pool 10 was deleted and only Pool 20 remains
  mempool::osdmap::map<int64_t, pg_pool_t> active_pools;
  active_pools[20] = pg_pool_t();

  objecter->prune_pool_throttles_test(active_pools);

  // Pool 10 must be pruned; Pool 20 must remain
  EXPECT_FALSE(objecter->has_pool_throttle_test(10));
  EXPECT_TRUE(objecter->has_pool_throttle_test(20));

  // Test in-flight op holding budget on deleted pool
  auto op30 = make_test_op(30, "p30_obj");
  int b30 = objecter->take_op_budget_test(op30);
  EXPECT_TRUE(objecter->has_pool_throttle_test(30));

  // Pruning while ops are in-flight preserves throttle until drained
  objecter->prune_pool_throttles_test(active_pools);
  EXPECT_TRUE(objecter->has_pool_throttle_test(30));

  // Returning budget allows subsequent prune to clean it up
  objecter->put_op_budget_test(b30, 30);
  objecter->prune_pool_throttles_test(active_pools);
  EXPECT_FALSE(objecter->has_pool_throttle_test(30));

  op30->put();
}

TEST_F(ObjecterPoolThrottleTest, DisabledThrottleBehavior) {
  g_ceph_context->_conf.set_val("objecter_pool_throttle_enable", "false");
  g_ceph_context->_conf.apply_changes(nullptr);

  auto op = make_test_op(100, "disabled_obj");
  int b = objecter->take_op_budget_test(op);

  // Per-pool throttle should not be allocated
  EXPECT_EQ(objecter->pool_throttle_count_test(), 0);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 1);

  objecter->put_op_budget_test(b, 100);
  EXPECT_EQ(objecter->get_global_ops_throttle().get_current(), 0);

  op->put();
}
