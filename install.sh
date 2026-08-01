#!/bin/sh

set -eu

APP_DIR="/opt/isp-loss-monitor"
CONFIG_DIR="/etc/isp-loss-monitor"
DATA_DIR="/var/lib/isp-loss-monitor"
UNIT_FILE="/etc/systemd/system/isp-loss-monitor.service"
SYSCONFIG_FILE="/etc/sysconfig/isp-loss-monitor"
SERVICE_USER="ispmon"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run this installer as root (sudo ./install.sh)." >&2
    exit 1
fi

if [ "$(uname -s)" != "Linux" ] || [ ! -d /run/systemd/system ]; then
    echo "ERROR: This installer targets AlmaLinux 10 with systemd." >&2
    exit 1
fi

for command in python3 ping traceroute ip curl psql systemctl getent useradd; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "ERROR: Required command is missing: ${command}" >&2
        echo "On AlmaLinux 10: dnf install python3 iputils traceroute curl postgresql" >&2
        exit 1
    fi
done

if ! getent passwd "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd \
        --system \
        --home-dir "${DATA_DIR}" \
        --shell /sbin/nologin \
        --comment "Uplink Ledger" \
        "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}" "${APP_DIR}/web"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}"
install -d -o root -g root -m 0755 /etc/sysconfig

install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/isp_loss_monitor.py" \
    "${APP_DIR}/isp_loss_monitor.py"
install -o root -g root -m 0755 \
    "${SCRIPT_DIR}/import_csv_to_postgres.py" \
    "${APP_DIR}/import_csv_to_postgres.py"
install -o root -g root -m 0644 \
    "${SCRIPT_DIR}/README.md" \
    "${APP_DIR}/README.md"
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
    "${SCRIPT_DIR}/systemd/isp-loss-monitor.service" \
    "${UNIT_FILE}"

if [ ! -e "${SYSCONFIG_FILE}" ]; then
    install -o root -g root -m 0644 \
        "${SCRIPT_DIR}/sysconfig/isp-loss-monitor" \
        "${SYSCONFIG_FILE}"
fi

systemctl daemon-reload

echo "Uplink Ledger ${APP_DIR}/isp_loss_monitor.py installed."
echo
echo "Next:"
echo "  1. Create the local PostgreSQL role/database for peer-auth user ${SERVICE_USER}"
echo "  2. Import the existing CSV with import_csv_to_postgres.py"
echo "  3. Put the certificate at ${CONFIG_DIR}/server.crt"
echo "  4. Put the unencrypted private key at ${CONFIG_DIR}/server.key"
echo "  5. Run: chown root:${SERVICE_USER} ${CONFIG_DIR}/server.crt ${CONFIG_DIR}/server.key"
echo "  6. Run: chmod 0640 ${CONFIG_DIR}/server.crt ${CONFIG_DIR}/server.key"
echo "  7. Review ${SYSCONFIG_FILE}"
echo "  8. Run: systemctl enable --now isp-loss-monitor"
