// Daily weather pull for major Canadian cities.
// Source: Environment and Climate Change Canada CityPage Weather (MSC Datamart).
//
// Runs on an Ubuntu agent. Creates its own virtualenv, pulls all cities,
// archives the CSV/JSON, and prunes old runs.
//
// Exit-code contract from ca_weather_pull.py:
//   0 = every city collected          -> SUCCESS
//   2 = partial, above threshold      -> UNSTABLE (someone should look, nothing is broken)
//   1 = below threshold or fatal      -> FAILURE

pipeline {

    // Change 'ubuntu' to whatever label your Ubuntu build agent carries,
    // or use `agent any` if the controller runs the job itself.
    agent { label 'ubuntu' }

    parameters {
        string(name: 'OUTDIR', defaultValue: '/var/lib/ca-weather',
               description: 'Persistent output directory on the agent')
        string(name: 'CITIES', defaultValue: '',
               description: 'Optional comma-separated subset, e.g. Toronto,Winnipeg. Blank = all')
        string(name: 'RETAIN_DAYS', defaultValue: '90',
               description: 'Delete dated output folders older than this. 0 = keep forever')
        string(name: 'MIN_SUCCESS_PCT', defaultValue: '80',
               description: 'Below this share of successful cities the build fails')
        booleanParam(name: 'VALIDATE_ONLY', defaultValue: false,
               description: 'Only check that every site code resolves; write no files')
    }

    // 06:30 every day, Winnipeg time. Environment Canada refreshes hourly, so the
    // exact minute does not matter much - this just puts it before the workday.
    triggers {
        cron('''TZ=America/Winnipeg
                30 6 * * *''')
    }

    options {
        timeout(time: 20, unit: 'MINUTES')
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '60', artifactNumToKeepStr: '30'))
    }

    environment {
        VENV          = "${WORKSPACE}/.venv"
        PY            = "${WORKSPACE}/.venv/bin/python"
        CA_WEATHER_TZ = 'America/Winnipeg'
        CA_WEATHER_UA = 'ca-weather-pull/1.1 (Scootaround IT; Jenkins daily job)'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
                sh 'git --no-pager log -1 --oneline || true'
            }
        }

        stage('Environment') {
            steps {
                sh """
                    set -eu
                    python3 --version
                    if [ ! -x "\$PY" ]; then
                        echo "Creating virtualenv at \$VENV"
                        python3 -m venv "\$VENV"
                    fi
                    "\$VENV/bin/pip" install --quiet --upgrade pip
                    "\$VENV/bin/pip" install --quiet -r requirements.txt
                    "\$PY" ca_weather_pull.py --version
                    mkdir -p '${params.OUTDIR}'
                """
            }
        }

        stage('Unit tests') {
            steps {
                // Offline parser tests - no network, so a datamart outage cannot
                // make these fail. If they break, the parser broke.
                sh '"$PY" -m unittest test_parse -v'
            }
        }

        stage('Pull weather') {
            steps {
                script {
                    def subset = params.CITIES?.trim() ? "--cities '${params.CITIES.trim()}'" : ''
                    def mode   = params.VALIDATE_ONLY ? '--validate-only' : ''

                    def rc = sh(returnStatus: true, script: """
                        set -o pipefail
                        "\$PY" ca_weather_pull.py \\
                            --outdir '${params.OUTDIR}' \\
                            --retain-days ${params.RETAIN_DAYS} \\
                            --min-success-pct ${params.MIN_SUCCESS_PCT} \\
                            --log-file '${WORKSPACE}/ca_weather_pull.log' \\
                            ${subset} ${mode}
                    """)

                    if (rc == 0) {
                        echo 'All cities collected.'
                    } else if (rc == 2) {
                        unstable('Some cities failed but the run stayed above the success threshold.')
                    } else {
                        error("ca_weather_pull.py exited ${rc} - see the log above.")
                    }
                }
            }
        }

        stage('Collect artifacts') {
            when { expression { !params.VALIDATE_ONLY } }
            steps {
                sh """
                    set -eu
                    rm -rf '${WORKSPACE}/artifacts'
                    mkdir -p '${WORKSPACE}/artifacts'
                    cp '${params.OUTDIR}'/latest.csv  '${WORKSPACE}/artifacts/' 2>/dev/null || true
                    cp '${params.OUTDIR}'/latest.json '${WORKSPACE}/artifacts/' 2>/dev/null || true
                    TODAY=\$(TZ=America/Winnipeg date +%F)
                    if [ -d '${params.OUTDIR}'/\$TODAY ]; then
                        cp '${params.OUTDIR}'/\$TODAY/* '${WORKSPACE}/artifacts/' || true
                    fi
                    ls -lh '${WORKSPACE}/artifacts'
                """
            }
        }

        stage('Summary') {
            when { expression { !params.VALIDATE_ONLY } }
            steps {
                script {
                    def summary = sh(returnStdout: true, script: """
                        "\$PY" - <<'EOF'
import json, os
p = os.path.join('${params.OUTDIR}', 'latest.json')
d = json.load(open(p, encoding='utf-8'))
print(f"{d['ok_count']}/{d['city_count']} cities  |  run date {d['run_date']}")
alerts = [(c['city'], a['description'])
          for c in d['cities'] for a in (c.get('alerts') or [])]
print(f"active weather alerts: {len(alerts)}")
for city, desc in alerts:
    print(f"  ! {city}: {desc}")
EOF
                    """).trim()
                    echo summary
                    currentBuild.description = summary.readLines()[0]
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'artifacts/*.csv, artifacts/*.json, ca_weather_pull.log',
                             allowEmptyArchive: true, fingerprint: true
        }
        unstable {
            echo 'Build UNSTABLE - one or more cities did not return data.'
            // mail to: 'it@scootaround.com',
            //      subject: "UNSTABLE: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
            //      body: "Partial weather pull. ${env.BUILD_URL}"
        }
        failure {
            echo 'Build FAILED - the weather pull did not produce usable output.'
            // mail to: 'it@scootaround.com',
            //      subject: "FAILED: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
            //      body: "Weather pull failed. ${env.BUILD_URL}console"
        }
        cleanup {
            sh "rm -rf '${WORKSPACE}/artifacts' || true"
        }
    }
}
