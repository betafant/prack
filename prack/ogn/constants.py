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

# APRS "tocall" (destination) -> data source
SOURCES: dict[str, str] = {
    "OGFLR": "FLARM",
    "OGFLR6": "FLARM",
    "OGFLR7": "FLARM",
    "OGNFNT": "FANET",
    "OGNTRK": "OGN tracker",
    "OGNSKY": "SafeSky",
    "OGNPUR": "PureTrack",
    "OGPUR": "PureTrack",
    "OGLT24": "LiveTrack24",
    "OGNLT24": "LiveTrack24",
    "OGSPOT": "SPOT",
    "OGINRE": "inReach",
    "OGNINRE": "inReach",
    "OGFLYM": "Flymaster",
    "OGSKYL": "SkyLines",
    "OGCAPT": "Capturs",
    "OGNAVI": "Naviter",
    "OGADSB": "ADS-B",
    "OGNADSB": "ADS-B",
    "OGPAW": "PilotAware",
    "OGNMTK": "Microtrak",
    "OGNXCG": "XCGlobe",
    "OGNDSX": "DeviceX",
    "OGNEMO": "Emotion",
    "OGAPIK": "APIK",
}

# Destination calls used by receivers (ground stations), never aircraft
RECEIVER_TOCALLS = {"OGNSDR", "OGNDVS", "OGNDELAY"}


def source_for(tocall: str) -> str:
    if tocall in SOURCES:
        return SOURCES[tocall]
    if tocall.startswith("OGFLR"):
        return "FLARM"
    return tocall


def category_for(aircraft_type: int) -> str:
    for cat in CATEGORIES:
        if aircraft_type in cat["types"]:
            return cat["id"]
    return "ot"
