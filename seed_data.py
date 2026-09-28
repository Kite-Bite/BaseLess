"""
seed_data.py - the incident library Blameless remembers.

20 hand-written production incidents from a fictional org's on-call history
(2025-02 -> 2026-06), written by hand on purpose: generated incidents would all
share one LLM's phrasing and the recall demo would look fake. Real logs,
hostnames, severities and timestamps make the memory plausible and make the
"Recalled from memory" panel worth reading on screen.

The library is deliberately organised into RELATED FAMILIES - incidents that
share a signature but have a different trigger. This is what makes memory pay
off: a new incident matches an old *family*, and the recalled root cause is a
starting hypothesis, not the answer.

  family                incidents                        what recall should connect
  --------------------  -------------------------------  ---------------------------
  postgres connection   pg-pool, pg-pool-2, pg-maxconn   "FATAL: too many clients"
  pool exhaustion
  disk exhaustion       disk-varlog, disk-docker         "No space left on device"
  TLS expiry            tls-expired, tls-expired-2       cert notAfter passed
  edge 502s             nginx-502, nginx-502-2           502 + upstream failures
  container runtime     oom-container, oom-2,            restart loops / DNS
                       docker-dns, docker-dns-2
  credential exposure   leaked-key, leaked-key-2,        key in git / in image
                       leak-abuse

Root causes are written BLAMELESSLY: they describe the system and the process
that allowed the failure, never a person. "The logrotate config was not covered
by the config test suite", not "ops forgot to check disk".
"""

from __future__ import annotations

SEED_INCIDENTS: list[dict] = [
    # ------------------------------------------------------------------
    # FAMILY: PostgreSQL connection pool exhaustion (3 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-03-11-pg-pool",
        "title": "PostgreSQL connection pool exhausted, billing-api returning 500s",
        "system": "billing-api",
        "severity": "SEV1",
        "timestamp": "2025-03-11T02:14:00+00:00",
        "symptoms": """
2025-03-11 02:14:07 ERROR billing-api/api: Unhandled exception in POST /invoices
psycopg2.errors.SemaphoreRelatedResourceAvailable: pool timeout: no connection available after 30.00s
  (pool "billing-api-pool", size=10, max_size=20, timeout=30)
2025-03-11 02:14:11 WARN  billing-api/api: 412/500s in last 60s, upstream=db-primary.internal:5432
2025-03-11 02:15:02 INFO  postgres@db-primary: FATAL: sorry, too many clients already
2025-03-11 02:16:44 INFO  prometheus: db_max_connections{service="billing-api"} 42
2025-03-11 02:19:10 ERROR billing-api/worker: queue=invoice-events lag=48210 messages
""".strip(),
        "root_cause": (
            "Connection-pool sizing was never derived from the Postgres max_connections "
            "budget. billing-api runs 12 replicas x max_size 20 = 240 potential "
            "connections, plus analytics and migrations, against a max_connections of 300. "
            "Any traffic spike pushed aggregate demand past the database ceiling, and "
            "the pool's 30s acquisition timeout converted a fast-failing condition "
            "into a 30-second stall that took the whole replica's worker with it."
        ),
        "resolution": (
            "Moved billing-api in front of pgbouncer (transaction pooling, default_pool_size "
            "= 60, reserve_pool = 20) and capped the application pool at max_size 10. "
            "Added a deploy-time check that rejects any service whose "
            "replicas x pool_max_size exceeds 15% of the database's max_connections, so "
            "the relationship is enforced by the pipeline rather than by reviewer memory."
        ),
    },
    {
        "id": "inc-2025-07-02-pg-pool-2",
        "title": "Same connection-pool exhaustion signature on payments-worker after failover",
        "system": "payments-worker",
        "severity": "SEV1",
        "timestamp": "2025-07-02T19:38:00+00:00",
        "symptoms": """
2025-07-02 19:38:22 ERROR payments-worker/consumer: pool timeout: no connection available after 30.00s
  (pool "payments-pool", size=5, max_size=15, timeout=30)
2025-07-02 19:38:25 INFO  postgres@db-primary: FATAL: sorry, too many clients already
2025-07-02 19:39:01 WARN  payments-worker: retry storm detected, 900 msg/s, backoff disabled
2025-07-02 19:41:30 ERROR payments-worker: 0/12 consumers alive, circuit breaker OPEN
2025-07-02 19:44:02 INFO  grafana: payments_success_rate 41% (baseline 99.8%)
""".strip(),
        "root_cause": (
            "The same systemic gap as the March billing-api exhaustion, resurfacing in a "
            "service that was onboarded after pgbouncer existed and therefore never went "
            "through it. Connect-direct was the documented default, so the pool ceiling was "
            "per-replica and multiplied silently by 12 replicas, and the retry storm with "
            "backoff disabled turned a capacity limit into an outage: every failed "
            "consumer immediately reconnected, so recovering clients competed with "
            "recovering clients."
        ),
        "resolution": (
            "Routed payments-worker through pgbouncer like the other DB-facing services and "
            "enabled exponential backoff with jitter on the consumer retry policy. The "
            "admission check from the March fix would have blocked this deploy, which is "
            "exactly why it now runs in CI: the process gap, not the one-off config, was "
            "the thing worth fixing."
        ),
    },
    {
        "id": "inc-2025-02-19-pg-maxconn",
        "title": "Postgres rejecting all connections after a monitoring exporter leak",
        "system": "postgres-primary",
        "severity": "SEV1",
        "timestamp": "2025-02-19T04:02:00+00:00",
        "symptoms": """
2025-02-19 04:02:55 ERROR postgres@db-primary: FATAL: sorry, too many clients already
2025-02-19 04:03:10 INFO  postgres@db-primary: remaining connection slots are reserved
2025-02-19 04:03:44 ERROR monitoring/node_exporter: dial tcp 10.0.4.21:5432: connect: connection refused
2025-02-19 04:05:19 INFO  psql: SELECT count(*), state FROM pg_stat_activity GROUP BY state;
             count | state
            --------+-------
                 1 | idle in transaction
                 289 | active
                 10 | idle
2025-02-19 04:06:00 WARN  grafana: db_up=0 for 3m
""".strip(),
        "root_cause": (
            "A custom Postgres exporter opened a fresh connection per metric scrape and "
            "returned it to a pool it never actually checked out of, so idle sessions "
            "accumulated at 12 per scrape across 24 pods. Nothing rejected them because "
            "the exporter was a trusted in-cluster client, and nothing alerted because "
            "the exporter's own dashboards only tracked query latency, not connection "
            "counts. The monitoring system was the outage."
        ),
        "resolution": (
            "Replaced the exporter with one that uses a single pooled session with a hard "
            "max lifetime, and added an alert on pg_stat_activity count > 80% of "
            "max_connections. Documented the pgbouncer default (new DB-facing services go "
            "through it) so the next service does not have to rediscover the ceiling."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: disk exhaustion (2 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-04-14-disk-varlog",
        "title": "/var/log filled on three app hosts, journald cannot write, services went read-only",
        "system": "app-node-07/08/11",
        "severity": "SEV2",
        "timestamp": "2025-04-14T11:26:00+00:00",
        "symptoms": """
2025-04-14 11:26:12 ERR app-node-07 systemd-journald: Failed to write to /var/log/journal/2025-04-14: No space left on device
2025-04-14 11:26:19 ERR app-node-07 kernel: EXT4-fs error (device nvme0n1p1): 1882565 times, last at 11:26:11
2025-04-14 11:26:44 WARN app-node-08 billing-api: cannot write audit log, falling back to stdout
2025-04-14 11:27:03 ERR app-node-11 sshd: PAM: pam_open_session(): cannot open /var/log/lastlog: Read-only file system
2025-04-14 11:28:40 INFO node-exporter: node_filesystem_avail_bytes{mountpoint="/var/log"} 0
2025-04-14 11:30:15 CRIT app-node-07 app: FATAL unable to write audit trail, self-terminating
""".strip(),
        "root_cause": (
            "logrotate was configured for /var/log/*.log but the audit trail was written "
            "to a per-host file outside the pattern, so it was never rotated and grew "
            "unbounded. The config-review process had no assertion that every directory "
            "under /var/log was covered by a rotation rule, so a new audit sink could be "
            "added without anyone noticing it was unmanaged. The alert existed but fired "
            "on the root filesystem while the growth was on a dedicated log partition."
        ),
        "resolution": (
            "Shipped the truncated audit files, then added systemd-journald's own "
            "SystemMaxUse=2G as a hard backstop so the failure mode cannot recur even if a "
            "log escapes logrotate. Added a CI check that any file created under /var/log "
            "must match a rotation rule, and re-pointed the disk alert at every mount, not "
            "just /."
        ),
    },
    {
        "id": "inc-2026-01-27-disk-docker",
        "title": "docker system df growing to 100% after json-file logging driver change",
        "system": "docker-host-03",
        "severity": "SEV2",
        "timestamp": "2026-01-27T03:48:00+00:00",
        "symptoms": """
2026-01-27 03:48:22 ERR docker-host-03 dockerd: failed to create task for container: no space left on device
2026-01-27 03:48:50 ERR docker-host-03 containerd: unpack failed: write /var/lib/docker/overlay2/...: no space left on device
2026-01-27 03:49:11 WARN docker-host-03 kubelet: FailedSync: containerd: failed to start container
2026-01-27 03:51:30 INFO docker-host-03 docker system df:
              TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
     Containers          48        12     1.2GB      1.1GB
     Images             31         9      94.7GB     94.0GB
     Local Volumes      6         6      412MB      0B
2026-01-27 03:55:04 ERR docker-host-03 api-gateway: no route to host 10.4.2.17:8443
""".strip(),
        "root_cause": (
            "A host-wide daemon.json edit set logging-driver to json-file without "
            "log-opts max-size/max-file, which the json-file driver does not apply by "
            "default. The change looked safe in review because the previous daemon.json "
            "only set storage-driver, and the diff showed one added line. There was no "
            "post-deploy check on container log growth, so a policy that had been the "
            "image default since the base image was changed silently reverted on these "
            "hosts only."
        ),
        "resolution": (
            "docker image prune reclaimed 94GB and the containers came back. Restored the "
            "log-opts in the shared daemon.json and templated them per host group so they "
            "cannot be dropped by an unrelated edit. Added a nightly job that reports "
            "docker system df growth and fails when a host exceeds 80% on /var/lib, "
            "turning a silent 3-week drift into a noisy daily one."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: TLS expiry (2 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-05-05-tls-expired",
        "title": "Expired TLS certificate on api.acme-corp.net, all external traffic 502'd",
        "system": "edge-lb",
        "severity": "SEV1",
        "timestamp": "2025-05-05T00:03:00+00:00",
        "symptoms": """
2025-05-05 00:03:11 ERR edge-lb-01 nginx: [error] 31504#0: SSL_do_handshake() failed
  (SSL: certificate has expired) while reading client request, client: 203.0.113.44
2025-05-05 00:03:19 ERR edge-lb-01 nginx: [error] 31505#0: no live upstreams
2025-05-05 00:04:02 CRIT synthetics: GET https://api.acme-corp.net/healthz -> 502 (SSL error)
2025-05-05 00:07:44 INFO ops: openssl s_client -connect api.acme-corp.net:443
             notAfter=Apr 30 12:00:00 2025 GMT
""".strip(),
        "root_cause": (
            "The cert was issued by an internal CA with a 30-day lifetime and renewed by a "
            "job that had no alerting and no owner in the on-call rotation, so a single "
            "missed run expired the cert. Renewal was treated as a task rather than as a "
            "system with a failure mode: nothing detected the run had failed, and nothing "
            "detected the cert would expire even if the run had never been attempted."
        ),
        "resolution": (
            "Issued a replacement cert and added two independent alerts: one on cert "
            "expiry < 14 days, one on the renewal job's own success/failure for 48h. "
            "Longer term this moves to cert-manager with renewal as a reconciler - the "
            "lesson was that manual renewal has an unowned failure mode no matter how "
            "careful any individual run is."
        ),
    },
    {
        "id": "inc-2026-03-08-tls-expired-2",
        "title": "Wildcard cert expired across 11 internal services, mutual TLS handshakes failing",
        "system": "internal-mesh",
        "severity": "SEV2",
        "timestamp": "2026-03-08T08:31:00+00:00",
        "symptoms": """
2026-03-08 08:31:44 ERR sidecar-proxy-03: unable to verify the first certificate
  (x509: certificate has expired or is not yet valid: current time 2026-03-08 is after
  2026-02-14 00:00:00 +0000 UTC)
2026-03-08 08:32:10 ERR payments-worker: SSL error connecting to ledger.internal:8443
2026-03-08 08:33:55 WARN k8s liveness-probe: Get "https://ledger.internal:8443/healthz": x509 expired
2026-03-08 08:36:20 INFO vault-pki: last successful issue 2026-02-14, ttl requested 720h
""".strip(),
        "root_cause": (
            "The internal wildcard was issued with a 30-day TTL and the issuing job was "
            "removed during the platform migration, but the dependent services kept "
            "pointing at the same CA and nobody re-pointed them at cert-manager, which "
            "had been the replacement. Certificate lifecycle ownership was lost in a "
            "migration checklist item that read 'CA compatible', not 'renewal owned'. "
            "The same systemic gap as the May 2025 edge incident: expiry is a scheduled "
            "failure that only has to happen once."
        ),
        "resolution": (
            "Re-issued the wildcard and moved all 11 services to cert-manager's "
            "renewal controller with a 21-day renewal window, so a missed job is a "
            "reconciler retry rather than an expiry. Added the internal wildcard's "
            "notAfter to the shared cert dashboard alongside the public certs, so both "
            "lifecycles are visible to whoever is on call."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: edge 502s (2 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-06-21-nginx-502",
        "title": "NGINX 502s for 40 minutes after a deploy with a changed upstream port",
        "system": "edge-lb",
        "severity": "SEV1",
        "timestamp": "2025-06-21T14:02:00+00:00",
        "symptoms": """
2025-06-21 14:02:33 ERR edge-lb-02 nginx: [error] 8812#0: connect() failed (111: Connection refused)
  while connecting to upstream, upstream: "http://10.6.1.44:8081/checkout", request: "POST /checkout"
2025-06-21 14:02:58 ERR edge-lb-02 nginx: [error] 8820#0: no live upstreams while reading response header
2025-06-21 14:05:41 ERR edge-lb-02 nginx: upstream timed out (110: Connection timed out) while reading response header
2025-06-21 14:09:02 WARN prometheus: nginx_5xx_ratio{edge="lb"} 0.41
2025-06-21 14:12:50 CRIT checkout-api: 0 requests served, all traffic returning 502
""".strip(),
        "root_cause": (
            "The deploy changed the app's listen port (8080 -> 8081) as part of a "
            "dependency upgrade, but the NGINX upstream definition is a separate config "
            "repo with its own release cadence, so the two could not be deployed "
            "atomically. The rollout pipeline had no config-drift check between the app "
            "manifest and the live upstream block, which meant the only way to find this "
            "class of mismatch was a production 502. The process allowed two sources of "
            "truth for one fact."
        ),
        "resolution": (
            "Rolled back the app image and reset the upstream block. The lasting change is "
            "structural: upstream definitions now render from the same service manifest "
            "that configures the app, and the pipeline runs a synthetic checkout against "
            "the canary before promoting, so a port or path drift fails the deploy instead "
            "of the users."
        ),
    },
    {
        "id": "inc-2025-12-09-nginx-502-2",
        "title": "Intermittent 502s after proxy timeouts lowered to 2s for a slow upstream",
        "system": "edge-lb",
        "severity": "SEV3",
        "timestamp": "2025-12-09T09:17:00+00:00",
        "symptoms": """
2025-12-09 09:17:14 ERR edge-lb-01 nginx: [error] 22140#0: upstream timed out (110: Connection timed out)
  while reading response header, upstream: "http://10.6.1.51:8080/reports"
2025-12-09 09:17:15 ERR edge-lb-01 nginx: [error] 22141#0: upstream timed out, request: "GET /reports"
2025-12-09 09:18:30 ERR reports-api: p99 latency 2.4s (baseline 380ms), GC pause 900ms
2025-12-09 09:20:11 WARN grafana: nginx_upstream_time_p99 2.1s
2025-12-09 09:25:44 INFO edge-lb-01: proxy_read_timeout 2s -> 30s
""".strip(),
        "root_cause": (
            "proxy_read_timeout was lowered from 30s to 2s during a hardening exercise "
            "that reduced idle timeouts, with the intent of reclaiming stuck connections. "
            "The change was applied globally rather than per-route, so a report endpoint "
            "that legitimately takes 2-3s started being cut off mid-response. The tuning "
            "was reasonable, the blast radius was not: one default changed the behaviour "
            "of every upstream in the same file."
        ),
        "resolution": (
            "Restored proxy_read_timeout to 30s and moved the aggressive timeout to the "
            "one route that needed it, using a location block instead of a server-level "
            "default. The reports endpoint was also given its own p99 budget and alert so "
            "the latency that made the 2s timeout look tempting is now visible on its own "
            "terms."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: SSH brute force / fail2ban (2 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-04-29-ssh-bruteforce",
        "title": "SSH brute force from 400+ IPs filled auth.log, masked a real intrusion attempt",
        "system": "bastion-host-01",
        "severity": "SEV2",
        "timestamp": "2025-04-29T22:03:00+00:00",
        "symptoms": """
2025-04-29 22:03:11 INFO sshd[21104]: Failed password for invalid user admin from 45.83.12.201 port 44122 ssh2
2025-04-29 22:03:12 INFO sshd[21107]: Failed password for invalid user root from 193.32.44.9 port 51203 ssh2
2025-04-29 22:06:40 INFO sshd[22891]: Failed password for root from 45.83.12.201 port 44990 ssh2
2025-04-29 22:41:05 ERR sshd[30118]: Accepted password for deploy from 10.0.3.19 port 52201 ssh2
2025-04-29 22:41:07 ERR sshd[30118]: pam_unix(sshd:session): session opened for user deploy
2025-04-29 22:41:50 INFO auth-anomaly: 1 successful root password auth from 91.219.236.7
""".strip(),
        "root_cause": (
            "fail2ban was installed with the sshd jail disabled and password auth enabled "
            "for the deploy user, so ~14k failed logins in 20 minutes changed nothing. "
            "The successful login from an unrecognised IP was buried in the noise and only "
            "surfaced when someone read auth.log manually the next morning. The systemic "
            "issue is that brute-force volume and real intrusion both arrive through the "
            "same log stream, and the alerting was on the log's size rather than on the "
            "security signal inside it."
        ),
        "resolution": (
            "Confirmed the 91.219.236.7 login was a scanner that had guessed a reused "
            "password and was locked out before interactive use. Moved deploy to key-only "
            "auth, enabled the sshd jail with a 10-attempt threshold, and - the real fix - "
            "added a rule that alerts on *any* successful auth from outside the known "
            "bastion range, independent of failure volume. Noise is not a security control."
        ),
    },
    {
        "id": "inc-2026-02-12-ssh-bruteforce-2",
        "title": "fail2ban jail silently not loaded on edge nodes after a package upgrade",
        "system": "edge-lb-01..04",
        "severity": "SEV2",
        "timestamp": "2026-02-12T05:44:00+00:00",
        "symptoms": """
2026-02-12 05:44:02 INFO sshd[9021]: Failed password for invalid user oracle from 91.219.236.7 port 3310 ssh2
2026-02-12 05:44:03 INFO sshd[9024]: Failed password for invalid user test from 193.32.44.9 port 4402 ssh2
2026-02-12 05:51:19 INFO fail2ban-client[1201]: Jail list:
2026-02-12 05:51:20 INFO fail2ban-regex: Found no Fail2ban log files
2026-02-12 06:20:44 ERR fail2ban.service: Failed to start because the dependency 'network.target' is not satisfiable in a chroot
2026-02-12 06:20:45 INFO systemd: fail2ban.service: Failed with result 'exit-code'.
""".strip(),
        "root_cause": (
            "The edge nodes' hardening image packages fail2ban with a unit that assumes "
            "network.target, which does not exist inside the chroot those images use, so "
            "the service failed to start on every node. Because nobody asserted 'this "
            "security control is actually running' after the image refresh, the nodes ran "
            "unprotected for 19 days while the service silently failed. The protection was "
            "assumed present because it was present in the image, not because anything "
            "checked the running state - a control that is never verified is not a control."
        ),
        "resolution": (
            "Added a post-deploy assertion in the image pipeline that fails the build if "
            "fail2ban is installed but its jail list is empty, plus a daily check across "
            "all edge nodes that reports the running jail count. Repackaged the unit "
            "without the network.target dependency so the service starts in the chroot. "
            "This is the control-verification gap the April 2025 incident also had, one "
            "layer up: we alert on attacks, never on our defences being off."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: container runtime (4 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-08-04-oom-container",
        "title": "search-indexer OOM-killed repeatedly, restart loop exhausted node memory",
        "system": "search-indexer",
        "severity": "SEV2",
        "timestamp": "2025-08-04T16:22:00+00:00",
        "symptoms": """
2025-08-04 16:22:31 KERNEL node-14 kernel: Memory cgroup out of memory: Killed process 4821 (java)
  total-vm:9812344kB, anon-rss:2051588kB, file-rss:9244kB, shmem-rss:1024kB
2025-08-04 16:22:32 KERNEL node-14 kernel: Memory cgroup out of memory: Killed process 4821 (java)
2025-08-04 16:23:10 INFO kubelet: Container search-indexer (pid 4821) exceeded memory limit 2Gi
2025-08-04 16:23:40 INFO search-indexer: OutOfMemoryError: Java heap space, index shard 7
2025-08-04 16:25:02 INFO kubelet: Back-off restarting failed container search-indexer
2025-08-04 16:31:19 WARN prometheus: node_memory_MemAvailable_bytes{node="node-14"} 0
""".strip(),
        "root_cause": (
            "The indexer was given a 2Gi limit sized for a small shard, and the shard "
            "assignment logic had no memory awareness: after a rebalance it placed four "
            "shards on node-14, each sized independently, so the node's total allocation "
            "exceeded physical memory. Limits were per-container correct and collectively "
            "incoherent - the scheduling layer had no view of the sum, so no limit ever "
            "appeared to be violated at the point where it mattered."
        ),
        "resolution": (
            "Removed the overloaded node and rebalanced with a memory-weighted scheduler "
            "profile so shard placement sums allocations against node capacity. Raised the "
            "limit to 3Gi and capped the JVM heap at 70% of it so the container cannot grow "
            "into its own limit. Alerts now fire on container OOMKills directly rather "
            "than only on node-level memory, which is where the signal was lost."
        ),
    },
    {
        "id": "inc-2026-04-18-oom-container-2",
        "title": "api-gateway OOM-killed after an unbounded response cache was added",
        "system": "api-gateway",
        "severity": "SEV2",
        "timestamp": "2026-04-18T13:09:00+00:00",
        "symptoms": """
2026-04-18 13:09:44 KERNEL node-07 kernel: Memory cgroup out of memory: Killed process 9117 (python3)
  total-vm:7183920kB, anon-rss:1934284kB
2026-04-18 13:10:02 INFO api-gateway: worker 3 stopped responding, connection reset by peer
2026-04-18 13:10:20 INFO prometheus: container_memory_working_set_bytes{service="api-gateway"} 2031614976
2026-04-18 13:12:55 ERR api-gateway: 502 from upstream, 3/8 workers down
2026-04-18 13:15:31 INFO review: PR #482 "add response cache" merged 2026-04-11, cache size: unlimited
""".strip(),
        "root_cause": (
            "A response cache was merged as a performance improvement with no bound and no "
            "eviction policy, and no code-review checklist item asked whether a new cache "
            "has a size limit. Under a traffic shift to large responses the cache retained "
            "everything until the container hit its 2Gi limit and was OOM-killed, taking "
            "3 of 8 workers with it. The feature worked exactly as written; the process "
            "never asked what 'unlimited' costs in a memory-bounded runtime."
        ),
        "resolution": (
            "Added a bounded LRU cache (max 512 entries, 256MB) and shipped the container "
            "with a 512Mi cache working set budget enforced in review. Added an OOMKill "
            "alert per container, since the node never ran out of memory and the only "
            "visible symptom was workers disappearing - the observability gap the August "
            "2025 indexer incident also had."
        ),
    },
    {
        "id": "inc-2025-09-30-docker-dns",
        "title": "Docker containers could not resolve external DNS after an overlay network change",
        "system": "docker-host-01",
        "severity": "SEV3",
        "timestamp": "2025-09-30T10:15:00+00:00",
        "symptoms": """
2025-09-30 10:15:22 ERR checkout-api: urllib3.exceptions.NameResolutionError: Failed to resolve 'api.stripe.com'
2025-09-30 10:15:24 ERR checkout-api: Temporary failure in name resolution (Try again)
2025-09-30 10:16:03 ERR docker-host-01 dockerd: bridge network dns-backend internal: resolver 127.0.0.11 unreachable
2025-09-30 10:17:40 INFO docker-host-01 docker network inspect bridge: "DNS": ["127.0.0.11"]
2025-09-30 10:19:11 INFO docker-host-01: /etc/resolv.conf options ndots:5 search corp.internal
2025-09-30 10:22:50 ERR checkout-api: dependency timeouts, 0 checkouts for 6 minutes
""".strip(),
        "root_cause": (
            "The host's /etc/resolv.conf was regenerated by the network management agent "
            "with options ndots:5 and an internal search list, a configuration that is "
            "correct for the host but hostile to containers: five search-domain expansions "
            "per lookup meant every external name hit the internal resolver first, and "
            "the embedded Docker resolver at 127.0.0.11 timed out before trying the "
            "upstream. Host and container DNS configuration were managed by different "
            "systems, so the change was invisible from either one's point of view."
        ),
        "resolution": (
            "Set explicit --dns and --dns-search on the affected containers so they do not "
            "inherit the host's search list, and reduced ndots:2 in the image's "
            "resolv.conf template. The structural fix was to have the network agent write "
            "a container-specific resolver file the compose stack mounts explicitly, so "
            "host DNS changes can no longer silently redefine container resolution."
        ),
    },
    {
        "id": "inc-2026-05-02-docker-dns-2",
        "title": "DNS failures for compose services after an override file replaced dns_opt",
        "system": "compose-stack-metrics",
        "severity": "SEV3",
        "timestamp": "2026-05-02T07:48:00+00:00",
        "symptoms": """
2026-05-02 07:48:11 ERR metrics-collector: socket.gaierror: [Errno -2] Name or service not known: 'postgres'
2026-05-02 07:48:44 ERR compose: service "metrics-collector" depends on "postgres" which is not healthy
2026-05-02 07:50:03 ERR metrics-collector: connection refused 172.19.0.4:5432
2026-05-02 07:51:29 INFO docker network inspect metrics_default: "DNS": []
2026-05-02 07:53:12 ERR grafana: no data for 5m, dashboard empty
""".strip(),
        "root_cause": (
            "A new docker-compose override file for the metrics stack defined the same "
            "services as the base file. Compose merges list-type fields by replacing them, "
            "so specifying dns_opt in the override silently dropped the base file's "
            "dns_opt, and the service went back to the host resolver and lost the "
            "service-name resolution it depended on. Two files that each look correct "
            "produce a broken merge, and nothing in the review compared the effective "
            "config - only the two files in isolation."
        ),
        "resolution": (
            "Removed the duplicated service definitions and put the shared network "
            "settings in a single base fragment every stack extends. Added a pre-deploy "
            "step that renders the merged config and diffs the network/DNS fields against "
            "the last deployed version, so a silent override replacement fails the "
            "pipeline. Same lesson as the 2025-09-30 incident: config that is correct in "
            "isolation is not the same as config that is correct when merged."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: cron / mail queue flooding (2 incidents)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-11-15-cron-mail",
        "title": "Broken cron job emailed 180k times and filled the mail queue",
        "system": "mail-relay",
        "severity": "SEV3",
        "timestamp": "2025-11-15T06:00:00+00:00",
        "symptoms": """
2025-11-15 06:00:01 CRON app-node-04: CMD (/opt/scripts/nightly-reconcile.sh)
2025-11-15 06:00:31 ERROR reconcile: cannot reach ledger-db:5432 (certificate verify failed)
2025-11-15 06:00:31 ERROR reconcile: exiting 1
2025-11-15 06:30:31 INFO postfix/smtp[8812]: 3D8F2A1: to=<ops@acme-corp.net>, relay=..., delay=0.4s
2025-11-15 09:14:02 INFO postfix: message queue size: 184203
2025-11-15 11:40:19 ERR postfix/smtp: 452 4.3.2 Mail queue full - try again later
2025-11-15 11:41:00 CRIT ops: legitimate alerting email rejected, mail queue full
""".strip(),
        "root_cause": (
            "The nightly reconcile job wrote its failure to stdout, and the crontab piped "
            "cron's output to mail - so a job that failed every minute emailed ops 1440 "
            "times a day, and the incident mail itself was one of the victims when the "
            "queue filled. The trigger was a TLS verification failure after the ledger "
            "cert was reissued, but the outage cause was the reporting path: cron had no "
            "failure dedup, no output cap, and no route for 'this failed' that is not "
            "email. Alerting on human email is a single point of failure for the alerts."
        ),
        "resolution": (
            "Flushed the queue and paused the job. Rewrote it to log structured output to "
            "the collection agent and to alert once per state change rather than once per "
            "run. Added MAILTO suppression and a queue-depth alert at 10k so a future "
            "runaway reports itself before it blocks delivery of everything else."
        ),
    },
    {
        "id": "inc-2026-06-05-cron-mail-2",
        "title": "Log rotation cron grew unbounded and its own mail alerts mailed every minute",
        "system": "app-node-09",
        "severity": "SEV3",
        "timestamp": "2026-06-05T12:00:00+00:00",
        "symptoms": """
2026-06-05 12:00:04 CRON app-node-09: CMD (/opt/scripts/prune-logs.sh)
2026-06-05 12:00:05 WARN prune-logs: /var/log/app never rotated, size 38G
2026-06-05 12:00:05 WARN prune-logs: disk usage 91% on /var/log
2026-06-05 12:01:07 INFO postfix/smtp[9912]: to=<ops@acme-corp.net>
2026-06-05 14:22:31 INFO postfix: message queue size: 62190
2026-06-05 12:05:11 INFO postfix: 452 4.3.2 Mail queue full
2026-06-05 12:09:44 ERR prometheus: alertmanager could not deliver alert
""".strip(),
        "root_cause": (
            "The script that was supposed to be the safety net for unbounded log growth "
            "warned instead of acting, and its warning went out by email every minute. "
            "Two compounding process gaps: a remediation script that reports a condition "
            "instead of remediating it is not a control, and a per-run alert is an alert "
            "channel with no rate limit, so the thing meant to report the problem became "
            "the outage. Root cause of the growth itself is the same missing rotation "
            "coverage found on app-node-07/08/11 in April 2025: the process gap was "
            "identified then and only half-closed."
        ),
        "resolution": (
            "Cleared the queue, made the script prune the offending directory rather than "
            "warn, and moved its output to the structured log pipeline with a 1-hour dedup "
            "window. Extended the rotation-coverage CI check from app services to every "
            "directory on every host group, closing the half-finished remediation from the "
            "April 2025 incident rather than re-fixing the same thing on a new host."
        ),
    },
    # ------------------------------------------------------------------
    # FAMILY: credential exposure (3 incidents, incl. the consequences)
    # ------------------------------------------------------------------
    {
        "id": "inc-2025-07-25-leaked-api-key",
        "title": "Live Stripe secret key committed to a public repository",
        "system": "payments-service",
        "severity": "SEV1",
        "timestamp": "2025-07-25T14:52:00+00:00",
        "symptoms": """
2025-07-25 14:52:31 git: commit 8f3a91c "add deployment notes" modified README.md (+41 -3)
2025-07-25 14:52:33 git: +  STRIPE_SECRET_KEY=sk_live_51Nx..........................................qWd
2025-07-25 14:53:01 gitleaks: no rule matched (pattern not in config, .gitleaks.toml from 2024)
2025-07-25 15:20:44 stripe: API request from IP 185.234.72.19 (not an allowlisted range)
2025-07-25 15:21:03 stripe: livemode request, endpoint /v1/customers, 4,182 calls in 60s
2025-07-25 15:31:19 stripe: alert: unusual refund volume, 217 refunds
""".strip(),
        "root_cause": (
            "The key was pasted into a README by a developer following a 'quick start' "
            "that had no credential-handling step, and the secret scanner did not catch it "
            "because the org's gitleaks config predates the key prefix this provider now "
            "issues - the control was present, running, and silently out of date. The "
            "push protection rule only covered the default ruleset, so a valid-looking key "
            "in an allowed file type passed straight through. Missing review checklist item: "
            "documentation changes carry the same secret risk as code changes."
        ),
        "resolution": (
            "Rotated the key immediately, purged the commit from history, and enabled "
            "provider-side IP allowlisting for the account. Updated gitleaks to the current "
            "ruleset and wired its output into the pre-receive hook so the push is blocked, "
            "not reported. Added a mandatory 'do not paste credentials' step to the "
            "quick-start template, because the doc is part of the delivery system and the "
            "next provider will introduce new key formats the scanner must learn about."
        ),
    },
    {
        "id": "inc-2025-08-19-leak-abuse",
        "title": "Key from the July repository leak used for fraud: refund abuse and data scraping",
        "system": "payments-service",
        "severity": "SEV1",
        "timestamp": "2025-08-19T03:14:00+00:00",
        "symptoms": """
2025-08-19 03:14:22 stripe: livemode request from 185.234.72.19, endpoint /v1/charges
2025-08-19 03:19:50 INFO payments-api: refund_rate 11.4% (baseline 0.3%), 1,940 refunds in 6h
2025-08-19 03:41:18 stripe: fraud rule triggered, 63 disputed charges
2025-08-19 05:02:55 ERR webhook: signature verification failed, 12,004 rejected events
2025-08-19 05:40:30 INFO payments-api: chargebacks_opened 71, exposure estimated $84,200
""".strip(),
        "root_cause": (
            "The July key leak was remediated by rotation, but the rotation had no "
            "downstream cleanup and no detection for use of the *revoked* key: the provider "
            "rejected the calls, but nothing on our side noticed that a stream of traffic "
            "was still arriving with the old credential and using it against other systems "
            "where the same pattern had been reused. The remediation was complete for the "
            "key and incomplete for the exposure, and the fraud continued for 25 days "
            "before a rate anomaly - not a security signal - surfaced it."
        ),
        "resolution": (
            "Worked with the provider on the fraud, and added a specific control this "
            "incident justified: any credential rotation now also fires an alert when the "
            "revoked key is used anywhere for 30 days, and the incident runbook requires "
            "auditing for pattern reuse of the same secret. Treat a leak as an ongoing "
            "adversary, not a point-in-time event - the exposure outlives the rotation."
        ),
    },
    {
        "id": "inc-2026-01-14-leaked-key-2",
        "title": "Internal service token baked into a container image layer and readable by anyone with pull access",
        "system": "container-registry",
        "severity": "SEV1",
        "timestamp": "2026-01-14T09:26:00+00:00",
        "symptoms": """
2026-01-14 09:26:11 CI: step "build image" - 0 findings from gitleaks (source tree clean)
2026-01-14 09:26:44 registry: layer sha256:9c4e...f21 manifest built for svc-reporting:1.14.2
2026-01-14 09:31:02 trivy: HIGH CVE-2026-1188 misconfigured Dockerfile, severity ignored
2026-01-14 10:02:33 INFO auth-svc: token svc-reporting presented from 10.9.4.88 (unknown workload)
2026-01-14 10:04:19 INFO auth-svc: token svc-reporting authenticated, scope=admin:reports
2026-01-14 10:19:47 WARN audit: bulk export 41,000 records by principal svc-reporting
""".strip(),
        "root_cause": (
            "The token was injected with an ARG in the Dockerfile, which bakes the value "
            "into a layer permanently - the source tree was clean, so the source scanner "
            "passed exactly as designed. The image scanner flagged the Dockerfile pattern "
            "as a misconfiguration but the finding was set to 'ignored' in the scanner "
            "policy with no expiry, so the class of vulnerability was muted rather than "
            "fixed. Two controls were present and neither was load-bearing: source "
            "scanning cannot see runtime-injected secrets, and a permanently ignored "
            "finding is indistinguishable from a permanently accepted risk."
        ),
        "resolution": (
            "Revoked the token, rebuilt the image with BuildKit secret mounts so "
            "credentials never enter a layer, and purged the affected manifests from the "
            "registry. Changed the scanner policy: ignored findings now expire after 30 "
            "days and must name an owner, so 'ignore' is a decision with a deadline rather "
            "than a permanent hole. This is the same systemic lesson as the July 2025 leak "
            "- secrets reach production through paths our controls do not cover, so the "
            "controls get breadth, not more exceptions."
        ),
    },
]


def get_seed_incidents() -> list[dict]:
    """Return the hand-written incident library.

    Shallow-copied so a caller (or the /seed endpoint) cannot mutate the module
    level constant, which would make a re-seed non-deterministic for the demo.
    """
    return [dict(inc) for inc in SEED_INCIDENTS]
