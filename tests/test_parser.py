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


# Real message formats of other OGN sources (glidernet/ogn-aprs-protocol, valid_messages)

def test_naviter_40bit_id():
    # SeeYou Navigator / Oudie: 10 hex digit id, aircraft type in bits 34-37
    line = "NAV07220E>OGNAVI,qAS,NAVITER:/125447h4557.77N/01220.19E'258/056/A=006562 !W76! id1C4007220E +180fpm +0.0rot"
    b = parse_line(line, datetime(2026, 7, 15, 12, 55))
    assert isinstance(b, AircraftBeacon)
    assert (b.source, b.aircraft_type, b.address, b.address_type) == ("Naviter", 7, "07220E", 4)
    assert not b.stealth and not b.no_tracking
    glider = parse_line(
        "NAV042121>OGNAVI-1,qAS,NAVITER:/140648h4550.36N/01314.85E'090/152/A=001086 !W47! id0440042121 +000fpm +0.5rot",
        datetime(2026, 7, 15, 14, 7),
    )
    assert (glider.source, glider.aircraft_type) == ("Naviter", 1)  # versioned tocall "OGNAVI-1"


def test_flymaster_without_id_counts_as_paraglider():
    b = parse_line("FMT924469>OGFLYM,qAS,FLYMASTER:/155232h3720.70N/00557.97W^222/092/A=000029 !W52!", datetime(2026, 7, 15, 15, 53))
    assert (b.source, b.aircraft_type, b.address) == ("Flymaster", 7, "924469")


def test_skybase_aircraft_over_tcpip_path():
    b = parse_line("SKYBASE>OGSKYB,TCPIP*:/130837h4704.16N/01526.90E'123/018/A=001201 id1C97DDFD +120fpm", datetime(2026, 7, 15, 13, 9))
    assert (b.source, b.aircraft_type, b.address) == ("SkyBase", 7, "97DDFD")


def test_sources_without_type_are_unknown():
    lt24 = parse_line("FLRDDE48A>OGLT24,qAS,LT24:/102606h4030.47N/00338.38W'000/018/A=002267 id25387 +000fpm GPS", datetime(2026, 7, 15, 10, 27))
    assert (lt24.source, lt24.aircraft_type, lt24.address) == ("LiveTrack24", 0, "DDE48A")
    spot = parse_line("ICA3E7540>OGSPOT,qAS,SPOT:/161427h1448.35S/04610.86W'000/000/A=008677 id0-2860357 SPOT3 GOOD", datetime(2026, 7, 15, 16, 15))
    assert (spot.source, spot.aircraft_type) == ("SPOT", 0)


def test_stations_are_not_aircraft():
    ref = datetime(2026, 7, 15, 21, 5)
    # FANET weather station, FANET ground station, OGN receiver status, position without altitude
    assert parse_line("FNT0828B8>OGNFNT,qAS,Huenenb2:/210414h4710.43N/00826.96E_152/001g002t057r000p000h48b10227 0.0dB", ref) is None
    assert parse_line("FNB1103CE>OGNFNT,TCPIP*,qAC,GLIDERN3:/183738h5057.95NI00801.00E&/A=001042", ref) is None
    assert parse_line("LILH>OGNSDR,TCPIP*,qAC,GLIDERN2:>132201h v0.2.7.RPI-GPU CPU:0.7 RAM:770.2/968.2MB", ref) is None
    assert parse_line("FLRDDEEF1>OGCAPT,qAS,CAPTURS:/062744h4845.03N/00230.46E'000/000/", ref) is None


def test_adsb_paraglider_category_is_unknown():
    # ADS-B emitter category "ultralight / hang glider / paraglider" is used by microlights
    line = "ICA3FF19F>OGADSB,qAS,AVX1368:/065130h4730.12N/01041.92E^112/165/A=009836 !W51! id1D3FF19F +000fpm"
    b = parse_line(line, datetime(2026, 10, 1, 6, 52))
    assert (b.source, b.aircraft_type) == ("ADS-B", 0)
    flarm_pg = parse_line(line.replace(">OGADSB", ">OGFLR").replace("ICA", "FLR"), datetime(2026, 10, 1, 6, 52))
    assert flarm_pg.aircraft_type == 7
