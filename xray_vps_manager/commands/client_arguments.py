"""Разбор позиционных форм xray-client после проверки commands.arguments."""

from datetime import date
import re
import sys

from xray_vps_manager.clients import access as client_access
from xray_vps_manager.clients import crud as client_crud
from xray_vps_manager.clients import limits as client_limits
from xray_vps_manager.clients import payments as client_payments
from xray_vps_manager.clients.settings import FINGERPRINTS
from xray_vps_manager.runner import fail
from xray_vps_manager.xray import caddy as xray_caddy
from xray_vps_manager.xray import config as xray_config

CONNECTION_NAME_RE = re.compile(r"^[^\r\n|]{1,64}$")


def usage():
    print("""Usage:
  xray-client list
  xray-client connection-list
  xray-client add-connection NAME PORT SNI [FINGERPRINT] [TRANSPORT] [--transport tcp|grpc|xhttp] [--grpc-service-name NAME] [--xhttp-path PATH] [--xhttp-mode MODE] [--xhttp-extra-json JSON]
  xray-client add-connection NAME LOCAL_PORT DOMAIN [FINGERPRINT] --security tls --transport xhttp [--xhttp-path PATH] [--xhttp-mode MODE] [--xhttp-extra-json JSON] [--public-port PORT] [--install-caddy] [--tls-min-version tls1.2|tls1.3|default] [--tls-max-version tls1.2|tls1.3|default]
  xray-client add-trojan-connection NAME LOCAL_PORT DOMAIN [FINGERPRINT] [--ws-path PATH] [--public-port PORT] [--no-caddy] [--tls-min-version tls1.2|tls1.3|default] [--tls-max-version tls1.2|tls1.3|default]
  xray-client add-trojan-connection NAME PORT DOMAIN CERT_FILE KEY_FILE [FINGERPRINT] [--transport tcp]
  xray-client update-trojan-connection NAME_OR_TAG [--domain DOMAIN] [--local-port PORT] [--public-port PORT] [--ws-path PATH] [--fingerprint FINGERPRINT] [--tls-min-version tls1.2|tls1.3|default] [--tls-max-version tls1.2|tls1.3|default]
  xray-client connection-rename NAME_OR_TAG NEW_NAME
  xray-client connection-transport NAME_OR_TAG tcp|grpc|xhttp [--grpc-service-name NAME] [--xhttp-path PATH] [--xhttp-mode MODE] [--xhttp-extra-json JSON]
  xray-client connection-xhttp-extra NAME_OR_TAG --xhttp-extra-json JSON|--clear-xhttp-extra
  xray-client remove-connection NAME_OR_TAG
  xray-client add NAME [DAYS] [--connection TAG|--protocol vless|trojan] [--payment paid|free]
  xray-client disable NAME [--connection TAG]
  xray-client enable NAME [--connection TAG]
  xray-client move-connection NAME CONNECTION_NAME_OR_TAG
  xray-client remove NAME
  xray-client link NAME [--connection TAG]
  xray-client key NAME
  xray-client trojan-password-check
  xray-client rotate-trojan-password NAME [--connection TAG]
  xray-client set-days NAME DAYS
  xray-client extend-days NAME DAYS
  xray-client set-payment NAME paid|free
  xray-client sync-routes
  xray-client route NAME [COUNTRY_OR_TAG]
  xray-client expire-due [--quiet]
  xray-client traffic-summary [YYYY-MM]
  xray-client traffic-day NAME [YYYY-MM-DD]
  xray-client traffic-week NAME [START_DATE]
  xray-client traffic-month NAME [YYYY-MM]
  xray-client traffic-period NAME START_DATE END_DATE
  xray-client limit-list
  xray-client set-limit NAME daily|monthly LIMIT_GB
  xray-client clear-limit NAME
  xray-client enforce-limits [--quiet] [--sync]
  xray-client timezone
  xray-client set-timezone TIMEZONE|server
""")


def parse_access_days(value):
    try:
        return client_access.parse_access_days(value)
    except ValueError as exc:
        fail(str(exc))


def parse_extend_days(value):
    try:
        return client_access.parse_extend_days(value)
    except ValueError as exc:
        fail(str(exc))


def parse_limit_gb(value):
    try:
        return client_limits.parse_limit_gb(value)
    except ValueError as exc:
        fail(str(exc))


def validate_limit_period(value):
    try:
        return client_limits.validate_limit_period(value)
    except ValueError as exc:
        fail(str(exc))


def validate_connection_name(name):
    if not CONNECTION_NAME_RE.match((name or "").strip()):
        fail("Connection name must be 1-64 chars and must not contain new lines or |")
    return name.strip()


def validate_port(value):
    if not re.fullmatch(r"[0-9]+", value or ""):
        fail("PORT must be a number from 1 to 65535.")
    port = int(value, 10)
    if port < 1 or port > 65535:
        fail("PORT must be a number from 1 to 65535.")
    return port


def validate_host(value, label="SNI"):
    if not value or "/" in value or ":" in value or not re.fullmatch(r"[A-Za-z0-9.-]+", value):
        fail(f"{label} must be a domain without https://, path, or port.")
    return value


def validate_absolute_path(value, label="PATH"):
    path = (value or "").strip()
    if not path.startswith("/") or "\n" in path or "\r" in path:
        fail(f"{label} must be an absolute one-line path.")
    return path


def validate_fingerprint(value):
    value = (value or "").strip().lower()
    if value not in FINGERPRINTS:
        fail("FINGERPRINT must be one of: " + ", ".join(sorted(FINGERPRINTS)))
    return value


def validate_reality_transport(value):
    try:
        return xray_config.normalize_reality_transport(value)
    except ValueError as exc:
        fail(str(exc))


def validate_connection_security(value):
    security = (value or "reality").strip().lower()
    if security not in ("reality", "tls"):
        fail("SECURITY must be reality or tls.")
    return security


def validate_tls_version(value, default="tls1.2"):
    try:
        return xray_caddy.normalize_tls_version(value, default)
    except ValueError as exc:
        fail(str(exc))


def validate_grpc_service_name(value):
    try:
        return xray_config.normalize_grpc_service_name(value)
    except ValueError as exc:
        fail(str(exc))


def validate_xhttp_path(value):
    try:
        return xray_config.normalize_xhttp_path(value)
    except ValueError as exc:
        fail(str(exc))


def validate_trojan_ws_path(value):
    try:
        return xray_config.normalize_trojan_ws_path(value)
    except ValueError as exc:
        fail(str(exc))


def validate_xhttp_mode(value):
    try:
        return xray_config.normalize_xhttp_mode(value)
    except ValueError as exc:
        fail(str(exc))


def validate_xhttp_extra_json(value):
    try:
        return xray_config.normalize_xhttp_extra_json(value)
    except ValueError as exc:
        fail(str(exc))


def parse_date_value(value, label="DATE"):
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(f"{label} must be in YYYY-MM-DD format.")


def parse_month_value(value=None):
    value = value or client_access.local_now().strftime("%Y-%m")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        fail("MONTH must be in YYYY-MM format.")
    year, month = (int(part, 10) for part in value.split("-", 1))
    if month < 1 or month > 12:
        fail("MONTH must be in YYYY-MM format.")
    return f"{year:04d}-{month:02d}"


def parse_add_args(args):
    if not args:
        usage()
        sys.exit(1)
    name = args[0]
    rest = list(args[1:])
    connection_tag = None
    protocol = ""
    payment_type = ""
    if "--connection" in rest:
        index = rest.index("--connection")
        if index + 1 >= len(rest):
            fail("--connection requires TAG")
        connection_tag = rest[index + 1]
        del rest[index:index + 2]
    if "--protocol" in rest:
        index = rest.index("--protocol")
        if index + 1 >= len(rest):
            fail("--protocol requires vless or trojan")
        try:
            protocol = client_crud.normalize_connection_protocol(rest[index + 1])
        except ValueError as exc:
            fail(str(exc))
        del rest[index:index + 2]
    if connection_tag and protocol:
        fail("Use either --connection TAG or --protocol vless|trojan.")
    if "--payment" in rest:
        index = rest.index("--payment")
        if index + 1 >= len(rest):
            fail("--payment requires paid or free")
        payment_type = normalize_payment_type(rest[index + 1])
        del rest[index:index + 2]
    if len(rest) > 1:
        usage()
        sys.exit(1)
    if rest:
        return name, parse_access_days(rest[0]), False, connection_tag, protocol, payment_type
    return name, None, True, connection_tag, protocol, payment_type


def parse_connection_add_args(args):
    grpc_service_name = ""
    xhttp_path = ""
    xhttp_mode = ""
    xhttp_extra_json = ""
    transport = ""
    security = "reality"
    public_port = ""
    install_caddy = False
    tls_min_version = "tls1.2"
    tls_max_version = "tls1.2"
    rest = list(args)
    index = 0
    positional = []
    while index < len(rest):
        item = rest[index]
        if item == "--security":
            if index + 1 >= len(rest):
                fail("--security requires reality or tls")
            security = rest[index + 1]
            index += 2
            continue
        if item == "--transport":
            if index + 1 >= len(rest):
                fail("--transport requires tcp, grpc, or xhttp")
            transport = rest[index + 1]
            index += 2
            continue
        if item == "--grpc-service-name":
            if index + 1 >= len(rest):
                fail("--grpc-service-name requires value")
            grpc_service_name = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-path":
            if index + 1 >= len(rest):
                fail("--xhttp-path requires value")
            xhttp_path = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-mode":
            if index + 1 >= len(rest):
                fail("--xhttp-mode requires value")
            xhttp_mode = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-extra-json":
            if index + 1 >= len(rest):
                fail("--xhttp-extra-json requires JSON value")
            xhttp_extra_json = rest[index + 1]
            index += 2
            continue
        if item == "--public-port":
            if index + 1 >= len(rest):
                fail("--public-port requires PORT")
            public_port = rest[index + 1]
            index += 2
            continue
        if item == "--install-caddy":
            install_caddy = True
            index += 1
            continue
        if item == "--tls-min-version":
            if index + 1 >= len(rest):
                fail("--tls-min-version requires tls1.2, tls1.3, or default")
            tls_min_version = rest[index + 1]
            index += 2
            continue
        if item == "--tls-max-version":
            if index + 1 >= len(rest):
                fail("--tls-max-version requires tls1.2, tls1.3, or default")
            tls_max_version = rest[index + 1]
            index += 2
            continue
        if item.startswith("--"):
            fail(f"Unknown option: {item}")
        positional.append(item)
        index += 1

    if len(positional) < 3 or len(positional) > 5:
        usage()
        sys.exit(1)

    name, port, sni = positional[:3]
    fingerprint_value = "chrome"
    if len(positional) >= 4:
        maybe_value = positional[3]
        if maybe_value.lower() in xray_config.REALITY_TRANSPORTS:
            transport = maybe_value
        else:
            fingerprint_value = maybe_value
    if len(positional) == 5:
        transport = positional[4]

    transport = transport or ("xhttp" if validate_connection_security(security) == "tls" else "tcp")
    return (
        name,
        port,
        sni,
        fingerprint_value,
        transport,
        grpc_service_name,
        xhttp_path,
        xhttp_mode,
        xhttp_extra_json,
        security,
        public_port,
        install_caddy,
        tls_min_version,
        tls_max_version,
    )


def parse_trojan_connection_add_args(args):
    if not args:
        usage()
        sys.exit(1)
    transport = "ws"
    transport_explicit = False
    ws_path = xray_config.DEFAULT_TROJAN_WS_PATH
    public_port = ""
    install_caddy = True
    tls_min_version = xray_config.DEFAULT_TROJAN_TLS_MIN_VERSION
    tls_max_version = xray_config.DEFAULT_TROJAN_TLS_MAX_VERSION
    fingerprint_value = ""
    rest = list(args)
    positional = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in ("--caddy", "--install-caddy"):
            transport = "ws"
            transport_explicit = True
            install_caddy = True
            index += 1
            continue
        if item in ("--no-caddy", "--no-install-caddy"):
            transport = "ws"
            transport_explicit = True
            install_caddy = False
            index += 1
            continue
        if item == "--transport":
            if index + 1 >= len(rest):
                fail("--transport requires tcp or ws")
            transport = rest[index + 1]
            transport_explicit = True
            if transport.strip().lower() == "tcp":
                install_caddy = False
            index += 2
            continue
        if item == "--ws-path":
            if index + 1 >= len(rest):
                fail("--ws-path requires PATH")
            ws_path = rest[index + 1]
            transport = "ws"
            transport_explicit = True
            index += 2
            continue
        if item == "--public-port":
            if index + 1 >= len(rest):
                fail("--public-port requires PORT")
            public_port = rest[index + 1]
            transport = "ws"
            transport_explicit = True
            index += 2
            continue
        if item == "--fingerprint":
            if index + 1 >= len(rest):
                fail("--fingerprint requires value")
            fingerprint_value = rest[index + 1]
            index += 2
            continue
        if item == "--tls-min-version":
            if index + 1 >= len(rest):
                fail("--tls-min-version requires tls1.2, tls1.3, or default")
            tls_min_version = rest[index + 1]
            transport = "ws"
            transport_explicit = True
            index += 2
            continue
        if item == "--tls-max-version":
            if index + 1 >= len(rest):
                fail("--tls-max-version requires tls1.2, tls1.3, or default")
            tls_max_version = rest[index + 1]
            transport = "ws"
            transport_explicit = True
            index += 2
            continue
        if item.startswith("--"):
            fail(f"Unknown option: {item}")
        positional.append(item)
        index += 1

    if not transport_explicit and len(positional) in (5, 6):
        transport = "tcp"
        install_caddy = False
    transport = (transport or "ws").strip().lower()
    if transport not in ("tcp", "ws"):
        fail("Trojan --transport requires tcp or ws")
    if transport == "ws":
        if len(positional) not in (3, 4):
            usage()
            sys.exit(1)
        if len(positional) == 4:
            fingerprint_value = positional[3]
        return (
            positional[0],
            positional[1],
            positional[2],
            "",
            "",
            fingerprint_value or "chrome",
            "ws",
            ws_path,
            public_port,
            install_caddy,
            tls_min_version,
            tls_max_version,
        )

    if len(positional) not in (5, 6):
        usage()
        sys.exit(1)
    if len(positional) == 6:
        fingerprint_value = positional[5]
    return (
        positional[0],
        positional[1],
        positional[2],
        positional[3],
        positional[4],
        fingerprint_value or "chrome",
        "tcp",
        "",
        "",
        False,
        tls_min_version,
        tls_max_version,
    )


def parse_trojan_connection_update_args(args):
    if len(args) < 2:
        usage()
        sys.exit(1)
    identifier = args[0]
    domain = None
    local_port = None
    public_port = None
    ws_path = None
    fingerprint_value = None
    tls_min_version = None
    tls_max_version = None
    rest = list(args[1:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in ("--domain", "--tls-domain"):
            if index + 1 >= len(rest):
                fail("--domain requires DOMAIN")
            domain = rest[index + 1]
            index += 2
            continue
        if item == "--local-port":
            if index + 1 >= len(rest):
                fail("--local-port requires PORT")
            local_port = rest[index + 1]
            index += 2
            continue
        if item == "--public-port":
            if index + 1 >= len(rest):
                fail("--public-port requires PORT")
            public_port = rest[index + 1]
            index += 2
            continue
        if item == "--ws-path":
            if index + 1 >= len(rest):
                fail("--ws-path requires PATH")
            ws_path = rest[index + 1]
            index += 2
            continue
        if item == "--fingerprint":
            if index + 1 >= len(rest):
                fail("--fingerprint requires value")
            fingerprint_value = rest[index + 1]
            index += 2
            continue
        if item == "--tls-min-version":
            if index + 1 >= len(rest):
                fail("--tls-min-version requires tls1.2, tls1.3, or default")
            tls_min_version = rest[index + 1]
            index += 2
            continue
        if item == "--tls-max-version":
            if index + 1 >= len(rest):
                fail("--tls-max-version requires tls1.2, tls1.3, or default")
            tls_max_version = rest[index + 1]
            index += 2
            continue
        fail(f"Unknown option: {item}")

    update_values = (domain, local_port, public_port, ws_path, fingerprint_value, tls_min_version, tls_max_version)
    if all(value is None for value in update_values):
        usage()
        sys.exit(1)
    return (
        identifier,
        domain,
        local_port,
        public_port,
        ws_path,
        fingerprint_value,
        tls_min_version,
        tls_max_version,
    )


def parse_connection_transport_args(args):
    if len(args) < 2:
        usage()
        sys.exit(1)
    identifier = args[0]
    transport = args[1]
    grpc_service_name = ""
    xhttp_path = ""
    xhttp_mode = ""
    xhttp_extra_json = None
    rest = list(args[2:])
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--grpc-service-name":
            if index + 1 >= len(rest):
                fail("--grpc-service-name requires value")
            grpc_service_name = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-path":
            if index + 1 >= len(rest):
                fail("--xhttp-path requires value")
            xhttp_path = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-mode":
            if index + 1 >= len(rest):
                fail("--xhttp-mode requires value")
            xhttp_mode = rest[index + 1]
            index += 2
            continue
        if item == "--xhttp-extra-json":
            if index + 1 >= len(rest):
                fail("--xhttp-extra-json requires JSON value")
            xhttp_extra_json = rest[index + 1]
            index += 2
            continue
        fail(f"Unknown option: {item}")
    return identifier, transport, grpc_service_name, xhttp_path, xhttp_mode, xhttp_extra_json


def parse_connection_xhttp_extra_args(args):
    if len(args) < 2:
        usage()
        sys.exit(1)
    identifier = args[0]
    rest = list(args[1:])
    clear = False
    xhttp_extra_json = None
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--clear-xhttp-extra":
            clear = True
            index += 1
            continue
        if item == "--xhttp-extra-json":
            if index + 1 >= len(rest):
                fail("--xhttp-extra-json requires JSON value")
            xhttp_extra_json = rest[index + 1]
            index += 2
            continue
        fail(f"Unknown option: {item}")
    if clear and xhttp_extra_json is not None:
        fail("Use either --xhttp-extra-json or --clear-xhttp-extra.")
    if not clear and xhttp_extra_json is None:
        fail("connection-xhttp-extra requires --xhttp-extra-json JSON or --clear-xhttp-extra.")
    return identifier, xhttp_extra_json, clear


def parse_name_connection_args(args):
    if not args:
        usage()
        sys.exit(1)
    name = args[0]
    rest = list(args[1:])
    connection_tag = None
    if "--connection" in rest:
        index = rest.index("--connection")
        if index + 1 >= len(rest):
            fail("--connection requires TAG")
        connection_tag = rest[index + 1]
        del rest[index:index + 2]
    if rest:
        usage()
        sys.exit(1)
    return name, connection_tag


def normalize_payment_type(value):
    try:
        return client_payments.normalize_payment_type(value)
    except ValueError as exc:
        fail(str(exc))
