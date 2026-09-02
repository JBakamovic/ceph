// Microbenchmark for the EVP_MD_fetch() caching change in
// common/ceph_crypto.cc (wip-common-crypto-cache-evp-md-fetch).
//
// Simulates rgw's per-PUT etag pattern: one MD5 digest instance per
// request over a 4KB payload. Mode "fetch" reproduces the old behavior
// (EVP_MD_fetch per digest instance, as OpenSSLDigest::SetFlags did);
// mode "cached" reproduces the new behavior (process-wide fetch once +
// EVP_MD_up_ref per instance); mode "implicit" is the FIPS-off baseline
// (EVP_md5() legacy static, no explicit fetch).
//
// Build:
//   g++ -O2 -std=c++17 -pthread bench_evp_md_fetch.cc -lcrypto -o bench_evp_md_fetch
// Run:
//   ./bench_evp_md_fetch <fetch|cached|implicit> <threads> [iters_per_thread]
// Suggested matrix: each mode x threads in {1, 8, 32}, e.g.
//   for m in fetch cached implicit; do for t in 1 8 32; do ./bench_evp_md_fetch $m $t; done; done

#include <openssl/evp.h>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

static constexpr size_t PAYLOAD = 4096;

static const EVP_MD* get_cached_md5() {
  static EVP_MD* const md = EVP_MD_fetch(nullptr, "MD5", "fips=no");
  return md;
}

enum class Mode { fetch, cached, implicit };

static void worker(Mode mode, long iters, const unsigned char* payload,
                   unsigned char* sink) {
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int dlen = 0;
  for (long i = 0; i < iters; i++) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();          // per request, as rgw does
    EVP_MD* owned = nullptr;
    const EVP_MD* md = nullptr;
    switch (mode) {
    case Mode::fetch:                            // old: fetch per instance
      owned = EVP_MD_fetch(nullptr, "MD5", "fips=no");
      md = owned;
      break;
    case Mode::cached:                           // new: shared fetch + up_ref
      owned = const_cast<EVP_MD*>(get_cached_md5());
      EVP_MD_up_ref(owned);
      md = owned;
      break;
    case Mode::implicit:                         // baseline: legacy static
      md = EVP_md5();
      break;
    }
    EVP_DigestInit_ex(ctx, md, nullptr);
    EVP_DigestUpdate(ctx, payload, PAYLOAD);
    EVP_DigestFinal_ex(ctx, digest, &dlen);
    EVP_MD_CTX_free(ctx);
    if (owned) EVP_MD_free(owned);
    sink[0] ^= digest[0];                        // defeat dead-code elimination
  }
}

int main(int argc, char** argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: %s <fetch|cached|implicit> <threads> [iters]\n", argv[0]);
    return 1;
  }
  Mode mode;
  if (!strcmp(argv[1], "fetch")) mode = Mode::fetch;
  else if (!strcmp(argv[1], "cached")) mode = Mode::cached;
  else if (!strcmp(argv[1], "implicit")) mode = Mode::implicit;
  else { fprintf(stderr, "bad mode\n"); return 1; }

  const int nthreads = atoi(argv[2]);
  const long iters = argc > 3 ? atol(argv[3]) : 200000;

  std::vector<unsigned char> payload(PAYLOAD, 0xab);
  std::vector<unsigned char> sinks(nthreads);

  get_cached_md5();  // warm the cache outside the timed region

  auto t0 = std::chrono::steady_clock::now();
  std::vector<std::thread> threads;
  for (int t = 0; t < nthreads; t++)
    threads.emplace_back(worker, mode, iters, payload.data(), &sinks[t]);
  for (auto& t : threads) t.join();
  auto t1 = std::chrono::steady_clock::now();

  double secs = std::chrono::duration<double>(t1 - t0).count();
  double total = double(iters) * nthreads;
  printf("%-8s threads=%-3d iters/thread=%ld  wall=%.3fs  ops/s=%.0f  ns/op=%.0f\n",
         argv[1], nthreads, iters, secs, total / secs, secs / total * 1e9);
  unsigned char acc = 0; for (auto s : sinks) acc ^= s;
  return acc == 255 ? 2 : 0;
}
