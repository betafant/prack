"""OGN code tables."""

# Aircraft type, bits 2..5 of the OGN "id" flags byte (STttttaa).
AIRCRAFT_TYPES: dict[int, tuple[str, str]] = {
    0: ("unknown", "Unknown"),
    1: ("glider", "Glider"),
    2: ("tow", "Tow plane"),
    3: ("helicopter", "Helicopter"),
    4: ("skydiver", "Skydiver"),
    5: ("dropplane", "Drop plane"),
    6: ("hangglider", "Hang glider"),
    7: ("paraglider", "Paraglider"),
    8: ("powered", "Powered aircraft"),
    9: ("jet", "Jet"),
    10: ("ufo", "UFO"),
    11: ("balloon", "Balloon"),
    12: ("airship", "Airship"),
    13: ("uav", "Drone"),
    14: ("ground", "Ground support"),
    15: ("static", "Static object"),
}

# UI categories. Paragliders, hang gliders (deltas) and gliders are first class,
# everything else is grouped as "other".
CATEGORIES: list[dict] = [
    {"id": "pg", "label": "PG", "name": "Paragliders", "types": [7], "color": "#4ade80"},
    {"id": "hg", "label": "HG", "name": "Hang gliders", "types": [6], "color": "#38bdf8"},
    {"id": "gl", "label": "GL", "name": "Gliders", "types": [1], "color": "#fbbf24"},
    {"id": "ot", "label": "OTH", "name": "Other", "types": [0, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15], "color": "#c084fc"},
]

ADDRESS_TYPES = {0: "random", 1: "ICAO", 2: "FLARM", 3: "OGN"}

# APRS "tocall" (destination) -> data source, see
# https://github.com/glidernet/ogn-aprs-protocol/blob/master/tocalls.txt
SOURCES: dict[str, str] = {
    "APRS": "OGN",
    "OGFLR": "FLARM",
    "OGNFLR": "FLARM",
    "OGFLR6": "FLARM",
    "OGFLR7": "FLARM",
    "OGNFNT": "FANET",
    "OGNTRK": "OGN tracker",
    "OGADSL": "OGN tracker (ADS-L)",
    "OGNMYC": "MyCloudbase",
    "OGADSB": "ADS-B",
    "OGNADSB": "ADS-B",
    "OGMLAT": "OGN MLAT",
    "OGNSKY": "SafeSky",
    "OGNPUR": "PureTrack",
    "OGPUR": "PureTrack",
    "OGNAVI": "Naviter",
    "OGFLYM": "Flymaster",
    "OGLT24": "LiveTrack24",
    "OGNLT24": "LiveTrack24",
    "OGSKYL": "SkyLines",
    "OGSPOT": "SPOT",
    "OGSPID": "Spider",
    "OGINRE": "inReach",
    "OGNINRE": "inReach",
    "OGCAPT": "Capturs",
    "OGAIRM": "AirMate",
    "OGNWMN": "Wingman",
    "OGNWGL": "WeGlide",
    "OGSKYB": "SkyBase",
    "OGNVVO": "VarioVoice",
    "OGNVOL": "Volandoo",
    "OGPGP": "pgpilot",
    "OGNALP": "Alpium",
    "OGEVARIO": "eVario",
    "FXCAPP": "flyXC",
    "OGPAW": "PilotAware",
    "OGNPAW": "PilotAware",
    "OGNMTK": "Microtrak",
    "OGNMKT": "Microtrak",
    "OGNDSX": "DSX",
    "OGNMAV": "MAVLink",
    "OGNTTN": "TTN",
    "OGNHEL": "Helium",
    "OGAVZ": "Aviaze",
    "OGSTUX": "Stratux",
    "OGAPIK": "APIK",
    "OGMSHT": "Meshtastic",
    "OGBSTOP": "BirdStop",
    "OGNFNO": "Flying Neurons",
}

# Destination calls used by ground stations / weather stations, never aircraft
RECEIVER_TOCALLS = {"OGNSDR", "OGNDVS", "OGNEMO", "OGNSXR"}

# Sources whose position messages carry no "id" field at all
NO_ID_SOURCES = {"OGFLYM", "OGCAPT"}

# Aircraft type for sources that do not transmit one. Flymaster builds paragliding / hang
# gliding instruments, so its users are counted as paragliders.
SOURCE_DEFAULT_TYPES = {"OGFLYM": 7}


# ADS-B emitter category B4 ("ultralight / hang glider / paraglider") arrives as OGN type 7. Paragliders
# and hang gliders do not carry ADS-B transponders, microlights do: such targets are "unknown".
ADSB_TOCALLS = {"OGADSB", "OGNADSB"}

# Highest plausible ground speed per aircraft type (km/h, incl. a strong tail wind). A "paraglider" that
# keeps flying faster is a powered aircraft or a wrongly configured device.
MAX_TYPE_SPEED_KMH = {7: 130.0, 6: 180.0}


# One device heard over several protocols (FLARM + FANET, FANET + ADS-L, ...) is shown and recorded once,
# under the identity of the preferred source. FLARM first: it sends the most frequent and precise
# positions. A FANET pilot name is kept whichever source wins.
SOURCE_PRIORITY = {"FLARM": 0, "FANET": 1, "OGN tracker": 2, "OGN tracker (ADS-L)": 3}


def source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 9)


def normalize_tocall(tocall: str) -> str:
    """Strip an APRS version suffix: "OGNAVI-1" -> "OGNAVI"."""
    return tocall.split("-", 1)[0].upper()


def source_for(tocall: str) -> str:
    tocall = normalize_tocall(tocall)
    if tocall in SOURCES:
        return SOURCES[tocall]
    for prefix, name in (("OGFLR", "FLARM"), ("OGTTN", "TTN"), ("OGNTTN", "TTN")):
        if tocall.startswith(prefix):
            return name
    return tocall


def category_for(aircraft_type: int) -> str:
    for cat in CATEGORIES:
        if aircraft_type in cat["types"]:
            return cat["id"]
    return "ot"
