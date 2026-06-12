// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab ft=cpp

#ifndef CEPH_CONCEPTS_H
 #define CEPH_CONCEPTS_H

/*
 * Ceph - scalable distributed file system
 *
 * Copyright (C) 2026 International Business Machines
 *
 * This is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License version 2.1, as published by the Free Software
 * Foundation.  See file COPYING.
 *
 */

#include <ranges>
#include <cstddef>
#include <utility>
#include <concepts>
#include <iterator>
#include <algorithm>
#include <type_traits>

/* A helpful collection of C++ Concepts that aren't available in the C++23
* standard. The goal here is to provide common and practical concepts and
* capability queries.
*
* Some of the material is derived from:
*  - https://eel.is/c++draft/range.utility.conv
*  - https://en.cppreference.com/w/cpp/ranges/to
*
* The facilities provided by this mini-library include:
*   - some Concepts not in C++ (such as associative_container and unordered_container);
*   - a local name for some exposition-only C++ concepts used by the standard (such as
*   container_compatible_range);
*
*   - capability query predicates, which make metaprogramming tasks more straightforward
*   and are very useful in a variety of algorithms;
*
*   - end()/begin() style function forms for some container operations (like clear());
*
*   - utility functions for capturing common cases like optionally clearing a container
*   based on whether or not the operation is supported by its concrete type;
*
*   Note that a number of the things you might wish were in here are already in implemented
*   in the Standard Library-- as a guide (depending on your mental model!) this may be helpful:
*   - ceph::concepts::sequence -> std::ranges::input_range
*   - ceph::concepts::contiguous_sequence -> std::ranges::contiguous_range
*   - ceph::util::erase_if -> std::erase_if
*   - ceph::util::size -> std::ranges::size
*   - ceph::util::empty -> std::ranges::empty
*
* Capability predicates such as can_erase_if and has_size are not direct
* operation wrappers; keep them when they make generic constraints clearer.
*
* This header also keeps local names for useful C++23 exposition-only
* constraints from ranges::to, including reservable_container and
* container_appendable.
*/

// General Concepts:
namespace ceph::concepts {

template <typename ContainerT>
concept associative_container = requires(ContainerT& c, const ContainerT& cc,
                                         const typename ContainerT::key_type& key) {
  requires std::ranges::range<ContainerT>;

  typename ContainerT::key_type;
  typename ContainerT::value_type;
  typename ContainerT::key_compare;
  typename ContainerT::value_compare;
  typename ContainerT::iterator;
  typename ContainerT::const_iterator;
  typename ContainerT::size_type;

  { cc.key_comp() } -> std::same_as<typename ContainerT::key_compare>;
  { cc.value_comp() } -> std::same_as<typename ContainerT::value_compare>;
  { c.find(key) } -> std::same_as<typename ContainerT::iterator>;
  { cc.find(key) } -> std::same_as<typename ContainerT::const_iterator>;
  { c.count(key) } -> std::convertible_to<typename ContainerT::size_type>;
  { c.lower_bound(key) } -> std::same_as<typename ContainerT::iterator>;
  { cc.lower_bound(key) } -> std::same_as<typename ContainerT::const_iterator>;
  { c.upper_bound(key) } -> std::same_as<typename ContainerT::iterator>;
  { cc.upper_bound(key) } -> std::same_as<typename ContainerT::const_iterator>;
  { c.equal_range(key) };
  { cc.equal_range(key) };
};

template <typename ContainerT>
concept unordered_container = requires(ContainerT& c, const ContainerT& cc,
                                       const typename ContainerT::key_type& key) {
  requires std::ranges::range<ContainerT>;

  typename ContainerT::key_type;
  typename ContainerT::value_type;
  typename ContainerT::hasher;
  typename ContainerT::key_equal;
  typename ContainerT::iterator;
  typename ContainerT::const_iterator;
  typename ContainerT::size_type;

  { cc.hash_function() } -> std::same_as<typename ContainerT::hasher>;
  { cc.key_eq() } -> std::same_as<typename ContainerT::key_equal>;
  { c.find(key) } -> std::same_as<typename ContainerT::iterator>;
  { cc.find(key) } -> std::same_as<typename ContainerT::const_iterator>;
  { c.count(key) } -> std::convertible_to<typename ContainerT::size_type>;
  { c.equal_range(key) };
  { cc.equal_range(key) };
  { c.bucket_count() } -> std::convertible_to<typename ContainerT::size_type>;
};

template <typename RangeT, typename T>
concept container_compatible_range =
  std::ranges::input_range<RangeT> && std::convertible_to<std::ranges::range_reference_t<RangeT>, T>;

template <typename ContainerT>
concept reservable_container =
  std::ranges::sized_range<ContainerT> &&
  requires(ContainerT& c, std::ranges::range_size_t<ContainerT> n) {
    c.reserve(n);
    { c.capacity() } -> std::same_as<decltype(n)>;
    { c.max_size() } -> std::same_as<decltype(n)>;
  };

template <typename ContainerT, typename RefT>
concept container_appendable = requires(ContainerT& c, RefT&& ref) {
  requires
    requires { c.emplace_back(std::forward<RefT>(ref)); } ||
    requires { c.push_back(std::forward<RefT>(ref)); } ||
    requires { { c.emplace(c.end(), std::forward<RefT>(ref)) } -> std::same_as<decltype(c.end())>; } ||
    requires { c.insert(c.end(), std::forward<RefT>(ref)); };
};

} // namespace ceph::concepts

// Capability Queries:
namespace ceph::concepts {

template <typename ContainerT, typename... ArgsT>
concept has_emplace_back = requires(ContainerT& c, ArgsT&&... args) {
  c.emplace_back(std::forward<ArgsT>(args)...);
};

template <typename ContainerT, typename T>
concept has_push_back = requires(ContainerT& c, T&& v) {
  c.push_back(std::forward<T>(v));
};

template <typename ContainerT, typename T>
concept has_emplace_append = requires(ContainerT& c, T&& v) {
  { c.emplace(std::end(c), std::forward<T>(v)) } -> std::same_as<typename ContainerT::iterator>;
};

template <typename ContainerT, typename T>
concept has_insert_append = requires(ContainerT& c, T&& v) {
  c.insert(std::end(c), std::forward<T>(v));
};

template <typename ContainerT, typename RangeT>
concept has_append_range = requires(ContainerT& c, RangeT&& range) {
  c.append_range(std::forward<RangeT>(range));
};

template <typename ContainerT, typename RangeT>
concept has_insert_range_append = requires(ContainerT& c, RangeT&& range) {
  c.insert_range(std::end(c), std::forward<RangeT>(range));
};

template <typename ContainerT, typename RangeT>
concept has_insert_iterator_range_append = requires(ContainerT& c, RangeT&& range) {
  requires std::ranges::input_range<RangeT>;

  c.insert(std::end(c), std::begin(range), std::end(range));
};

template <typename ContainerT, typename IteratorT, typename RangeT>
concept has_insert_range = requires(ContainerT& c, IteratorT pos, RangeT&& range) {
  c.insert_range(pos, std::forward<RangeT>(range));
};

template <typename ContainerT, typename IteratorT, typename RangeT>
concept has_insert_iterator_range = requires(ContainerT& c, IteratorT pos, RangeT&& range) {
  requires std::ranges::input_range<RangeT>;

  c.insert(pos, std::begin(range), std::end(range));
};

template <typename ContainerT, typename T>
concept has_emplace_front = requires(ContainerT& c, T&& v) {
  c.emplace_front(std::forward<T>(v));
};

template <typename ContainerT, typename T>
concept has_push_front = requires(ContainerT& c, T&& v) {
  c.push_front(std::forward<T>(v));
};

template <typename ContainerT, typename T>
concept has_emplace_at_begin = requires(ContainerT& c, T&& v) {
  { c.emplace(std::begin(c), std::forward<T>(v)) } -> std::same_as<typename ContainerT::iterator>;
};

template <typename ContainerT, typename T>
concept has_insert_at_begin = requires(ContainerT& c, T&& v) {
  c.insert(std::begin(c), std::forward<T>(v));
};

template <typename ContainerT>
concept has_pop_front = requires(ContainerT& c) {
  c.pop_front();
};

template <typename ContainerT>
concept has_erase_begin = requires(ContainerT& c) {
  c.erase(std::begin(c));
};

template <typename ContainerT, typename ArgT>
concept has_erase = requires(ContainerT& c, ArgT&& v) {
  c.erase(std::forward<ArgT>(v));
};

template <typename ContainerT, typename IteratorT, typename SentinelT>
concept has_erase_range = requires(ContainerT& c, IteratorT first, SentinelT last) {
  c.erase(first, last);
};

template <typename ContainerT, typename PredicateT>
concept has_remove_if = requires(ContainerT& c, PredicateT pred) {
  c.remove_if(pred);
};

template <typename ContainerT>
concept has_clear = requires(ContainerT& c) {
  c.clear();
};

template <typename ContainerT>
concept has_reserve = requires(ContainerT& c, std::size_t n) {
  c.reserve(n);
};

template <typename ContainerT>
concept has_capacity = requires(const ContainerT& c) {
  { c.capacity() } -> std::convertible_to<std::size_t>;
};

template <typename ContainerT>
concept has_max_size = requires(const ContainerT& c) {
  { c.max_size() } -> std::convertible_to<std::size_t>;
};

template <typename ContainerT>
concept has_size = requires(const ContainerT& c) {
  { c.size() } -> std::convertible_to<std::size_t>;
};

template <typename ContainerT>
concept has_empty = requires(const ContainerT& c) {
  { c.empty() } -> std::convertible_to<bool>;
};

template <typename ContainerT>
concept has_resize = requires(ContainerT& c, std::size_t n) {
  c.resize(n);
};

} // namespace ceph::concepts

// Helpers for composing aggregate capability checks:
namespace ceph::concepts::detail {

template <typename ContainerT, typename RangeT>
concept can_append_with_insert =
  ceph::concepts::has_insert_range_append<ContainerT, RangeT> || ceph::concepts::has_insert_iterator_range_append<ContainerT, RangeT>;

} // namespace ceph::concepts::detail

// Aggregate Capability Checks:
namespace ceph::concepts {

template <typename ContainerT, typename T>
concept can_append =
  container_appendable<ContainerT, T>;

template <typename ContainerT, typename RangeT>
concept can_append_range =
  std::ranges::input_range<RangeT> &&
  (has_append_range<ContainerT, RangeT> ||
   detail::can_append_with_insert<ContainerT, RangeT> ||
   can_append<ContainerT, std::ranges::range_reference_t<RangeT>>);

template <typename ContainerT, typename IteratorT, typename RangeT>
concept can_insert_any_range =
  has_insert_range<ContainerT, IteratorT, RangeT> || has_insert_iterator_range<ContainerT, IteratorT, RangeT>;

template <typename ContainerT, typename T>
concept can_prepend =
  has_emplace_front<ContainerT, T> ||
  has_push_front<ContainerT, T> ||
  has_emplace_at_begin<ContainerT, T> ||
  has_insert_at_begin<ContainerT, T>;

template <typename ContainerT, typename PredicateT>
concept can_erase_remove_if = requires(ContainerT& c, PredicateT pred) {
  requires std::ranges::common_range<ContainerT>;
  requires has_erase_range<ContainerT,
                           std::ranges::iterator_t<ContainerT>,
                           std::ranges::sentinel_t<ContainerT>>;

  std::remove_if(std::begin(c), std::end(c), pred);
};

template <typename ContainerT, typename PredicateT>
concept can_erase_if =
  has_remove_if<ContainerT, PredicateT> || can_erase_remove_if<ContainerT, PredicateT>;

template <typename ContainerT>
concept can_remove_front =
  has_pop_front<ContainerT> || has_erase_begin<ContainerT>;

} // namespace ceph::concepts

// Helpers:
//  - most of these try to "do the right thing" with the underlying container
//  as-appropriate; use some caution in performance-sensitive situations (although
//  the answer may be far from cut and dried as cache locality and other features
//  of modern CPUs can produce extremely counterintuitive results vis-a-vis O(n),
//  n may need to be surprisingly large before inserting at the head of a list is
//  *actually* faster than shifting an array, for instance):
namespace ceph::util {

template <typename ContainerT, typename T>
requires ceph::concepts::can_append<ContainerT, T>
constexpr void push_back(ContainerT& c, T&& v)
{
  if constexpr (ceph::concepts::has_emplace_back<ContainerT, T>) {
    c.emplace_back(std::forward<T>(v));
    return;
  }

  if constexpr (ceph::concepts::has_push_back<ContainerT, T>) {
    c.push_back(std::forward<T>(v));
    return;
  }

  if constexpr (ceph::concepts::has_emplace_append<ContainerT, T>) {
    c.emplace(std::end(c), std::forward<T>(v));
    return;
  }

  if constexpr (ceph::concepts::has_insert_append<ContainerT, T>) {
    c.insert(std::end(c), std::forward<T>(v));
    return;
  }
}

// Note: emplace_back() can return a reference, hence decltype(auto):
template <typename ContainerT, typename... ArgsT>
constexpr decltype(auto) emplace_back(ContainerT& c, ArgsT&&... args)
{
  return c.emplace_back(std::forward<ArgsT>(args)...);
}

template <typename ContainerT, typename RangeT>
requires ceph::concepts::can_append_range<ContainerT, RangeT>
constexpr void append_range(ContainerT& c, RangeT&& range)
{
  if constexpr (ceph::concepts::has_append_range<ContainerT, RangeT>) {
    c.append_range(std::forward<RangeT>(range));
    return;
  }

  if constexpr (ceph::concepts::has_insert_range_append<ContainerT, RangeT>) {
    c.insert_range(std::end(c), std::forward<RangeT>(range));
    return;
  }

  if constexpr (ceph::concepts::has_insert_iterator_range_append<ContainerT, RangeT>) {
    c.insert(std::end(c), std::begin(range), std::end(range));
    return;
  }

  if constexpr (ceph::concepts::can_append<ContainerT, std::ranges::range_reference_t<RangeT>>) {
    for (auto&& v : range) {
      push_back(c, std::forward<decltype(v)>(v));
    }
    return;
  }
}

template <typename ContainerT, typename IteratorT, typename RangeT>
requires ceph::concepts::can_insert_any_range<ContainerT, IteratorT, RangeT>
constexpr void insert_range(ContainerT& c, IteratorT pos, RangeT&& range)
{
  if constexpr (ceph::concepts::has_insert_range<ContainerT, IteratorT, RangeT>) {
    c.insert_range(pos, std::forward<RangeT>(range));
    return;
  }

  if constexpr (ceph::concepts::has_insert_iterator_range<ContainerT, IteratorT, RangeT>) {
    c.insert(pos, std::begin(range), std::end(range));
    return;
  }
}

template <typename ContainerT, typename ArgT>
constexpr auto erase(ContainerT& c, ArgT&& v)
{
  return c.erase(std::forward<ArgT>(v));
}

template <typename ContainerT, typename IteratorT, typename SentinelT>
constexpr auto erase(ContainerT& c, IteratorT first, SentinelT last)
{
  return c.erase(first, last);
}

template <typename ContainerT, typename T>
requires ceph::concepts::can_prepend<ContainerT, T>
constexpr void push_front(ContainerT& c, T&& v)
{
  if constexpr (ceph::concepts::has_emplace_front<ContainerT, T>) {
    c.emplace_front(std::forward<T>(v));
    return;
  }

  if constexpr (ceph::concepts::has_push_front<ContainerT, T>) {
    c.push_front(std::forward<T>(v));
    return;
  }

  if constexpr (ceph::concepts::has_emplace_at_begin<ContainerT, T>) {
    c.emplace(std::begin(c), std::forward<T>(v));
    return;
  }

  if constexpr (ceph::concepts::has_insert_at_begin<ContainerT, T>) {
    c.insert(std::begin(c), std::forward<T>(v));
    return;
  }
}

template <typename ContainerT>
requires ceph::concepts::can_remove_front<ContainerT>
constexpr void pop_front(ContainerT& c)
{
  if constexpr (ceph::concepts::has_pop_front<ContainerT>) {
    c.pop_front();
    return;
  }

  if constexpr (ceph::concepts::has_erase_begin<ContainerT>) {
    c.erase(std::begin(c));
    return;
  }
}

template <typename ContainerT>
constexpr std::size_t capacity(const ContainerT& c)
{
  return c.capacity();
}

template <typename ContainerT>
constexpr std::size_t max_size(const ContainerT& c)
{
  return c.max_size();
}

template <typename ContainerT>
constexpr void resize(ContainerT& c, std::size_t n)
{
  c.resize(n);
}

template <typename ContainerT>
constexpr void clear(ContainerT& c)
{
  c.clear();
}

// It's often handy to be able to easily resize if possible,
// but continue along either way-- so, here we are:
template <typename ContainerT>
constexpr void maybe_resize(ContainerT& c, std::size_t n)
{
  if constexpr (ceph::concepts::has_resize<ContainerT>) {
    c.resize(n);
  }
}

// reserve() also is frequently "nice to have" in some algorithms:
template <typename ContainerT>
constexpr void maybe_reserve(ContainerT& c, std::size_t n)
{
  if constexpr (ceph::concepts::has_reserve<ContainerT>) {
    c.reserve(n);
  }
}


// JFW: note: I haven't gotten to the associative-array specific versions of these, but
// in practice I think they're less often-used.

} // namespace ceph::util

#endif
