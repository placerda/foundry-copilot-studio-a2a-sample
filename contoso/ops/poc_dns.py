"""Temporary, loopback-only DNS for the Contoso presenter VPN."""

import argparse
from contextlib import ExitStack, contextmanager
import errno
import ipaddress
import json
import logging
from pathlib import Path
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import threading
import xml.etree.ElementTree as ET

from dnslib import A, CLASS, DNSError, DNSHeader, DNSRecord, QTYPE, RCODE, RR


LISTEN_ADDRESS = "127.0.0.1"
PROFILE_NAME = "Contoso VPN"
TTL = 30
MAX_PACKET = 4096
ZONES = (
    "privatelink.services.ai.azure.com",
    "privatelink.cognitiveservices.azure.com",
    "privatelink.openai.azure.com",
    "privatelink.azurecr.io",
    "privatelink.blob.core.windows.net",
)
MONITOR_ZONES = (
    "privatelink.monitor.azure.com",
    "privatelink.oms.opinsights.azure.com",
    "privatelink.ods.opinsights.azure.com",
    "privatelink.agentsvc.azure-automation.net",
)
MONITOR_API = "api.privatelink.monitor.azure.com"
MONITOR_QUERY_NAMES = (
    "api.loganalytics.azure.com",
    "api.loganalytics.io",
    "api.applicationinsights.azure.com",
    "api.applicationinsights.io",
)
LOG = logging.getLogger(__name__)


def canonical_name(value):
    if not isinstance(value, str):
        raise ValueError("DNS names must be strings")
    name = value.rstrip(".").lower()
    if len(name) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in name.split(".")
    ):
        raise ValueError("Invalid exact DNS name")
    return name


def validate_records(document):
    networks = [ipaddress.IPv4Network(value) for value in document["networks"]]
    private_networks = [
        ipaddress.IPv4Network(value) for value in
        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    if not networks or not all(
        any(network.subnet_of(private) for private in private_networks)
        for network in networks
    ):
        raise ValueError("Only RFC1918 VNet networks are allowed")
    records = {}
    for raw_name, values in document["records"].items():
        name = canonical_name(raw_name)
        if name in records or not isinstance(values, list) or not values:
            raise ValueError("Duplicate or empty DNS record")
        addresses = [ipaddress.IPv4Address(value) for value in values]
        if not all(any(address in network for network in networks) for address in addresses):
            raise ValueError("DNS address outside the selected VNet")
        records[name] = tuple(dict.fromkeys(str(address) for address in addresses))
    if not records or len(records) > 128:
        raise ValueError("Expected 1 to 128 exact PoC DNS names")
    return records


def build_records(networks, zone_records):
    records = {}
    for zone, entries in zone_records.items():
        if zone not in ZONES + MONITOR_ZONES:
            raise ValueError("Unsupported private DNS zone")
        for entry in entries:
            private_name = canonical_name(entry["fqdn"])
            if not private_name.endswith("." + zone):
                raise ValueError("DNS record does not belong to its source zone")
            addresses = [item["ipv4Address"] for item in entry["aRecords"]]
            public_name = private_name.replace(".privatelink.", ".", 1)
            for name in (private_name, public_name):
                if name in records and records[name] != addresses:
                    raise ValueError("Conflicting private endpoint records")
                records[name] = addresses
    if MONITOR_API in records:
        for name in MONITOR_QUERY_NAMES:
            records[name] = records[MONITOR_API]
    document = {"networks": networks, "records": records}
    validate_records(document)
    return document


def make_profile(source, names):
    if len(source) > 262144:
        raise ValueError("Unexpected VPN profile size")
    root = ET.fromstring(source)
    namespace = root.tag.partition("}")[0].removeprefix("{")
    if root.tag != f"{{{namespace}}}AzVpnProfile":
        raise ValueError("Expected the original Azure VPN Client profile")
    ET.register_namespace("", namespace)
    ET.register_namespace("i", "http://www.w3.org/2001/XMLSchema-instance")

    def tag(name):
        return f"{{{namespace}}}{name}"

    name = root.find(tag("name"))
    if name is None:
        raise ValueError("VPN profile has no name")
    name.text = PROFILE_NAME
    config = root.find(tag("clientconfig"))
    if config is None:
        config = ET.SubElement(root, tag("clientconfig"))
    config.attrib.pop("{http://www.w3.org/2001/XMLSchema-instance}nil", None)
    for field in ("dnsservers", "dnssuffixes"):
        previous = config.find(tag(field))
        if previous is not None:
            config.remove(previous)
    servers = ET.SubElement(config, tag("dnsservers"))
    ET.SubElement(servers, tag("dnsserver")).text = LISTEN_ADDRESS
    suffixes = ET.SubElement(config, tag("dnssuffixes"))
    for hostname in sorted({canonical_name(value) for value in names}):
        ET.SubElement(suffixes, tag("dnssuffix")).text = "." + hostname
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def answer(packet, records):
    if len(packet) > MAX_PACKET:
        raise ValueError("DNS packet exceeds the PoC limit")
    query = DNSRecord.parse(packet)
    reply = DNSRecord(DNSHeader(
        id=query.header.id, qr=1, aa=1, rd=query.header.rd, ra=0,
    ))
    if query.header.qr or query.header.opcode != 0:
        reply.header.rcode = RCODE.NOTIMP
    elif len(query.questions) != 1:
        reply.header.rcode = RCODE.FORMERR
    else:
        question = query.questions[0]
        reply.add_question(question)
        hostname = str(question.qname).rstrip(".").lower()
        if question.qclass != CLASS.IN or hostname not in records:
            reply.header.rcode = RCODE.REFUSED
        elif question.qtype == QTYPE.A:
            for address in records[hostname]:
                reply.add_answer(RR(
                    question.qname, QTYPE.A, rclass=CLASS.IN,
                    ttl=TTL, rdata=A(address),
                ))
            LOG.info("answered A query for %s", hostname)
        elif question.qtype not in (QTYPE.AAAA, QTYPE.HTTPS, QTYPE.TXT, QTYPE.CNAME):
            reply.header.rcode = RCODE.REFUSED
    return reply.pack()


def _reply(packet, records):
    try:
        return answer(packet, records)
    except (DNSError, ValueError, struct.error):
        LOG.warning("Rejected malformed DNS packet")
        if len(packet) < 2:
            return b""
        request_id = struct.unpack("!H", packet[:2])[0]
        return DNSRecord(DNSHeader(
            id=request_id, qr=1, ra=0, rcode=RCODE.FORMERR,
        )).pack()


def _receive(connection, count):
    data = b""
    while len(data) < count:
        part = connection.recv(count - len(data))
        if not part:
            raise ConnectionError("Incomplete TCP DNS frame")
        data += part
    return data


class _ExclusiveBinding:
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _UDPServer(_ExclusiveBinding, socketserver.UDPServer):
    max_packet_size = MAX_PACKET + 1


class _TCPServer(_ExclusiveBinding, socketserver.TCPServer):
    request_queue_size = 8


@contextmanager
def running_server(records, *, port=53):
    class UDPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            packet, connection = self.request
            response = _reply(packet, records)
            if response:
                connection.sendto(response, self.client_address)

    class TCPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                length = struct.unpack("!H", _receive(self.request, 2))[0]
                if length > MAX_PACKET:
                    LOG.warning("Rejected oversized TCP DNS frame")
                    return
                response = _reply(_receive(self.request, length), records)
                if response:
                    self.request.sendall(struct.pack("!H", len(response)) + response)
            except (TimeoutError, ConnectionError):
                LOG.warning("Incomplete or timed-out TCP DNS request")

    with ExitStack() as stack:
        # Windows can allocate consecutive TCP ports inside a UDP-reserved range.
        for attempt in range(128 if port == 0 else 1):
            candidate = port if attempt == 0 else 49152 + secrets.randbelow(16384)
            try:
                with ExitStack() as bindings:
                    tcp = bindings.enter_context(_TCPServer((LISTEN_ADDRESS, candidate), TCPHandler))
                    actual_port = tcp.server_address[1]
                    udp = bindings.enter_context(_UDPServer((LISTEN_ADDRESS, actual_port), UDPHandler))
                    stack.enter_context(bindings.pop_all())
                break
            except OSError as error:
                if (port != 0 or attempt == 127
                        or error.errno not in (errno.EACCES, errno.EADDRINUSE)):
                    raise
                LOG.debug("Retrying an unavailable automatically allocated DNS port")
        threads = [
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
            for server in (udp, tcp)
        ]
        for thread in threads:
            thread.start()
        try:
            yield actual_port
        finally:
            udp.shutdown()
            tcp.shutdown()
            for thread in threads:
                thread.join(timeout=3)


def prepare(args):
    def azure(*command):
        result = subprocess.run(
            [args.azure_cli, *command, "--subscription", args.subscription,
             "--resource-group", args.resource_group, "--output", "json",
             "--only-show-errors"],
            capture_output=True, text=True, check=True, timeout=90,
        )
        return json.loads(result.stdout)

    vnet = azure("network", "vnet", "show", "--name", args.vnet_name)
    zones = ZONES + (MONITOR_ZONES if args.include_monitor else ())
    zone_records = {
        zone: azure("network", "private-dns", "record-set", "a", "list",
                    "--zone-name", zone)
        for zone in zones
    }
    document = build_records(vnet["addressSpace"]["addressPrefixes"], zone_records)
    if args.include_monitor and MONITOR_API not in document["records"]:
        raise ValueError("Azure Monitor private API DNS record is required")
    profile = make_profile(
        args.source_profile.read_bytes(), document["records"],
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    record_path = args.output_dir / "records.json"
    profile_path = args.output_dir / "Contoso-VPN.azurevpnconfig.xml"
    if record_path.exists() or profile_path.exists():
        raise ValueError("Output already exists. Use a new directory to preserve rollback.")
    record_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    profile_path.write_bytes(profile)
    print(json.dumps({
        "records": str(record_path), "profile": str(profile_path),
        "name_count": len(document["records"]), "listen_address": LISTEN_ADDRESS,
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("prepare")
    for option in ("azure-cli", "subscription", "resource-group", "vnet-name"):
        export.add_argument("--" + option, required=True)
    export.add_argument("--source-profile", required=True, type=Path)
    export.add_argument("--output-dir", required=True, type=Path)
    export.add_argument(
        "--include-monitor", action="store_true",
        help="Include existing private Azure Monitor zones and exact query API names",
    )
    serve = commands.add_parser("serve")
    serve.add_argument("--records", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "prepare":
            prepare(args)
        else:
            records = validate_records(json.loads(args.records.read_text(encoding="utf-8")))
            with running_server(records):
                print(json.dumps({
                    "status": "ready", "address": LISTEN_ADDRESS,
                    "port": 53, "names": len(records),
                }), flush=True)
                threading.Event().wait()
    except KeyboardInterrupt:
        LOG.info("Temporary PoC DNS stopped")
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError,
            subprocess.SubprocessError) as exc:
        LOG.error("PoC DNS failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
