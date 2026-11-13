// IBIQ-3028: Auto-enabling 30-day Active Search on IQ enablement
// Repo: ckarthickinfoblox/IBIQ-3028-Automation, branch main
//
// Paste this into a Pipeline job (Definition: "Pipeline script from SCM").
pipeline {
    agent { label 'uddi_automation_clients' }

    options {
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
        disableConcurrentBuilds()
    }

    triggers {
        cron('H 7 * * 1')
    }

    parameters {
        string(name: 'GIT_BRANCH', defaultValue: 'main',
               description: 'Branch to run')
        string(name: 'BASE_URL', defaultValue: 'https://env-2a.test.infoblox.com',
               description: 'CSP base URL to test against')
    }

    environment {
        BASE_URL         = "${params.BASE_URL}"
        IB_TOKEN         = credentials('IB_TOKEN_ENV2A')
        IB_RESULT_XML    = "${env.WORKSPACE}/reports/ibiq3028_junit.xml"
        IB_RESULT_HTML   = "${env.WORKSPACE}/reports/ibiq3028_report.html"
        PYTHONUNBUFFERED = '1'
    }

    stages {
        stage('Checkout from GitHub') {
            steps {
                git url: 'https://github.com/ckarthickinfoblox/Auto-enabling-30-day-active-search-on-IQ-enablement.git',
                    branch: params.GIT_BRANCH,
                    credentialsId: 'GITHUB_CREDS'
            }
        }

        stage('Setup') {
            steps {
                sh '''
                    python3 --version
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install -q -r requirements.txt
                    rm -rf reports && mkdir -p reports
                '''
            }
        }

        stage('Token check') {
            steps {
                sh '''#!/bin/bash
                    code=$(printf 'header = "Authorization: token=%s"\\n' "$IB_TOKEN" | \
                        curl -s -o /dev/null -w '%{http_code}' \
                        --max-time 20 -K - \
                        "${BASE_URL}/api/iqconfig/v1/iqoperations")
                    echo "IQ operations endpoint: HTTP $code"
                    if [ "$code" = "401" ] || [ "$code" = "403" ]; then
                        echo "Token rejected — update the IB_TOKEN_ENV2A credential."
                        exit 1
                    fi
                '''
            }
        }

        stage('Run IBIQ-3028 Test Suite') {
            steps {
                sh '''#!/bin/bash
                    set -o pipefail
                    . .venv/bin/activate
                    pytest IBIQ_3028_test.py -v --tb=short \
                        --junit-xml="$IB_RESULT_XML" \
                        --html="$IB_RESULT_HTML" --self-contained-html \
                        2>&1 | tee reports/run_output.log
                '''
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'reports/ibiq3028_junit.xml'
            publishHTML(target: [
                allowMissing          : true,
                alwaysLinkToLastBuild : true,
                keepAll               : true,
                reportDir             : 'reports',
                reportFiles           : 'ibiq3028_report.html',
                reportName            : 'IBIQ-3028 Test Report'
            ])
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
        }
    }
}
