"""One-off: fill `products` / `competitor_products` for the names that lack them.

RUNBOOK: "השדות שקובעים את איכות הכיסוי העקיף הם peer_names, competitor_products
ו-themes. בלעדיהם השם ייאסף אבל לא תקבל עליו קריאה צולבת." 17 of 22 names had no
competitor_products, so PRODUCT_RIVAL read-across was impossible for them by
construction.

Terms are deliberately distinctive and multi-word. A bare category word becomes a
DIRECT match rule and tags unrelated documents as the company's own news - which
is exactly how "POWER" made a Bombardier airworthiness directive read as Tower
Semiconductor news.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PRODUCTS: dict[str, dict[str, list[str]]] = {
    "ALLT": {
        "NETWORKSECURE": ["NetworkSecure", "network-based security", "clean pipe security"],
        "DDOS_SECURE": ["DDoS Secure", "DDoS mitigation"],
        "ALLOT_SMART": ["Allot Smart", "deep packet inspection", "traffic management"],
    },
    "AUDC": {
        "MEDIANT_SBC": ["Mediant SBC", "session border controller"],
        "VOCA_CIC": ["Voca CIC", "conversational IVR"],
        "LIVE_FOR_TEAMS": ["Live for Teams", "Teams Direct Routing", "Operator Connect"],
    },
    "BWAY": {
        "DEEP_TMS": ["Deep TMS", "dTMS", "H-coil", "H1 coil"],
        "BRAINSWAY_360": ["BrainsWay 360"],
    },
    "CAMT": {
        "EAGLE_INSPECTION": ["Camtek Eagle", "Eagle inspection"],
        "HAWK_INSPECTION": ["Camtek Hawk", "Hawk inspection"],
        "GOLDEN_EYE": ["Golden Eye inspection"],
    },
    "ESLT": {
        "IRON_FIST": ["Iron Fist", "active protection system"],
        "HERMES_UAV": ["Hermes 900", "Hermes 450", "Hermes 650"],
        "PULS_LAUNCHER": ["PULS rocket", "Precise Universal Launching System"],
        "JMUSIC_DIRCM": ["J-MUSIC", "DIRCM", "infrared countermeasures"],
    },
    "GILT": {
        "SKYEDGE": ["SkyEdge", "SkyEdge IV", "SkyEdge II-c"],
        "TAURUS_MODEM": ["Taurus modem"],
        "GILAT_ESA": ["electronically steered antenna", "ESA terminal"],
    },
    "ICL": {
        "POTASH_ICL": ["muriate of potash", "potash contract"],
        "BROMINE_ICL": ["elemental bromine", "flame retardant"],
        "SPECIALTY_PHOSPHATES": ["specialty phosphates", "phosphate fertilizer"],
    },
    "KEN": {
        "OPC_ENERGY": ["OPC Energy", "OPC Rotem", "OPC Hadera"],
        "CPV_GROUP": ["CPV Group", "Competitive Power Ventures"],
    },
    "LPSN": {
        "CONVERSATIONAL_CLOUD": ["Conversational Cloud", "LiveEngage"],
        "LIVEPERSON_AI": ["LivePerson AI", "conversational AI platform"],
    },
    "NICE": {
        "CXONE": ["CXone", "NICE CXone"],
        "ACTIMIZE": ["Actimize", "financial crime detection"],
        "ENLIGHTEN_AI": ["Enlighten AI", "Enlighten Copilot"],
    },
    "NVMI": {
        "VERAFLEX": ["VeraFlex"],
        "METRION": ["Nova Metrion"],
        "PRISM_OCD": ["Nova PRISM", "optical CD metrology"],
        "ELIPSON": ["Nova Elipson"],
    },
    "NYAX": {
        "VPOS_TOUCH": ["VPOS Touch", "Nayax VPOS"],
        "MONYX_WALLET": ["Monyx Wallet"],
        "NAYAX_CORE": ["Nayax Core", "unattended payment terminal"],
    },
    "ORA": {
        "ORMAT_ENERGY_CONVERTER": ["Ormat Energy Converter", "OEC unit"],
        "GEOTHERMAL_PLANT": ["binary geothermal", "geothermal power plant"],
        "RECOVERED_ENERGY": ["recovered energy generation"],
    },
    "ORMP": {
        "ORMD_0801": ["ORMD-0801", "oral insulin"],
        "ORMD_0901": ["ORMD-0901", "oral GLP-1"],
    },
    "PERI": {
        "PERION_WILDFIRE": ["Perion Wildfire"],
        "UNDERTONE": ["Undertone", "high impact advertising"],
        "PERION_SORT": ["Perion SORT", "cookieless targeting"],
    },
    "TATT": {
        "AVIATION_HEAT_TRANSFER": ["aircraft heat exchanger", "aviation heat transfer"],
        "APU_OVERHAUL": ["APU overhaul", "auxiliary power unit"],
        "LANDING_GEAR_MRO": ["landing gear overhaul"],
    },
}

RIVAL_PRODUCTS: dict[str, list[str]] = {
    "ALLT": ["Sandvine ActiveLogic", "Nokia Deepfield", "Cisco Ultra Traffic",
             "Radware DefensePro", "NetScout Arbor", "Procera PacketLogic",
             "Radcom ACE", "Huawei SmartCare"],
    "AUDC": ["Ribbon SBC", "Oracle Acme Packet", "Cisco CUBE", "Sangoma SBC",
             "Metaswitch Perimeta", "Avaya Aura", "Anywhere365"],
    "BWAY": ["NeuroStar", "Neuronetics", "MagVenture", "Magstim", "Nexstim",
             "SAINT neuromodulation", "Magnus Medical", "Spravato", "esketamine"],
    "CAMT": ["Onto Innovation Dragonfly", "KLA ICOS", "Rudolph Technologies",
             "Cohu inspection", "Nordson test", "Toray inspection"],
    "ESLT": ["Rafael Trophy", "Trophy APS", "Rheinmetall StrikeShield",
             "Leonardo Falco", "Thales Watchkeeper", "Bayraktar TB2",
             "General Atomics MQ-9", "Anduril Ghost"],
    "GILT": ["Hughes JUPITER", "Viasat terminal", "ST Engineering iDirect",
             "Comtech modem", "Kratos OpenSpace", "Starlink terminal",
             "OneWeb terminal", "Kymeta antenna"],
    "ICL": ["Nutrien potash", "Mosaic phosphate", "K+S potash", "Uralkali potash",
            "Belaruskali", "Albemarle bromine", "Lanxess bromine", "OCP phosphate",
            "Compass Minerals"],
    "KEN": ["Talen Energy", "Vistra Energy", "NRG Energy", "Constellation Energy",
            "Israel Electric Corporation"],
    "LPSN": ["Intercom Fin", "Zendesk AI", "Salesforce Einstein", "Genesys Cloud CX",
             "Twilio Flex", "Sprinklr Service", "Kore.ai", "Verint Da Vinci"],
    "NICE": ["Genesys Cloud CX", "Five9 Intelligent CX", "Verint Da Vinci",
             "Twilio Flex", "Amazon Connect", "Salesforce Service Cloud",
             "Zoom Contact Center", "Pegasystems Customer Service"],
    "NVMI": ["KLA SpectraFilm", "KLA Aleris", "Onto Innovation Atlas",
             "Hitachi High-Tech CD-SEM", "Bruker metrology", "Screen Holdings metrology"],
    "NYAX": ["Cantaloupe ePort", "PayRange", "USA Technologies", "Ingenico terminal",
             "Worldline terminal", "Adyen unattended", "Crane Payment Innovations"],
    "ORA": ["Calpine geothermal", "Enel Green Power geothermal", "Fervo Energy",
            "Eavor Loop", "Tesla Megapack", "Fluence storage", "Turboden ORC"],
    "ORMP": ["Rybelsus", "oral semaglutide", "Afrezza", "inhaled insulin",
             "Mounjaro", "Ozempic", "Tresiba", "Toujeo", "insulin icodec", "Awiqli"],
    "PERI": ["Taboola Realize", "Outbrain Onyx", "Criteo Commerce Media",
             "Magnite ClearLine", "PubMatic Connect", "The Trade Desk Kokai",
             "Microsoft Advertising"],
    "TATT": ["Honeywell APU", "Pratt Whitney APU", "Liebherr heat exchanger",
             "Collins Aerospace", "AAR Corp", "HEICO parts", "Triumph Group"],
    "TSEM": ["GlobalFoundries 22FDX", "UMC RF-SOI", "X-FAB SiC", "SkyWater foundry",
             "Vanguard International Semiconductor", "TSMC specialty",
             "Samsung Foundry 8LPP"],
}


def _fmt_list(key: str, values: list[str], indent: str = "    ") -> list[str]:
    inline = f"{indent}{key}: [" + ", ".join(f'"{v}"' for v in values) + "]"
    if len(inline) <= 96:
        return [inline + "\n"]

    out = [f"{indent}{key}:"]
    line, first = f"{indent}  [", True
    for i, v in enumerate(values):
        piece = f'"{v}"' + ("," if i < len(values) - 1 else "]")
        sep = "" if first else " "
        if len(line) + len(sep) + len(piece) > 96:
            out.append(line)
            line, sep = f"{indent}   ", ""
        line += sep + piece
        first = False
    out.append(line)
    return [l + "\n" for l in out]


def main() -> int:
    path = Path("config/universe.yaml")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    out: list[str] = []
    current: str | None = None
    pending: list[str] = []
    added: list[str] = []

    for line in lines:
        m = re.match(r"^  ([A-Z][A-Z0-9.]*):\s*$", line)
        if m:
            current = m.group(1)
        # Insert just before the next sibling key that follows `themes:`.
        if pending and re.match(r"^    [a-z_]+:", line):
            out.extend(pending)
            pending = []
        if re.match(r"^    themes:", line) and current:
            block: list[str] = []
            if current in PRODUCTS:
                block.append("    products:\n")
                for k, vals in PRODUCTS[current].items():
                    block.extend(_fmt_list(k, vals, indent="      "))
            if current in RIVAL_PRODUCTS:
                block.append(
                    "    # A rival's launch, approval or design win here reads across.\n"
                )
                block.extend(_fmt_list("competitor_products", RIVAL_PRODUCTS[current]))
            if block:
                pending = block
                added.append(current)
        out.append(line)

    if pending:
        out.extend(pending)

    path.write_text("".join(out), encoding="utf-8")
    print(f"updated {len(added)} names: {', '.join(sorted(added))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
