from datetime import datetime

import pytest

from prack.ogn.parser import AircraftBeacon, StatusBeacon, decode_time, parse_line

REF = datetime(2026, 7, 15, 16, 9, 0)


def test_flarm_beacon():
    line = (
        "FLRDDA5BA>OGFLR,qAS,LFMX:/160829h4415.41N/00600.03E'342/049/A=005524 !W52! "
        "id0ADDA5BA -454fpm -1.1rot 8.8dB 0e +51.2kHz gps4x5"
    )
    b = parse_line(line, REF)
    assert isinstance(b, AircraftBeacon)
    assert b.callsign == "FLRDDA5BA"
    assert b.source == "FLARM"
    assert b.receiver == "LFMX"
    assert b.timestamp == datetime(2026, 7, 15, 16, 8, 29)
    assert b.lat == pytest.approx(44 + 15.415 / 60)
    assert b.lon == pytest.approx(6 + 0.032 / 60)
    assert b.alt == pytest.approx(5524 * 0.3048)
    assert b.track == 342
    assert b.speed == pytest.approx(49 * 1.852)
    assert b.climb == pytest.approx(-454 * 0.00508)
    assert b.turn == pytest.approx(-3.3)
    assert b.address == "DDA5BA"
    assert b.address_type == 2  # FLARM
    assert b.aircraft_type == 2  # tow plane
    assert not b.stealth and not b.no_tracking
    assert (b.signal, b.errors, b.freq_offset, b.gps) == (8.8, 0, 51.2, "4x5")


def test_fanet_paraglider_and_flags():
    line = "FNT1103CE>OGNFNT,qAS,FNB1103CE:/183727h5145.36S/00607.02W'000/000/A=000613 !W56! id5F1103CE -098fpm +0.0rot"
    b = parse_line(line, datetime(2026, 7, 15, 18, 40))
    assert b.source == "FANET"
    assert b.aircraft_type == 7  # paraglider
    assert b.address_type == 3
    assert b.no_tracking  # 0x5F = 0101 1111 -> no-tracking bit set
    assert not b.stealth
    assert b.lat < 0 and b.lon < 0
    assert b.track is None and b.speed == 0


def test_fanet_name_status():
    s = parse_line('FNT1103CE>OGNFNT,qAS,FNB1103CE:>101520h Name="Juergen" 45.0dB -5.0kHz 2e', REF)
    assert isinstance(s, StatusBeacon)
    assert s.name == "Juergen"


def test_receiver_beacons_are_ignored():
    assert parse_line("LFMX>OGNSDR,TCPIP*,qAC,GLIDERN2:/160856h4414.99NI00600.28E&/A=002214", REF) is None
    assert parse_line("Koenigsd>APRS,TCPIP*,qAC,GLIDERN1:/160838h4735.60NI01125.90E&000/000/A=002952 v0.2.8", REF) is None
    assert parse_line("# aprsc 2.1.4-g408ed49", REF) is None
    assert parse_line("garbage", REF) is None


def test_negative_altitude():
    line = "FLR123456>OGFLR,qAS,Rx:/120000h4700.00N/00800.00E'090/020/A=-00050 id1E123456 +000fpm"
    b = parse_line(line, datetime(2026, 7, 15, 12, 0, 5))
    assert b.alt == pytest.approx(-50 * 0.3048)
    assert b.aircraft_type == 7


def test_midnight_rollover():
    assert decode_time("235950", "h", datetime(2026, 7, 16, 0, 0, 30)) == datetime(2026, 7, 15, 23, 59, 50)
    assert decode_time("000010", "h", datetime(2026, 7, 15, 23, 59, 55)) == datetime(2026, 7, 16, 0, 0, 10)
