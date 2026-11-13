cd "${WORKSPACE}"

# ── Venv setup ───────────────────────────────────────────────────────────────
python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt

# ── Token (keep out of console log) ─────────────────────────────────────────
{ set +x; } 2>/dev/null
export IB_TOKEN="${ib_token}"
set -x

# ── Optional: DNS server IP (auto-discovered if not set) ─────────────────────
export DNS_SERVER_IP="${dns_server_ip:-}"

# ── Output paths ─────────────────────────────────────────────────────────────
export PYTHONUNBUFFERED=1
mkdir -p reports
IB_RESULT_XML="${WORKSPACE}/reports/ibiq3028_junit.xml"
IB_RESULT_HTML="${WORKSPACE}/reports/ibiq3028_report.html"
rm -f "$IB_RESULT_XML" "$IB_RESULT_HTML"

# ── Token preflight ──────────────────────────────────────────────────────────
{ set +x; } 2>/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    -H "Authorization: token=${IB_TOKEN}" \
    "${base_url:-https://env-2a.test.infoblox.com}/api/iqconfig/v1/iqoperations")
set -x
echo "Token preflight: HTTP $code"
if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    echo "Token rejected — update the ib_token credential."
    exit 1
fi

# ── Run pytest ───────────────────────────────────────────────────────────────
set +e
pytest IBIQ_3028_test.py -v --tb=short \
    --junit-xml="$IB_RESULT_XML" \
    --html="$IB_RESULT_HTML" --self-contained-html \
    2>&1 | tee reports/run_output.log
rc=$?
set -e

# ── Upload JUnit results to ReportPortal ─────────────────────────────────────
if [ -s "$IB_RESULT_XML" ]; then
    rp_zip="${WORKSPACE}/${JOB_NAME}_env2a.zip"
    python3 -c "
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED)
z.write(sys.argv[2], 'ibiq3028_junit.xml')
z.close()
" "$rp_zip" "$IB_RESULT_XML"

    Reporting_portal_ip=$(python3 "${WORKSPACE}/API_Automation/aws_scripts_automation/reporting_portal_details.py" rp_ip)
    { set +x; } 2>/dev/null
    Reporting_portal_Access_token_id=$(python3 "${WORKSPACE}/API_Automation/aws_scripts_automation/reporting_portal_details.py" rp_token_id)
    echo "Uploading to ReportPortal: uddi_solution_qa_stage"
    rp_code=$(curl -s -o "${WORKSPACE}/rp_upload_response.json" -w '%{http_code}' --max-time 120 -X POST \
        --header 'Content-Type: multipart/form-data' \
        --header 'Accept: application/json' \
        --header "Authorization: ${Reporting_portal_Access_token_id}" \
        -F file=@"${rp_zip}" \
        "${Reporting_portal_ip}uddi_solution_qa_stage/launch/import") || rp_code="curl-error"
    unset Reporting_portal_Access_token_id
    set -x
    echo "ReportPortal upload: HTTP ${rp_code}"
    cat "${WORKSPACE}/rp_upload_response.json" 2>/dev/null; echo
else
    echo "No JUnit XML — skipping ReportPortal upload"
fi

exit $rc
