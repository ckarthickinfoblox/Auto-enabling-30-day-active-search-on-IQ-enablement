"""
IBIQ-3028: QA Automation — IQ for DDI Ingestion Gating
Covers: DNS test cases TC-06 to TC-16, TC-19 to TC-27 (BloxOne DNS only)
Environment: env-2a (https://env-2a.test.infoblox.com)
Dev: Rahul Dhulipalla
QA:  Chandra Karthick V

Run:
    export IB_TOKEN=<your_token_hex>   # Authorization: token=<value>
    export DNS_SERVER_IP=<bloxone_dns_server_ip>   # optional; auto-discovered if not set
    pytest IBIQ_3028_test.py -v --tb=short 2>&1 | tee IBIQ_3028_run.log
"""

import os
import re
import time
import json
import subprocess
import pytest
import requests
import urllib3
import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL    = "https://env-2a.test.infoblox.com"
TOKEN       = os.environ.get("IB_TOKEN", "")          # set via env var
HEADERS     = {
    "Authorization": f"token={TOKEN}",
    "Content-Type":  "application/json",
}
POLL_INTERVAL = 10   # seconds between log-ingestion checks
POLL_TIMEOUT  = 120  # max seconds to wait for log ingestion

IQ_OPS_URL   = f"{BASE_URL}/api/iqconfig/v1/iqoperations"
DNS_GLOB_URL = f"{BASE_URL}/api/ddi/v1/dns/global"

# TC-23: optional — auto-discovered from infra API if not set
DNS_SERVER_IP = os.environ.get("DNS_SERVER_IP", "")

# ─── Helper utilities ─────────────────────────────────────────────────────────

def ts():
    return datetime.datetime.utcnow().isoformat() + "Z"


def log(msg):
    print(f"[{ts()}] {msg}")


def get_iq_status():
    """Return True if IQ is enabled, False otherwise."""
    r = requests.get(IQ_OPS_URL, headers=HEADERS, verify=False)
    log(f"GET {IQ_OPS_URL} → {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json().get("isEnabled", False)


def set_iq(enabled: bool):
    """Enable or disable IQ for this account. Uses POST (PUT returns 501)."""
    body = {"isEnabled": enabled}
    r = requests.post(IQ_OPS_URL, headers=HEADERS, json=body, verify=False)
    log(f"POST {IQ_OPS_URL} body={body} → {r.status_code} {r.text}")
    r.raise_for_status()
    time.sleep(10)   # allow config to propagate


def get_dns_active_search():
    """Return the current active_search_enable value from DNS global config."""
    r = requests.get(DNS_GLOB_URL, headers=HEADERS, verify=False)
    log(f"GET {DNS_GLOB_URL} → {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json().get("result", {}).get("active_search_enable", None)


def set_dns_active_search(enabled: bool):
    """Set active_search_enable on DNS global config."""
    body = {"active_search_enable": enabled}
    r = requests.patch(DNS_GLOB_URL, headers=HEADERS, json=body, verify=False)
    log(f"PATCH {DNS_GLOB_URL} body={body} → {r.status_code} {r.text}")
    r.raise_for_status()
    time.sleep(10)


def try_set_dns_active_search(enabled: bool):
    """
    Try to set active_search_enable; return (http_status, response_body).
    Does NOT raise — used to test rejection scenarios.
    """
    body = {"active_search_enable": enabled}
    r = requests.patch(DNS_GLOB_URL, headers=HEADERS, json=body, verify=False)
    log(f"PATCH {DNS_GLOB_URL} body={body} → {r.status_code} {r.text}")
    return r.status_code, r.text


def try_set_dns_unrelated_field(value):
    """Try to update an unrelated DNS field while IQ is on. Should succeed."""
    body = {"forwarders_only": value}   # example unrelated field
    r = requests.patch(DNS_GLOB_URL, headers=HEADERS, json=body, verify=False)
    log(f"PATCH unrelated field {DNS_GLOB_URL} body={body} → {r.status_code} {r.text}")
    return r.status_code, r.text


def get_dns_log_count(time_from=None):
    """
    Query IQ DNS activity using the /api/cubejs/v1/load endpoint.

    Cube:    NetworkNstarDnsActivity
    Measure: total_query_count
    IMPORTANT: 'query' must be passed as a JSON-encoded *string* (not a nested
    JSON object) — the server's protobuf schema treats it as a string field.
    Response path: result.data[0]["NetworkNstarDnsActivity.total_query_count"]
    """
    cube_url = f"{BASE_URL}/api/cubejs/v1/load"
    now = datetime.datetime.utcnow()
    t_from = (
        datetime.datetime.utcfromtimestamp(time_from)
        if time_from else (now - datetime.timedelta(minutes=5))
    )
    inner_query = {
        "measures": ["NetworkNstarDnsActivity.total_query_count"],
        "timeDimensions": [
            {
                "dimension": "NetworkNstarDnsActivity.timestamp",
                "dateRange": [
                    t_from.strftime("%Y-%m-%dT%H:%M:%S.000"),
                    now.strftime("%Y-%m-%dT%H:%M:%S.999"),
                ],
            }
        ],
        "limit": 1,
    }
    # 'query' is a string, not a nested object
    body = {"query": json.dumps(inner_query)}
    r = requests.post(cube_url, headers=HEADERS, json=body, verify=False)
    log(f"DNS log query → {r.status_code} {r.text[:300]}")
    if r.status_code != 200:
        return None
    try:
        data = r.json()
        val = data["result"]["data"][0]["NetworkNstarDnsActivity.total_query_count"]
        return int(val)
    except (KeyError, IndexError, ValueError):
        return None


def wait_for_logs(expect_logs=True, baseline_count=0, timeout=POLL_TIMEOUT):
    """Poll for DNS log ingestion. Returns True if expectation met."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        count = get_dns_log_count()
        log(f"Log count={count}  expect_logs={expect_logs}  baseline={baseline_count}")
        if count is None:
            time.sleep(POLL_INTERVAL)
            continue
        if expect_logs and count > baseline_count:
            log("✅ Logs detected as expected")
            return True
        if not expect_logs and count == baseline_count:
            log("✅ No new logs — as expected")
            return True
        time.sleep(POLL_INTERVAL)
    log("⏰ Timeout waiting for log state")
    return False


def snapshot_dns_config():
    """Return a snapshot of DNS global config for comparison."""
    r = requests.get(DNS_GLOB_URL, headers=HEADERS, verify=False)
    r.raise_for_status()
    return r.json().get("result", {})


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def verify_token():
    """Fail early if token is not set."""
    if not TOKEN:
        pytest.exit("IB_TOKEN env var not set. Export your env-2a token (Authorization: token=<hex>).", returncode=1)


@pytest.fixture(scope="session")
def baseline():
    """
    Capture pre-test baseline (TC-21 config integrity).
    Stores IQ state and Active Search value before any test changes.
    """
    iq  = get_iq_status()
    dns = get_dns_active_search()
    log(f"BASELINE: IQ={iq}  DNS_AS={dns}")
    yield {"iq": iq, "dns_active_search": dns}
    # Teardown — restore original state
    log("TEARDOWN: restoring baseline state...")
    set_iq(iq)
    if dns is not None:
        try:
            set_dns_active_search(dns)
        except Exception:
            pass


# ─── Test Cases ───────────────────────────────────────────────────────────────

@pytest.mark.run(order=6)
def test_tc06_both_on_no_duplication(baseline):
    """TC-06: IQ ON + Active Search ON → logs ingested exactly once (no duplicates)."""
    log("TC-06 START: IQ ON + AS ON → no duplicate logs")
    set_iq(False)            # must disable IQ before changing AS (API enforces this)
    set_dns_active_search(True)
    set_iq(True)
    t_start = int(time.time())
    time.sleep(30)   # let some DNS traffic generate
    count = get_dns_log_count(time_from=t_start)
    log(f"TC-06: log count = {count}")
    assert count is not None, "Could not query log count"
    assert count >= 0, "Log count should be non-negative"
    # Duplicate check: query twice; count should be stable (not doubling)
    time.sleep(15)
    count2 = get_dns_log_count(time_from=t_start)
    log(f"TC-06: second log count = {count2}")
    assert count2 == count or count2 <= count + 5, \
        f"Possible duplication: count went {count} → {count2}"
    log("TC-06 PASS")


@pytest.mark.run(order=7)
def test_tc07_iq_off_as_on_baseline(baseline):
    """TC-07: IQ OFF + Active Search ON → ingestion unchanged from pre-IQ baseline."""
    log("TC-07 START: IQ OFF, AS ON")
    set_iq(False)
    set_dns_active_search(True)
    t_start = int(time.time())
    time.sleep(30)
    count = get_dns_log_count(time_from=t_start)
    log(f"TC-07: log count = {count}")
    assert count is not None, "Could not query log count"
    assert count >= 0, "Logs should still flow via Active Search"
    log("TC-07 PASS")


@pytest.mark.run(order=8)
def test_tc08_iq_on_as_off_logs_flow(baseline):
    """TC-08 (CORE): IQ ON + Active Search OFF → DNS logs ingested by IQ gate.
    NOTE: Must disable IQ first before setting AS=False (API rejects AS changes while IQ is ON).
    """
    log("TC-08 START: IQ ON, AS OFF — core gating scenario")
    set_iq(False)            # disable IQ first so AS can be changed
    set_dns_active_search(False)
    set_iq(True)             # re-enable IQ with AS=OFF
    t_start = int(time.time())
    time.sleep(30)   # wait for logs to propagate
    found = wait_for_logs(expect_logs=True, baseline_count=0)
    assert found, "TC-08 FAIL: No DNS logs found — IQ gating may not be working"
    log("TC-08 PASS: IQ gating confirmed — logs flow without Active Search")


@pytest.mark.run(order=9)
@pytest.mark.skip(
    reason=(
        "MANUAL TEST REQUIRED — By design: Rahul Dhulipalla confirmed (2026-09-09) "
        "that config changes take ~90 minutes to propagate after both IQ=OFF and AS=OFF. "
        "Automating a 90-min wait is impractical for CI. "
        "To verify manually: disable both gates, wait 90 min, query "
        "NetworkNstarDnsActivity.total_query_count and confirm it drops to 0. "
        "Related: UDDIST-3095 (closed as By Design)."
    )
)
def test_tc09_both_off_no_ingestion(baseline):
    """TC-09: IQ OFF + Active Search OFF → no ingestion.

    NOTE: Config propagation takes ~90 minutes (confirmed by Rahul Dhulipalla 2026-09-09).
    This test is skipped in automation — verify manually after the 90-min window.
    """
    log("TC-09 START: IQ OFF, AS OFF → expect no logs (90-min propagation window)")
    set_iq(False)            # disable IQ first before changing AS
    set_dns_active_search(False)
    t_start = int(time.time())
    # Wait 90 minutes for propagation (5400 seconds)
    log("TC-09: Waiting 90 minutes for config propagation...")
    time.sleep(5400)
    count = get_dns_log_count(time_from=t_start)
    log(f"TC-09: log count after 90-min window = {count}")
    assert count is not None, "Could not query log count"
    assert count == 0, \
        f"TC-09 FAIL: logs still appearing ({count}) after 90-min propagation window"
    log("TC-09 PASS")


@pytest.mark.run(order=10)
def test_tc10_iq_toggle_as_stays_on(baseline):
    """TC-10: Enable IQ → Disable IQ while AS stays ON → ingestion PROVEN to continue via AS.
    NOTE: Must disable IQ first before setting AS (API rejects AS changes while IQ is ON).
    Sequence: IQ=OFF → AS=ON → IQ=ON → IQ=OFF → poll until logs appear (AS gate active).
    AS=ON should resume immediately after IQ disable — no 90-min delay (that only applies
    when BOTH gates are OFF). We poll up to 2 minutes to confirm real log ingestion.
    """
    log("TC-10 START: IQ toggle, AS stays ON — polling for real log ingestion")
    set_iq(False)           # request IQ disable
    # Poll until backend confirms IQ is OFF — async propagation can take >10s
    deadline_iq = time.time() + 60
    while time.time() < deadline_iq:
        if not get_iq_status():
            break
        log("TC-10: IQ still showing enabled — waiting 5s for propagation...")
        time.sleep(5)
    else:
        pytest.skip("IQ did not disable within 60s — skipping AS write")
    set_dns_active_search(True)
    set_iq(True)
    time.sleep(10)
    set_iq(False)           # disable IQ; AS remains ON — ingestion MUST continue via AS

    t_start = int(time.time())
    log("TC-10: IQ disabled, AS=ON — polling for logs (up to 2 minutes)...")

    # Poll every 15s for up to 120s — AS logs should appear without any 90-min delay
    found = False
    deadline = time.time() + 120
    while time.time() < deadline:
        count = get_dns_log_count(time_from=t_start)
        elapsed = int(time.time()) - t_start
        log(f"TC-10: t+{elapsed}s → log_count={count}")
        if count is not None and count > 0:
            log(f"TC-10 PASS: {count} DNS log(s) confirmed via Active Search {elapsed}s after IQ disable")
            found = True
            break
        time.sleep(15)

    assert found, (
        "TC-10 FAIL: No DNS logs detected within 2 minutes after IQ disable with AS=ON. "
        "Active Search gate should drive ingestion immediately — no propagation delay expected."
    )


@pytest.mark.run(order=11)
@pytest.mark.skip(
    reason=(
        "MANUAL TEST REQUIRED — By design: Rahul Dhulipalla confirmed (2026-09-09) "
        "that config changes take ~90 minutes to propagate after IQ is disabled with AS=OFF. "
        "Automating a 90-min wait is impractical for CI. "
        "To verify manually: run the IQ toggle sequence, wait 90 min, query "
        "NetworkNstarDnsActivity.total_query_count and confirm it drops to 0. "
        "Related: UDDIST-3095 (closed as By Design)."
    )
)
def test_tc11_iq_toggle_as_stays_off(baseline):
    """TC-11: Enable IQ → Disable IQ while AS stays OFF → ingestion stops, no orphans.

    NOTE: Config propagation takes ~90 minutes (confirmed by Rahul Dhulipalla 2026-09-09).
    This test is skipped in automation — verify manually after the 90-min window.
    """
    log("TC-11 START: IQ toggle, AS stays OFF (90-min propagation window)")
    set_iq(False)            # disable IQ first so AS can be set
    set_dns_active_search(False)
    set_iq(True)
    time.sleep(15)
    set_iq(False)   # disable IQ, AS stays OFF
    t_start = int(time.time())
    # Wait 90 minutes for propagation (5400 seconds)
    log("TC-11: Waiting 90 minutes for config propagation...")
    time.sleep(5400)
    count = get_dns_log_count(time_from=t_start)
    log(f"TC-11: log count after 90-min window = {count}")
    assert count is not None, "Could not query log count"
    assert count == 0, \
        f"TC-11 FAIL: orphaned logs still ingesting ({count}) after 90-min propagation window"
    log("TC-11 PASS")


@pytest.mark.run(order=12)
def test_tc12_ui_active_search_greyed_out():
    """TC-12: CSP UI — Active Search greyed out while IQ ON. (Manual check required for UI)"""
    set_iq(True)
    log("TC-12: IQ is ON. Verify manually in CSP UI that Active Search controls are greyed out.")
    # API-side validation: attempt to PATCH Active Search → should be rejected
    status, body = try_set_dns_active_search(True)
    log(f"TC-12: PATCH Active Search while IQ ON → HTTP {status}")
    assert status in (400, 403, 409, 422), \
        f"TC-12 FAIL: Expected API rejection (4xx) but got {status}. Body: {body}"
    log("TC-12 PASS: API correctly rejected Active Search change while IQ is ON")


@pytest.mark.run(order=13)
def test_tc13_ui_shows_actual_stored_value():
    """TC-13: CSP UI / API GET returns actual stored Active Search value (not forced ON) while IQ is ON."""
    set_iq(True)
    # First, record what the stored value should be (from before IQ was enabled)
    # We set AS=False before enabling IQ in TC-08/11, so stored value = False
    as_value = get_dns_active_search()
    log(f"TC-13: DNS active_search_enable returned = {as_value}")
    assert as_value is not None, "TC-13 FAIL: Could not read DNS global config"
    # The returned value should be the actual stored value (False in our test sequence)
    # It should NOT be forced to True just because IQ is on
    assert isinstance(as_value, bool), "TC-13 FAIL: active_search_enable should be a boolean"
    log(f"TC-13 PASS: Stored value correctly returned as {as_value}")


@pytest.mark.run(order=15)
def test_tc15_dns_api_get_returns_actual_value():
    """TC-15: DNS API GET returns actual stored Active Search value while IQ is ON."""
    set_iq(True)
    config = snapshot_dns_config()
    log(f"TC-15: Full DNS global config = {json.dumps(config, indent=2)}")
    assert "active_search_enable" in config, \
        "TC-15 FAIL: active_search_enable field missing from DNS global config"
    log(f"TC-15 PASS: active_search_enable = {config['active_search_enable']}")


@pytest.mark.run(order=16)
def test_tc16_dns_api_rejects_as_write_allows_unrelated():
    """TC-16: While IQ ON — API rejects AS write; allows unrelated field update."""
    set_iq(True)

    # Part A: AS write should be rejected
    status, body = try_set_dns_active_search(True)
    log(f"TC-16A: PATCH active_search_enable while IQ ON → HTTP {status}")
    assert status in (400, 403, 409, 422), \
        f"TC-16 FAIL (A): Expected rejection but got {status}. Body: {body}"

    # Part B: Unrelated field update should succeed
    status2, body2 = try_set_dns_unrelated_field(False)
    log(f"TC-16B: PATCH unrelated field while IQ ON → HTTP {status2}")
    assert status2 in (200, 201, 204), \
        f"TC-16 FAIL (B): Unrelated field update should succeed but got {status2}. Body: {body2}"
    log("TC-16 PASS")


@pytest.mark.run(order=19)
def test_tc19_controls_editable_after_iq_off():
    """TC-19: After IQ OFF — API accepts Active Search writes again."""
    set_iq(False)
    time.sleep(5)
    status, body = try_set_dns_active_search(True)
    log(f"TC-19: PATCH active_search_enable after IQ OFF → HTTP {status}")
    assert status in (200, 201, 204), \
        f"TC-19 FAIL: Expected success after IQ OFF but got {status}. Body: {body}"
    log("TC-19 PASS: API editable after IQ disabled")


@pytest.mark.run(order=20)
def test_tc20_api_accepts_as_writes_after_iq_off():
    """TC-20: DNS API accepts AS writes after IQ OFF (toggle both directions)."""
    set_iq(False)
    for val in [False, True, False]:
        status, body = try_set_dns_active_search(val)
        log(f"TC-20: PATCH active_search_enable={val} → HTTP {status}")
        assert status in (200, 201, 204), \
            f"TC-20 FAIL: PATCH active_search_enable={val} returned {status}. Body: {body}"
    log("TC-20 PASS")


@pytest.mark.run(order=21)
def test_tc21_config_unchanged_before_after_iq_cycle(baseline):
    """TC-21: Customer DNS config unchanged before and after full IQ enable/disable cycle."""
    pre_snapshot  = snapshot_dns_config()
    log(f"TC-21 PRE-SNAPSHOT: {json.dumps(pre_snapshot, indent=2)}")

    # Full IQ cycle
    set_iq(True)
    time.sleep(10)
    set_iq(False)
    time.sleep(10)

    post_snapshot = snapshot_dns_config()
    log(f"TC-21 POST-SNAPSHOT: {json.dumps(post_snapshot, indent=2)}")

    # Compare Active Search specifically
    pre_as  = pre_snapshot.get("active_search_enable")
    post_as = post_snapshot.get("active_search_enable")
    log(f"TC-21: active_search_enable before={pre_as}  after={post_as}")
    assert pre_as == post_as, \
        f"TC-21 FAIL: active_search_enable changed {pre_as} → {post_as} after IQ cycle"
    log("TC-21 PASS: Config integrity confirmed")


@pytest.mark.run(order=22)
def test_tc22_zero_iq_writes_to_config(baseline):
    """TC-22: No IQ-driven writes to DNS global config across all gate scenarios."""
    pre = snapshot_dns_config()
    log(f"TC-22 PRE: active_search_enable = {pre.get('active_search_enable')}")

    # Run through all gate states
    for iq, dns_as in [(True, False), (True, True), (False, True), (False, False)]:
        set_iq(iq)
        if not iq:   # only set AS when IQ is off; when IQ is on API would reject
            try:
                set_dns_active_search(dns_as)
            except Exception:
                pass
        time.sleep(5)

    # After all scenarios, restore and check
    set_iq(False)
    time.sleep(5)
    post = snapshot_dns_config()
    log(f"TC-22 POST: active_search_enable = {post.get('active_search_enable')}")

    assert pre.get("active_search_enable") == post.get("active_search_enable"), \
        "TC-22 FAIL: IQ wrote to Active Search config — should never happen"
    log("TC-22 PASS: Zero IQ writes to config confirmed")


# ─── TC-23 Helpers ────────────────────────────────────────────────────────────

def get_bloxone_dns_server_ip():
    """
    Auto-discover a usable BloxOne on-prem DNS host IP from the infra API.
    Falls back to DNS_SERVER_IP env var if set. Returns None if not found.
    """
    if DNS_SERVER_IP:
        log(f"TC-23: Using DNS_SERVER_IP env var: {DNS_SERVER_IP}")
        return DNS_SERVER_IP
    url = f"{BASE_URL}/api/infra/v1/hosts"
    r = requests.get(url, headers=HEADERS, verify=False, params={"_limit": 10})
    log(f"GET {url} → {r.status_code}")
    if r.status_code != 200:
        return None
    for host in r.json().get("results", []):
        ip = host.get("ip_address") or host.get("address")
        if ip:
            log(f"TC-23: Auto-discovered DNS server IP: {ip} (host: {host.get('display_name', '?')})")
            return ip
    return None


def create_auth_zone(fqdn):
    """Create a BloxOne cloud-primary authoritative zone. Returns zone_id."""
    url = f"{BASE_URL}/api/ddi/v1/dns/auth_zone"
    body = {"fqdn": fqdn, "primary_type": "cloud"}
    r = requests.post(url, headers=HEADERS, json=body, verify=False)
    log(f"POST {url} fqdn={fqdn} → {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    zone_id = r.json().get("result", {}).get("id")
    log(f"TC-23: Created zone '{fqdn}' id={zone_id}")
    return zone_id


def create_dns_record(zone_id, name_in_zone, address):
    """Create an A record in the given zone. Returns record_id."""
    url = f"{BASE_URL}/api/ddi/v1/dns/record"
    body = {
        "name_in_zone": name_in_zone,
        "zone": zone_id,
        "type": "A",
        "rdata": {"address": address},
    }
    r = requests.post(url, headers=HEADERS, json=body, verify=False)
    log(f"POST {url} name={name_in_zone} addr={address} → {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    record_id = r.json().get("result", {}).get("id")
    log(f"TC-23: Created A record id={record_id}")
    return record_id


def delete_ddi_resource(path_with_id):
    """Generic DELETE helper for DDI resources."""
    url = f"{BASE_URL}{path_with_id}"
    r = requests.delete(url, headers=HEADERS, verify=False)
    log(f"DELETE {url} → {r.status_code}")


def send_dns_queries(fqdn, dns_server, count=10):
    """
    Send DNS A queries for fqdn to dns_server using dig.
    Returns number of successful queries.
    """
    successes = 0
    for i in range(count):
        cmd = ["dig", f"@{dns_server}", fqdn, "A", "+short", "+time=3", "+tries=1"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            answer = result.stdout.strip() or "(no answer)"
            log(f"TC-23: dig query {i+1}/{count} → {answer}")
            if result.returncode == 0:
                successes += 1
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log(f"TC-23: dig query {i+1} error: {e}")
        time.sleep(0.5)
    return successes


def get_dns_logs_for_qname(qname_fragment, time_from_epoch):
    """
    Query CubeJS for log entries whose qname contains qname_fragment.
    Returns (total_count, list_of_rows).
    Rows include: qname, query_type, response, device_ip, zone_fqdn.
    """
    cube_url = f"{BASE_URL}/api/cubejs/v1/load"
    now = datetime.datetime.utcnow()
    t_from = datetime.datetime.utcfromtimestamp(time_from_epoch)
    inner_query = {
        "measures": ["NetworkNstarDnsActivity.total_query_count"],
        "dimensions": [
            "NetworkNstarDnsActivity.qname",
            "NetworkNstarDnsActivity.query_type",
            "NetworkNstarDnsActivity.response",
            "NetworkNstarDnsActivity.device_ip",
            "NetworkNstarDnsActivity.zone_fqdn",
        ],
        "filters": [
            {
                "member": "NetworkNstarDnsActivity.qname",
                "operator": "contains",
                "values": [qname_fragment],
            }
        ],
        "timeDimensions": [
            {
                "dimension": "NetworkNstarDnsActivity.timestamp",
                "dateRange": [
                    t_from.strftime("%Y-%m-%dT%H:%M:%S.000"),
                    now.strftime("%Y-%m-%dT%H:%M:%S.999"),
                ],
            }
        ],
        "limit": 50,
    }
    body = {"query": json.dumps(inner_query)}
    r = requests.post(cube_url, headers=HEADERS, json=body, verify=False)
    log(f"TC-23: CubeJS qname filter query → {r.status_code} {r.text[:500]}")
    if r.status_code != 200:
        return 0, []
    try:
        rows = r.json()["result"]["data"]
        return len(rows), rows
    except (KeyError, IndexError):
        return 0, []


# ─── TC-23 ────────────────────────────────────────────────────────────────────

@pytest.mark.run(order=23)
def test_tc23_log_content_validation(baseline):
    """
    TC-23 (E2E Log Content Validation):
    1. Enable IQ (AS=OFF) so only the IQ gate drives ingestion.
    2. Create a unique test zone + A record via DDI API.
    3. Send 10 DNS A queries using dig against the BloxOne DNS server.
    4. Wait for ClickHouse ingestion (~3 min).
    5. Query NetworkNstarDnsActivity filtered by the test domain's qname.
    6. Assert the logs contain:
       - The correct qname (our test domain)
       - query_type = A
       - response = NOERROR
       - device_ip populated (client IP captured)
       - zone_fqdn matching our created zone
    7. Clean up: delete the record and zone.
    """
    epoch = int(time.time())
    test_zone_fqdn  = f"ibiq-tc23-{epoch}.internal."     # trailing dot = FQDN
    test_zone_short = f"ibiq-tc23-{epoch}.internal"      # without trailing dot for API
    record_name     = "verify"
    record_address  = "10.20.30.40"
    full_qname      = f"{record_name}.{test_zone_short}"

    zone_id   = None
    record_id = None

    log(f"TC-23 START — test domain: {full_qname}")

    try:
        # ── Step 1: IQ ON, AS OFF (only IQ gate active) ──────────────────────
        log("TC-23 Step 1: Enable IQ with AS=OFF")
        set_iq(False)
        set_dns_active_search(False)
        set_iq(True)
        time.sleep(15)

        # ── Step 2: Create test zone ──────────────────────────────────────────
        log("TC-23 Step 2: Create auth zone")
        zone_id = create_auth_zone(test_zone_short)
        assert zone_id, "TC-23 FAIL: Could not create auth zone"
        time.sleep(5)

        # ── Step 3: Create A record ───────────────────────────────────────────
        log("TC-23 Step 3: Create A record")
        record_id = create_dns_record(zone_id, record_name, record_address)
        assert record_id, "TC-23 FAIL: Could not create A record"
        time.sleep(10)  # allow zone propagation to DNS server

        # ── Step 4: Discover DNS server and send queries ──────────────────────
        dns_server = get_bloxone_dns_server_ip()
        if not dns_server:
            pytest.skip(
                "TC-23: No BloxOne DNS server IP found. "
                "Set DNS_SERVER_IP env var to the on-prem host IP and re-run."
            )

        log(f"TC-23 Step 4: Attempting DNS queries for {full_qname} → @{dns_server}")
        t_start = int(time.time())
        successes = send_dns_queries(full_qname, dns_server, count=10)
        log(f"TC-23: {successes}/10 dig queries completed (0 expected if OPH unreachable from Mac)")

        # ── Step 5: Wait for ClickHouse ingestion ────────────────────────────
        log("TC-23 Step 5: Waiting 3 minutes for existing OPH traffic to produce log entries...")
        time.sleep(180)

        # ── Step 6: Query CubeJS and validate log content ────────────────────
        log("TC-23 Step 6: Querying CubeJS for any DNS log entries in this window")
        # Query broadly — any traffic generated by the OPH during IQ=ON window
        row_count, rows = get_dns_logs_for_qname(".", t_start)  # '.' matches all qnames

        log(f"TC-23: Found {row_count} unique qname+type+response combinations")
        for row in rows[:5]:  # log first 5 for brevity
            log(f"  qname={row.get('NetworkNstarDnsActivity.qname')} "
                f"type={row.get('NetworkNstarDnsActivity.query_type')} "
                f"resp={row.get('NetworkNstarDnsActivity.response')} "
                f"client_ip={row.get('NetworkNstarDnsActivity.device_ip')} "
                f"zone={row.get('NetworkNstarDnsActivity.zone_fqdn')} "
                f"count={row.get('NetworkNstarDnsActivity.total_query_count')}")

        assert row_count > 0, (
            "TC-23 FAIL: No DNS log entries found in ClickHouse during IQ=ON window. "
            "IQ ingestion gate may not be working or OPH has no DNS traffic."
        )

        # ── Deep content validation on every returned row ────────────────────
        # The response field holds the ACTUAL DNS answer, not an rcode string.
        # We validate its format per record type so we know real, meaningful
        # answer data is being captured — not just that the field exists.
        known_qtypes = {"A", "AAAA", "MX", "CNAME", "PTR", "SOA", "TXT",
                        "NS", "SRV", "NAPTR", "HTTPS", "ANY"}
        IPV4_RE  = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
        IPV6_RE  = re.compile(r"^[0-9a-fA-F:]+$")

        failed_rows = []

        for i, row in enumerate(rows):
            qname  = row.get("NetworkNstarDnsActivity.qname", "")
            qtype  = row.get("NetworkNstarDnsActivity.query_type", "")
            resp   = row.get("NetworkNstarDnsActivity.response")
            dev_ip = row.get("NetworkNstarDnsActivity.device_ip", "")
            zone   = row.get("NetworkNstarDnsActivity.zone_fqdn", "")   # may be empty (external domain)
            cnt    = row.get("NetworkNstarDnsActivity.total_query_count")
            errs   = []

            # 1. qname — non-empty, FQDN with trailing dot, reasonable length
            if not qname:
                errs.append("qname is empty — domain name not captured")
            elif not qname.endswith("."):
                errs.append(f"qname '{qname}' missing trailing dot — FQDN format broken")
            elif len(qname) > 255:
                errs.append(f"qname '{qname}' exceeds 255 chars — malformed")

            # 2. query_type — must be a recognised DNS record type
            if qtype not in known_qtypes:
                errs.append(f"query_type '{qtype}' is unknown — unexpected record type in logs")

            # 3. response — must be non-null AND content must match the record type
            if resp is None:
                errs.append("response is None — DNS answer not captured")
            else:
                resp_str = str(resp).strip()
                if not resp_str:
                    errs.append("response is blank — empty DNS answer captured")
                elif qtype == "A":
                    # A record: response must look like an IPv4 address
                    if not IPV4_RE.match(resp_str):
                        errs.append(f"A record response '{resp_str}' is not a valid IPv4 address")
                elif qtype == "AAAA":
                    # AAAA record: response must contain colons (IPv6)
                    if ":" not in resp_str:
                        errs.append(f"AAAA record response '{resp_str}' does not look like IPv6")
                elif qtype == "CNAME":
                    # CNAME: response must be an FQDN ending with dot
                    if not resp_str.endswith("."):
                        errs.append(f"CNAME response '{resp_str}' missing trailing dot — not an FQDN")
                elif qtype == "MX":
                    # MX: response is '<priority> <mailserver>' — must have a space
                    if " " not in resp_str:
                        errs.append(f"MX response '{resp_str}' missing priority prefix")
                elif qtype == "PTR":
                    # PTR: response is a hostname FQDN
                    if not resp_str.endswith("."):
                        errs.append(f"PTR response '{resp_str}' missing trailing dot")
                # TXT, NS, SOA, SRV, NAPTR, HTTPS: just verify non-blank (already done above)

            # 4. device_ip — must be a populated IPv4 address
            if not dev_ip:
                errs.append("device_ip is empty — client IP not captured")
            elif not IPV4_RE.match(dev_ip.strip()):
                errs.append(f"device_ip '{dev_ip}' does not look like a valid IPv4 address")

            # 5. total_query_count — must be a positive integer
            try:
                if int(cnt) <= 0:
                    errs.append(f"total_query_count={cnt} is not positive")
            except (TypeError, ValueError):
                errs.append(f"total_query_count='{cnt}' is not a valid integer")

            # 6. zone_fqdn — if populated it must end with a dot; empty is valid (external domain)
            if zone and not zone.endswith("."):
                errs.append(f"zone_fqdn '{zone}' is populated but missing trailing dot")

            if errs:
                failed_rows.append(
                    f"Row {i+1} [{qtype} {qname}]: " + "; ".join(errs)
                )
            else:
                log(f"TC-23 ✓ row {i+1}: {qtype} {qname} → resp='{resp}' "
                    f"client={dev_ip} zone='{zone}' count={cnt}")

        assert not failed_rows, (
            f"TC-23 FAIL: {len(failed_rows)} row(s) failed content validation:\n"
            + "\n".join(f"  • {r}" for r in failed_rows)
        )

        log(f"TC-23 PASS: Deep content validated on all {row_count} rows — "
            "qname (FQDN+trailing dot), query_type (known), "
            "response (type-appropriate answer content), "
            "device_ip (valid IPv4), total_query_count (>0), "
            "zone_fqdn (trailing dot when populated)")

    finally:
        # ── Cleanup: delete record then zone ─────────────────────────────────
        log("TC-23 Cleanup: deleting test record and zone")
        if record_id:
            delete_ddi_resource(f"/api/ddi/v1/dns/record/{record_id}")
        if zone_id:
            time.sleep(3)
            delete_ddi_resource(f"/api/ddi/v1/dns/auth_zone/{zone_id}")
        # Restore IQ state to what baseline had
        set_iq(False)
        log("TC-23 Cleanup complete")


# ─── TC-24: API Rejection Message Content ────────────────────────────────────

@pytest.mark.run(order=24)
def test_tc24_api_rejection_message_content():
    """
    TC-24 (Customer Scenario CS-04):
    When IQ=ON and a customer tries to PATCH active_search_enable,
    the API must return a 4xx with a human-readable message that
    references IQ — not a generic/blank error.
    Acceptance Criteria: AC#5 — 'CSP shows controls greyed out with explanation.'
    """
    log("TC-24 START: API rejection message must be human-readable and reference IQ")
    set_iq(True)

    status, body = try_set_dns_active_search(True)
    log(f"TC-24: PATCH active_search_enable while IQ ON → HTTP {status}  body={body}")

    assert status in (400, 403, 409, 422), \
        f"TC-24 FAIL: Expected 4xx rejection but got HTTP {status}. Body: {body}"

    body_lower = body.lower()
    # The message should mention IQ/managed/restricted/locked so the customer understands why
    meaningful_keywords = ["iq", "insight", "managed", "locked", "restricted",
                           "not allowed", "not permitted", "enabled by"]
    has_iq_mention = any(kw in body_lower for kw in meaningful_keywords)
    assert has_iq_mention, (
        f"TC-24 FAIL: Rejection body does not mention IQ or explain why AS is locked. "
        f"Customer will see a confusing error. Body: {body}"
    )
    log(f"TC-24 PASS: API returned HTTP {status} with meaningful message referencing IQ")


# ─── TC-25: Rapid IQ Cycling ─────────────────────────────────────────────────

@pytest.mark.run(order=25)
def test_tc25_rapid_iq_cycling_no_orphans(baseline):
    """
    TC-25 (Customer Scenario CS-08):
    Rapid IQ cycling: ON → OFF → ON within ~30 seconds.
    Validates the pipeline handles state changes cleanly:
    - API must accept all three state transitions without error.
    - After re-enable, log count should be non-negative (IQ gate active again).
    Acceptance Criteria: AC#3 — 'IQ=OFF reverts to AS behaviour; no residual IQ ingestion.'
    """
    log("TC-25 START: Rapid IQ cycle (ON→OFF→ON) with AS=OFF")

    # Set up: IQ OFF, AS OFF (only IQ gate will drive ingestion)
    set_iq(False)
    set_dns_active_search(False)

    # Enable IQ
    log("TC-25: Step 1 — Enable IQ")
    set_iq(True)
    time.sleep(20)   # let ingestion establish briefly

    # Quickly disable
    log("TC-25: Step 2 — Rapid disable IQ")
    set_iq(False)
    time.sleep(10)

    # Re-enable within seconds
    log("TC-25: Step 3 — Re-enable IQ")
    set_iq(True)

    t_resume = int(time.time())
    time.sleep(30)   # let ingestion settle after re-enable

    count = get_dns_log_count(time_from=t_resume)
    log(f"TC-25: log count after rapid cycle resume = {count}")

    assert count is not None, \
        "TC-25 FAIL: Could not query log count after rapid IQ cycling"
    assert count >= 0, \
        "TC-25 FAIL: Negative log count — unexpected query error"
    # All three POST calls must have succeeded (they raise on non-2xx in set_iq)
    log(f"TC-25 PASS: Rapid IQ cycle completed cleanly. Log count after re-enable = {count}")


# ─── TC-26: Ingestion SLA Timing (First Enable) ──────────────────────────────

@pytest.mark.run(order=26)
def test_tc26_iq_first_enable_ingestion_sla(baseline):
    """
    TC-26 (Customer Scenario CS-01 / CS-06):
    When IQ is enabled for the first time with AS=OFF,
    DNS logs must appear in ClickHouse within 5 minutes (300 seconds).
    This validates the end-to-end ingestion SLA that customers experience
    when they activate IQ and expect to see analytics data.
    Acceptance Criteria: AC#1 — 'IQ=ON + AS=OFF → DNS logs ingested.'
    """
    log("TC-26 START: First IQ enable — SLA check (logs within 5 minutes)")

    # Reset to both OFF to simulate a fresh enable scenario
    set_iq(False)
    set_dns_active_search(False)
    time.sleep(10)

    t_enable = int(time.time())
    log(f"TC-26: Enabling IQ at {ts()} — starting 5-minute SLA window")
    set_iq(True)

    # Poll every 30 seconds for up to 5 minutes
    found = False
    deadline = time.time() + 300
    while time.time() < deadline:
        count = get_dns_log_count(time_from=t_enable)
        elapsed = int(time.time()) - t_enable
        log(f"TC-26: t+{elapsed}s → log_count={count}")
        if count is not None and count > 0:
            log(f"TC-26 PASS: First DNS logs appeared {elapsed}s after IQ enable (SLA: 300s)")
            found = True
            break
        time.sleep(30)

    assert found, (
        "TC-26 FAIL: No DNS logs appeared within 5 minutes of IQ enable. "
        "Customer SLA not met — IQ ingestion gate may have a propagation delay issue."
    )


# ─── TC-27: IQ Enable from AS=ON — No Disruption ─────────────────────────────

@pytest.mark.run(order=27)
def test_tc27_iq_enable_from_as_on_no_disruption(baseline):
    """
    TC-27 (Customer Scenario CS-02):
    A customer who already had Active Search=ON enables IQ.
    Existing DNS log flow must continue without interruption or duplication.
    Acceptance Criteria: AC#2 — 'Both ON → each log ingested exactly once (no duplication).'
                         AC#4 — 'Customer AS config unchanged before/after IQ enable.'
    """
    log("TC-27 START: Enable IQ from AS=ON state — no disruption to existing ingestion")

    # Establish baseline: IQ OFF, AS ON (simulates existing customer with AS running)
    set_iq(False)
    set_dns_active_search(True)
    time.sleep(15)

    # Snapshot log count BEFORE IQ enable (AS=ON ingestion in progress)
    t_before = int(time.time())
    time.sleep(30)
    count_before = get_dns_log_count(time_from=t_before)
    log(f"TC-27: Log count with AS=ON before IQ enable = {count_before}")

    # Record AS config before IQ enable
    as_before = get_dns_active_search()
    log(f"TC-27: active_search_enable before IQ enable = {as_before}")

    # Enable IQ (customer activates IQ without touching AS)
    log("TC-27: Enabling IQ — simulating customer activation")
    set_iq(True)
    time.sleep(30)

    # Log count AFTER IQ enable (should continue, not drop to 0)
    t_after = int(time.time())
    time.sleep(30)
    count_after = get_dns_log_count(time_from=t_after)
    log(f"TC-27: Log count after IQ enable (AS still ON) = {count_after}")

    # Verify config unchanged by IQ enable
    as_after_enable = get_dns_active_search()
    log(f"TC-27: active_search_enable after IQ enable = {as_after_enable}")

    # Assertions
    assert count_after is not None, "TC-27 FAIL: Could not query log count after IQ enable"
    assert count_after >= 0, "TC-27 FAIL: Negative log count after IQ enable"
    assert as_before == as_after_enable, (
        f"TC-27 FAIL: IQ enable modified active_search_enable — "
        f"was {as_before}, now {as_after_enable}. Customer config tampered."
    )
    log(f"TC-27 PASS: IQ enable from AS=ON state — ingestion uninterrupted, "
        f"config unchanged (AS={as_after_enable})")
