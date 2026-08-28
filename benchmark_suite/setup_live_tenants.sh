#!/bin/bash

BIN_DIR="/home/jbakamovic/development/build-ceph/release/bin"
CONF="/home/jbakamovic/development/build-ceph/release/ceph.conf"
ADMIN="$BIN_DIR/radosgw-admin -c $CONF"

echo "[*] Provisioning live S3 benchmark users..."

# 1. VIP Tenant
$ADMIN user create --uid=vip_tenant --display-name="VIP Enterprise Tenant" \
  --access-key=VIPACCESSKEY --secret-key=VIPSECRETKEY123456789 >/dev/null

# 2. Bully Tenant
$ADMIN user create --uid=bully_tenant --display-name="Bulk Ingestion Bully" \
  --access-key=BULLYACCESSKEY --secret-key=BULLYSECRETKEY123456789 >/dev/null

# 3. Health Monitor Tenant
$ADMIN user create --uid=health_monitor --display-name="Cluster Health Monitor" \
  --access-key=HEALTHACCESSKEY --secret-key=HEALTHSECRETKEY123456789 >/dev/null

# 4. Dynamic Tenants 01 to 08
for i in $(seq 1 8); do
  $ADMIN user create --uid="dynamic_$i" --display-name="Dynamic Tenant $i" \
    --access-key="DYNAMICKEY$i" --secret-key="DYNAMICSECRET$i" >/dev/null
done

# 5. Multi-Tiered QoS Personas
$ADMIN user create --uid=tier1_platinum --display-name="Tier 1 Platinum VIP" \
  --access-key=PLATINUMKEY --secret-key=PLATINUMSECRET123456789 >/dev/null

$ADMIN user create --uid=tier2_gold --display-name="Tier 2 Gold Standard" \
  --access-key=GOLDKEY --secret-key=GOLDSECRET123456789 >/dev/null

$ADMIN user create --uid=tier3_silver --display-name="Tier 3 Silver Standard" \
  --access-key=SILVERKEY --secret-key=SILVERSECRET123456789 >/dev/null

$ADMIN user create --uid=tier4_bronze --display-name="Tier 4 Bronze Standard" \
  --access-key=BRONZEKEY --secret-key=BRONZESECRET123456789 >/dev/null

$ADMIN user create --uid=tier5_free --display-name="Tier 5 Free Bulk" \
  --access-key=FREEKEY --secret-key=FREESECRET123456789 >/dev/null

# 6. Metadata vs Data Personas
$ADMIN user create --uid=metadata_crawler --display-name="Metadata Crawler" \
  --access-key=CRAWLERKEY --secret-key=CRAWLERSECRET123456789 >/dev/null

$ADMIN user create --uid=media_streamer --display-name="Media Streamer" \
  --access-key=STREAMERKEY --secret-key=STREAMERSECRET123456789 >/dev/null

$ADMIN user create --uid=mobile_client --display-name="Mobile Client" \
  --access-key=MOBILEKEY --secret-key=MOBILESECRET123456789 >/dev/null

# 7. Retry Storm Personas
$ADMIN user create --uid=batch_retry_client --display-name="Batch Client With Retries" \
  --access-key=BATCHRETRYKEY --secret-key=BATCHRETRYSECRET123456789 >/dev/null

$ADMIN user create --uid=api_retry_client --display-name="API Client With Retries" \
  --access-key=APIRETRYKEY --secret-key=APIRETRYSECRET123456789 >/dev/null

echo "[+] Successfully created all test users and credentials."
