#!/usr/bin/env python3
# Concurrent 4KB PUT load generator against a vstart RGW (localhost:8000).
# Uses the s3tests.conf.SAMPLE main user that vstart creates.
#
#   pip install boto3   (or reuse the s3-tests venv)
#   python3 loadgen_4k_put.py --threads 64 --seconds 60
import argparse
import os
import threading
import time

import boto3
from botocore.config import Config

ENDPOINT = os.environ.get("RGW_ENDPOINT", "http://localhost:8000")
ACCESS = os.environ.get("RGW_ACCESS", "0555b35654ad1656d804")
SECRET = os.environ.get("RGW_SECRET",
                        "h7GhxuBLTrlhVUyxSPUKUV8r/2EI4ngqJxD7iBdBYLhwluN30JaT3Q==")
BUCKET = "perf-4k-put"
PAYLOAD = os.urandom(4096)

counts = []
stop = threading.Event()


def make_client():
    return boto3.client("s3", endpoint_url=ENDPOINT,
                        aws_access_key_id=ACCESS, aws_secret_access_key=SECRET,
                        config=Config(max_pool_connections=4,
                                      retries={"max_attempts": 1}))


def worker(tid: int):
    client = make_client()
    n = 0
    while not stop.is_set():
        client.put_object(Bucket=BUCKET, Key=f"t{tid}-{n}", Body=PAYLOAD)
        n += 1
    counts[tid] = n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()

    c = make_client()
    try:
        c.create_bucket(Bucket=BUCKET)
    except c.exceptions.BucketAlreadyOwnedByYou:
        pass

    counts.extend([0] * args.threads)
    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(args.threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    time.sleep(args.seconds)
    stop.set()
    for t in threads:
        t.join()
    dt = time.time() - t0
    total = sum(counts)
    print(f"threads={args.threads} seconds={dt:.1f} puts={total} "
          f"put/s={total / dt:.0f}")


if __name__ == "__main__":
    main()
