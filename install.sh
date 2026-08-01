#!/bin/sh

set -eu

APP_DIR="/opt/uplink-ledger"
CONFIG_DIR="/etc/uplink-ledger"
DATA_DIR="/var/lib/uplink-ledger"
UNIT_FILE="/etc/systemd/system/uplink-ledger.service"
SYSCONFIG_FILE="/etc/sysconfig/uplink-ledger"
SERVICE_USER="uplinkledger"
DATABASE_NAME="uplink_ledger"

LEGACY_APP_DIR="/opt/isp-loss-monitor"
LEGACY_CONFIG_DIR="/etc/isp-loss-monitor"
LEGACY_DATA_DIR="/var/lib/isp-loss-monitor"
LEGACY_UNIT_FILE="/etc/systemd/system/isp-loss-monitor.service"
LEGACY_SYSCONFIG_FILE="/etc/sysconfig/isp-loss-monitor"
LEGACY_SERVICE_USER="ispmon"
LEGACY_DATABASE_NAME="isp_loss_monitor"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MIGRATED_LEGACY=0
LEGACY_INSTALL=0

if [ -e "${LEGACY_UNIT_FILE}" ] \
   || [ -e "${LEGACY_APP_DIR}" ] \
   || [ -e "${LEGACY_CONFIG_DIR}" ] \
   || [ -e "${LEGACY_DATA_DIR}" ] \
   || [ -e "${LEGACY_SYSCONFIG_FILE}" ]; then
    LEGACY_INSTALL=1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run this installer as root (sudo ./install.sh)." >&2
    exit 1
fi

if [ "$(uname -s)" != "Linux" ] || [ ! -d /run/systemd/system ]; then
    echo "ERROR: This installer targets AlmaLinux 10 with systemd." >&2
    exit 1
fi

for command in python3 ping traceroute ip curl psql systemctl getent useradd usermod groupadd groupmod runuser; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: Required command is missing: ${command}" >&2
        echo "On AlmaLinux 10: dnf install python3 iputils traceroute curl postgresql shadow-utils util-linux" >&2
        exit 1
    fi
done

migrate_directory() {
    legacy_path="$1"
    current_path="$2"
    if [ -e "${legacy_path}" ] && [ -e "${current_path}" ]; then
        echo "ERROR: Both ${legacy_path} and ${current_path} exist." >&2
        echo "Resolve the duplicate installation paths before rerunning the installer." >&2
        exit 1
    fi
    if [ -e "${legacy_path}" ]; then
        mv "${legacy_path}" "${current_path}"
        MIGRATED_LEGACY=1
    fi
}

migrate_service_identity() {
    if ! getent passwd "${LEGACY_SERVICE_USER}" >/dev/null 2>&1; then
        return
    fi
    if getent passwd "${SERVICE_USER}" >/dev/null 2>&1; then
        echo "ERROR: Both ${LEGACY_SERVICE_USER} and ${SERVICE_USER} users exist." >&2
        echo "Resolve the duplicate service identities before rerunning the installer." >&2
        exit 1
    fi

    legacy_group=$(id -gn "${LEGACY_SERVICE_USER}")
    if [ "${legacy_group}" = "${LEGACY_SERVICE_USER}" ]; then
        if getent group "${SERVICE_USER}" >/dev/null 2>&1; then
            usermod --gid "${SERVICE_USER}" "${LEGACY_SERVICE_USER}"
        else
            groupmod --new-name "${SERVICE_USER}" "${LEGACY_SERVICE_USER}"
        fi
    elif ! getent group "${SERVICE_USER}" >/dev/null 2>&1; then
        groupadd --system "${SERVICE_USER}"
        usermod --gid "${SERVICE_USER}" "${LEGACY_SERVICE_USER}"
    else
        usermod --gid "${SERVICE_USER}" "${LEGACY_SERVICE_USER}"
    fi

    usermod \
        --login "${SERVICE_USER}" \
        --home "${DATA_DIR}" \
        --comment "Uplink Ledger" \
        "${LEGACY_SERVICE_USER}"
    MIGRATED_LEGACY=1
}

postgres_value() {
    runuser -u postgres -- psql \
        -X -A -t -q -v ON_ERROR_STOP=1 \
        --dbname postgres \
        --command "$1"
}

migrate_postgres_identity() {
    if ! postgres_value "SELECT 1;" >/dev/null 2>&1; then
        if [ "${LEGACY_INSTALL}" -eq 1 ]; then
            echo "ERROR: PostgreSQL must be running to migrate the existing installation." >&2
            echo "Start PostgreSQL, then rerun the installer." >&2
            exit 1
        fi
        return
    fi

    legacy_role=$(postgres_value "SELECT 1 FROM pg_roles WHERE rolname='${LEGACY_SERVICE_USER}';")
    current_role=$(postgres_value "SELECT 1 FROM pg_roles WHERE rolname='${SERVICE_USER}';")
    legacy_database=$(postgres_value "SELECT 1 FROM pg_database WHERE datname='${LEGACY_DATABASE_NAME}';")
    current_database=$(postgres_value "SELECT 1 FROM pg_database WHERE datname='${DATABASE_NAME}';")

    if [ "${legacy_role}" = "1" ] && [ "${current_role}" = "1" ]; then
        echo "ERROR: Both legacy and Uplink Ledger PostgreSQL roles exist." >&2
        exit 1
    fi
    if [ "${legacy_database}" = "1" ] && [ "${current_database}" = "1" ]; then
        echo "ERROR: Both legacy and Uplink Ledger PostgreSQL databases exist." >&2
        exit 1
    fi

    if [ "${legacy_role}" = "1" ]; then
        postgres_value "ALTER ROLE ${LEGACY_SERVICE_USER} RENAME TO ${SERVICE_USER};" >/dev/null
        MIGRATED_LEGACY=1
    fi
    if [ "${legacy_database}" = "1" ]; then
        postgres_value "ALTER DATABASE ${LEGACY_DATABASE_NAME} RENAME TO ${DATABASE_NAME};" >/dev/null
        MIGRATED_LEGACY=1
    fi
}

migrate_sysconfig() {
    if [ ! -f "${LEGACY_SYSCONFIG_FILE}" ]; then
        return
    fi
    if [ -e "${SYSCONFIG_FILE}" ]; then
        echo "ERROR: Both legacy and current sysconfig files exist." >&2
        echo "Merge them deliberately before rerunning the installer." >&2
        exit 1
    fi
    temporary_file=$(mktemp /tmp/uplink-ledger-sysconfig.XXXXXX)
    sed \
        -e 's/ISPMON_ARGS/UPLINK_LEDGER_ARGS/g' \
        -e 's#/opt/isp-loss-monitor#/opt/uplink-ledger#g' \
        -e 's#/etc/isp-loss-monitor#/etc/uplink-ledger#g' \
        -e 's#/var/lib/isp-loss-monitor#/var/lib/uplink-ledger#g' \
        -e 's/isp_loss_monitor/uplink_ledger/g' \
        -e 's/isp-packet-loss\.csv/uplink-ledger.csv/g' \
        "${LEGACY_SYSCONFIG_FILE}" > "${temporary_file}"
    install -o root -g root -m 0644 "${temporary_file}" "${SYSCONFIG_FILE}"
    rm -f "${temporary_file}"
    MIGRATED_LEGACY=1
}

for path_pair in \
    "${LEGACY_APP_DIR}|${APP_DIR}" \
    "${LEGACY_CONFIG_DIR}|${CONFIG_DIR}" \
    "${LEGACY_DATA_DIR}|${DATA_DIR}" \
    "${LEGACY_SYSCONFIG_FILE}|${SYSCONFIG_FILE}"
do
    legacy_path=${path_pair%%|*}
    current_path=${path_pair#*|}
    if [ -e "${legacy_path}" ] && [ -e "${current_path}" ]; then
        echo "ERROR: Both ${legacy_path} and ${current_path} exist." >&2
        echo "Resolve the duplicate installation paths before rerunning the installer." >&2
        exit 1
    fi
done

if getent passwd "${LEGACY_SERVICE_USER}" >/dev/null 2>&1 \
   && getent passwd "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "ERROR: Both ${LEGACY_SERVICE_USER} and ${SERVICE_USER} users exist." >&2
    echo "Resolve the duplicate service identities before rerunning the installer." >&2
    exit 1
fi

if [ -f "${LEGACY_UNIT_FILE}" ]; then
    systemctl stop isp-loss-monitor.service >/dev/null 2>&1 || true
    systemctl disable isp-loss-monitor.service >/dev/null 2>&1 || true
    MIGRATED_LEGACY=1
fi

migrate_postgres_identity
migrate_service_identity
migrate_directory "${LEGACY_APP_DIR}" "${APP_DIR}"
migrate_directory "${LEGACY_CONFIG_DIR}" "${CONFIG_DIR}"
migrate_directory "${LEGACY_DATA_DIR}" "${DATA_DIR}"
migrate_sysconfig

if ! getent group "${SERVICE_USER}" >/dev/null 2>&1; then
    groupadd --system "${SERVICE_USER}"
fi
if ! getent passwd "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd \
        --system \
        --gid "${SERVICE_USER}" \
        --home-dir "${DATA_DIR}" \
        --shell /sbin/nologin \
        --comment "Uplink Ledger" \
        "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 \
    "${APP_DIR}" \
    "${APP_DIR}/web" \
    "${APP_DIR}/docs"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}"
install -d -o root -g root -m 0755 /etc/sysconfig

for tls_file in "${CONFIG_DIR}/server.crt" "${CONFIG_DIR}/server.key"; do
    if [ -f "${tls_file}" ]; then
        chown root:"${SERVICE_USER}" "${tls_file}"
        chmod 0640 "${tls_file}"
    fi
done

if [ -f "${DATA_DIR}/isp-packet-loss.csv" ] && [ ! -e "${DATA_DIR}/uplink-ledger.csv" ]; then
    mv "${DATA_DIR}/isp-packet-loss.csv" "${DATA_DIR}/uplink-ledger.csv"
fi

install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/uplink_ledger.py" \
    "${APP_DIR}/uplink_ledger.py"
install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/import_csv_to_postgres.py" \
    "${APP_DIR}/import_csv_to_postgres.py"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/README.md" \
    "${APP_DIR}/README.md"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/CHANGELOG.md" \
    "${SCRIPT_DIR}/CONTRIBUTING.md" \
    "${SCRIPT_DIR}/LICENSE" \
    "${SCRIPT_DIR}/SECURITY.md" \
    "${APP_DIR}/"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}"/docs/*.md \
    "${APP_DIR}/docs/"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/web/index.html" \
    "${APP_DIR}/web/index.html"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/web/app.js" \
    "${APP_DIR}/web/app.js"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/web/styles.css" \
    "${APP_DIR}/web/styles.css"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/systemd/uplink-ledger.service" \
    "${UNIT_FILE}"

if [ ! -e "${SYSCONFIG_FILE}" ]; then
    install -o root -g root -m 0644 \
        "${SCRIPT_DIR}/sysconfig/uplink-ledger" \
        "${SYSCONFIG_FILE}"
fi

rm -f "${APP_DIR}/isp_loss_monitor.py"
rm -f "${LEGACY_UNIT_FILE}"
rm -f "${LEGACY_SYSCONFIG_FILE}"
systemctl daemon-reload

echo "Uplink Ledger ${APP_DIR}/uplink_ledger.py installed."
if [ "${MIGRATED_LEGACY}" -eq 1 ]; then
    echo "Legacy service paths, identity, database, and state were migrated."
    echo "Review PostgreSQL pg_hba.conf for any rule that still names ${LEGACY_DATABASE_NAME} or ${LEGACY_SERVICE_USER}."
fi
echo
echo "Next:"
echo "  1. Create the local PostgreSQL role/database for peer-auth user ${SERVICE_USER} if this is a new install"
echo "  2. Configure and verify PostgreSQL peer authentication"
echo "  3. Optionally import existing CSV history with import_csv_to_postgres.py"
echo "  4. Put the certificate at ${CONFIG_DIR}/server.crt"
echo "  5. Put the unencrypted private key at ${CONFIG_DIR}/server.key"
echo "  6. Run: chown root:${SERVICE_USER} ${CONFIG_DIR}/server.crt ${CONFIG_DIR}/server.key"
echo "  7. Run: chmod 0640 ${CONFIG_DIR}/server.crt ${CONFIG_DIR}/server.key"
echo "  8. Review ${SYSCONFIG_FILE}"
echo "  9. Run: systemctl enable --now uplink-ledger"
echo " 10. Read: ${APP_DIR}/docs/README.md"
