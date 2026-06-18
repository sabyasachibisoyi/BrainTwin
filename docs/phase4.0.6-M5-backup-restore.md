# Phase 4.0.6 M.5 — Backup & restore (SQLite + Chroma)

**Purpose:** verify the M.5 deliverables — Litestream is streaming the
SQLite WAL to S3, the nightly Chroma tarball is uploading, and a real
restore drill against a fresh path produces an identical DB.

Total time: ~15 min for verification, ~25 min for the restore drill.

> **You are here:** the M.5 deploy is done and the EC2 has been
> replaced via the manual-unblock dance from §14.1. Now we prove the
> backups actually work.

---

## 0. Prereqs

- M.5 successfully deployed (`./scripts/deploy.sh` from `BrainTwinCDK/`
  completed, EC2 was terminated to break the EBS deadlock, new EC2
  came up cleanly)
- SSM Session Manager plugin installed (from M.3 runbook §0)
- The new EC2 has been up at least ~5 minutes so Litestream has had a
  chance to upload its first snapshot

### A note on `--profile braintwin`

Every `aws` call in this runbook needs the `braintwin` profile. The
easiest way to avoid trapping yourself is to export it once at the top
of your shell session:

```bash
export AWS_PROFILE=braintwin
export AWS_REGION=us-west-2
```

The commands below still pass `--profile braintwin --region us-west-2`
explicitly for clarity, but with the env vars set the flags are
redundant.

**Subshell gotcha:** `$(aws sts get-caller-identity ...)` runs in its
own subshell and needs its own `--profile` flag (or the env vars set
above). Forgetting it returns an empty account ID and the bucket name
resolves to `braintwin-state--us-west-2` → `NoSuchBucket`.

---

## 1. Verify Litestream is replicating

### 1.1 Container is running

```bash
aws ssm start-session --target <i-xxx> --profile braintwin --region us-west-2
# Inside:
sudo docker compose -f /etc/braintwin/docker-compose.yml ps
```

Expected: 4 containers, all `Up`. Look for `braintwin-litestream`.

### 1.2 SQLite is in WAL mode

```bash
sudo apt-get install -y sqlite3 2>/dev/null
sudo sqlite3 /var/lib/braintwin/data/braintwin.db "PRAGMA journal_mode;"
# Expected output: wal
```

If you see `delete` instead of `wal`, the M.5 backend change to `db.py`
didn't make it into the image. Rebuild + push + redeploy.

### 1.3 Litestream uploaded a snapshot

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --profile braintwin --query Account --output text)
aws s3 ls "s3://braintwin-state-${ACCOUNT_ID}-us-west-2/litestream/braintwin.db/" \
  --profile braintwin --region us-west-2 --recursive | head -20
```

Expected: at least a `generations/<id>/snapshots/` file plus
`generations/<id>/wal/` entries. The WAL frames roll over as the app
writes; the snapshot is captured hourly per our config.

### 1.4 Litestream container log

```bash
aws logs tail /braintwin/app --filter-pattern '?litestream ?replica ?wal' \
  --since 10m --profile braintwin --region us-west-2
```

Expected lines like:
```
litestream: replica wrote: name=s3 generation=… index=X size=Y
litestream: snapshot uploaded: generation=… index=X size=Y
```

---

## 2. Verify Chroma backup ran

The systemd timer fires at 03:30 UTC daily. If you've just deployed,
you can either wait or trigger one manually.

### 2.1 Force a run now (for testing)

```bash
sudo systemctl start braintwin-chroma-backup.service
sudo journalctl -u braintwin-chroma-backup.service --since "5 minutes ago"
```

Expected lines:
```
braintwin-chroma-backup: starting
Tarball /tmp/chroma-… (N bytes)
braintwin-chroma-backup: done
```

### 2.2 Confirm upload to S3

```bash
# (uses $ACCOUNT_ID exported in §1.3 — if you started a fresh shell, re-run that line)
aws s3 ls "s3://braintwin-state-${ACCOUNT_ID}-us-west-2/chroma-nightly/" \
  --profile braintwin --region us-west-2
```

Expected: one or more `chroma-YYYYMMDD-HHMMSS.tar.gz` objects.

### 2.3 Verify the lifecycle rule

```bash
aws s3api get-bucket-lifecycle-configuration \
  --bucket "braintwin-state-${ACCOUNT_ID}-us-west-2" \
  --profile braintwin --region us-west-2 \
  --query 'Rules[?Prefix==`chroma-nightly/`].[Id,Expiration.Days]' --output text
```

Expected: `chroma-nightly-expire-7d  7`

### 2.4 Verify the systemd timer is enabled

```bash
sudo systemctl list-timers braintwin-chroma-backup.timer
```

Expected: shows next run time (next 03:30 UTC).

---

## 3. RESTORE DRILL — the portfolio sentence

This is the part that actually matters. Anyone can write a backup; the
question is "can you actually restore?"

### 3.1 Note current row count

```bash
# Inside SSM
COUNT_BEFORE=$(sudo sqlite3 /var/lib/braintwin/data/braintwin.db "SELECT COUNT(*) FROM captures;")
echo "Captures before: $COUNT_BEFORE"
```

Save this number — you'll compare against it after restore.

### 3.2 Stop the app + bot to release the SQLite file

Litestream keeps streaming; app + bot need to release the file handle
so we can move it aside.

```bash
sudo docker compose -f /etc/braintwin/docker-compose.yml stop app bot
# litestream stays running — it'll see the file go away momentarily
# and resume when the new file appears
```

### 3.3 Move the existing DB aside (simulate loss)

```bash
sudo mv /var/lib/braintwin/data/braintwin.db /var/lib/braintwin/data/braintwin.db.orig
sudo mv /var/lib/braintwin/data/braintwin.db-wal /var/lib/braintwin/data/braintwin.db-wal.orig 2>/dev/null || true
sudo mv /var/lib/braintwin/data/braintwin.db-shm /var/lib/braintwin/data/braintwin.db-shm.orig 2>/dev/null || true
```

### 3.4 Time the restore

```bash
# Use Litestream's restore command via the container so we don't need
# litestream installed on the host.
date +%s > /tmp/restore-start
sudo docker run --rm \
  -v /var/lib/braintwin/data:/data \
  -v /etc/braintwin/litestream.yml:/etc/litestream.yml:ro \
  litestream/litestream:0.3.13 \
  restore -config /etc/litestream.yml /data/braintwin.db
date +%s > /tmp/restore-end
ELAPSED=$(( $(cat /tmp/restore-end) - $(cat /tmp/restore-start) ))
echo "Restore took ${ELAPSED}s"
```

**Record this number in this runbook (§4 below).**

### 3.5 Verify row count matches

```bash
COUNT_AFTER=$(sudo sqlite3 /var/lib/braintwin/data/braintwin.db "SELECT COUNT(*) FROM captures;")
echo "Captures after restore: $COUNT_AFTER"
echo "Captures before: $COUNT_BEFORE"
test "$COUNT_BEFORE" = "$COUNT_AFTER" && echo "MATCH ✓" || echo "MISMATCH ✗"
```

A clean restore matches exactly. If you've been writing during the
drill (Chrome captures or Telegram forwards), the restore should be
within a few rows because Litestream's RPO is ~1s. Any larger gap is
worth investigating.

### 3.6 Restart the app

```bash
sudo docker compose -f /etc/braintwin/docker-compose.yml start app bot
# Wait for healthcheck
sleep 60
sudo docker compose -f /etc/braintwin/docker-compose.yml ps
# braintwin-app should be Up (healthy)
```

Smoke check:

```bash
curl -fsS http://127.0.0.1:8000/health
# {"status":"ok","vision_api_configured":true}
```

### 3.7 Clean up the `.orig` files

```bash
sudo rm -f /var/lib/braintwin/data/braintwin.db.orig{,-wal,-shm}
```

---

## 4. Recorded timings

| Drill date | Captures | Restore time | DB size | Notes |
|---|---|---|---|---|
| YYYY-MM-DD | ___ | ___ s | ___ MB | first drill, baseline |

Fill this in after running §3. Repeat every quarter or when the DB
crosses a 10x size threshold.

---

## 5. M.5 acceptance checklist

- [ ] `braintwin-litestream` container is `Up`
- [ ] SQLite reports `journal_mode = wal`
- [ ] S3 `litestream/braintwin.db/` contains snapshots + WAL frames
- [ ] CloudWatch agent is sending metrics (check CloudWatch console →
      Metrics → `BrainTwin/System` namespace — `cpu_usage_idle`, etc.)
- [ ] `braintwin-chroma-backup.timer` is enabled
- [ ] Manual `systemctl start braintwin-chroma-backup.service` uploads
      a tarball to `s3://.../chroma-nightly/`
- [ ] Restore drill produces a row-count match
- [ ] Restore timing recorded in §4

---

## 6. Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `PRAGMA journal_mode` returns `delete` | M.5 backend change didn't land in the image | Rebuild + push: `./scripts/build-and-push.sh` then `./scripts/deploy.sh` |
| Litestream logs `attempt to write a readonly database` | Litestream container runs as root, but `cap_drop: ALL` removed `CAP_DAC_OVERRIDE` — so root can't write the DB owned by UID 10001 | Already fixed in source: the compose Litestream service has `user: "10001:10001"`. If the running EC2 was provisioned before that fix, edit `/etc/braintwin/docker-compose.yml` on the box and add the line, then `docker compose up -d litestream`. See design doc §14.6. |
| Litestream logs `NoCredentialProviders: no valid providers in chain` | EC2 IMDS hop limit is 1 (default with `requireImdsv2: true`); containers can't reach `169.254.169.254` because docker0 counts as a hop | Already fixed in source: the LaunchTemplate now sets `HttpPutResponseHopLimit: 2` via a CDK Aspect. For a running instance that pre-dates the fix: `aws ec2 modify-instance-metadata-options --instance-id <i-xxx> --http-put-response-hop-limit 2 --http-tokens required --profile braintwin --region us-west-2` (takes effect immediately, no restart). See design doc §14.5. |
| Litestream container in `Restarting` loop | Most often: bucket name mismatch in `litestream.yml`, or IAM perms missing | `sudo docker logs braintwin-litestream`, check the YAML, verify `s3:PutObject` is in the instance role's grants |
| `chroma-backup.timer` doesn't list a next run | Timer not enabled | `sudo systemctl enable --now braintwin-chroma-backup.timer` |
| Restore command errors `no replica found` | Litestream hasn't had time to upload anything yet | Wait 5 min, retry. Check `aws s3 ls s3://.../litestream/` |
| CloudWatch namespace empty | Agent not running | `sudo systemctl status amazon-cloudwatch-agent`, restart with `sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s` |

---

## 7. What's intentionally NOT in M.5

- **Cross-region replication** — Litestream supports it (one config
  block per replica destination); deferred. For a personal product the
  same-region S3 replica with EBS DLM snapshots is sufficient redundancy
- **Per-table point-in-time queries** — Litestream provides full-DB
  restore at a timestamp. Reading "row X at time T" requires restoring
  to a side-car then querying. Fine for our scale
- **App-level metrics** (latency, request count per route) — deferred
  to M.11 (Phase 4.0.6.1). CloudWatch Agent only handles OS-level
  metrics
