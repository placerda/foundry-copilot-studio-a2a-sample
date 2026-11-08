from copy import deepcopy
import errno
import socket
import struct
import xml.etree.ElementTree as ET

from dnslib import DNSQuestion, DNSRecord, RCODE
import pytest

from contoso.ops import poc_dns


HOST = "contoso-test.services.ai.azure.com"
IP = "10.76.2.23"


def document():
    return {"networks": ["10.76.0.0/16"], "records": {HOST: [IP]}}


def test_actual_private_records_produce_exact_public_and_private_names():
    result = poc_dns.build_records(["10.76.0.0/16"], {
        "privatelink.services.ai.azure.com": [{
            "fqdn": "contoso-test.privatelink.services.ai.azure.com.",
            "aRecords": [{"ipv4Address": IP}],
        }],
    })
    assert result["records"] == {
        HOST: [IP], "contoso-test.privatelink.services.ai.azure.com": [IP],
    }


@pytest.mark.parametrize("change", [
    {"networks": ["0.0.0.0/0"]},
    {"records": {HOST: ["8.8.8.8"]}},
    {"records": {HOST: ["169.254.169.254"]}},
    {"records": {HOST: ["10.85.0.1"]}},
    {"records": {HOST: []}},
    {"records": {}},
    {"records": {"*.services.ai.azure.com": [IP]}},
    {"records": {HOST: IP}},
    {"records": {HOST: [IP], HOST.upper(): [IP]}},
])
def test_rejects_broad_or_invalid_configuration(change):
    with pytest.raises(ValueError):
        poc_dns.validate_records({**document(), **change})


def test_ttl_flags_and_multiple_a_records():
    query = DNSRecord.question(HOST.upper())
    response = DNSRecord.parse(poc_dns.answer(query.pack(), {HOST: (IP, "10.76.2.24")}))
    assert response.header.id == query.header.id
    assert response.header.aa == 1
    assert response.header.ra == 0
    assert response.header.rcode == RCODE.NOERROR
    assert [str(rr.rdata) for rr in response.rr] == [IP, "10.76.2.24"]
    assert all(rr.ttl == 30 for rr in response.rr)


@pytest.mark.parametrize("hostname", [
    "example.com", "other.services.ai.azure.com", "sub." + HOST,
])
def test_no_recursion_or_wildcard_answers(hostname):
    response = DNSRecord.parse(poc_dns.answer(
        DNSRecord.question(hostname).pack(), {HOST: (IP,)},
    ))
    assert response.header.rcode == RCODE.REFUSED
    assert not response.rr


@pytest.mark.parametrize("kind", ["AAAA", "HTTPS", "TXT", "CNAME"])
def test_known_name_non_a_types_return_no_data(kind):
    response = DNSRecord.parse(poc_dns.answer(
        DNSRecord.question(HOST, kind).pack(), {HOST: (IP,)},
    ))
    assert response.header.rcode == RCODE.NOERROR
    assert not response.rr


def test_zone_transfer_refused_and_multiple_questions_rejected():
    response = DNSRecord.parse(poc_dns.answer(
        DNSRecord.question(HOST, "AXFR").pack(), {HOST: (IP,)},
    ))
    assert response.header.rcode == RCODE.REFUSED
    query = DNSRecord.question(HOST)
    query.add_question(DNSQuestion(HOST))
    response = DNSRecord.parse(poc_dns.answer(query.pack(), {HOST: (IP,)}))
    assert response.header.rcode == RCODE.FORMERR


def test_malformed_packets_are_bounded_and_do_not_crash():
    assert poc_dns._reply(b"\x00", {HOST: (IP,)}) == b""
    response = DNSRecord.parse(poc_dns._reply(b"\x12\x34garbage", {HOST: (IP,)}))
    assert response.header.id == 0x1234
    assert response.header.rcode == RCODE.FORMERR
    with pytest.raises(ValueError, match="limit"):
        poc_dns.answer(bytes(4097), {HOST: (IP,)})


def test_profile_preserves_authentication_routes_and_original_input():
    ns = "http://schemas.datacontract.org/2004/07/"
    source = f"""<AzVpnProfile xmlns="{ns}" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
<name>CONTOSO original</name><servervalidation><cert>original</cert></servervalidation>
<clientauth><aad><tenant>original-tenant</tenant></aad></clientauth>
<clientconfig><dnsservers><dnsserver>10.76.253.4</dnsserver></dnsservers>
<dnssuffixes><dnssuffix>.services.ai.azure.com</dnssuffix></dnssuffixes>
<includeroutes><route>original-route</route></includeroutes></clientconfig>
<serverlist><serverentry>original-server</serverentry></serverlist></AzVpnProfile>""".encode()
    before = ET.fromstring(source)
    saved = deepcopy(before)
    result = poc_dns.make_profile(source, [HOST])
    assert result.startswith(b"<AzVpnProfile")
    after = ET.fromstring(result)
    q = lambda name: f"{{{ns}}}{name}"
    assert after.find(q("name")).text == poc_dns.PROFILE_NAME == "Contoso VPN"
    config = after.find(q("clientconfig"))
    assert config.find(q("dnsservers"))[0].text == "127.0.0.1"
    assert [item.text for item in config.find(q("dnssuffixes"))] == ["." + HOST]
    for field in ("servervalidation", "clientauth", "serverlist"):
        assert ET.tostring(after.find(q(field))) == ET.tostring(saved.find(q(field)))
    assert config.find(q("includeroutes"))[0].text == "original-route"
    assert ET.tostring(before) == ET.tostring(saved)


def test_nil_clientconfig_is_enabled():
    source = b"""<AzVpnProfile xmlns="http://schemas.datacontract.org/2004/07/"
xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><name>CONTOSO</name>
<clientconfig i:nil="true"/></AzVpnProfile>"""
    after = ET.fromstring(poc_dns.make_profile(source, [HOST]))
    config = next(item for item in after if item.tag.endswith("}clientconfig"))
    assert not config.attrib
    assert len(config) == 2


def test_real_udp_and_tcp_listener_on_dedicated_loopback():
    records = poc_dns.validate_records(document())
    query = DNSRecord.question(HOST).pack()
    with poc_dns.running_server(records, port=0) as port:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(2)
            udp.sendto(query, (poc_dns.LISTEN_ADDRESS, port))
            udp_response = DNSRecord.parse(udp.recv(4096))
        with socket.create_connection((poc_dns.LISTEN_ADDRESS, port), timeout=2) as tcp:
            tcp.sendall(struct.pack("!H", len(query)) + query)
            size = struct.unpack("!H", poc_dns._receive(tcp, 2))[0]
            tcp_response = DNSRecord.parse(poc_dns._receive(tcp, size))
        assert str(udp_response.rr[0].rdata) == IP
        assert str(tcp_response.rr[0].rdata) == IP
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
        released.bind((poc_dns.LISTEN_ADDRESS, port))


def test_occupied_port_fails_without_replacing_the_existing_listener():
    records = poc_dns.validate_records(document())
    with poc_dns.running_server(records, port=0) as port:
        with pytest.raises(OSError):
            with poc_dns.running_server(records, port=port):
                pytest.fail("An existing listener must never be replaced")
        query = DNSRecord.question(HOST)
        response = DNSRecord.parse(query.send(poc_dns.LISTEN_ADDRESS, port, timeout=2))
        assert str(response.rr[0].rdata) == IP


def test_ephemeral_pair_retries_reserved_udp_port_without_leaking_tcp_listener(monkeypatch):
    original = poc_dns._UDPServer
    original_tcp = poc_dns._TCPServer
    attempts = []
    tcp_ports = []

    def bind_tcp(address, handler):
        tcp_ports.append(address[1])
        return original_tcp(address, handler)

    def bind(address, handler):
        attempts.append(address)
        if len(attempts) == 1:
            raise PermissionError(errno.EACCES, "Reserved UDP port")
        return original(address, handler)

    monkeypatch.setattr(poc_dns, "_UDPServer", bind)
    monkeypatch.setattr(poc_dns, "_TCPServer", bind_tcp)
    with poc_dns.running_server(poc_dns.validate_records(document()), port=0) as port:
        response = DNSRecord.parse(DNSRecord.question(HOST).send(poc_dns.LISTEN_ADDRESS, port, timeout=2))
        assert str(response.rr[0].rdata) == IP
    assert len(attempts) >= 2
    assert tcp_ports[0] == 0
    assert all(49152 <= port <= 65535 for port in tcp_ports[1:])
    with socket.socket() as released:
        released.bind(attempts[0])


def test_automatic_port_allocation_failure_is_bounded(monkeypatch):
    attempts = []

    def blocked(address, handler):
        attempts.append(address)
        raise PermissionError(errno.EACCES, "Unavailable automatic port")

    monkeypatch.setattr(poc_dns, "_TCPServer", blocked)
    with pytest.raises(PermissionError):
        with poc_dns.running_server(poc_dns.validate_records(document()), port=0):
            pytest.fail("Port allocation must fail after its bounded attempts")
    assert len(attempts) == 128
    assert attempts[0] == (poc_dns.LISTEN_ADDRESS, 0)
    assert all(49152 <= address[1] <= 65535 for address in attempts[1:])


def test_explicit_port_is_never_retried_or_replaced(monkeypatch):
    attempts = []

    def blocked(address, handler):
        attempts.append(address)
        raise PermissionError(errno.EACCES, "Unavailable selected port")

    monkeypatch.setattr(poc_dns, "_TCPServer", blocked)
    with pytest.raises(PermissionError):
        with poc_dns.running_server(poc_dns.validate_records(document()), port=5353):
            pytest.fail("The selected unavailable port must fail closed")
    assert attempts == [(poc_dns.LISTEN_ADDRESS, 5353)]


def test_oversized_tcp_frame_does_not_stop_the_server():
    records = poc_dns.validate_records(document())
    with poc_dns.running_server(records, port=0) as port:
        with socket.create_connection((poc_dns.LISTEN_ADDRESS, port), timeout=2) as tcp:
            tcp.sendall(struct.pack("!H", 4097))
            assert tcp.recv(1) == b""
        response = DNSRecord.parse(
            DNSRecord.question(HOST).send(poc_dns.LISTEN_ADDRESS, port, timeout=2),
        )
        assert str(response.rr[0].rdata) == IP
