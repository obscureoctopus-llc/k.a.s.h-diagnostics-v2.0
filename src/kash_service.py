#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import threading
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import serial
import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

try:
    import can  # type: ignore
except ImportError:  # pragma: no cover
    can = None

VERSION = "2.0"
BASE_DIR = Path(__file__).resolve().parent
START_TIME = time.monotonic()
HARDWARE_CONNECTED = "CONNECTED"
HARDWARE_NOT_CONNECTED = "NOT_CONNECTED"
LOCALHOST_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]


def _env_int(name: str, default: str, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: str, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_log_level(name: str, default: str) -> str:
    value = os.getenv(name, default).upper()
    return value if value in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"} else default.upper()


def _env_origins() -> List[str]:
    raw = os.getenv("KASH_ALLOW_ORIGINS", ",".join(LOCALHOST_ORIGINS)).strip()
    return [origin.strip() for origin in raw.split(",") if origin.strip()] or LOCALHOST_ORIGINS[:]


KASH_HOST = os.getenv("KASH_HOST", "0.0.0.0")
KASH_PORT = _env_int("KASH_PORT", "8000", minimum=1, maximum=65535)
KASH_LOG_LEVEL = _env_log_level("KASH_LOG_LEVEL", "INFO")
KASH_GPIO_PORT = os.getenv("KASH_GPIO_PORT", "/dev/ttyAMA0")
KASH_BAUD_RATE = _env_int("KASH_BAUD_RATE", "9600", minimum=1, maximum=10000000)
KASH_RECONNECT_INTERVAL = _env_float("KASH_RECONNECT_INTERVAL", "5.0", minimum=0.1, maximum=3600.0)
KASH_ALLOW_ORIGINS = _env_origins()


def configure_logging() -> logging.Logger:
    formatter = logging.Formatter("[%(asctime)s] %(name)s — %(levelname)s: %(message)s")
    root = logging.getLogger()
    if root.handlers:
        for handler in list(root.handlers):
            root.removeHandler(handler)
    root.setLevel(getattr(logging, KASH_LOG_LEVEL, logging.INFO))
    file_handler = RotatingFileHandler(BASE_DIR.parent / "kash_service.log", maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return logging.getLogger("kash.service")


service_log = configure_logging()
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("kash.diag")


# ═══════════════════════════════════════════════════════════════════
#  SECTION 1 — VEHICLE TYPE TAXONOMY
# ═══════════════════════════════════════════════════════════════════

class VehicleType(IntEnum):
    CAR              = auto()
    LIGHT_TRUCK      = auto()
    HEAVY_TRUCK      = auto()
    MOTORCYCLE       = auto()
    ATV              = auto()
    UTV_SXS          = auto()
    MARINE           = auto()
    AGRICULTURE      = auto()
    CONSTRUCTION     = auto()
    SNOWMOBILE       = auto()
    GOLF_CART        = auto()
    FORKLIFT         = auto()
    LAWN_MOWER       = auto()
    ELECTRIC_VEHICLE = auto()
    RV_MOTORHOME     = auto()
    TRAILER          = auto()


class Protocol(IntEnum):
    # Automotive OBD-II (1996+)
    OBD2_CAN_500      = auto()   # ISO 15765-4 CAN 500 kbps 11-bit
    OBD2_CAN_500_29   = auto()   # ISO 15765-4 CAN 500 kbps 29-bit
    OBD2_CAN_250      = auto()   # ISO 15765-4 CAN 250 kbps 11-bit
    OBD2_CAN_250_29   = auto()   # ISO 15765-4 CAN 250 kbps 29-bit
    OBD2_ISO9141      = auto()   # ISO 9141-2 K-Line (5 baud init)
    OBD2_KWP2000_SLOW = auto()   # ISO 14230 KWP slow init (5 baud)
    OBD2_KWP2000_FAST = auto()   # ISO 14230 KWP fast init
    OBD2_J1850_VPW    = auto()   # SAE J1850 VPW (GM)
    OBD2_J1850_PWM    = auto()   # SAE J1850 PWM (Ford)

    # Pre-OBD / OBD-I (1980–1995)
    GM_ALDL_160       = auto()   # GM 160 baud ALDL
    GM_ALDL_8192      = auto()   # GM 8192 baud ALDL
    FORD_EEC3         = auto()   # Ford EEC-III
    FORD_EEC4         = auto()   # Ford EEC-IV
    CHRYSLER_SCI      = auto()   # Chrysler SCI 7812.5 baud
    TOYOTA_DLC        = auto()   # Toyota diagnostic connector
    HONDA_DLC         = auto()   # Honda 2-pin/3-pin
    NISSAN_CONSULT    = auto()   # Nissan Consult I/II
    MAZDA_DLC         = auto()   # Mazda diagnostic
    SUBARU_SSM        = auto()   # Subaru Select Monitor
    MITSUBISHI_MUT    = auto()   # Mitsubishi MUT-II

    # Heavy-Duty Truck
    J1939             = auto()   # SAE J1939 CAN 250 kbps 29-bit
    J1708_J1587       = auto()   # SAE J1708/1587 RS-485 9600 baud
    RP1210            = auto()   # RP1210 generic HD interface

    # Motorcycle
    KAWASAKI_KDS      = auto()   # K-Line 10400 baud
    HONDA_HDS_MOTO    = auto()   # K-Line 10400 baud
    YAMAHA_YDS        = auto()   # K-Line 10400 baud
    SUZUKI_SDS        = auto()   # K-Line 9600/10400 baud
    HARLEY_ESPFI      = auto()   # J1850/CAN (year-dependent)
    BMW_GS911         = auto()   # K-Line → CAN (2014+)
    DUCATI_DDS        = auto()   # K-Line / CAN
    KTM_DEALER        = auto()   # K-Line / CAN
    TRIUMPH_DEALER    = auto()   # K-Line / CAN
    APRILIA_DIAG      = auto()   # Piaggio/Aprilia diagnostic
    INDIAN_DIAG       = auto()   # Indian Motorcycle (Polaris-based)
    ROYAL_ENFIELD     = auto()   # RE diagnostic

    # ATV / UTV / Side-by-Side
    POLARIS_RIDE      = auto()   # Polaris CAN diagnostic
    CANAM_BUDS        = auto()   # BRP BUDS (CAN)
    HONDA_ATV         = auto()   # Honda K-Line ATV
    YAMAHA_ATV        = auto()   # Yamaha K-Line ATV
    KAWASAKI_ATV      = auto()   # Kawasaki KDS ATV
    CFMOTO_DIAG       = auto()   # CF Moto CAN/K-Line
    ARCTIC_CAT_DIC    = auto()   # Arctic Cat / Textron

    # Marine
    NMEA2000          = auto()   # CAN 250 kbps 29-bit (all modern)
    MERCURY_SMARTCRAFT= auto()   # Mercury/MerCruiser CAN
    YAMAHA_MARINE     = auto()   # Yamaha Command Link
    SUZUKI_MARINE     = auto()   # Suzuki DF CAN
    HONDA_MARINE      = auto()   # Honda BF K-Line/CAN
    EVINRUDE_ICON     = auto()   # Evinrude ICON (now BRP)
    VOLVO_PENTA_EVC   = auto()   # Volvo Penta EVC-E/C/D
    TOHATSU_DIAG      = auto()   # Tohatsu outboard

    # Agriculture
    ISOBUS            = auto()   # ISO 11783 (CAN 250 kbps)
    JD_SERVICE_ADVISOR= auto()   # John Deere Service ADVISOR
    CASE_EDS          = auto()   # Case IH / New Holland EDS
    AGCO_EDT          = auto()   # AGCO Electronic Diagnostic Tool
    CLAAS_CDS         = auto()   # CLAAS Diagnostic System
    KUBOTA_DIAGMASTER = auto()   # Kubota DiagMaster

    # Construction
    CAT_ET            = auto()   # Caterpillar Electronic Technician
    JD_ADVISOR_CONST  = auto()   # John Deere construction
    BOBCAT_SID        = auto()   # Bobcat Service Analyzer
    KOMATSU_KOMTRAX   = auto()   # Komatsu KOMTRAX
    VOLVO_CE_VCADS    = auto()   # Volvo Construction VCADS Pro
    HITACHI_DR_ZX     = auto()   # Hitachi Dr. ZX
    CASE_CE_EDS       = auto()   # Case Construction EDS
    LIEBHERR_LDS      = auto()   # Liebherr Diagnostic System

    # Snowmobile
    BRP_BUDS          = auto()   # Ski-Doo / Lynx BUDS
    POLARIS_SNOW      = auto()   # Polaris snowmobile CAN
    ARCTIC_CAT_SNOW   = auto()   # Arctic Cat snowmobile
    YAMAHA_SNOW       = auto()   # Yamaha snowmobile

    # Golf Cart
    CLUB_CAR_PREC     = auto()   # Club Car Precedent/Onward
    EZGO_TXT_RXV      = auto()   # EZ-GO TXT/RXV
    YAMAHA_CART        = auto()   # Yamaha Drive/Drive2
    STAR_EV           = auto()   # Star EV

    # Forklift
    TOYOTA_FORKLIFT   = auto()   # Toyota/Raymond CAN
    HYSTER_YALE       = auto()   # Hyster-Yale CAN
    CROWN_DIAG        = auto()   # Crown InfoLink
    CATERPILLAR_LIFT  = auto()   # Cat lift truck
    KOMATSU_LIFT      = auto()   # Komatsu forklift

    # Lawn / Zero-Turn
    KOHLER_INTELLIPWR = auto()   # Kohler IntelliPower CAN
    BRIGGS_INFOHUB    = auto()   # Briggs & Stratton InfoHub
    KAWASAKI_FX_FR    = auto()   # Kawasaki FX/FR engine
    JOHN_DEERE_LAWN   = auto()   # John Deere mower CAN
    HUSQVARNA_DIAG    = auto()   # Husqvarna Automower

    # EV / Hybrid
    TESLA_CAN         = auto()   # Tesla CAN diagnostic
    RIVIAN_CAN        = auto()   # Rivian CAN diagnostic
    NISSAN_LEAF_CAN   = auto()   # Nissan Leaf EV-CAN
    CHEVY_BOLT_CAN    = auto()   # Chevy Bolt EV
    ZERO_MOTO_CAN     = auto()   # Zero Motorcycles
    ENERGICA_CAN      = auto()   # Energica EV motorcycle

    # RV / Motorhome
    SPARTAN_CHASSIS   = auto()   # Spartan chassis CAN
    FREIGHTLINER_XC   = auto()   # Freightliner XC chassis
    FORD_F53_CHASSIS  = auto()   # Ford F-53 stripped chassis
    WORKHORSE_CHASSIS = auto()   # Workhorse chassis

    # Trailer
    ABS_TRAILER       = auto()   # Trailer ABS (Bendix/Wabco/Haldex PLC)
    TIRE_MONITOR      = auto()   # Trailer TPMS


# ═══════════════════════════════════════════════════════════════════
#  SECTION 2 — UNIVERSAL VEHICLE DATABASE
# ═══════════════════════════════════════════════════════════════════

VEHICLE_DATABASE = {
    # ──────────────────────── CARS & LIGHT TRUCKS ────────────────────────

    VehicleType.CAR: {
        "GM": {
            "years": "1982–present",
            "protocols": {
                "1982-1985": [Protocol.GM_ALDL_160],
                "1986-1995": [Protocol.GM_ALDL_8192],
                "1996-2006": [Protocol.OBD2_J1850_VPW, Protocol.OBD2_CAN_500],
                "2007-2008": [Protocol.OBD2_CAN_500],
                "2008+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Chevrolet","GMC","Buick","Cadillac","Pontiac","Oldsmobile","Saturn","Hummer","Saab (GM)"],
            "ecu_families": {
                "E38":   {"years":"2005-2013","type":"PCM","can_id":"0x7E0/0x7E8","desc":"Gen IV LS V8 (LS2/LS3/L76/L77)"},
                "E67":   {"years":"2007-2014","type":"PCM","can_id":"0x7E0/0x7E8","desc":"Truck V8 (L92/L9H/LC9/LH6)"},
                "E92":   {"years":"2010-2019","type":"PCM","can_id":"0x7E0/0x7E8","desc":"Gen V LT V8 (LT1/LT4/L86)"},
                "E41":   {"years":"2006-2012","type":"TCM","can_id":"0x7E1/0x7E9","desc":"6L80/6L90 transmission"},
                "T87A":  {"years":"2019+","type":"PCM","can_id":"0x7E0/0x7E8","desc":"Gen V V8 + Dynamic Fuel Mgmt"},
                "E39A":  {"years":"2012-2019","type":"PCM","can_id":"0x7E0/0x7E8","desc":"Ecotec 4-cyl turbo"},
                "P01":   {"years":"1999-2005","type":"PCM","can_id":"—","desc":"LS1/LS6 Gen III (J1850 VPW)"},
                "P59":   {"years":"2002-2005","type":"PCM","can_id":"—","desc":"Truck Gen III (4.8/5.3/6.0)"},
            },
            "modules": [
                "PCM","TCM","BCM","EBCM (ABS)","SRS (Airbag)","IPC (Cluster)",
                "HVAC","OnStar","PDM","TCCM (4WD)","FICM (Fuel Inj Ctrl)",
                "TIPM","BCM2","ACC","LKA","PCS","TPMS","StabiliTrak",
            ],
        },
        "Ford": {
            "years": "1984–present",
            "protocols": {
                "1984-1988": [Protocol.FORD_EEC3],
                "1988-1995": [Protocol.FORD_EEC4],
                "1996-2005": [Protocol.OBD2_J1850_PWM, Protocol.OBD2_CAN_500],
                "2005-2008": [Protocol.OBD2_CAN_500],
                "2008+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Ford","Lincoln","Mercury"],
            "ecu_families": {
                "PCM_EEC-V":   {"years":"1996-2004","type":"PCM","can_id":"—","desc":"EEC-V J1850 PWM"},
                "PCM_CAN":     {"years":"2005+","type":"PCM","can_id":"0x7E0/0x7E8","desc":"CAN-based PCM"},
                "TCM_6R80":    {"years":"2009+","type":"TCM","can_id":"0x7E1/0x7E9","desc":"6R80/10R80 trans"},
            },
            "modules": [
                "PCM","TCM","ABS","RCM (Restraint)","IPC","GEM","APIM (Sync)",
                "PSCM (Power Steering)","SCCM (Clock Spring)","TCCM","ACM",
                "GPCM (Glow Plug)","BECM (Battery Energy Ctrl)","SOBDMC","DCDC",
                "HVAC","BCM","PAM (Parking Aid)","IPMB","HCM",
            ],
        },
        "Chrysler": {
            "years": "1983–present",
            "protocols": {
                "1983-1995": [Protocol.CHRYSLER_SCI],
                "1996-2006": [Protocol.OBD2_J1850_VPW, Protocol.OBD2_ISO9141],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Dodge","Ram","Jeep","Chrysler","Plymouth","Eagle","Fiat (FCA)"],
            "modules": [
                "PCM","TCM","ABS","ORC (Airbag)","IPC","TIPM","BCM","WCM",
                "DTCM (Dual Transfer Case)","FDCM (Final Drive)","SKREEM",
                "HVAC","SKIM","RFH","ESM","ACC","EPS","PLGM","AMP",
            ],
        },
        "Toyota": {
            "years": "1988–present",
            "protocols": {
                "1988-1995": [Protocol.TOYOTA_DLC],
                "1996-2003": [Protocol.OBD2_ISO9141, Protocol.OBD2_KWP2000_SLOW],
                "2004-2006": [Protocol.OBD2_CAN_500, Protocol.OBD2_KWP2000_FAST],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Toyota","Lexus","Scion"],
            "modules": [
                "ECM","ECT (Trans)","ABS/VSC","SRS","Combination Meter","A/C",
                "EPS","Skid Control","Occupant","Steering Angle","Parking Assist",
                "Battery Monitor (Hybrid)","MG ECU (Hybrid)","HV ECU (Hybrid)",
            ],
        },
        "Honda": {
            "years": "1992–present",
            "protocols": {
                "1992-1995": [Protocol.HONDA_DLC],
                "1996-2003": [Protocol.OBD2_ISO9141],
                "2004-2007": [Protocol.OBD2_KWP2000_FAST],
                "2008+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Honda","Acura"],
            "modules": [
                "PCM/ECM","TCM","VSA (ABS)","SRS","Gauge Assembly","EPS",
                "HVAC","Body Electrical","Smart Entry","ACC","LKA","CMBS",
                "Battery Sensor (Hybrid)","IMA (Hybrid)","MCU (Hybrid)",
            ],
        },
        "Nissan": {
            "years": "1989–present",
            "protocols": {
                "1989-1995": [Protocol.NISSAN_CONSULT],
                "1996-2006": [Protocol.OBD2_ISO9141, Protocol.OBD2_KWP2000_SLOW],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Nissan","Infiniti","Datsun"],
        },
        "Hyundai_Kia": {
            "years": "1996–present",
            "protocols": {
                "1996-2006": [Protocol.OBD2_ISO9141, Protocol.OBD2_KWP2000_SLOW],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Hyundai","Kia","Genesis"],
        },
        "VW_Audi": {
            "years": "1996–present",
            "protocols": {
                "1996-2004": [Protocol.OBD2_KWP2000_SLOW, Protocol.OBD2_ISO9141],
                "2005+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Volkswagen","Audi","Porsche","Bentley","Lamborghini","Bugatti","SEAT","Skoda"],
            "modules": [
                "ECM","TCM","ABS","Airbags","Instrument Cluster","Central Electronics",
                "Climatronic","Steering Angle","Gateway","Parking Aid","MMI/Infotainment",
                "Adaptive Cruise","Lane Assist","Battery Management","Hybrid Manager",
            ],
        },
        "BMW": {
            "years": "1996–present",
            "protocols": {
                "1996-2000": [Protocol.OBD2_ISO9141],
                "2001-2006": [Protocol.OBD2_KWP2000_FAST],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["BMW","MINI","Rolls-Royce"],
        },
        "Mercedes": {
            "years": "1996–present",
            "protocols": {
                "1996-2002": [Protocol.OBD2_ISO9141, Protocol.OBD2_KWP2000_SLOW],
                "2003+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Mercedes-Benz","Smart","Maybach","AMG"],
        },
        "Subaru": {
            "years": "1996–present",
            "protocols": {
                "1996-2004": [Protocol.SUBARU_SSM, Protocol.OBD2_ISO9141],
                "2005+":     [Protocol.OBD2_CAN_500],
            },
        },
        "Mazda": {
            "years": "1996–present",
            "protocols": {
                "1996-2006": [Protocol.MAZDA_DLC, Protocol.OBD2_ISO9141],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
        },
        "Mitsubishi": {
            "years": "1996–present",
            "protocols": {
                "1996-2006": [Protocol.MITSUBISHI_MUT, Protocol.OBD2_ISO9141],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
        },
        "Volvo": {
            "years": "1996–present",
            "protocols": {
                "1996-2005": [Protocol.OBD2_ISO9141],
                "2006+":     [Protocol.OBD2_CAN_500],
            },
        },
        "Jaguar_LandRover": {
            "years": "1996–present",
            "protocols": {
                "1996-2006": [Protocol.OBD2_ISO9141, Protocol.OBD2_KWP2000_SLOW],
                "2007+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Jaguar","Land Rover","Range Rover"],
        },
        "Stellantis_EU": {
            "years": "1996–present",
            "protocols": {
                "1996-2007": [Protocol.OBD2_ISO9141, Protocol.OBD2_KWP2000_SLOW],
                "2008+":     [Protocol.OBD2_CAN_500],
            },
            "brands": ["Peugeot","Citroën","Opel/Vauxhall","DS","Alfa Romeo","Maserati","Lancia"],
        },
    },

    # ──────────────────────── HEAVY-DUTY TRUCKS ────────────────────────

    VehicleType.HEAVY_TRUCK: {
        "Cummins": {
            "years": "1988–present",
            "protocols": {
                "1988-2003": [Protocol.J1708_J1587],
                "2004+":     [Protocol.J1939],
            },
            "engines": ["ISB 6.7","ISC 8.3","ISL 8.9","ISM 10.8","ISX 15","X12","X15","B6.7","L9"],
            "modules": ["ECM","ACM (Aftertreatment)","DPF Controller","DEF Dosing","Turbo Actuator"],
        },
        "Detroit_Diesel": {
            "years": "1993–present",
            "protocols": {
                "1993-2003": [Protocol.J1708_J1587],
                "2004+":     [Protocol.J1939],
            },
            "engines": ["DD13","DD15","DD16","Series 60","MBE 900/4000"],
        },
        "Paccar": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.J1939]},
            "engines": ["MX-11","MX-13"],
            "brands": ["Kenworth","Peterbilt"],
        },
        "Navistar": {
            "years": "1993–present",
            "protocols": {
                "1993-2003": [Protocol.J1708_J1587],
                "2004+":     [Protocol.J1939],
            },
            "engines": ["MaxxForce 7/9/10/11/13/15","DT466","N13","A26"],
            "brands": ["International","IC Bus"],
        },
        "Volvo_Truck": {
            "years": "2003–present",
            "protocols": {
                "2003+": [Protocol.J1939],
            },
            "engines": ["D11","D13","D16"],
            "brands": ["Volvo Trucks","Mack"],
        },
        "Freightliner": {
            "years": "2000–present",
            "protocols": {
                "2000-2003": [Protocol.J1708_J1587],
                "2004+":     [Protocol.J1939],
            },
            "brands": ["Freightliner","Western Star","Thomas Built Buses"],
            "modules": [
                "MCM (Motor Control)","ACM","TCM (Allison)","ABS (Bendix/Wabco)",
                "Body Controller","Instrument Cluster","HVAC","Bulkhead Module",
                "Collision Mitigation","Lane Departure","Adaptive Cruise",
            ],
        },
        "Hino": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939]},
            "engines": ["J05E","J08E","A09C"],
        },
        "Isuzu_Truck": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939]},
            "engines": ["4HK1","6HK1","4JJ1"],
            "brands": ["Isuzu","UD Trucks"],
        },
        "Allison_Trans": {
            "years": "2000–present",
            "protocols": {"2000+": [Protocol.J1939]},
            "families": ["1000","2000","3000","4000","TC10","FuelSense 2.0"],
            "modules": ["TCM","Prognostics","Retarder Controller"],
        },
        "Eaton_Trans": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939]},
            "families": ["Fuller Advantage","Endurant","Procision"],
        },
        "Bendix_ABS": {
            "years": "2000–present",
            "protocols": {"2000+": [Protocol.J1939]},
            "modules": ["EC-60","EC-80","Wingman ACB","Wingman Fusion","SmarTire TPMS"],
        },
        "Wabco_ABS": {
            "years": "2000–present",
            "protocols": {"2000+": [Protocol.J1939]},
            "modules": ["TEBS G2","VCSIII","SmartTrac","OnGuard"],
        },
    },

    # ──────────────────────── MOTORCYCLES ────────────────────────

    VehicleType.MOTORCYCLE: {
        "Kawasaki": {
            "years": "2004–present",
            "protocols": {
                "2004-2018": [Protocol.KAWASAKI_KDS],
                "2019+":     [Protocol.KAWASAKI_KDS, Protocol.OBD2_CAN_500],
            },
            "connector": "4-pin KDS (under seat or left fairing)",
            "ecu_addr": 0x11,
            "k_line_baud": 10400,
            "families": {
                "Ninja":     ["Ninja 250/300/400","ZX-6R","ZX-10R","ZX-14R","H2/H2R","ZX-4RR"],
                "Z Series":  ["Z400","Z650","Z900","Z900RS","Z H2"],
                "Versys":    ["Versys 650","Versys 1000"],
                "Vulcan":    ["Vulcan 900","Vulcan 1700","Vulcan S"],
                "KLR/KLX":   ["KLR650","KLX250","KLX300","KLX450R"],
                "Concours":  ["Concours 14 (GTR1400)"],
                "W Series":  ["W800"],
                "Eliminator":["Eliminator 400/500"],
            },
        },
        "Honda": {
            "years": "2002–present",
            "protocols": {
                "2002-2012": [Protocol.HONDA_HDS_MOTO],
                "2013+":     [Protocol.HONDA_HDS_MOTO, Protocol.OBD2_CAN_500],
            },
            "connector": "4-pin DLC (varies by model)",
            "k_line_baud": 10400,
            "families": {
                "CBR":     ["CBR250RR","CBR300R","CBR500R","CBR600RR","CBR650R","CBR1000RR-R"],
                "CB":      ["CB300R","CB500F","CB650R","CB1000R"],
                "CRF":     ["CRF250L/Rally","CRF300L","CRF450L/RL","CRF1100L Africa Twin"],
                "Gold Wing":["GL1800"],
                "Rebel":   ["CMX300","CMX500","CMX1100"],
                "NC/NX":   ["NC750X","NX500"],
                "ADV":     ["ADV160","ADV350","X-ADV"],
            },
        },
        "Yamaha": {
            "years": "2004–present",
            "protocols": {
                "2004-2015": [Protocol.YAMAHA_YDS],
                "2016+":     [Protocol.YAMAHA_YDS, Protocol.OBD2_CAN_500],
            },
            "connector": "4-pin diagnostic (under seat)",
            "k_line_baud": 10400,
            "families": {
                "YZF-R":   ["YZF-R3","YZF-R6","YZF-R7","YZF-R1/R1M"],
                "MT":      ["MT-03","MT-07","MT-09","MT-10"],
                "Ténéré":  ["Ténéré 700"],
                "Tracer":  ["Tracer 700","Tracer 900 GT"],
                "XSR":     ["XSR700","XSR900"],
                "VMAX":    ["VMAX 1700"],
                "Star":    ["Bolt","V Star"],
                "WR":      ["WR250R","WR450F"],
            },
        },
        "Suzuki": {
            "years": "2004–present",
            "protocols": {
                "2004-2014": [Protocol.SUZUKI_SDS],
                "2015+":     [Protocol.SUZUKI_SDS, Protocol.OBD2_CAN_500],
            },
            "connector": "4-pin/6-pin Suzuki DLC",
            "families": {
                "GSX-R":   ["GSX-R600","GSX-R750","GSX-R1000/R"],
                "GSX-S":   ["GSX-S750","GSX-S1000","GSX-8S","GSX-S1000GX"],
                "V-Strom": ["V-Strom 650","V-Strom 1050/DE"],
                "Hayabusa":["GSX1300R Hayabusa"],
                "SV":      ["SV650","SV650X"],
                "DR":      ["DR650","DR-Z400"],
                "Boulevard":["M50","C50","M109R"],
            },
        },
        "Harley-Davidson": {
            "years": "2001–present",
            "protocols": {
                "2001-2010": [Protocol.HARLEY_ESPFI],
                "2011-2020": [Protocol.HARLEY_ESPFI, Protocol.OBD2_J1850_VPW],
                "2021+":     [Protocol.OBD2_CAN_500],
            },
            "connector": "4-pin Deutsch (under seat) or 6-pin (2021+)",
            "families": {
                "Sportster":  ["Sportster S","Nightster","Iron 883","Forty-Eight"],
                "Softail":    ["Street Bob","Low Rider","Fat Boy","Heritage","Breakout"],
                "Touring":    ["Road Glide","Street Glide","Road King","Electra Glide","Ultra Limited"],
                "CVO":        ["CVO Road Glide","CVO Street Glide","CVO Tri Glide"],
                "Pan America": ["Pan America 1250"],
                "LiveWire":   ["LiveWire One","S2 Del Mar","S2 Mulholland"],
                "Trike":      ["Tri Glide","Freewheeler"],
            },
        },
        "BMW_Motorrad": {
            "years": "2004–present",
            "protocols": {
                "2004-2013": [Protocol.BMW_GS911],
                "2014+":     [Protocol.BMW_GS911, Protocol.OBD2_CAN_500],
            },
            "families": {
                "GS":    ["R 1250 GS/Adventure","R 1300 GS","F 850 GS","F 750 GS","G 310 GS"],
                "RT":    ["R 1250 RT"],
                "R":     ["R nineT","R 18"],
                "S":     ["S 1000 R","S 1000 RR","S 1000 XR","M 1000 RR"],
                "F":     ["F 900 R","F 900 XR"],
                "C":     ["C 400 X/GT","CE 04"],
            },
        },
        "Ducati": {
            "years": "2004–present",
            "protocols": {
                "2004-2015": [Protocol.DUCATI_DDS],
                "2016+":     [Protocol.DUCATI_DDS, Protocol.OBD2_CAN_500],
            },
            "families": {
                "Panigale":     ["Panigale V2","Panigale V4/S/R"],
                "Monster":      ["Monster","Monster SP"],
                "Multistrada":  ["Multistrada V2","Multistrada V4/S/Rally"],
                "Scrambler":    ["Scrambler Icon","Desert Sled","Full Throttle"],
                "Diavel":       ["Diavel V4"],
                "Streetfighter": ["Streetfighter V2","Streetfighter V4/S"],
                "Hypermotard":  ["Hypermotard 950"],
                "DesertX":      ["DesertX","DesertX Rally"],
            },
        },
        "KTM": {
            "years": "2006–present",
            "protocols": {
                "2006-2016": [Protocol.KTM_DEALER],
                "2017+":     [Protocol.KTM_DEALER, Protocol.OBD2_CAN_500],
            },
            "families": {
                "Duke":    ["125 Duke","200 Duke","390 Duke","790 Duke","890 Duke","1290 Super Duke R"],
                "RC":      ["RC 125","RC 390"],
                "Adventure":["390 Adventure","790 Adventure","890 Adventure","1290 Super Adventure"],
                "EXC":     ["EXC-F 250/350/450/500"],
                "SX":      ["SX-F 250/350/450"],
                "SMC":     ["690 SMC R"],
            },
            "brands_also": ["Husqvarna Motorcycles","GasGas"],
        },
        "Triumph": {
            "years": "2006–present",
            "protocols": {
                "2006-2015": [Protocol.TRIUMPH_DEALER],
                "2016+":     [Protocol.TRIUMPH_DEALER, Protocol.OBD2_CAN_500],
            },
            "families": {
                "Street Triple": ["Street Triple 660/765"],
                "Speed Triple":  ["Speed Triple 1200"],
                "Tiger":         ["Tiger 660","Tiger 850","Tiger 900","Tiger 1200"],
                "Bonneville":    ["Bonneville T100","T120","Speedmaster","Bobber"],
                "Thruxton":      ["Thruxton RS"],
                "Scrambler":     ["Scrambler 900/1200"],
                "Rocket":        ["Rocket 3"],
                "Trident":       ["Trident 660"],
                "Speed 400":     ["Speed 400","Scrambler 400 X"],
            },
        },
        "Aprilia": {
            "years": "2006–present",
            "protocols": {"2006+": [Protocol.APRILIA_DIAG, Protocol.OBD2_CAN_500]},
            "families": {"RS":["RS 660"],"Tuono":["Tuono 660","Tuono V4"],"RSV4":["RSV4"],"Tuareg":["Tuareg 660"]},
        },
        "Indian": {
            "years": "2014–present",
            "protocols": {"2014+": [Protocol.INDIAN_DIAG, Protocol.OBD2_CAN_500]},
            "families": {"Chief":["Chief","Super Chief"],"Scout":["Scout","Scout Bobber"],"Chieftain":["Chieftain"],"Challenger":["Challenger"],"Pursuit":["Pursuit"],"FTR":["FTR 1200"]},
        },
        "Royal_Enfield": {
            "years": "2017–present",
            "protocols": {"2017+": [Protocol.ROYAL_ENFIELD, Protocol.OBD2_CAN_500]},
            "families": {"Classic":["Classic 350"],"Meteor":["Meteor 350"],"Hunter":["Hunter 350"],"Himalayan":["Himalayan 450"],"Interceptor":["Interceptor 650"],"Continental GT":["Continental GT 650"],"Super Meteor":["Super Meteor 650"],"Shotgun":["Shotgun 650"]},
        },
    },

    # ──────────────────────── ATV / UTV / SxS ────────────────────────

    VehicleType.ATV: {
        "Polaris": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.POLARIS_RIDE]},
            "families": {
                "Sportsman": ["Sportsman 450/570/850/1000","Sportsman XP"],
                "Scrambler": ["Scrambler XP 1000"],
                "Outlaw":    ["Outlaw 70/110"],
            },
        },
        "Can-Am": {
            "years": "2006–present",
            "protocols": {"2006+": [Protocol.CANAM_BUDS]},
            "families": {
                "Outlander":  ["Outlander 450/570/650/850/1000"],
                "Renegade":   ["Renegade 570/850/1000"],
                "DS":         ["DS 90/250"],
            },
        },
        "Honda_ATV": {
            "years": "2004–present",
            "protocols": {"2004+": [Protocol.HONDA_ATV]},
            "families": {
                "Rancher":   ["TRX420 Rancher"],
                "Foreman":   ["TRX520 Foreman"],
                "Rubicon":   ["TRX520 Rubicon"],
                "Rincon":    ["TRX680 Rincon"],
                "Recon":     ["TRX250 Recon"],
                "FourTrax":  ["TRX250X","TRX90X"],
                "Sport":     ["TRX400EX","TRX450R","TRX700XX"],
            },
        },
        "Yamaha_ATV": {
            "years": "2006–present",
            "protocols": {"2006+": [Protocol.YAMAHA_ATV]},
            "families": {
                "Grizzly":   ["Grizzly 700"],
                "Kodiak":    ["Kodiak 450/700"],
                "Raptor":    ["Raptor 700R"],
                "YFZ":       ["YFZ450R"],
                "Wolverine": ["Wolverine RMAX"],
            },
        },
        "Kawasaki_ATV": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.KAWASAKI_ATV]},
            "families": {
                "Brute Force": ["Brute Force 300/750"],
                "KFX":         ["KFX 50/90/450R"],
            },
        },
        "CF_Moto": {
            "years": "2015–present",
            "protocols": {"2015+": [Protocol.CFMOTO_DIAG]},
            "families": {
                "CForce":    ["CForce 400/500/600/800/1000"],
            },
        },
        "Arctic_Cat": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.ARCTIC_CAT_DIC]},
            "families": {
                "Alterra":   ["Alterra 300/450/570/600/700"],
            },
        },
    },

    VehicleType.UTV_SXS: {
        "Polaris_UTV": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.POLARIS_RIDE]},
            "families": {
                "RZR":       ["RZR Trail/200/570","RZR 900","RZR XP 1000","RZR XP Turbo","RZR Pro XP","RZR Pro R"],
                "Ranger":    ["Ranger 500/570/1000","Ranger XP 1000","Ranger Crew"],
                "General":   ["General 1000","General XP 1000"],
                "XPEDITION": ["XPEDITION XP/ADV"],
            },
        },
        "Can-Am_UTV": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.CANAM_BUDS]},
            "families": {
                "Maverick": ["Maverick Sport","Maverick Trail","Maverick X3/X3 Max"],
                "Defender": ["Defender HD5/HD7/HD8/HD9/HD10","Defender MAX"],
                "Commander":["Commander 700/1000"],
            },
        },
        "Honda_UTV": {
            "years": "2009–present",
            "protocols": {"2009+": [Protocol.HONDA_ATV]},
            "families": {
                "Pioneer":  ["Pioneer 500","Pioneer 700","Pioneer 1000"],
                "Talon":    ["Talon 1000R/1000X"],
            },
        },
        "Yamaha_UTV": {
            "years": "2014–present",
            "protocols": {"2014+": [Protocol.YAMAHA_ATV]},
            "families": {
                "YXZ":       ["YXZ1000R"],
                "Wolverine": ["Wolverine X2/X4","Wolverine RMAX2/RMAX4"],
                "Viking":    ["Viking"],
            },
        },
        "Kawasaki_UTV": {
            "years": "2012–present",
            "protocols": {"2012+": [Protocol.KAWASAKI_ATV]},
            "families": {
                "Teryx":  ["Teryx S/4","Teryx KRX 1000"],
                "Mule":   ["Mule PRO-FX/FXT/DX/DXT","Mule SX"],
                "Ridge":  ["Ridge"],
            },
        },
    },

    # ──────────────────────── MARINE / BOATS ────────────────────────

    VehicleType.MARINE: {
        "Mercury": {
            "years": "2003–present",
            "protocols": {
                "2003-2014": [Protocol.MERCURY_SMARTCRAFT],
                "2015+":     [Protocol.MERCURY_SMARTCRAFT, Protocol.NMEA2000],
            },
            "families": {
                "Outboard":    ["FourStroke 15–300","Pro XS","SeaPro","Verado","V6/V8/V10/V12"],
                "Sterndrive":  ["MerCruiser 3.0L–8.2L","Bravo One/Two/Three"],
                "Inboard":     ["MerCruiser inboard"],
                "Jet":         ["Mercury Jet 25–80"],
            },
            "modules": ["ECM","Helm Master EX","VesselView","SmartCraft Gateway","DTS","Joystick Piloting"],
        },
        "Yamaha_Marine": {
            "years": "2005–present",
            "protocols": {
                "2005+": [Protocol.YAMAHA_MARINE, Protocol.NMEA2000],
            },
            "families": {
                "Outboard":   ["F25–F425","VMAX SHO","XTO Offshore V8"],
                "WaveRunner": ["VX","FX","GP1800R","SuperJet"],
            },
            "modules": ["ECM","Helm Master EX","CL5/CL7 Display","Command Link Plus"],
        },
        "Suzuki_Marine": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.SUZUKI_MARINE, Protocol.NMEA2000]},
            "families": {
                "Outboard": ["DF25A–DF350A","DF140BG","DF200A/250A/300A"],
            },
        },
        "Honda_Marine": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.HONDA_MARINE, Protocol.NMEA2000]},
            "families": {
                "Outboard": ["BF2.3–BF250"],
            },
        },
        "Evinrude_BRP": {
            "years": "2003–2020",
            "protocols": {"2003+": [Protocol.EVINRUDE_ICON, Protocol.NMEA2000]},
            "families": {
                "E-TEC":  ["E-TEC 25–300"],
                "G2":     ["E-TEC G2 150–300"],
            },
            "note": "Evinrude discontinued 2020 — BRP now Johnson OB only for parts. Diagnostics still supported.",
        },
        "Volvo_Penta": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.VOLVO_PENTA_EVC, Protocol.NMEA2000]},
            "families": {
                "Sterndrive": ["4.3L–8.1L GXi/OSi","D3/D4/D6","V6-200/V6-240/V6-280/V8-300/V8-380"],
                "IPS":        ["IPS 350/400/500/600/650/700/800/950"],
                "Inboard":    ["D1/D2 diesel"],
            },
            "modules": ["EVC-E","EVC-C","EVC-D","Glass Cockpit","Joystick"],
        },
        "Tohatsu": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.TOHATSU_DIAG]},
            "families": {"Outboard": ["MFS 9.8–250"]},
        },
        "NMEA_Generic": {
            "years": "2001–present",
            "protocols": {"2001+": [Protocol.NMEA2000]},
            "desc": "Any NMEA 2000 certified device — engine, MFD, GPS, chartplotter, fish finder, autopilot, weather, AIS",
            "pgns": "Supports reading all standard PGNs (engine RPM, temps, fuel, trim, GPS, depth, wind, etc.)",
        },
    },

    # ──────────────────────── AGRICULTURE ────────────────────────

    VehicleType.AGRICULTURE: {
        "John_Deere_AG": {
            "years": "2000–present",
            "protocols": {
                "2000-2010": [Protocol.J1939],
                "2011+":     [Protocol.J1939, Protocol.ISOBUS, Protocol.JD_SERVICE_ADVISOR],
            },
            "families": {
                "Tractors":   ["1–4 Series Compact","5–6 Series Utility","7–8 Series Row-Crop","9 Series 4WD"],
                "Combines":   ["S700/S800 Series","X9 1100"],
                "Sprayers":   ["R4030/R4038/R4044/R4045","400/600/800 Series"],
                "Planters":   ["1725/1745/1755/1775/1795","ExactEmerge"],
                "Balers":     ["Round/Square Baler"],
                "Hay":        ["Mower Conditioner","Windrower"],
            },
            "modules": ["ECU","TCU","BCU","ATC (AutoTrac)","StarFire GPS","CommandCenter Display","Gen 4 Display","JDLink"],
        },
        "Case_IH": {
            "years": "2003–present",
            "protocols": {"2003+": [Protocol.J1939, Protocol.ISOBUS, Protocol.CASE_EDS]},
            "families": {
                "Tractors":  ["Farmall (Utility)","Maxxum","Puma","Magnum","Steiger/Quadtrac"],
                "Combines":  ["Axial-Flow 150/160/250 Series"],
                "Planters":  ["Early Riser 2000 Series"],
                "Sprayers":  ["Patriot/Trident"],
            },
            "brands_also": ["New Holland Agriculture"],
        },
        "AGCO": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939, Protocol.ISOBUS, Protocol.AGCO_EDT]},
            "brands": ["Massey Ferguson","Fendt","Challenger","Gleaner","Valtra"],
        },
        "Kubota_AG": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.J1939, Protocol.ISOBUS, Protocol.KUBOTA_DIAGMASTER]},
            "families": {
                "Tractors": ["BX (Sub-compact)","L/MX (Compact)","M (Utility)","M7 (Row-crop)"],
                "Mowers":   ["Z700/Z400/Z200 ZTR"],
                "UTV":      ["RTV-X900/X1120/X1140","Sidekick"],
                "Excavator":["KX/U Series Mini Excavator"],
            },
        },
        "CLAAS": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939, Protocol.ISOBUS, Protocol.CLAAS_CDS]},
            "families": {"Tractors":["Arion","Axion","Xerion"],"Combines":["Lexion","Trion","Evion"],"Forage":["Jaguar"]},
        },
        "ISOBUS_Generic": {
            "years": "2001–present",
            "protocols": {"2001+": [Protocol.ISOBUS]},
            "desc": "Any ISO 11783 (ISOBUS) implement — planters, sprayers, spreaders, balers, tillage, grain carts. K.A.S.H. reads all standard ISOBUS PGNs for implement diagnostics.",
        },
    },

    # ──────────────────────── CONSTRUCTION ────────────────────────

    VehicleType.CONSTRUCTION: {
        "Caterpillar": {
            "years": "1996–present",
            "protocols": {
                "1996-2003": [Protocol.J1708_J1587],
                "2004+":     [Protocol.J1939, Protocol.CAT_ET],
            },
            "families": {
                "Excavator":    ["Mini (301–310)","Small (311–320)","Medium (325–340)","Large (349–395)"],
                "Dozer":        ["D1–D11"],
                "Wheel Loader": ["906–994"],
                "Backhoe":      ["415–444"],
                "Skid Steer":   ["226–272"],
                "Motor Grader": ["120–18M"],
                "Articulated":  ["725–745"],
                "Compactor":    ["CB (Tandem)","CS (Soil)"],
                "Telehandler":  ["TH255–TH514"],
                "On-Highway":   ["CT660","CT680","CT681"],
            },
            "engines": ["C0.5–C3.6 (compact)","C4.4","C7.1","C9.3","C13","C15","C18","C27","C32"],
            "modules": ["ECM","TCU","Machine ECM","Implement ECM","Display","Product Link (telematics)","Blade Control","Grade Control"],
        },
        "John_Deere_CE": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939, Protocol.JD_ADVISOR_CONST]},
            "families": {
                "Excavator":    ["17G–870G"],
                "Dozer":        ["450–1050K"],
                "Wheel Loader": ["244L–844L"],
                "Backhoe":      ["310L–710L"],
                "Skid Steer":   ["312–332G"],
                "ADT":          ["260E–460E"],
                "Motor Grader": ["622–872GP"],
            },
        },
        "Bobcat": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939, Protocol.BOBCAT_SID]},
            "families": {
                "Skid Steer":    ["S450–S850"],
                "Compact Track": ["T450–T870"],
                "Excavator":     ["E10–E165"],
                "Telehandler":   ["TL30.70–V923"],
                "Mower":         ["ZT2000–ZT7000 ZTR"],
                "UTV":           ["UV34/UV34XL"],
            },
        },
        "Kubota_CE": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.J1939, Protocol.KUBOTA_DIAGMASTER]},
            "families": {
                "Excavator":  ["KX/U Series (0.8t–8t)"],
                "Track Loader":["SVL65–SVL97"],
                "Wheel Loader":["R430–R640"],
            },
        },
        "Komatsu": {
            "years": "2004–present",
            "protocols": {"2004+": [Protocol.J1939, Protocol.KOMATSU_KOMTRAX]},
            "families": {
                "Excavator":    ["PC30–PC2000"],
                "Dozer":        ["D21–D475"],
                "Wheel Loader": ["WA70–WA900"],
                "Motor Grader": ["GD555–GD825"],
                "ADT":          ["HM300–HM400"],
            },
        },
        "Volvo_CE": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939, Protocol.VOLVO_CE_VCADS]},
            "families": {"Excavator":["EC55–EC950"],"Wheel Loader":["L20–L350"],"ADT":["A25–A60"],"Compactor":["DD25–DD140"]},
        },
        "Hitachi_CE": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.J1939, Protocol.HITACHI_DR_ZX]},
            "families": {"Excavator":["ZX17–ZX890"],"Wheel Loader":["ZW80–ZW550"]},
        },
        "Case_CE": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.J1939, Protocol.CASE_CE_EDS]},
            "families": {"Excavator":["CX17–CX800"],"Dozer":["650–2050M"],"Wheel Loader":["21G–1121G"],"Backhoe":["580–695"],"Skid Steer":["SR130–SR270/SV185–SV340/TR270–TR340"]},
        },
        "Liebherr": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.J1939, Protocol.LIEBHERR_LDS]},
            "families": {"Excavator":["A/R 900–R 9800"],"Wheel Loader":["L 506–L 586"],"Dozer":["PR 716–PR 776"],"Crane":["Mobile/Crawler Cranes"]},
        },
    },

    # ──────────────────────── SNOWMOBILES ────────────────────────

    VehicleType.SNOWMOBILE: {
        "Ski-Doo": {
            "years": "2006–present",
            "protocols": {"2006+": [Protocol.BRP_BUDS]},
            "families": {
                "MXZ":       ["MXZ Sport/TNT/X-RS/Blizzard"],
                "Summit":    ["Summit Edge/X/Expert"],
                "Renegade":  ["Renegade Sport/Adrenaline/X-RS/Enduro"],
                "Backcountry":["Backcountry/Backcountry X"],
                "Freeride":  ["Freeride 146/154/165"],
                "Expedition":["Expedition Sport/LE/Xtreme"],
                "Grand Touring":["Grand Touring Sport/Limited"],
                "Skandic":   ["Skandic WT/SWT"],
                "Tundra":    ["Tundra Sport/LT"],
            },
            "engines": ["Rotax 600R E-TEC","600 EFI","Rotax 850 E-TEC","Rotax 900 ACE Turbo R"],
            "modules": ["ECM","DESS (Digital Encoded Security)","Gauge/Display","iBR (Intelligent Brake & Reverse)"],
        },
        "Polaris_Snow": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.POLARIS_SNOW]},
            "families": {
                "Indy":     ["Indy VR1/XC/Adventure"],
                "RMK":      ["RMK Khaos/Slash"],
                "Switchback":["Switchback Assault"],
                "Voyageur":  ["Voyageur 146/155"],
                "Rush":      ["Rush Pro-S/XCR"],
                "600/850":   ["Patriot 650/850 platform"],
                "Matryx":    ["Matryx platform models"],
            },
        },
        "Arctic_Cat_Snow": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.ARCTIC_CAT_SNOW]},
            "families": {
                "ZR":       ["ZR 200/6000/8000/9000"],
                "M":        ["M 8000/M 900"],
                "Riot":     ["Riot 6000/8000/9000"],
                "Blast":    ["Blast ZR/M/LT"],
                "Norseman": ["Norseman X 8000"],
                "Lynx":     ["Lynx (BRP partnership models)"],
            },
            "note": "Textron Off Road / Arctic Cat — uses DIC protocol",
        },
        "Yamaha_Snow": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.YAMAHA_SNOW]},
            "families": {
                "Sidewinder":["Sidewinder SRX/L-TX/B-TX/M-TX/X-TX"],
                "SRViper":   ["SRViper L-TX/M-TX/R-TX"],
                "Transporter":["Transporter Lite"],
                "VK Pro":    ["VK Professional II"],
            },
            "note": "Yamaha uses own EFI systems. Older carbureted models (pre-2008) use Yamaha flash codes.",
        },
    },

    # ──────────────────────── GOLF CARTS ────────────────────────

    VehicleType.GOLF_CART: {
        "Club_Car": {
            "years": "2004–present",
            "protocols": {"2004+": [Protocol.CLUB_CAR_PREC]},
            "families": {
                "Precedent":  ["Precedent i2/i3","Tempo","Onward"],
                "Villager":   ["Villager 2/4/6/8"],
                "Carryall":   ["Carryall 300/500/502/510/550/700/710"],
                "Café Express":["Café Express"],
            },
            "controllers": ["Curtis 1268/1510/1515 (DC)","Navitas TSX (AC)","Excel (48V)"],
            "modules": ["Motor Controller","Charger","Display (IQ)","GPS/Fleet Mgmt"],
            "diagnostics": {
                "flash_codes": True,
                "controller_codes": ["Code 0 (No fault)","Code 1 (Battery Over-Voltage)","Code 2 (Battery Under-Voltage)",
                    "Code 3 (HPD — High Pedal Disable)","Code 4 (Controller Over-Temp)","Code 5 (Motor Over-Temp)",
                    "Code 6 (Current Limit)","Code 7 (Motor Stall)","Code 8 (Contactor Weld)","Code 9 (Precharge Fail)"],
            },
        },
        "EZGO": {
            "years": "2002–present",
            "protocols": {"2002+": [Protocol.EZGO_TXT_RXV]},
            "families": {
                "TXT":    ["TXT Gas","TXT Electric (48V DC)","TXT ELiTE (AC)"],
                "RXV":    ["RXV Gas","RXV Electric (48V DC)","RXV ELiTE (AC)"],
                "Freedom":["Freedom RXV/TXT"],
                "Liberty": ["Liberty"],
                "L6":     ["L6 (6-passenger)"],
                "Express": ["Express S4/L6/S6"],
                "Shuttle":["Shuttle 2+2/4+2/6/8"],
            },
            "controllers": ["Curtis 1268 (DC)","Curtis 1234 (AC)","Textron Delta-Q charger"],
            "diagnostics": {
                "flash_codes": True,
                "controller_codes": ["Code 1 (Motor Over-Current)","Code 2 (Throttle Fault)",
                    "Code 3 (HPD)","Code 4 (Controller Over-Temp)","Code 5 (Battery Low)",
                    "Code 6 (Battery High)","Code 7 (Contactor Fault)","Code 8 (Precharge Fault)"],
            },
        },
        "Yamaha_Cart": {
            "years": "2007–present",
            "protocols": {"2007+": [Protocol.YAMAHA_CART]},
            "families": {
                "Drive":  ["Drive PTV Gas","Drive PTV Electric"],
                "Drive2": ["Drive2 PTV Gas","Drive2 PTV Electric","Drive2 PTV AC","Drive2 QuieTech EFI"],
                "Concierge": ["Concierge 4/6"],
                "Adventurer": ["Adventurer Sport 2+2"],
                "Umax":   ["UMAX One/Two/Rally/Bistro"],
            },
            "diagnostics": {
                "flash_codes": True,
                "efi_codes": True,
                "desc": "EFI models (QuieTech) have full OBD-like diagnostics. Electric models use controller flash codes.",
            },
        },
        "Star_EV": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.STAR_EV]},
            "families": {
                "Classic": ["Classic 36-2/48-2/48-4/48-6"],
                "Sport":   ["Sport 48-2/48-4"],
                "Sirius":  ["Sirius 2+2"],
                "Capella":  ["Capella"],
            },
        },
    },

    # ──────────────────────── FORKLIFTS ────────────────────────

    VehicleType.FORKLIFT: {
        "Toyota_Forklift": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.TOYOTA_FORKLIFT]},
            "families": {
                "Core IC":    ["8FGU15–8FGU32 (gas)","8FDU15–8FDU32 (diesel)"],
                "Core Electric":["8FBMT15–8FBMT35 (3-wheel)","8FBE15–8FBE20 (4-wheel)"],
                "Large IC":   ["8FG35–8FG80"],
                "Reach":      ["8BRU18–8BRU23"],
                "Order Picker":["8BPU10–8BPU15"],
            },
            "modules": ["ECM (IC)","Motor Controller (EV)","Mast Controller","SAS (System of Active Stability)","Multifunction Display"],
        },
        "Hyster_Yale": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.HYSTER_YALE]},
            "families": {
                "Hyster IC":     ["H40–H120FT","H135–H190FT","H360–H700HD"],
                "Hyster Electric":["E30–E80XN","J40–J65XN"],
                "Yale IC":       ["GP040–GP120VX","GDP/GLP060–GDP/GLP120VX"],
                "Yale Electric": ["ERP030–ERP070VL","MPB045–MPB060VG"],
            },
            "modules": ["Motor Controller","Traction Controller","Hydraulic Controller","Display Module","Shift Controller"],
        },
        "Crown_Forklift": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.CROWN_DIAG]},
            "families": {
                "Sit-Down":  ["FC 5200 Series (4-wheel)","SC 6000 Series (3-wheel)"],
                "Reach":     ["RR 5700 Series","RM 6000 Series"],
                "Order Picker":["SP 3500/SP 3520 Series"],
                "Pallet Jack":["PE 4500 Series (rider)","WP 3000 Series (walkie)"],
                "Stacker":   ["ST/SX 3000 Series"],
                "Tow":       ["TSP 6500 Series"],
            },
            "modules": ["Traction","Hydraulic Pump","Steering","Access 1 2 3 System","InfoLink Fleet Mgmt"],
        },
        "Cat_Forklift": {
            "years": "2005–present",
            "protocols": {"2005+": [Protocol.CATERPILLAR_LIFT]},
            "families": {"IC":["GP15–GP55N","DP15–DP70N"],"Electric":["EP16–EP30(C)N","2ET2500–2ET4000"]},
        },
        "Komatsu_Forklift": {
            "years": "2008–present",
            "protocols": {"2008+": [Protocol.KOMATSU_LIFT]},
            "families": {"IC":["FG15–FG70"],"Electric":["FB12–FB30"]},
        },
    },

    # ──────────────────────── LAWN / ZERO-TURN ────────────────────────

    VehicleType.LAWN_MOWER: {
        "John_Deere_Lawn": {
            "years": "2010–present",
            "protocols": {"2010+": [Protocol.JOHN_DEERE_LAWN]},
            "families": {
                "Residential ZTR":  ["Z300/Z500/Z700 Series"],
                "Commercial ZTR":   ["Z900 Series (Z915/Z920/Z930/Z950/Z960/Z970/Z994)"],
                "Commercial Mower": ["QuikTrak 600/700/800 Series"],
                "Front Mower":      ["1400/1500/1600 Series"],
                "Tractor":          ["S100/S200 Series (lawn tractor)","X300/X500/X700 Series"],
            },
            "engines": ["Kawasaki FX/FS","Briggs Commercial","Yanmar diesel","John Deere iTorque"],
        },
        "Husqvarna": {
            "years": "2015–present",
            "protocols": {"2015+": [Protocol.HUSQVARNA_DIAG]},
            "families": {
                "ZTR":       ["Z200/Z400/Z500 Series","MZ48/MZ54/MZ61"],
                "Automower": ["Automower 115H/310/315X/405X/415X/435X AWD/450X/535 AWD"],
                "Tractor":   ["YT/YTH Series"],
                "Commercial":["P-ZT/PZ Series"],
            },
            "brands_also": ["Craftsman (Husqvarna-made)"],
        },
        "Kohler_Engines": {
            "years": "2018–present",
            "protocols": {"2018+": [Protocol.KOHLER_INTELLIPWR]},
            "families": {
                "Command PRO EFI": ["CH/CV EFI Series"],
                "Confidant EFI":   ["ZT710/ZT720/ZT730/ZT740 EFI"],
                "AEGIS EFI":       ["LH630/LH690/LH750/LH775 EFI"],
                "KDI Diesel":      ["KDI 1903/2504/3404"],
            },
            "diagnostics": {
                "desc": "EFI models have CAN-based diagnostics via IntelliPower. Flash codes on LED + full fault readout via CAN.",
                "flash_codes": True,
                "can_diag": True,
            },
        },
        "Briggs_Stratton": {
            "years": "2018–present",
            "protocols": {"2018+": [Protocol.BRIGGS_INFOHUB]},
            "families": {
                "Commercial EFI":  ["Vanguard 810/895/993 EFI"],
                "Commercial":      ["Vanguard Big Block"],
                "Residential EFI": ["InStart series"],
            },
        },
        "Kawasaki_Engines": {
            "years": "2015–present",
            "protocols": {"2015+": [Protocol.KAWASAKI_FX_FR]},
            "families": {
                "FX":   ["FX541V–FX1000V EFI (commercial)"],
                "FR":   ["FR541V–FR730V (residential)"],
                "FS":   ["FS481V–FS730V"],
                "FT":   ["FT730V EFI"],
            },
            "diagnostics": {
                "desc": "EFI models have diagnostic LED flash codes + CAN diagnostic port. Non-EFI use manual troubleshooting.",
            },
        },
    },

    # ──────────────────────── ELECTRIC VEHICLES ────────────────────────

    VehicleType.ELECTRIC_VEHICLE: {
        "Tesla": {
            "years": "2012–present",
            "protocols": {"2012+": [Protocol.TESLA_CAN, Protocol.OBD2_CAN_500]},
            "families": {
                "Model S":  ["Model S (2012+)","Model S Plaid (2021+)"],
                "Model 3":  ["Model 3 (2017+)","Model 3 Highland (2024+)"],
                "Model X":  ["Model X (2015+)","Model X Plaid (2021+)"],
                "Model Y":  ["Model Y (2020+)","Model Y Juniper (2025+)"],
                "Cybertruck":["Cybertruck (2024+)"],
                "Roadster": ["Roadster (2008–2012)"],
                "Semi":     ["Semi (2022+)"],
            },
            "modules": [
                "Drive Inverter (Front/Rear)","Battery Management System (BMS)","Vehicle Controller",
                "Thermal Controller","Autopilot ECU","Infotainment (MCU)","Body Controller",
                "Charge Controller","DC-DC Converter","Park Assist","HVAC Compressor ECU",
                "Gateway","Steering Column Module","Airbag Module","TPMS",
            ],
        },
        "Rivian": {
            "years": "2022–present",
            "protocols": {"2022+": [Protocol.RIVIAN_CAN, Protocol.OBD2_CAN_500]},
            "families": {"R1T":["R1T"],"R1S":["R1S"],"Commercial":["Amazon EDV"]},
        },
        "Nissan_Leaf": {
            "years": "2011–present",
            "protocols": {"2011+": [Protocol.NISSAN_LEAF_CAN, Protocol.OBD2_CAN_500]},
            "families": {"Leaf":["Leaf (Gen 1, 2011–2017)","Leaf (Gen 2, 2018+)","Leaf Plus (62 kWh)"],"Ariya":["Ariya"]},
            "modules": ["VCM","BMS (LBC)","Motor Controller (MCU)","OBC (On-Board Charger)","DC-DC","PTC Heater","Heat Pump","TCU"],
        },
        "Chevy_Bolt": {
            "years": "2017–2023",
            "protocols": {"2017+": [Protocol.CHEVY_BOLT_CAN, Protocol.OBD2_CAN_500]},
            "families": {"Bolt EV":["Bolt EV"],"Bolt EUV":["Bolt EUV"]},
        },
        "Zero_Motorcycle": {
            "years": "2013–present",
            "protocols": {"2013+": [Protocol.ZERO_MOTO_CAN]},
            "families": {
                "Street": ["S","SR","SR/F","SR/S"],
                "Dual Sport":["DS","DSR","DSR/X"],
                "Supermoto":["FX","FXE","FXS"],
            },
            "modules": ["Motor Controller","BMS","Main Controller","Charger","Dash"],
        },
        "Energica": {
            "years": "2015–present",
            "protocols": {"2015+": [Protocol.ENERGICA_CAN]},
            "families": {"Ego":["Ego/Ego+"],"Eva":["Eva Ribelle/EsseEsse9"],"Experia":["Experia"]},
        },
    },

    # ──────────────────────── RVs / MOTORHOMES ────────────────────────

    VehicleType.RV_MOTORHOME: {
        "Chassis": {
            "desc": "K.A.S.H. diagnoses the chassis drivetrain and coach systems separately",
            "chassis_types": {
                "Ford":          {"models": ["E-450 (Class C)","F-53 (Class A)","F-59 (Step Van)"], "protocols": [Protocol.OBD2_CAN_500, Protocol.FORD_F53_CHASSIS]},
                "Chevy/GM":      {"models": ["Express 3500 (Class C)","P-Chassis (Step Van)","Workhorse W-Series"], "protocols": [Protocol.OBD2_CAN_500]},
                "Freightliner":  {"models": ["XC Chassis (Class A diesel)","S2RV","Custom Chassis"], "protocols": [Protocol.J1939, Protocol.FREIGHTLINER_XC]},
                "Spartan":       {"models": ["K1/K2/K3 Chassis (luxury Class A)","S&S Fire apparatus"], "protocols": [Protocol.J1939, Protocol.SPARTAN_CHASSIS]},
                "Ram ProMaster": {"models": ["ProMaster 2500/3500 (Class B)"], "protocols": [Protocol.OBD2_CAN_500]},
                "Mercedes":      {"models": ["Sprinter 2500/3500 (Class B/C)"], "protocols": [Protocol.OBD2_CAN_500]},
                "Ford Transit":  {"models": ["Transit 250/350 (Class B)"], "protocols": [Protocol.OBD2_CAN_500]},
            },
        },
        "Coach_Systems": {
            "desc": "Motorhome house/coach systems connected via RV-C (CAN) or proprietary",
            "systems": [
                "Generator (Onan/Cummins — J1939 or proprietary)","Inverter/Charger (Magnum, Xantrex, Victron)",
                "Leveling Jacks (HWH, Lippert)","Slide-Out Controller (Lippert, Schwintek)",
                "HVAC (Dometic, Coleman)","Aqua-Hot / Hydronic Heating","Solar MPPT Controller",
                "Battery Monitor (Victron BMV, Xanbus)","Fresh/Gray/Black Tank Sensors",
                "Awning Controller (Carefree, Lippert)","Firefly / Spyder Multiplex Wiring",
                "SilverLeaf Electronics (VMS 440/442)","TripTek (RV computer)","Tire Pressure (TST, TireMinder)",
            ],
        },
    },

    # ──────────────────────── TRAILERS ────────────────────────

    VehicleType.TRAILER: {
        "Trailer_ABS": {
            "years": "2001–present",
            "protocols": {"2001+": [Protocol.ABS_TRAILER, Protocol.J1939]},
            "systems": {
                "Bendix":   ["Tabs-6 Basic","Tabs-6 Advanced","Tabs-6 Premium","ADB22X"],
                "Wabco":    ["ABS-D (Standard)","ABS-E (Full)","EBS E2","SmartTrac","RSS"],
                "Haldex":   ["ABS Gen 3/4/5","EB+ Gen 2/3","GRSO"],
                "Meritor_WABCO": ["ECBS","ABS Standard/Plus"],
            },
            "modules": ["ABS ECU","EBS ECU","TPMS","Tire Inflation System","Liftgate Controller","Reefer Controller"],
        },
    },
}


# ═══════════════════════════════════════════════════════════════════
#  SECTION 3 — UNIVERSAL OBD-II DTC DATABASE
# ═══════════════════════════════════════════════════════════════════

# Full P0xxx/P1xxx/P2xxx/P3xxx/B/C/U code ranges
DTC_CATEGORIES = {
    "P0": "Powertrain (Generic)",
    "P1": "Powertrain (Manufacturer-Specific)",
    "P2": "Powertrain (Generic, Extended)",
    "P3": "Powertrain (Manufacturer-Specific, Extended)",
    "B0": "Body (Generic)",
    "B1": "Body (Manufacturer-Specific)",
    "B2": "Body (Manufacturer-Specific, Extended)",
    "B3": "Body (Reserved)",
    "C0": "Chassis (Generic)",
    "C1": "Chassis (Manufacturer-Specific)",
    "C2": "Chassis (Manufacturer-Specific, Extended)",
    "C3": "Chassis (Reserved)",
    "U0": "Network (Generic)",
    "U1": "Network (Manufacturer-Specific)",
    "U2": "Network (Manufacturer-Specific, Extended)",
    "U3": "Network (Reserved)",
}

# Standard OBD-II generic DTCs (P0xxx) — EVERY car 1996+ uses these
GENERIC_DTCS = {
    # ── Fuel & Air Metering ──
    "P0001": {"desc":"Fuel Volume Regulator Control Circuit/Open","system":"Fuel","severity":"high"},
    "P0002": {"desc":"Fuel Volume Regulator Control Circuit Range/Performance","system":"Fuel","severity":"high"},
    "P0003": {"desc":"Fuel Volume Regulator Control Circuit Low","system":"Fuel","severity":"high"},
    "P0004": {"desc":"Fuel Volume Regulator Control Circuit High","system":"Fuel","severity":"high"},
    "P0010": {"desc":"'A' Camshaft Position Actuator Circuit (Bank 1)","system":"Valve Timing","severity":"medium"},
    "P0011": {"desc":"'A' Camshaft Position — Timing Over-Advanced or System Performance (Bank 1)","system":"Valve Timing","severity":"medium"},
    "P0012": {"desc":"'A' Camshaft Position — Timing Over-Retarded (Bank 1)","system":"Valve Timing","severity":"medium"},
    "P0013": {"desc":"'B' Camshaft Position Actuator Circuit (Bank 1)","system":"Valve Timing","severity":"medium"},
    "P0014": {"desc":"'B' Camshaft Position — Timing Over-Advanced or System Performance (Bank 1)","system":"Valve Timing","severity":"medium"},
    "P0015": {"desc":"'B' Camshaft Position — Timing Over-Retarded (Bank 1)","system":"Valve Timing","severity":"low"},
    "P0016": {"desc":"Crankshaft Position — Camshaft Position Correlation (Bank 1 Sensor A)","system":"Valve Timing","severity":"high"},
    "P0017": {"desc":"Crankshaft Position — Camshaft Position Correlation (Bank 1 Sensor B)","system":"Valve Timing","severity":"high"},
    "P0018": {"desc":"Crankshaft Position — Camshaft Position Correlation (Bank 2 Sensor A)","system":"Valve Timing","severity":"high"},
    "P0019": {"desc":"Crankshaft Position — Camshaft Position Correlation (Bank 2 Sensor B)","system":"Valve Timing","severity":"high"},
    "P0020": {"desc":"'A' Camshaft Position Actuator Circuit (Bank 2)","system":"Valve Timing","severity":"medium"},
    "P0021": {"desc":"'A' Camshaft Position — Timing Over-Advanced or System Performance (Bank 2)","system":"Valve Timing","severity":"medium"},
    "P0022": {"desc":"'A' Camshaft Position — Timing Over-Retarded (Bank 2)","system":"Valve Timing","severity":"medium"},
    "P0030": {"desc":"HO2S Heater Control Circuit (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0031": {"desc":"HO2S Heater Control Circuit Low (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0032": {"desc":"HO2S Heater Control Circuit High (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0036": {"desc":"HO2S Heater Control Circuit (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0037": {"desc":"HO2S Heater Control Circuit Low (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0038": {"desc":"HO2S Heater Control Circuit High (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0050": {"desc":"HO2S Heater Control Circuit (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0051": {"desc":"HO2S Heater Control Circuit Low (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0052": {"desc":"HO2S Heater Control Circuit High (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0056": {"desc":"HO2S Heater Control Circuit (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0057": {"desc":"HO2S Heater Control Circuit Low (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0058": {"desc":"HO2S Heater Control Circuit High (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"low"},

    # ── Engine Misfires ──
    "P0100": {"desc":"Mass or Volume Air Flow Circuit Malfunction","system":"Air Metering","severity":"high"},
    "P0101": {"desc":"Mass or Volume Air Flow Circuit Range/Performance","system":"Air Metering","severity":"medium"},
    "P0102": {"desc":"Mass or Volume Air Flow Circuit Low Input","system":"Air Metering","severity":"high"},
    "P0103": {"desc":"Mass or Volume Air Flow Circuit High Input","system":"Air Metering","severity":"high"},
    "P0104": {"desc":"Mass or Volume Air Flow Circuit Intermittent","system":"Air Metering","severity":"medium"},
    "P0105": {"desc":"Manifold Absolute Pressure/Barometric Pressure Circuit Malfunction","system":"Air Metering","severity":"high"},
    "P0106": {"desc":"MAP/Barometric Pressure Circuit Range/Performance","system":"Air Metering","severity":"medium"},
    "P0107": {"desc":"MAP/Barometric Pressure Circuit Low Input","system":"Air Metering","severity":"high"},
    "P0108": {"desc":"MAP/Barometric Pressure Circuit High Input","system":"Air Metering","severity":"high"},
    "P0110": {"desc":"Intake Air Temperature Circuit Malfunction","system":"Air Metering","severity":"low"},
    "P0111": {"desc":"Intake Air Temperature Circuit Range/Performance","system":"Air Metering","severity":"low"},
    "P0112": {"desc":"Intake Air Temperature Circuit Low Input","system":"Air Metering","severity":"low"},
    "P0113": {"desc":"Intake Air Temperature Circuit High Input","system":"Air Metering","severity":"low"},
    "P0115": {"desc":"Engine Coolant Temperature Circuit Malfunction","system":"Cooling","severity":"high"},
    "P0116": {"desc":"Engine Coolant Temperature Circuit Range/Performance","system":"Cooling","severity":"medium"},
    "P0117": {"desc":"Engine Coolant Temperature Circuit Low Input","system":"Cooling","severity":"medium"},
    "P0118": {"desc":"Engine Coolant Temperature Circuit High Input","system":"Cooling","severity":"high"},
    "P0120": {"desc":"Throttle/Pedal Position Sensor/Switch 'A' Circuit Malfunction","system":"Throttle","severity":"high"},
    "P0121": {"desc":"Throttle/Pedal Position Sensor/Switch 'A' Circuit Range/Performance","system":"Throttle","severity":"medium"},
    "P0122": {"desc":"Throttle/Pedal Position Sensor/Switch 'A' Circuit Low Input","system":"Throttle","severity":"high"},
    "P0123": {"desc":"Throttle/Pedal Position Sensor/Switch 'A' Circuit High Input","system":"Throttle","severity":"high"},
    "P0125": {"desc":"Insufficient Coolant Temperature for Closed Loop Fuel Control","system":"Cooling","severity":"low"},
    "P0128": {"desc":"Coolant Thermostat (Coolant Temperature Below Thermostat Regulating Temperature)","system":"Cooling","severity":"low"},
    "P0130": {"desc":"O2 Sensor Circuit Malfunction (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0131": {"desc":"O2 Sensor Circuit Low Voltage (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0132": {"desc":"O2 Sensor Circuit High Voltage (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0133": {"desc":"O2 Sensor Circuit Slow Response (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0134": {"desc":"O2 Sensor Circuit No Activity Detected (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0135": {"desc":"O2 Sensor Heater Circuit Malfunction (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0136": {"desc":"O2 Sensor Circuit Malfunction (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0137": {"desc":"O2 Sensor Circuit Low Voltage (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0138": {"desc":"O2 Sensor Circuit High Voltage (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0139": {"desc":"O2 Sensor Circuit Slow Response (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0140": {"desc":"O2 Sensor Circuit No Activity Detected (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0141": {"desc":"O2 Sensor Heater Circuit Malfunction (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0150": {"desc":"O2 Sensor Circuit Malfunction (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0151": {"desc":"O2 Sensor Circuit Low Voltage (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0152": {"desc":"O2 Sensor Circuit High Voltage (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0153": {"desc":"O2 Sensor Circuit Slow Response (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0154": {"desc":"O2 Sensor Circuit No Activity Detected (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P0155": {"desc":"O2 Sensor Heater Circuit Malfunction (Bank 2 Sensor 1)","system":"O2 Sensor","severity":"low"},
    "P0156": {"desc":"O2 Sensor Circuit Malfunction (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0157": {"desc":"O2 Sensor Circuit Low Voltage (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0158": {"desc":"O2 Sensor Circuit High Voltage (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0159": {"desc":"O2 Sensor Circuit Slow Response (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0160": {"desc":"O2 Sensor Circuit No Activity Detected (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P0161": {"desc":"O2 Sensor Heater Circuit Malfunction (Bank 2 Sensor 2)","system":"O2 Sensor","severity":"low"},
    "P0170": {"desc":"Fuel Trim Malfunction (Bank 1)","system":"Fuel","severity":"medium"},
    "P0171": {"desc":"System Too Lean (Bank 1)","system":"Fuel","severity":"medium"},
    "P0172": {"desc":"System Too Rich (Bank 1)","system":"Fuel","severity":"medium"},
    "P0173": {"desc":"Fuel Trim Malfunction (Bank 2)","system":"Fuel","severity":"medium"},
    "P0174": {"desc":"System Too Lean (Bank 2)","system":"Fuel","severity":"medium"},
    "P0175": {"desc":"System Too Rich (Bank 2)","system":"Fuel","severity":"medium"},

    # ── Ignition ──
    "P0200": {"desc":"Injector Circuit Malfunction","system":"Fuel Injection","severity":"high"},
    "P0201": {"desc":"Injector Circuit Malfunction — Cylinder 1","system":"Fuel Injection","severity":"high"},
    "P0202": {"desc":"Injector Circuit Malfunction — Cylinder 2","system":"Fuel Injection","severity":"high"},
    "P0203": {"desc":"Injector Circuit Malfunction — Cylinder 3","system":"Fuel Injection","severity":"high"},
    "P0204": {"desc":"Injector Circuit Malfunction — Cylinder 4","system":"Fuel Injection","severity":"high"},
    "P0205": {"desc":"Injector Circuit Malfunction — Cylinder 5","system":"Fuel Injection","severity":"high"},
    "P0206": {"desc":"Injector Circuit Malfunction — Cylinder 6","system":"Fuel Injection","severity":"high"},
    "P0207": {"desc":"Injector Circuit Malfunction — Cylinder 7","system":"Fuel Injection","severity":"high"},
    "P0208": {"desc":"Injector Circuit Malfunction — Cylinder 8","system":"Fuel Injection","severity":"high"},
    "P0215": {"desc":"Engine Shutoff Solenoid Malfunction","system":"Engine Control","severity":"critical"},
    "P0217": {"desc":"Engine Overtemperature Condition","system":"Cooling","severity":"critical"},
    "P0218": {"desc":"Transmission Over Temperature Condition","system":"Transmission","severity":"critical"},
    "P0219": {"desc":"Engine Overspeed Condition","system":"Engine Control","severity":"critical"},
    "P0220": {"desc":"Throttle/Pedal Position Sensor/Switch 'B' Circuit Malfunction","system":"Throttle","severity":"high"},
    "P0221": {"desc":"Throttle/Pedal Position Sensor/Switch 'B' Circuit Range/Performance","system":"Throttle","severity":"medium"},
    "P0222": {"desc":"Throttle/Pedal Position Sensor/Switch 'B' Circuit Low Input","system":"Throttle","severity":"high"},
    "P0223": {"desc":"Throttle/Pedal Position Sensor/Switch 'B' Circuit High Input","system":"Throttle","severity":"high"},

    # ── Speed / Idle ──
    "P0300": {"desc":"Random/Multiple Cylinder Misfire Detected","system":"Ignition","severity":"critical"},
    "P0301": {"desc":"Cylinder 1 Misfire Detected","system":"Ignition","severity":"high"},
    "P0302": {"desc":"Cylinder 2 Misfire Detected","system":"Ignition","severity":"high"},
    "P0303": {"desc":"Cylinder 3 Misfire Detected","system":"Ignition","severity":"high"},
    "P0304": {"desc":"Cylinder 4 Misfire Detected","system":"Ignition","severity":"high"},
    "P0305": {"desc":"Cylinder 5 Misfire Detected","system":"Ignition","severity":"high"},
    "P0306": {"desc":"Cylinder 6 Misfire Detected","system":"Ignition","severity":"high"},
    "P0307": {"desc":"Cylinder 7 Misfire Detected","system":"Ignition","severity":"high"},
    "P0308": {"desc":"Cylinder 8 Misfire Detected","system":"Ignition","severity":"high"},
    "P0325": {"desc":"Knock Sensor 1 Circuit Malfunction (Bank 1)","system":"Ignition","severity":"medium"},
    "P0327": {"desc":"Knock Sensor 1 Circuit Low Input (Bank 1)","system":"Ignition","severity":"medium"},
    "P0328": {"desc":"Knock Sensor 1 Circuit High Input (Bank 1)","system":"Ignition","severity":"medium"},
    "P0330": {"desc":"Knock Sensor 2 Circuit Malfunction (Bank 2)","system":"Ignition","severity":"medium"},
    "P0335": {"desc":"Crankshaft Position Sensor 'A' Circuit Malfunction","system":"Ignition","severity":"critical"},
    "P0336": {"desc":"Crankshaft Position Sensor 'A' Circuit Range/Performance","system":"Ignition","severity":"high"},
    "P0340": {"desc":"Camshaft Position Sensor Circuit Malfunction (Bank 1)","system":"Ignition","severity":"high"},
    "P0341": {"desc":"Camshaft Position Sensor Circuit Range/Performance (Bank 1)","system":"Ignition","severity":"high"},
    "P0345": {"desc":"Camshaft Position Sensor Circuit Malfunction (Bank 2)","system":"Ignition","severity":"high"},

    # ── Emissions ──
    "P0400": {"desc":"Exhaust Gas Recirculation Flow Malfunction","system":"EGR","severity":"medium"},
    "P0401": {"desc":"Exhaust Gas Recirculation Flow Insufficient Detected","system":"EGR","severity":"medium"},
    "P0402": {"desc":"Exhaust Gas Recirculation Flow Excessive Detected","system":"EGR","severity":"medium"},
    "P0403": {"desc":"Exhaust Gas Recirculation Circuit Malfunction","system":"EGR","severity":"medium"},
    "P0404": {"desc":"Exhaust Gas Recirculation Circuit Range/Performance","system":"EGR","severity":"medium"},
    "P0405": {"desc":"EGR Sensor 'A' Circuit Low","system":"EGR","severity":"medium"},
    "P0406": {"desc":"EGR Sensor 'A' Circuit High","system":"EGR","severity":"medium"},
    "P0410": {"desc":"Secondary Air Injection System Malfunction","system":"Emissions","severity":"low"},
    "P0411": {"desc":"Secondary Air Injection System Incorrect Flow Detected","system":"Emissions","severity":"low"},
    "P0420": {"desc":"Catalyst System Efficiency Below Threshold (Bank 1)","system":"Catalyst","severity":"medium"},
    "P0421": {"desc":"Warm Up Catalyst Efficiency Below Threshold (Bank 1)","system":"Catalyst","severity":"medium"},
    "P0430": {"desc":"Catalyst System Efficiency Below Threshold (Bank 2)","system":"Catalyst","severity":"medium"},
    "P0440": {"desc":"Evaporative Emission Control System Malfunction","system":"EVAP","severity":"low"},
    "P0441": {"desc":"Evaporative Emission Control System Incorrect Purge Flow","system":"EVAP","severity":"low"},
    "P0442": {"desc":"Evaporative Emission Control System Leak Detected (small leak)","system":"EVAP","severity":"low"},
    "P0443": {"desc":"Evaporative Emission Control System Purge Control Valve Circuit Malfunction","system":"EVAP","severity":"low"},
    "P0446": {"desc":"Evaporative Emission Control System Vent Control Circuit Malfunction","system":"EVAP","severity":"low"},
    "P0449": {"desc":"Evaporative Emission Control System Vent Valve/Solenoid Circuit Malfunction","system":"EVAP","severity":"low"},
    "P0451": {"desc":"EVAP Emission Control System Pressure Sensor Range/Performance","system":"EVAP","severity":"low"},
    "P0452": {"desc":"EVAP Emission Control System Pressure Sensor Low Input","system":"EVAP","severity":"low"},
    "P0453": {"desc":"EVAP Emission Control System Pressure Sensor High Input","system":"EVAP","severity":"low"},
    "P0455": {"desc":"Evaporative Emission Control System Leak Detected (gross leak)","system":"EVAP","severity":"medium"},
    "P0456": {"desc":"Evaporative Emission Control System Leak Detected (very small leak)","system":"EVAP","severity":"low"},

    # ── Vehicle Speed / Idle ──
    "P0500": {"desc":"Vehicle Speed Sensor Malfunction","system":"Speed","severity":"high"},
    "P0501": {"desc":"Vehicle Speed Sensor Range/Performance","system":"Speed","severity":"medium"},
    "P0503": {"desc":"Vehicle Speed Sensor Intermittent/Erratic/High","system":"Speed","severity":"medium"},
    "P0504": {"desc":"Brake Switch 'A'/'B' Correlation","system":"Brake","severity":"medium"},
    "P0505": {"desc":"Idle Control System Malfunction","system":"Idle","severity":"medium"},
    "P0506": {"desc":"Idle Control System RPM Lower Than Expected","system":"Idle","severity":"low"},
    "P0507": {"desc":"Idle Control System RPM Higher Than Expected","system":"Idle","severity":"low"},
    "P0520": {"desc":"Engine Oil Pressure Sensor/Switch Circuit Malfunction","system":"Lubrication","severity":"high"},

    # ── Transmission ──
    "P0700": {"desc":"Transmission Control System Malfunction","system":"Transmission","severity":"high"},
    "P0705": {"desc":"Transmission Range Sensor Circuit Malfunction (PRNDL Input)","system":"Transmission","severity":"high"},
    "P0706": {"desc":"Transmission Range Sensor Circuit Range/Performance","system":"Transmission","severity":"medium"},
    "P0710": {"desc":"Transmission Fluid Temperature Sensor Circuit Malfunction","system":"Transmission","severity":"medium"},
    "P0715": {"desc":"Input/Turbine Speed Sensor Circuit Malfunction","system":"Transmission","severity":"high"},
    "P0720": {"desc":"Output Speed Sensor Circuit Malfunction","system":"Transmission","severity":"high"},
    "P0725": {"desc":"Engine Speed Input Circuit Malfunction","system":"Transmission","severity":"high"},
    "P0730": {"desc":"Incorrect Gear Ratio","system":"Transmission","severity":"high"},
    "P0731": {"desc":"Gear 1 Incorrect Ratio","system":"Transmission","severity":"high"},
    "P0732": {"desc":"Gear 2 Incorrect Ratio","system":"Transmission","severity":"high"},
    "P0733": {"desc":"Gear 3 Incorrect Ratio","system":"Transmission","severity":"high"},
    "P0734": {"desc":"Gear 4 Incorrect Ratio","system":"Transmission","severity":"high"},
    "P0735": {"desc":"Gear 5 Incorrect Ratio","system":"Transmission","severity":"high"},
    "P0740": {"desc":"Torque Converter Clutch Circuit Malfunction","system":"Transmission","severity":"medium"},
    "P0741": {"desc":"Torque Converter Clutch Circuit Performance or Stuck Off","system":"Transmission","severity":"medium"},
    "P0742": {"desc":"Torque Converter Clutch Circuit Stuck On","system":"Transmission","severity":"medium"},
    "P0743": {"desc":"Torque Converter Clutch Circuit Electrical","system":"Transmission","severity":"medium"},
    "P0750": {"desc":"Shift Solenoid 'A' Malfunction","system":"Transmission","severity":"high"},
    "P0751": {"desc":"Shift Solenoid 'A' Performance or Stuck Off","system":"Transmission","severity":"high"},
    "P0755": {"desc":"Shift Solenoid 'B' Malfunction","system":"Transmission","severity":"high"},
    "P0756": {"desc":"Shift Solenoid 'B' Performance or Stuck Off","system":"Transmission","severity":"high"},
    "P0760": {"desc":"Shift Solenoid 'C' Malfunction","system":"Transmission","severity":"high"},
    "P0765": {"desc":"Shift Solenoid 'D' Malfunction","system":"Transmission","severity":"high"},
    "P0770": {"desc":"Shift Solenoid 'E' Malfunction","system":"Transmission","severity":"high"},

    # ── Drivetrain ──
    "P0826": {"desc":"Up and Down Shift Switch Circuit","system":"Transmission","severity":"low"},

    # ── Diesel / Aftertreatment ──
    "P0401": {"desc":"EGR Flow Insufficient Detected","system":"EGR","severity":"medium"},
    "P2002": {"desc":"Diesel Particulate Filter Efficiency Below Threshold (Bank 1)","system":"DPF","severity":"high"},
    "P2031": {"desc":"Exhaust Gas Temperature Sensor Circuit (Bank 1 Sensor 2)","system":"Exhaust","severity":"medium"},
    "P2032": {"desc":"Exhaust Gas Temperature Sensor Circuit Low (Bank 1 Sensor 2)","system":"Exhaust","severity":"medium"},
    "P2033": {"desc":"Exhaust Gas Temperature Sensor Circuit High (Bank 1 Sensor 2)","system":"Exhaust","severity":"medium"},
    "P2047": {"desc":"Reductant Injector Circuit/Open (Bank 1 Unit 1)","system":"DEF/SCR","severity":"high"},
    "P2048": {"desc":"Reductant Injector Circuit Low (Bank 1 Unit 1)","system":"DEF/SCR","severity":"high"},
    "P2049": {"desc":"Reductant Injector Circuit High (Bank 1 Unit 1)","system":"DEF/SCR","severity":"high"},
    "P200A": {"desc":"Intake Manifold Air Temperature Sensor Circuit (Bank 1)","system":"Air Metering","severity":"medium"},
    "P2080": {"desc":"Exhaust Gas Temperature Sensor Circuit Range/Performance (Bank 1 Sensor 1)","system":"Exhaust","severity":"medium"},
    "P2100": {"desc":"Throttle Actuator Control Motor Circuit/Open","system":"Throttle","severity":"critical"},
    "P2101": {"desc":"Throttle Actuator Control Motor Circuit Range/Performance","system":"Throttle","severity":"critical"},
    "P2110": {"desc":"Throttle Actuator Control System — Forced Limited RPM","system":"Throttle","severity":"critical"},
    "P2111": {"desc":"Throttle Actuator Control System — Stuck Open","system":"Throttle","severity":"critical"},
    "P2112": {"desc":"Throttle Actuator Control System — Stuck Closed","system":"Throttle","severity":"critical"},
    "P2122": {"desc":"Throttle/Pedal Position Sensor/Switch 'D' Circuit Low Input","system":"Throttle","severity":"high"},
    "P2123": {"desc":"Throttle/Pedal Position Sensor/Switch 'D' Circuit High Input","system":"Throttle","severity":"high"},
    "P2127": {"desc":"Throttle/Pedal Position Sensor/Switch 'E' Circuit Low Input","system":"Throttle","severity":"high"},
    "P2128": {"desc":"Throttle/Pedal Position Sensor/Switch 'E' Circuit High Input","system":"Throttle","severity":"high"},
    "P2135": {"desc":"Throttle/Pedal Position Sensor/Switch 'A'/'B' Voltage Correlation","system":"Throttle","severity":"critical"},
    "P2138": {"desc":"Throttle/Pedal Position Sensor/Switch 'D'/'E' Voltage Correlation","system":"Throttle","severity":"critical"},
    "P2196": {"desc":"O2 Sensor Signal Biased/Stuck Rich (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P2197": {"desc":"O2 Sensor Signal Biased/Stuck Lean (Bank 1 Sensor 1)","system":"O2 Sensor","severity":"medium"},
    "P2270": {"desc":"O2 Sensor Signal Biased/Stuck Lean (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"medium"},
    "P2271": {"desc":"O2 Sensor Signal Biased/Stuck Rich (Bank 1 Sensor 2)","system":"O2 Sensor","severity":"medium"},

    # ── Network / Communication ──
    "U0001": {"desc":"High Speed CAN Communication Bus","system":"Network","severity":"high"},
    "U0002": {"desc":"High Speed CAN Communication Bus — Performance","system":"Network","severity":"high"},
    "U0073": {"desc":"Control Module Communication Bus 'A' Off","system":"Network","severity":"critical"},
    "U0100": {"desc":"Lost Communication With ECM/PCM 'A'","system":"Network","severity":"critical"},
    "U0101": {"desc":"Lost Communication With TCM","system":"Network","severity":"high"},
    "U0121": {"desc":"Lost Communication With ABS Control Module","system":"Network","severity":"high"},
    "U0140": {"desc":"Lost Communication With BCM","system":"Network","severity":"high"},
    "U0155": {"desc":"Lost Communication With Instrument Panel Cluster","system":"Network","severity":"medium"},
    "U0164": {"desc":"Lost Communication With HVAC Control Module","system":"Network","severity":"low"},
    "U0184": {"desc":"Lost Communication With Radio","system":"Network","severity":"low"},

    # ── ABS / Chassis ──
    "C0035": {"desc":"Left Front Wheel Speed Sensor Circuit","system":"ABS","severity":"medium"},
    "C0040": {"desc":"Right Front Wheel Speed Sensor Circuit","system":"ABS","severity":"medium"},
    "C0045": {"desc":"Left Rear Wheel Speed Sensor Circuit","system":"ABS","severity":"medium"},
    "C0050": {"desc":"Right Rear Wheel Speed Sensor Circuit","system":"ABS","severity":"medium"},
    "C0110": {"desc":"Pump Motor Circuit","system":"ABS","severity":"high"},
    "C0161": {"desc":"ABS/TCS Brake Switch Circuit","system":"ABS","severity":"medium"},
    "C0242": {"desc":"PCM Indicated Traction Control Malfunction","system":"ABS","severity":"medium"},
    "C0265": {"desc":"EBCM Motor Relay Circuit","system":"ABS","severity":"high"},
    "C0267": {"desc":"Pump Motor Circuit Open/Shorted","system":"ABS","severity":"high"},
    "C0327": {"desc":"Transfer Case 4WD Shift Relay Coil Circuit","system":"4WD","severity":"medium"},
    "C0376": {"desc":"Transfer Case Contact Plate 'A' Circuit","system":"4WD","severity":"medium"},
    "C0387": {"desc":"TCCM Unable to Determine Transfer Case Position","system":"4WD","severity":"medium"},
}


# ═══════════════════════════════════════════════════════════════════
#  SECTION 4 — SAE J1939 DTC DATABASE (Heavy-Duty)
# ═══════════════════════════════════════════════════════════════════

J1939_SPNS = {
    # Suspect Parameter Numbers — engine
    84:   {"name":"Vehicle Speed","unit":"km/h","pgn":65265},
    91:   {"name":"Accelerator Pedal Position","unit":"%","pgn":61443},
    100:  {"name":"Engine Oil Pressure","unit":"kPa","pgn":65263},
    102:  {"name":"Boost Pressure","unit":"kPa","pgn":65270},
    108:  {"name":"Barometric Pressure","unit":"kPa","pgn":65269},
    110:  {"name":"Engine Coolant Temperature","unit":"°C","pgn":65262},
    157:  {"name":"Rail Pressure","unit":"MPa","pgn":65253},
    168:  {"name":"Battery Voltage","unit":"V","pgn":65271},
    171:  {"name":"Ambient Air Temperature","unit":"°C","pgn":65269},
    174:  {"name":"Fuel Temperature","unit":"°C","pgn":65262},
    175:  {"name":"Engine Oil Temperature","unit":"°C","pgn":65262},
    183:  {"name":"Fuel Rate","unit":"L/h","pgn":65266},
    190:  {"name":"Engine Speed","unit":"rpm","pgn":61444},
    247:  {"name":"Total Engine Hours","unit":"hr","pgn":65253},
    250:  {"name":"Total Fuel Used","unit":"L","pgn":65257},
    513:  {"name":"Actual Engine — Percent Torque","unit":"%","pgn":61444},
    899:  {"name":"Engine Torque Mode","unit":"","pgn":61444},
    1172: {"name":"Turbocharger Compressor Inlet Temperature","unit":"°C","pgn":65270},
    1176: {"name":"Turbocharger Compressor Inlet Pressure","unit":"kPa","pgn":65270},
    1761: {"name":"Aftertreatment DEF Tank Level","unit":"%","pgn":65110},
    3031: {"name":"Aftertreatment DPF Differential Pressure","unit":"kPa","pgn":64920},
    3226: {"name":"Aftertreatment SCR Intake NOx","unit":"ppm","pgn":64946},
    3246: {"name":"Aftertreatment SCR Outlet NOx","unit":"ppm","pgn":64946},
    3251: {"name":"Aftertreatment DPF Intake Temperature","unit":"°C","pgn":64948},
    3252: {"name":"Aftertreatment DPF Outlet Temperature","unit":"°C","pgn":64948},
    3361: {"name":"Aftertreatment DEF Dosing Valve","unit":"","pgn":65110},
    3363: {"name":"Aftertreatment DEF Pump","unit":"","pgn":65110},
    3364: {"name":"Aftertreatment DEF Concentration","unit":"%","pgn":65110},
    4766: {"name":"Aftertreatment DPF Soot Load","unit":"%","pgn":64920},
    5246: {"name":"Aftertreatment DPF Ash Load","unit":"%","pgn":64920},
    5397: {"name":"Hybrid Battery SOC","unit":"%","pgn":64867},
}

J1939_FMI_CODES = {
    0:  "Data Valid But Above Normal Operating Range — Most Severe Level",
    1:  "Data Valid But Below Normal Operating Range — Most Severe Level",
    2:  "Data Erratic, Intermittent, or Incorrect",
    3:  "Voltage Above Normal, or Shorted to High Source",
    4:  "Voltage Below Normal, or Shorted to Low Source",
    5:  "Current Below Normal or Open Circuit",
    6:  "Current Above Normal or Grounded Circuit",
    7:  "Mechanical System Not Responding or Out of Adjustment",
    8:  "Abnormal Frequency or Pulse Width or Period",
    9:  "Abnormal Update Rate",
    10: "Abnormal Rate of Change",
    11: "Root Cause Not Known",
    12: "Bad Intelligent Device or Component",
    13: "Out of Calibration",
    14: "Special Instructions",
    15: "Data Valid But Above Normal Operating Range — Least Severe Level",
    16: "Data Valid But Above Normal Operating Range — Moderately Severe Level",
    17: "Data Valid But Below Normal Operating Range — Least Severe Level",
    18: "Data Valid But Below Normal Operating Range — Moderately Severe Level",
    19: "Received Network Data In Error",
    20: "Data Drifted High",
    21: "Data Drifted Low",
    31: "Condition Exists",
}


# ═══════════════════════════════════════════════════════════════════
#  SECTION 5 — DIAGNOSTIC PROCEDURES ENGINE
# ═══════════════════════════════════════════════════════════════════

@dataclass
class DiagStep:
    """A single diagnostic troubleshooting step."""
    number: int
    title: str
    detail: str
    tool: str = ""
    expected: str = ""
    if_fail: str = ""
    image: str = ""

@dataclass
class DiagProcedure:
    """A complete diagnostic procedure for a symptom or DTC."""
    id: str
    title: str
    applies_to: List[VehicleType]
    symptoms: List[str]
    related_dtcs: List[str]
    steps: List[DiagStep]
    difficulty: str = "intermediate"  # beginner / intermediate / advanced
    time_est: str = ""
    tools_needed: List[str] = field(default_factory=list)
    safety_warnings: List[str] = field(default_factory=list)


DIAGNOSTIC_PROCEDURES = [
    # ── ENGINE WON'T START ──
    DiagProcedure(
        id="no_start_gas",
        title="Engine Cranks But Won't Start (Gasoline)",
        applies_to=[VehicleType.CAR, VehicleType.LIGHT_TRUCK, VehicleType.RV_MOTORHOME],
        symptoms=["Engine cranks but won't start","No start condition","Cranks strong, no fire"],
        related_dtcs=["P0335","P0340","P0201","P0202","P0203","P0204","P0230","P0335","P0340","P0300"],
        difficulty="intermediate",
        time_est="30–90 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Fuel pressure gauge","Spark tester","Noid light set"],
        safety_warnings=["Do not crank for more than 15 seconds at a time","Let starter cool 30 seconds between cranks","Fuel system is under pressure — use caution"],
        steps=[
            DiagStep(1, "Scan for DTCs", "Connect K.A.S.H. → run full scan (all modules). Record all codes. DTCs narrow down fuel/spark/compression immediately.", tool="K.A.S.H."),
            DiagStep(2, "Check for spark", "Remove a spark plug wire/coil pack from any cylinder. Connect spark tester. Crank engine. Bright blue spark = ignition OK.", tool="Spark tester", expected="Bright blue spark", if_fail="No spark → check CKP sensor, CMP sensor, ignition coil, ignition relay, PCM power/ground"),
            DiagStep(3, "Check fuel pressure", "Connect fuel pressure gauge to test port on fuel rail. Key ON engine OFF. Check spec (typically 45–65 psi for port injection, 35–80 psi for TBI).", tool="Fuel pressure gauge", expected="Pressure within spec and holds", if_fail="No pressure → check fuel pump relay, fuel pump fuse, fuel pump, inertia switch (Ford)"),
            DiagStep(4, "Check injector pulse", "Install noid light on any injector connector. Crank engine. Noid light should flash.", tool="Noid light", expected="Noid light flashes during crank", if_fail="No pulse → PCM not commanding injectors. Check PCM power/ground, CKP sensor signal"),
            DiagStep(5, "Check security/immobilizer", "Many no-start conditions are caused by immobilizer faults. Check if security light is solid or flashing on dash. K.A.S.H. can read BCM/SKIM/WCM codes.", tool="K.A.S.H.", if_fail="Security light ON → immobilizer not recognizing key. Try spare key, reprogram via K.A.S.H. Module Init"),
            DiagStep(6, "Compression test", "If spark and fuel are confirmed, perform compression test on all cylinders. Should be 125–180 psi and within 10% of each other.", tool="Compression tester", expected="125–180 psi, even across cylinders", if_fail="Low/uneven compression → timing chain jumped, head gasket, valves, rings"),
        ],
    ),

    # ── ENGINE MISFIRES ──
    DiagProcedure(
        id="misfire",
        title="Engine Misfire Diagnosis",
        applies_to=[VehicleType.CAR, VehicleType.LIGHT_TRUCK, VehicleType.RV_MOTORHOME],
        symptoms=["Engine misfires","Rough idle","Flashing check engine light","Loss of power","Vibration at idle"],
        related_dtcs=["P0300","P0301","P0302","P0303","P0304","P0305","P0306","P0307","P0308"],
        difficulty="intermediate",
        time_est="30–120 min",
        tools_needed=["K.A.S.H. scanner","Spark plug socket","Torque wrench","Coil pack puller","Multimeter"],
        steps=[
            DiagStep(1, "Read misfire counters", "K.A.S.H. → Live Data → Misfire Counters. Identify which cylinder(s) are misfiring. Single cylinder = localized. Random/multiple = system-wide.", tool="K.A.S.H."),
            DiagStep(2, "Swap coil pack to different cylinder", "If single cylinder, swap ignition coil to a known-good cylinder. Clear codes, run engine. If misfire follows the coil → replace coil.", tool="Coil pack puller"),
            DiagStep(3, "Check spark plugs", "Remove and inspect spark plugs. Check gap, electrode wear, carbon fouling, oil fouling. Replace if worn/fouled.", tool="Spark plug socket", expected="Proper gap, tan/gray electrode color"),
            DiagStep(4, "Check fuel injector", "Swap injector to different cylinder (if accessible). Clear codes, run. If misfire follows → replace injector. Also check injector resistance (typically 12–16 ohms for high-impedance).", tool="Multimeter"),
            DiagStep(5, "Check compression", "Perform compression test on misfiring cylinder(s). Low compression can indicate valve, head gasket, or ring issues.", tool="Compression tester"),
            DiagStep(6, "Check for vacuum leaks", "Spray carb cleaner around intake manifold gaskets, vacuum lines, PCV valve, throttle body gaskets while engine runs. RPM change = leak found.", tool="Carb cleaner spray"),
        ],
    ),

    # ── OVERHEATING ──
    DiagProcedure(
        id="overheat",
        title="Engine Overheating Diagnosis",
        applies_to=[VehicleType.CAR, VehicleType.LIGHT_TRUCK, VehicleType.HEAVY_TRUCK, VehicleType.RV_MOTORHOME],
        symptoms=["Temperature gauge in red","Overheating","Coolant boiling over","Steam from hood","Hot coolant smell"],
        related_dtcs=["P0115","P0116","P0117","P0118","P0125","P0128","P0217"],
        difficulty="intermediate",
        time_est="30–60 min",
        tools_needed=["K.A.S.H. scanner","Cooling system pressure tester","IR thermometer","Multimeter"],
        safety_warnings=["NEVER open radiator cap on a hot engine","Coolant can cause severe burns","Let engine cool completely before servicing"],
        steps=[
            DiagStep(1, "Check coolant level", "With engine COLD, check coolant reservoir and radiator. Low coolant is the #1 cause of overheating."),
            DiagStep(2, "Scan for DTCs", "K.A.S.H. → scan ECM. Check for thermostat codes (P0128), sensor codes (P0115–P0118), fan codes.", tool="K.A.S.H."),
            DiagStep(3, "Watch live ECT data", "K.A.S.H. → Live Data → Coolant Temp. Watch temperature rise. It should stabilize at 195–220°F (90–104°C). If it keeps climbing past thermostat opening temp → thermostat is stuck closed.", tool="K.A.S.H."),
            DiagStep(4, "Check cooling fan operation", "At operating temp, fans should turn on. If electric fans, check relay, fuse, fan motor. If clutch fan, check engagement (hot engine, fan should resist spinning by hand).", expected="Fans engage at ~220°F"),
            DiagStep(5, "Pressure test cooling system", "Attach cooling system pressure tester. Pump to rated cap pressure (typically 13–16 psi). System should hold pressure without dropping for 15 min.", tool="Pressure tester", if_fail="Pressure drops → external or internal leak. Check hoses, water pump weep hole, radiator, heater core, head gasket"),
            DiagStep(6, "Check for combustion gas in coolant", "Use combustion leak tester (block test). Blue fluid turns yellow = head gasket leak allowing combustion gases into coolant.", tool="Block tester"),
        ],
    ),

    # ── TRANSMISSION ISSUES ──
    DiagProcedure(
        id="trans_slip",
        title="Transmission Slipping / Harsh Shifting",
        applies_to=[VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        symptoms=["Transmission slipping","Harsh shifts","Delayed engagement","Flare between gears","Won't shift"],
        related_dtcs=["P0700","P0730","P0731","P0732","P0733","P0734","P0740","P0741","P0750","P0755","P0760"],
        difficulty="advanced",
        time_est="45–120 min",
        tools_needed=["K.A.S.H. scanner","Trans fluid dipstick","Drain pan"],
        steps=[
            DiagStep(1, "Scan TCM for codes", "K.A.S.H. → Scan → TCM. Transmission codes give specific gear/solenoid/pressure faults.", tool="K.A.S.H."),
            DiagStep(2, "Check transmission fluid", "Check level (hot, running, in Park or Neutral depending on make). Check condition — should be red/pink, not brown/black. Smell — burnt fluid = internal damage.", expected="Correct level, red color, no burnt smell"),
            DiagStep(3, "Monitor live trans data", "K.A.S.H. → Live Data → Transmission. Watch TFT (temp), line pressure, solenoid states, gear ratio, slip RPM.", tool="K.A.S.H."),
            DiagStep(4, "Check for adaptation relearn needed", "Many modern transmissions need adaptation/relearn after battery disconnect or fluid change. K.A.S.H. → Module Init → TCM → Shift Adaptation Reset.", tool="K.A.S.H."),
            DiagStep(5, "Line pressure test", "If possible, connect line pressure gauge. Compare to spec at idle and stall. Low pressure = worn pump or valve body issue.", tool="Pressure gauge"),
        ],
    ),

    # ── ABS / BRAKES ──
    DiagProcedure(
        id="abs_light",
        title="ABS / Traction Control Warning Light",
        applies_to=[VehicleType.CAR, VehicleType.LIGHT_TRUCK, VehicleType.HEAVY_TRUCK],
        symptoms=["ABS light on","Traction control light on","StabiliTrak/ESC light","ABS activating at low speed"],
        related_dtcs=["C0035","C0040","C0045","C0050","C0110","C0161","C0265","C0267","C0242"],
        difficulty="intermediate",
        time_est="30–60 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Jack and jack stands"],
        steps=[
            DiagStep(1, "Scan ABS module for codes", "K.A.S.H. → Scan → ABS/EBCM. Wheel speed sensor codes (C0035/C0040/C0045/C0050) tell you exactly which wheel.", tool="K.A.S.H."),
            DiagStep(2, "Check wheel speed sensor wiring", "Inspect harness from sensor to ABS module. Look for chafing at suspension pivot points, corrosion at connectors.", tool="Visual inspection"),
            DiagStep(3, "Check sensor resistance", "Disconnect sensor connector. Measure resistance (typically 800–1400 ohms for passive sensors). Check for shorts to ground.", tool="Multimeter", expected="800–1400 ohms, no shorts"),
            DiagStep(4, "Check tone ring / reluctor", "With wheel off, spin hub and visually inspect tone ring for missing teeth, cracks, rust/debris buildup.", expected="Clean, intact tone ring"),
            DiagStep(5, "Check sensor air gap", "Measure gap between sensor tip and tone ring (typically 0.5–1.5mm). Adjust if out of spec.", tool="Feeler gauge"),
            DiagStep(6, "ABS module bleed", "If ABS module was replaced or air entered ABS unit: K.A.S.H. → Module Init → ABS Bleed → Follow procedure.", tool="K.A.S.H."),
        ],
    ),

    # ── 4WD / TRANSFER CASE ──
    DiagProcedure(
        id="four_wd",
        title="4WD / Transfer Case Diagnosis",
        applies_to=[VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        symptoms=["4WD light flashing","Service 4WD message","Transfer case won't shift","Grinding in 4WD","4WD auto not engaging"],
        related_dtcs=["C0327","C0376","C0387"],
        difficulty="intermediate",
        time_est="30–90 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Transfer case fluid"],
        steps=[
            DiagStep(1, "Scan TCCM for codes", "K.A.S.H. → Scan → TCCM / Transfer Case. Encoder motor codes are most common.", tool="K.A.S.H."),
            DiagStep(2, "Check encoder motor", "Encoder motor on transfer case may be sticking. Check connector, check for corrosion, check motor resistance.", tool="Multimeter"),
            DiagStep(3, "Check transfer case fluid", "Low or contaminated fluid causes shift issues. Check level and condition.", expected="Correct level, clean fluid"),
            DiagStep(4, "Run TCCM initialization", "K.A.S.H. → Module Init → TCCM → Run encoder motor relearn. This recalibrates the position sensor.", tool="K.A.S.H."),
            DiagStep(5, "Front axle actuator check", "On part-time 4WD, check front axle disconnect actuator. Listen for engagement click when shifting to 4WD.", tool="Visual/audio check"),
        ],
    ),

    # ── MOTORCYCLE — WON'T START ──
    DiagProcedure(
        id="moto_no_start",
        title="Motorcycle Won't Start",
        applies_to=[VehicleType.MOTORCYCLE],
        symptoms=["Motorcycle won't start","Cranks no start","FI light on","No crank"],
        related_dtcs=[],
        difficulty="intermediate",
        time_est="20–60 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Spark tester"],
        steps=[
            DiagStep(1, "Check kill switch and kickstand", "Kill switch in RUN position? Kickstand retracted (in gear)? Clutch pulled in? These are the top 3 'no start' causes on bikes."),
            DiagStep(2, "Scan ECU for fault codes", "K.A.S.H. → Connect via K-Line/CAN → Scan ECU. Common codes: TPS, CKP, fuel pump, tip-over sensor.", tool="K.A.S.H."),
            DiagStep(3, "Check battery voltage", "Minimum 12.4V for reliable start. Under load during crank should stay above 10.5V.", tool="Multimeter", expected="12.4V+ at rest, 10.5V+ cranking"),
            DiagStep(4, "Check fuel pump prime", "Key ON → listen for 2-second fuel pump whine. No prime = check fuel pump relay, fuse, pump wiring.", expected="Audible fuel pump prime on key-on"),
            DiagStep(5, "Check spark", "Remove spark plug, ground against engine, crank. Look for spark. On multi-cylinder, check all cylinders.", tool="Spark tester"),
            DiagStep(6, "Check tip-over sensor", "Ensure bike is upright and level. Some tip-over sensors need reset after a drop. K.A.S.H. → Module Init → Reset Tip-Over.", tool="K.A.S.H."),
        ],
    ),

    # ── HEAVY-DUTY — AFTERTREATMENT ──
    DiagProcedure(
        id="hd_aftertreatment",
        title="Aftertreatment / DPF / DEF System (Heavy-Duty)",
        applies_to=[VehicleType.HEAVY_TRUCK],
        symptoms=["Check engine light (aftertreatment)","DPF regen needed","DEF light on","Derate warning","5 MPH derate"],
        related_dtcs=["P2002"],
        difficulty="advanced",
        time_est="60–180 min",
        tools_needed=["K.A.S.H. scanner","IR thermometer","DEF refractometer"],
        safety_warnings=["DPF components reach 1100°F+ during regen","DEF fluid is corrosive to some metals","Park on level concrete/gravel for regen, not grass"],
        steps=[
            DiagStep(1, "Scan engine and ACM", "K.A.S.H. → J1939 scan → Engine ECM + ACM (Aftertreatment Control Module). Read SPNs and FMIs.", tool="K.A.S.H."),
            DiagStep(2, "Check DEF level and quality", "DEF tank level sensor reading vs actual. Test DEF concentration with refractometer (should be 32.5% urea).", tool="DEF refractometer", expected="32.5% concentration, adequate level"),
            DiagStep(3, "Check DPF soot and ash load", "K.A.S.H. → Live Data → DPF Soot Load % and Ash Load %. Soot >85% needs forced regen. Ash >100% needs DPF cleaning/replacement.", tool="K.A.S.H."),
            DiagStep(4, "Check exhaust temps", "K.A.S.H. → Live Data → EGT sensors (pre-DPF, post-DPF, pre-SCR). Should reach 550°C+ during regen. If not → check 7th injector, DOC.", tool="K.A.S.H."),
            DiagStep(5, "Force DPF regen", "If soot load is high and no other faults: K.A.S.H. → Module Init → Force DPF Regen. Vehicle must be parked, PTO off, engine at operating temp.", tool="K.A.S.H."),
            DiagStep(6, "Check NOx sensors", "SCR inlet/outlet NOx. Inlet should read 200–1500 ppm depending on load. Outlet should be < 20 ppm when SCR is working. If outlet is high → SCR catalyst degraded or DEF dosing issue.", tool="K.A.S.H."),
        ],
    ),

    # ── MARINE — ENGINE OVERHEAT ──
    DiagProcedure(
        id="marine_overheat",
        title="Marine Engine Overheating",
        applies_to=[VehicleType.MARINE],
        symptoms=["Engine overheat alarm","High temp warning on display","Steam from exhaust","Raw water flow reduced"],
        related_dtcs=[],
        difficulty="intermediate",
        time_est="30–90 min",
        tools_needed=["K.A.S.H. scanner","IR thermometer","Water pressure gauge","Impeller puller"],
        safety_warnings=["Never run outboard without water supply (or in water)","Raw water system components may be hot","Check for marine life blockage before starting"],
        steps=[
            DiagStep(1, "Scan engine ECM", "K.A.S.H. → NMEA 2000 / SmartCraft → Scan ECM. Check for overheat codes, coolant sensor faults.", tool="K.A.S.H."),
            DiagStep(2, "Check raw water intake", "Inspect water intake (seacock on inboard, lower unit on outboard) for blockage — weeds, barnacles, plastic bags, mud dauber nests.", expected="Clear intake, strong water flow"),
            DiagStep(3, "Inspect water pump impeller", "Remove water pump housing. Check impeller for missing vanes, deformation, cracking. Replace every 2 years or 300 hours.", tool="Impeller puller", expected="All vanes intact, flexible"),
            DiagStep(4, "Check thermostat", "Remove thermostat, test in hot water. Should open at rated temp (typically 140–160°F). Stuck closed = overheat. Stuck open = runs too cool.", tool="Pot of hot water, thermometer"),
            DiagStep(5, "Flush cooling system", "If salt water: flush entire cooling system with fresh water. Scale/salt buildup restricts flow through block and heat exchanger.", tool="Garden hose with flush adapter"),
            DiagStep(6, "Check exhaust system", "Restricted exhaust (collapsed hose, blocked riser/elbow) causes heat buildup. Check exhaust risers/elbows for corrosion (especially on stern drives — these rot from inside).", tool="Visual inspection"),
        ],
    ),

    # ── EV — HIGH VOLTAGE SYSTEM ──
    DiagProcedure(
        id="ev_hv_fault",
        title="EV / Hybrid High-Voltage System Fault",
        applies_to=[VehicleType.ELECTRIC_VEHICLE],
        symptoms=["High voltage warning","Reduced power","Turtle mode","Cannot charge","Battery fault light"],
        related_dtcs=[],
        difficulty="advanced",
        time_est="60–120 min",
        tools_needed=["K.A.S.H. scanner","Insulated gloves (Class 0, 1000V rated)","Insulated tools","Multimeter (CAT III 1000V)"],
        safety_warnings=[
            "HIGH VOLTAGE — EV battery packs are 200–800V DC and can be LETHAL",
            "ALWAYS wear Class 0 insulated gloves with leather protectors",
            "NEVER touch orange cables with bare hands",
            "Disable HV system and verify zero energy before any HV work",
            "Only qualified HV technicians should perform HV component service",
        ],
        steps=[
            DiagStep(1, "Scan all modules", "K.A.S.H. → EV scan → Read BMS, Motor Controller, Vehicle Controller, Charger, DC-DC. Most EV faults set multiple codes across modules.", tool="K.A.S.H."),
            DiagStep(2, "Check 12V system first", "Many EV 'won't start' issues are a dead 12V battery. The 12V system powers contactors, computers, and lights. Check 12V battery voltage.", tool="Multimeter", expected="12.4V+ at 12V battery"),
            DiagStep(3, "Check BMS cell balance", "K.A.S.H. → Live Data → BMS → Individual cell voltages. All cells should be within 0.05V of each other. Large delta = degraded cell.", tool="K.A.S.H.", expected="Cell delta < 0.05V"),
            DiagStep(4, "Check isolation resistance", "K.A.S.H. → Live Data → HV Isolation Resistance. Should be > 500 ohms/volt (> 200 kΩ for a 400V system). Low isolation = moisture ingress or damaged cable.", tool="K.A.S.H.", expected="> 500Ω/V"),
            DiagStep(5, "Check charge port and EVSE", "Inspect charge port for damage, corrosion, debris. Try different EVSE/charger. Check for pilot signal issues.", tool="Visual inspection"),
            DiagStep(6, "Check coolant system (battery thermal)", "EV batteries have liquid cooling. Check coolant level, pump operation, and temperature sensors. Overheating batteries trigger power reduction.", tool="K.A.S.H."),
        ],
    ),

    # ── GOLF CART — NO POWER ──
    DiagProcedure(
        id="golf_cart_no_power",
        title="Golf Cart Won't Move / No Power",
        applies_to=[VehicleType.GOLF_CART],
        symptoms=["Cart won't move","No power","Controller beeping","Flash codes on controller"],
        related_dtcs=[],
        difficulty="beginner",
        time_est="15–45 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Hydrometer (lead-acid)","Battery load tester"],
        steps=[
            DiagStep(1, "Read controller flash codes", "Check controller for LED flash codes. Count the flashes — each code means a specific fault (see K.A.S.H. → Golf Cart → Flash Code Lookup).", tool="K.A.S.H."),
            DiagStep(2, "Check battery pack voltage", "Measure total pack voltage at controller input (36V, 48V, or 72V depending on cart). Then check each individual battery.", tool="Multimeter", expected="Full charge: 6V battery=6.3V, 8V=8.4V, 12V=12.6V"),
            DiagStep(3, "Load test batteries", "A battery that shows full voltage but drops under load is bad. Load test each battery individually. Replace the weakest — batteries are only as good as the worst one.", tool="Battery load tester"),
            DiagStep(4, "Check solenoid click", "Press accelerator pedal. Listen for solenoid click from under the seat. No click = micro switch, solenoid, or controller issue.", expected="Audible click from solenoid"),
            DiagStep(5, "Check forward/reverse switch", "Toggle F/R switch. Check for loose/corroded contacts. This is a common failure point.", tool="Multimeter"),
            DiagStep(6, "Check throttle pot / ITS", "Measure throttle sensor voltage. Should sweep smoothly from min to max as pedal is pressed. Jumpy or dead spots = replace throttle sensor.", tool="Multimeter"),
        ],
    ),

    # ── FORKLIFT — FAULT CODES ──
    DiagProcedure(
        id="forklift_fault",
        title="Forklift Fault Code / Performance Issue",
        applies_to=[VehicleType.FORKLIFT],
        symptoms=["Error code on display","Reduced speed","Won't lift","Turtle mode","Beeping"],
        related_dtcs=[],
        difficulty="intermediate",
        time_est="30–60 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Hydraulic pressure gauge"],
        safety_warnings=["Never work under raised forks without proper blocking","Disconnect battery before electrical work","Forklifts can tip — never exceed load capacity"],
        steps=[
            DiagStep(1, "Read fault codes from controller", "K.A.S.H. → Forklift → Read fault codes from traction and hydraulic controllers. Note error number and description.", tool="K.A.S.H."),
            DiagStep(2, "Check battery voltage/SOC", "Electric: Check total pack voltage and individual cell voltages. IC: Check 12V battery and charging system.", tool="Multimeter"),
            DiagStep(3, "Check hydraulic fluid level", "Low hydraulic fluid causes lifting issues, erratic mast operation, and pump cavitation. Check level in reservoir.", expected="Fluid at proper level, clean and correct type"),
            DiagStep(4, "Check for overheating", "Motor controllers derate when hot. Check for blocked ventilation, debris in controller compartment, failed cooling fan.", tool="IR thermometer"),
            DiagStep(5, "Check hour meter", "Many faults are maintenance-related. Check if PM service is overdue (oil change, filter, brake adjustment, chain lubrication).", tool="Display"),
            DiagStep(6, "Clear codes and test", "After repair: K.A.S.H. → Clear Faults → Perform operational test: forward, reverse, lift, tilt, side shift. Monitor for code return.", tool="K.A.S.H."),
        ],
    ),

    # ── CONSTRUCTION — ENGINE DERATE ──
    DiagProcedure(
        id="construction_derate",
        title="Construction Equipment Engine Derate / Limp Mode",
        applies_to=[VehicleType.CONSTRUCTION],
        symptoms=["Engine derate","Reduced power","Limp mode","Check engine light","Flashing warning on display"],
        related_dtcs=[],
        difficulty="advanced",
        time_est="60–180 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","IR thermometer","Fuel sample jar"],
        safety_warnings=["Always engage parking brake and lower implements before diagnosis","Construction equipment operates at higher pressures — hydraulic injection injuries are medical emergencies"],
        steps=[
            DiagStep(1, "Scan engine ECM", "K.A.S.H. → J1939 → Scan Engine ECM. Read active and logged faults (SPN/FMI). Most derates are aftertreatment or sensor related.", tool="K.A.S.H."),
            DiagStep(2, "Check aftertreatment system", "Read DPF soot load, DEF level, SCR efficiency, exhaust temps. Same diagnostic flow as heavy-duty trucks — see HD Aftertreatment procedure.", tool="K.A.S.H."),
            DiagStep(3, "Check fuel quality", "Take fuel sample. Look for water contamination (water settles to bottom), algae growth, dark color. Contaminated fuel causes injector and filter issues.", tool="Fuel sample jar"),
            DiagStep(4, "Check air filtration", "Inspect air filter restriction indicator. Plugged air filter causes low power and can set MAF/MAP codes. Replace if restricted.", expected="Filter indicator in green zone"),
            DiagStep(5, "Check coolant system", "Verify coolant level, condition, and concentration. Low coolant can cause derate. Check for coolant in oil (head gasket).", tool="Refractometer"),
            DiagStep(6, "Check for software update", "Many OEM derates are resolved by ECM calibration updates. K.A.S.H. → Module Info → Check calibration version against latest TSB.", tool="K.A.S.H."),
        ],
    ),

    # ── AG — ISOBUS IMPLEMENT ──
    DiagProcedure(
        id="ag_isobus",
        title="ISOBUS Implement Communication / Operation Fault",
        applies_to=[VehicleType.AGRICULTURE],
        symptoms=["Implement not communicating","Section control not working","Rate control fault","Display not showing implement","ISOBUS error"],
        related_dtcs=[],
        difficulty="intermediate",
        time_est="20–60 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","ISOBUS cable (M12)"],
        steps=[
            DiagStep(1, "Check physical connection", "Inspect ISOBUS cable (M12 9-pin connector) for bent pins, corrosion, moisture. Check that connector is fully seated and locked.", expected="Clean, undamaged M12 connectors"),
            DiagStep(2, "Scan ISOBUS network", "K.A.S.H. → ISOBUS → Network Scan. List all devices on the bus. Verify tractor ECU, virtual terminal, and implement are all visible.", tool="K.A.S.H."),
            DiagStep(3, "Check CAN bus termination", "ISOBUS requires exactly 2 termination resistors (120Ω each, 60Ω total across CAN_H/CAN_L). Measure at tractor connector with implement disconnected, then with connected.", tool="Multimeter", expected="60Ω across CAN_H and CAN_L"),
            DiagStep(4, "Check implement power", "ISOBUS provides 12V power to implement ECU through the cable. Verify voltage at implement connector (should be battery voltage, 12–14V).", tool="Multimeter", expected="12–14V"),
            DiagStep(5, "Update implement software", "Some ISOBUS compatibility issues are firmware-related. Check implement manufacturer for latest firmware. K.A.S.H. can read current firmware version.", tool="K.A.S.H."),
            DiagStep(6, "Test with different implement/tractor", "If possible, connect implement to a different tractor (or different implement to this tractor) to isolate whether the issue is tractor-side or implement-side."),
        ],
    ),

    # ── SNOWMOBILE — EFI FAULT ──
    DiagProcedure(
        id="sled_efi",
        title="Snowmobile EFI / Engine Fault",
        applies_to=[VehicleType.SNOWMOBILE],
        symptoms=["Engine light on","EFI fault","Poor running","Won't rev","Backfiring","Hard start in cold"],
        related_dtcs=[],
        difficulty="intermediate",
        time_est="20–60 min",
        tools_needed=["K.A.S.H. scanner","Multimeter","Spark tester"],
        steps=[
            DiagStep(1, "Scan ECU", "K.A.S.H. → Snowmobile → BUDS/Polaris/Arctic Cat → Scan ECU. Common codes: TPS, CKPS, ECTS, fuel injector, O2 sensor.", tool="K.A.S.H."),
            DiagStep(2, "Check battery voltage", "Cold weather kills batteries. Minimum 12.2V for reliable EFI operation. Charge or replace if low.", tool="Multimeter", expected="12.4V+"),
            DiagStep(3, "Check fuel system", "Drain old fuel if sled sat all summer. Ethanol fuel causes gumming in 60 days. Fresh premium non-ethanol fuel recommended.", expected="Fresh, clean fuel"),
            DiagStep(4, "Check exhaust valves (2-stroke)", "2-stroke E-TEC/Rotax: exhaust power valves can stick with carbon buildup. Clean or replace. Check actuator motor operation.", tool="Visual inspection"),
            DiagStep(5, "Check throttle position sensor", "TPS calibration drifts. K.A.S.H. → Live Data → TPS. Should read 0% at idle, 100% at WOT. Recalibrate if off.", tool="K.A.S.H."),
            DiagStep(6, "Check for icing", "In extreme cold, throttle body and intake can ice up. Check for moisture in airbox. Ensure drain tubes are clear.", tool="Visual inspection"),
        ],
    ),
]


# ═══════════════════════════════════════════════════════════════════
#  SECTION 6 — MODULE INITIALIZATION PROCEDURES
# ═══════════════════════════════════════════════════════════════════

MODULE_INIT_PROCEDURES = {
    # ── Automotive ──
    "abs_bleed": {
        "title": "ABS Module Bleed",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        "when": "After ABS module replacement, opening hydraulic lines, spongy brake pedal after service",
        "steps": [
            "Enter ABS module diagnostic session (K.A.S.H. → UDS → Extended Session)",
            "Command ABS solenoids open (left front → right front → left rear → right rear)",
            "Apply firm brake pressure to flush fluid through solenoids",
            "Release solenoids",
            "Repeat for all 4 channels",
            "Verify pedal is firm — no sponge",
            "Clear ABS codes",
        ],
    },
    "tccm_init": {
        "title": "Transfer Case Control Module Initialization",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        "when": "After TCCM replacement, encoder motor replacement, transfer case service, 4WD shift issues",
        "steps": [
            "Key ON, engine OFF",
            "K.A.S.H. → Module Init → TCCM",
            "Select vehicle make/model",
            "Command encoder motor through full range (2HI → 4HI → 4LO → back)",
            "System learns encoder motor range and contact plate positions",
            "Verify 4WD operation: 2HI → 4HI → 4LO → 2HI",
            "Clear TCCM codes",
        ],
    },
    "throttle_body_relearn": {
        "title": "Throttle Body / Idle Relearn",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        "when": "After throttle body cleaning/replacement, battery disconnect, PCM reflash, rough/high/low idle",
        "steps": [
            "Ensure engine is at operating temperature (>160°F)",
            "Turn off all accessories (A/C, lights, radio)",
            "K.A.S.H. → Module Init → Throttle Body Relearn",
            "Follow on-screen prompts (typically: key on 10s, key off 10s, start and idle 3 min)",
            "Do NOT touch accelerator during procedure",
            "Idle should stabilize within 1–2 minutes",
            "Clear codes if needed",
        ],
    },
    "sas_calibration": {
        "title": "Steering Angle Sensor Calibration",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        "when": "After alignment, steering component replacement, ESC/StabiliTrak light on",
        "steps": [
            "Park on level ground, wheels straight ahead",
            "K.A.S.H. → Module Init → Steering Angle Sensor",
            "Turn steering wheel full lock left, then full lock right, then back to center",
            "System calibrates center position and full travel",
            "Clear ESC/StabiliTrak codes",
            "Test drive — verify stability control operates normally",
        ],
    },
    "battery_registration": {
        "title": "Battery Registration / Reset (BMW/Mercedes/VW/Audi)",
        "vehicle_types": [VehicleType.CAR],
        "when": "After battery replacement on European vehicles with IBS (Intelligent Battery Sensor)",
        "steps": [
            "Install new battery (correct type, capacity, and technology — AGM/EFB/Lead-Acid)",
            "K.A.S.H. → Module Init → Battery Registration",
            "Enter new battery specifications (Ah capacity, technology type, manufacturer)",
            "System resets charge strategy and alternator management",
            "Required on: BMW (all 2002+), Mercedes (all 2005+), VW/Audi (most 2010+), Volvo, some Ford/GM",
        ],
    },
    "dpf_forced_regen": {
        "title": "Forced DPF Regeneration",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK, VehicleType.HEAVY_TRUCK, VehicleType.CONSTRUCTION, VehicleType.AGRICULTURE],
        "when": "DPF soot load >85%, DPF warning light, reduced power due to soot accumulation",
        "safety": "Park on concrete/gravel (not grass). Exhaust temps will exceed 1100°F. Keep area clear.",
        "steps": [
            "Park vehicle, engage parking brake, ensure adequate fuel and DEF",
            "K.A.S.H. → Module Init → DPF Forced Regen",
            "System verifies prerequisites (coolant temp, no active faults blocking regen, DPF temp sensors OK)",
            "Regen starts — engine RPM will increase, exhaust temps rise to 550–650°C",
            "Monitor soot load % — should decrease during regen",
            "Regen takes 20–45 minutes (longer if very high soot)",
            "Do NOT shut engine off during regen unless emergency",
            "Verify soot load returned to near 0% after completion",
        ],
    },
    "trans_relearn": {
        "title": "Transmission Adaptation / Shift Relearn",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        "when": "After trans fluid change, valve body work, TCM replacement, harsh shifting, battery disconnect",
        "steps": [
            "K.A.S.H. → Module Init → TCM → Reset Shift Adaptations",
            "Adaptations cleared — transmission will re-learn shift points and pressures",
            "Drive cycle required: city driving with varied throttle positions for 30+ minutes",
            "Include: slow acceleration, moderate acceleration, highway cruising, downshifts, stops",
            "Shifts will be firm at first, then progressively smooth out as adaptations are learned",
        ],
    },
    "tpms_relearn": {
        "title": "TPMS Sensor Relearn",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK],
        "when": "After tire rotation, TPMS sensor replacement, wheel swap",
        "steps": [
            "Set all tires to correct pressure (placard on driver door jamb)",
            "K.A.S.H. → Module Init → TPMS Relearn",
            "Select relearn method: Auto (drive 10+ min) or Manual (activate each sensor with tool)",
            "For manual: follow LF → RF → RR → LR order (most vehicles)",
            "Activate each sensor — K.A.S.H. will confirm sensor ID registered",
            "TPMS light should turn off after successful relearn",
        ],
    },
    "oil_life_reset": {
        "title": "Oil Life / Service Interval Reset",
        "vehicle_types": [VehicleType.CAR, VehicleType.LIGHT_TRUCK, VehicleType.HEAVY_TRUCK],
        "when": "After oil change / scheduled service",
        "steps": [
            "Confirm oil change is complete (correct oil type and quantity, new filter)",
            "K.A.S.H. → Module Init → Oil Life Reset",
            "Select vehicle make/model",
            "Reset oil life to 100%",
            "Verify on instrument cluster — 'Change Oil' message cleared",
        ],
    },

    # ── Motorcycle-specific ──
    "moto_tps_calibrate": {
        "title": "Motorcycle TPS Calibration",
        "vehicle_types": [VehicleType.MOTORCYCLE],
        "when": "After throttle body sync, TPS replacement, idle issues, ECU reset",
        "steps": [
            "Warm engine to operating temperature, then shut off",
            "K.A.S.H. → Moto → Module Init → TPS Calibrate",
            "With throttle FULLY CLOSED — record baseline",
            "With throttle FULLY OPEN — record maximum",
            "System stores new TPS range",
            "Start engine — idle should stabilize at spec RPM",
        ],
    },
    "moto_tip_over_reset": {
        "title": "Motorcycle Tip-Over Sensor Reset",
        "vehicle_types": [VehicleType.MOTORCYCLE],
        "when": "After a drop/tip-over, bike won't restart, tip-over fault code",
        "steps": [
            "Ensure bike is upright on level ground",
            "Turn key OFF, wait 10 seconds",
            "Turn key ON — if engine light stays on, clear code with K.A.S.H.",
            "K.A.S.H. → Moto → Module Init → Reset Tip-Over Sensor",
            "Start engine — verify normal operation",
        ],
    },

    # ── Marine-specific ──
    "marine_throttle_cal": {
        "title": "Marine Throttle / Shift Calibration",
        "vehicle_types": [VehicleType.MARINE],
        "when": "After remote control replacement, throttle cable adjustment, electronic throttle issues, new helm install",
        "steps": [
            "Engine OFF, in neutral",
            "K.A.S.H. → Marine → Module Init → Throttle/Shift Calibration",
            "Move throttle from idle to full, then back to idle",
            "Move shift from neutral to forward, then reverse, then neutral",
            "System records min/max positions for throttle and shift",
            "Start engine in neutral — verify idle RPM and smooth throttle response",
        ],
    },

    # ── Heavy-Duty specific ──
    "hd_injector_cutout": {
        "title": "Cylinder Cutout Test (Heavy-Duty Diesel)",
        "vehicle_types": [VehicleType.HEAVY_TRUCK, VehicleType.CONSTRUCTION, VehicleType.AGRICULTURE],
        "when": "Rough running, misfire, power loss on diesel engine — identify weak cylinder",
        "steps": [
            "Engine at operating temp, running at idle or low RPM",
            "K.A.S.H. → J1939 → Module Init → Cylinder Cutout Test",
            "System disables one injector at a time, measures RPM drop",
            "Large RPM drop = that cylinder is contributing (good)",
            "Little or no RPM drop = that cylinder is weak (bad — check injector, compression, valve)",
            "Review results — K.A.S.H. displays RPM drop per cylinder as a bar chart",
        ],
    },

    # ── EV-specific ──
    "ev_hv_interlock_check": {
        "title": "EV High-Voltage Interlock Check",
        "vehicle_types": [VehicleType.ELECTRIC_VEHICLE],
        "when": "Before any HV service, after HV component replacement, interlock fault code",
        "safety": "Class 0 insulated gloves required. Follow manufacturer's HV disable procedure.",
        "steps": [
            "K.A.S.H. → EV → Module Init → HV Interlock Test",
            "System checks all HV interlock circuits in sequence",
            "All connectors and service disconnects must be properly seated",
            "Any open interlock will be reported with location",
            "After service: reconnect all interlocks, run test again to verify all circuits closed",
            "System must show all interlocks PASS before HV system is re-energized",
        ],
    },

    # ── Golf Cart specific ──
    "cart_controller_reset": {
        "title": "Golf Cart Controller Reset / Program",
        "vehicle_types": [VehicleType.GOLF_CART],
        "when": "After controller replacement, motor swap, speed limit change, flash code won't clear",
        "steps": [
            "Turn key OFF, disconnect battery for 60 seconds",
            "Reconnect battery",
            "K.A.S.H. → Golf Cart → Module Init → Controller Reset",
            "Enter cart specifications (motor type, battery voltage, desired speed limit)",
            "System programs controller parameters",
            "Key ON — verify no flash codes on controller LED",
            "Test: forward, reverse, acceleration, braking",
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════
#  SECTION 7 — READINESS MONITORS (Emissions Inspection)
# ═══════════════════════════════════════════════════════════════════

OBD2_READINESS_MONITORS = {
    "continuous": {
        "misfire": {"name":"Misfire Monitor","desc":"Detects engine misfires in real-time. Always running when engine is on."},
        "fuel_system": {"name":"Fuel System Monitor","desc":"Monitors fuel trim corrections. Always running."},
        "comprehensive": {"name":"Comprehensive Component Monitor","desc":"Checks sensor rationality. Always running."},
    },
    "non_continuous_spark": {
        "catalyst": {"name":"Catalyst (CAT) Monitor","desc":"Compares upstream/downstream O2 sensors to check catalytic converter efficiency.","drive_cycle":"Steady 55 mph for 3–5 min after full warm-up"},
        "heated_catalyst": {"name":"Heated Catalyst Monitor","desc":"Checks heated catalyst system (less common)."},
        "evap": {"name":"Evaporative System (EVAP) Monitor","desc":"Checks fuel vapor system for leaks. Requires specific conditions.","drive_cycle":"Cold start, 1/4–3/4 tank fuel, ambient 40–95°F, steady cruise"},
        "secondary_air": {"name":"Secondary Air Injection Monitor","desc":"Checks secondary air injection system (if equipped)."},
        "oxygen_sensor": {"name":"Oxygen Sensor Monitor","desc":"Tests O2 sensor response time and heater circuit.","drive_cycle":"Varied throttle, cruise, decel"},
        "oxygen_sensor_heater": {"name":"O2 Sensor Heater Monitor","desc":"Tests O2 sensor heater operation.","drive_cycle":"Cold start, first 2 min of idle"},
        "egr": {"name":"EGR System Monitor","desc":"Checks EGR valve operation and flow.","drive_cycle":"Decel from cruise, then steady cruise"},
    },
    "non_continuous_diesel": {
        "catalyst": {"name":"NMHC Catalyst Monitor","desc":"Non-methane hydrocarbon catalyst (diesel)."},
        "nox_aftertreatment": {"name":"NOx/SCR Aftertreatment Monitor","desc":"Checks SCR catalyst and DEF dosing efficiency."},
        "pm_filter": {"name":"PM Filter (DPF) Monitor","desc":"Checks diesel particulate filter efficiency."},
        "egr_vvt": {"name":"EGR / VVT Monitor","desc":"Checks EGR and variable valve timing (diesel)."},
        "boost_pressure": {"name":"Boost Pressure Monitor","desc":"Checks turbo boost pressure control."},
        "exhaust_gas_sensor": {"name":"Exhaust Gas Sensor Monitor","desc":"Checks NOx/PM sensors."},
    },
}


# ═══════════════════════════════════════════════════════════════════
#  SECTION 8 — PROTOCOL INTERFACE CLASSES
# ═══════════════════════════════════════════════════════════════════

class DiagnosticInterface:
    """
    Base class for all diagnostic protocol interfaces.
    Each vehicle type/protocol implements its own subclass.
    """

    def __init__(self, protocol: Protocol, can_bus=None, serial_port=None):
        self.protocol = protocol
        self.can = can_bus
        self.serial = serial_port
        self._connected = False

    def connect(self) -> bool:
        """Establish connection to vehicle ECU."""
        raise NotImplementedError

    def disconnect(self):
        """Close connection."""
        self._connected = False

    def scan_dtcs(self) -> List[Dict]:
        """Read all diagnostic trouble codes."""
        raise NotImplementedError

    def clear_dtcs(self) -> bool:
        """Clear all diagnostic trouble codes."""
        raise NotImplementedError

    def read_live_data(self, pids: List[int]) -> Dict[str, Any]:
        """Read real-time sensor data."""
        raise NotImplementedError

    def read_freeze_frame(self) -> Optional[Dict]:
        """Read freeze frame data captured when DTC was set."""
        raise NotImplementedError

    def read_readiness(self) -> Dict[str, bool]:
        """Read I/M readiness monitor status."""
        raise NotImplementedError

    def read_vin(self) -> Optional[str]:
        """Read Vehicle Identification Number."""
        raise NotImplementedError

    def read_module_info(self) -> Dict[str, str]:
        """Read ECU hardware/software/calibration IDs."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        return self._connected


class OBD2DiagInterface(DiagnosticInterface):
    """
    OBD-II diagnostic interface (ISO 15765-4 CAN, ISO 9141, KWP2000, J1850).
    Works with ANY 1996+ car/light truck sold in the US.
    """

    OBD2_REQUEST_ID  = 0x7DF  # broadcast
    OBD2_RESPONSE_BASE = 0x7E8

    def __init__(self, can_bus, protocol=Protocol.OBD2_CAN_500):
        super().__init__(protocol, can_bus=can_bus)

    def connect(self) -> bool:
        if not self.can or not self.can.is_connected():
            return False
        # Attempt to read supported PIDs (Mode 01 PID 00)
        resp = self._request(0x01, 0x00)
        self._connected = resp is not None
        return self._connected

    def scan_dtcs(self) -> List[Dict]:
        """Mode 03 — Read stored DTCs."""
        dtcs = []
        resp = self._raw_request(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]))
        if resp and len(resp) >= 2:
            num_dtcs = resp[0]
            for i in range(num_dtcs):
                offset = 1 + (i * 2)
                if offset + 1 < len(resp):
                    byte1, byte2 = resp[offset], resp[offset + 1]
                    dtc_str = self._decode_dtc(byte1, byte2)
                    info = GENERIC_DTCS.get(dtc_str, {"desc": "Unknown DTC", "system": "Unknown", "severity": "unknown"})
                    dtcs.append({
                        "code": dtc_str,
                        "desc": info["desc"],
                        "system": info.get("system", "Unknown"),
                        "severity": info.get("severity", "unknown"),
                        "status": "stored",
                    })
        return dtcs

    def clear_dtcs(self) -> bool:
        """Mode 04 — Clear DTCs and freeze frame."""
        resp = self._raw_request(bytes([0x01, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]))
        return resp is not None

    def read_readiness(self) -> Dict[str, bool]:
        """Mode 01 PID 01 — Readiness monitor status."""
        resp = self._request(0x01, 0x01)
        if resp is None or len(resp) < 4:
            return {}
        # Byte A = MIL status + DTC count
        mil_on = bool(resp[0] & 0x80)
        dtc_count = resp[0] & 0x7F
        # Byte B = Supported monitors
        # Byte C/D = Monitor complete status
        monitors = {
            "mil_on": mil_on,
            "dtc_count": dtc_count,
            "misfire": {"supported": bool(resp[1] & 0x01), "complete": not bool(resp[1] & 0x10)},
            "fuel_system": {"supported": bool(resp[1] & 0x02), "complete": not bool(resp[1] & 0x20)},
            "comprehensive": {"supported": bool(resp[1] & 0x04), "complete": not bool(resp[1] & 0x40)},
        }
        return monitors

    def read_vin(self) -> Optional[str]:
        """Mode 09 PID 02 — Vehicle Identification Number."""
        resp = self._request(0x09, 0x02)
        if resp and len(resp) >= 17:
            try:
                return bytes(resp[:17]).decode("ascii")
            except (UnicodeDecodeError, ValueError):
                return None
        return None

    def read_freeze_frame(self) -> Optional[Dict]:
        """Mode 02 — Freeze frame data."""
        result = {}
        # Read common freeze frame PIDs
        ff_pids = [0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x11]
        for pid in ff_pids:
            resp = self._request(0x02, pid)
            if resp:
                result[f"PID_{pid:02X}"] = resp
        return result if result else None

    def _request(self, service: int, pid: int) -> Optional[bytes]:
        """Send OBD-II request and wait for response."""
        data = bytes([0x02, service, pid, 0x00, 0x00, 0x00, 0x00, 0x00])
        return self._raw_request(data)

    def _raw_request(self, data: bytes) -> Optional[bytes]:
        """Low-level CAN request."""
        if not self.can:
            return None
        self.can.send(self.OBD2_REQUEST_ID, data)
        import time
        time.sleep(0.1)
        return None  # Actual response would come via CAN callback

    @staticmethod
    def _decode_dtc(byte1: int, byte2: int) -> str:
        """Decode 2-byte DTC into standard format (P0420, B1234, etc)."""
        prefix_map = {0: "P", 1: "C", 2: "B", 3: "U"}
        prefix = prefix_map.get((byte1 >> 6) & 0x03, "P")
        digit1 = (byte1 >> 4) & 0x03
        digit2 = byte1 & 0x0F
        digit3 = (byte2 >> 4) & 0x0F
        digit4 = byte2 & 0x0F
        return f"{prefix}{digit1}{digit2:X}{digit3:X}{digit4:X}"


class J1939DiagInterface(DiagnosticInterface):
    """
    SAE J1939 diagnostic interface for heavy-duty vehicles.
    CAN 250 kbps, 29-bit extended identifiers.
    """

    def __init__(self, can_bus):
        super().__init__(Protocol.J1939, can_bus=can_bus)
        self.source_addr = 0xF9  # K.A.S.H. tool address

    def connect(self) -> bool:
        if not self.can or not self.can.is_connected():
            return False
        # Send address claim (PGN 60928)
        self._connected = True
        return True

    def scan_dtcs(self) -> List[Dict]:
        """
        Read DTCs via DM1 (PGN 65226) — Active Diagnostic Trouble Codes.
        J1939 DTCs are SPN + FMI format.
        """
        dtcs = []
        # Request DM1 from all ECUs
        # In real implementation, parse DM1 broadcast messages
        return dtcs

    def read_live_data(self, spns: List[int] = None) -> Dict[str, Any]:
        """
        Read live J1939 parameters by SPN.
        Most J1939 data is broadcast — just listen and decode.
        """
        result = {}
        if spns is None:
            spns = [190, 84, 100, 102, 110, 168, 183, 91]  # Common engine SPNs
        for spn in spns:
            if spn in J1939_SPNS:
                info = J1939_SPNS[spn]
                result[info["name"]] = {
                    "spn": spn,
                    "unit": info["unit"],
                    "pgn": info["pgn"],
                    "value": None,  # Populated by live CAN listener
                }
        return result

    def request_dm2(self) -> List[Dict]:
        """Read previously active DTCs (DM2, PGN 65227)."""
        return []

    def request_dm5(self) -> Dict:
        """Read diagnostic readiness (DM5, PGN 65230)."""
        return {}

    def force_dpf_regen(self) -> bool:
        """Command a forced DPF regeneration via DM7 (PGN 58112)."""
        log.info("Commanding forced DPF regen via J1939 DM7")
        # Implementation depends on OEM
        return False

    def cylinder_cutout_test(self, cylinder: int) -> bool:
        """Disable a specific cylinder injector for cutout testing."""
        log.info(f"Cylinder cutout test — cylinder {cylinder}")
        return False


class KLineInterface(DiagnosticInterface):
    """
    K-Line diagnostic interface for motorcycles and older vehicles.
    Supports KWP2000 (ISO 14230) and Kawasaki KDS.
    """

    def __init__(self, serial_port: str = "/dev/ttyAMA0", baud: int = 10400,
                 protocol: Protocol = Protocol.KAWASAKI_KDS, ecu_addr: int = 0x11):
        super().__init__(protocol, serial_port=serial_port)
        self.baud = baud
        self.ecu_addr = ecu_addr
        self._serial = None

    def connect(self) -> bool:
        """5-baud or fast init depending on protocol."""
        try:
            import serial as pyserial
            self._serial = pyserial.Serial(
                self.serial, self.baud,
                bytesize=pyserial.EIGHTBITS,
                parity=pyserial.PARITY_NONE,
                stopbits=pyserial.STOPBITS_ONE,
                timeout=1.0
            )
            if self.protocol in (Protocol.KAWASAKI_KDS, Protocol.HONDA_HDS_MOTO,
                                  Protocol.YAMAHA_YDS, Protocol.SUZUKI_SDS):
                return self._init_kds()
            elif self.protocol == Protocol.OBD2_ISO9141:
                return self._init_iso9141()
            elif self.protocol in (Protocol.OBD2_KWP2000_SLOW, Protocol.OBD2_KWP2000_FAST):
                return self._init_kwp2000()
            return False
        except Exception as e:
            log.error(f"K-Line connect failed: {e}")
            return False

    def _init_kds(self) -> bool:
        """Kawasaki/Honda/Yamaha/Suzuki KDS initialization."""
        # Send ECU address at 5 baud for slow init
        self._send_5baud(self.ecu_addr)
        import time
        time.sleep(0.3)
        # Read keyword bytes
        if self._serial and self._serial.in_waiting >= 2:
            kb = self._serial.read(2)
            log.info(f"KDS init — keyword bytes: {kb.hex()}")
            self._connected = True
            return True
        return False

    def _init_iso9141(self) -> bool:
        """ISO 9141-2 slow init (5 baud, address 0x33)."""
        self._send_5baud(0x33)
        import time
        time.sleep(0.3)
        self._connected = True
        return True

    def _init_kwp2000(self) -> bool:
        """KWP2000 fast init (timed wakeup pattern)."""
        if self._serial:
            # 25ms low, 25ms high wakeup pulse
            import time
            self._serial.break_condition = True
            time.sleep(0.025)
            self._serial.break_condition = False
            time.sleep(0.025)
            # Send StartDiagnosticSession
            self._serial.write(bytes([0xC1, 0x33, 0xF1, 0x81, 0x66]))
            time.sleep(0.3)
            self._connected = True
            return True
        return False

    def _send_5baud(self, addr: int):
        """Transmit address byte at 5 baud (200ms per bit)."""
        import time
        if not self._serial:
            return
        # Bit-bang at 5 baud: start bit (low), 8 data bits, stop bit (high)
        # Each bit = 200ms
        self._serial.break_condition = True  # start bit
        time.sleep(0.2)
        for bit in range(8):
            if addr & (1 << bit):
                self._serial.break_condition = False
            else:
                self._serial.break_condition = True
            time.sleep(0.2)
        self._serial.break_condition = False  # stop bit
        time.sleep(0.2)

    def scan_dtcs(self) -> List[Dict]:
        """Read DTCs via KWP2000 ReadDiagnosticTroubleCodes."""
        if not self._serial or not self._connected:
            return []
        # Send ReadDTC request (service 0x13 for KDS, 0x18 for KWP2000)
        if self.protocol in (Protocol.KAWASAKI_KDS, Protocol.HONDA_HDS_MOTO,
                              Protocol.YAMAHA_YDS, Protocol.SUZUKI_SDS):
            self._serial.write(bytes([0x13]))
        else:
            self._serial.write(bytes([0x18, 0x00, 0xFF, 0x00]))
        import time
        time.sleep(0.3)
        # Parse response
        return []

    def read_live_data(self, pids: List[int] = None) -> Dict[str, Any]:
        """ReadDataByLocalIdentifier (service 0x21)."""
        result = {}
        if not self._serial or not self._connected:
            return result
        if pids is None:
            pids = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06]
        for pid in pids:
            self._serial.write(bytes([0x21, pid]))
            import time
            time.sleep(0.1)
            resp = self._serial.read(self._serial.in_waiting)
            if resp:
                result[f"PID_0x{pid:02X}"] = resp
        return result

    def clear_dtcs(self) -> bool:
        """ClearDiagnosticInformation."""
        if not self._serial or not self._connected:
            return False
        self._serial.write(bytes([0x14, 0xFF, 0xFF, 0xFF]))
        import time
        time.sleep(0.5)
        return True

    def disconnect(self):
        if self._serial:
            self._serial.close()
            self._serial = None
        super().disconnect()


class NMEA2000Interface(DiagnosticInterface):
    """
    NMEA 2000 diagnostic interface for marine vessels.
    CAN 250 kbps, 29-bit identifiers, ISO 11783-based.
    """

    # Common marine PGNs
    PGNS = {
        127488: {"name":"Engine Parameters, Rapid Update","params":["Engine Speed","Engine Tilt/Trim"]},
        127489: {"name":"Engine Parameters, Dynamic","params":["Oil Pressure","Oil Temp","Coolant Temp","Alternator Voltage","Fuel Rate","Total Hours","Coolant Pressure","Fuel Pressure"]},
        127493: {"name":"Transmission Parameters, Dynamic","params":["Trans Gear","Trans Oil Pressure","Trans Oil Temp"]},
        127497: {"name":"Trip Parameters, Engine","params":["Trip Fuel Used","Fuel Rate (avg)","Fuel Economy"]},
        127501: {"name":"Binary Switch Bank Status","params":["Switch Bank Status"]},
        127505: {"name":"Fluid Level","params":["Tank Type","Tank Level","Tank Capacity"]},
        127508: {"name":"Battery Status","params":["Battery Voltage","Battery Current","Battery Temp","Battery Instance"]},
        128259: {"name":"Speed, Water Referenced","params":["Speed Through Water"]},
        128267: {"name":"Water Depth","params":["Depth","Transducer Offset"]},
        129025: {"name":"Position, Rapid Update","params":["Latitude","Longitude"]},
        129026: {"name":"COG & SOG, Rapid Update","params":["Course Over Ground","Speed Over Ground"]},
        129029: {"name":"GNSS Position Data","params":["Latitude","Longitude","Altitude","HDOP","Satellites"]},
        130306: {"name":"Wind Data","params":["Wind Speed","Wind Direction"]},
        130310: {"name":"Environmental Parameters","params":["Water Temp","Outside Ambient Temp","Atmospheric Pressure"]},
        130311: {"name":"Environmental Parameters (extended)","params":["Temp Source","Humidity","Temp"]},
        130312: {"name":"Temperature, Extended Range","params":["Temperature Instance","Temperature Source","Actual Temperature"]},
        65280:  {"name":"Manufacturer Proprietary, Fast Packet","params":["Manufacturer Data"]},
    }

    def __init__(self, can_bus):
        super().__init__(Protocol.NMEA2000, can_bus=can_bus)

    def connect(self) -> bool:
        if not self.can or not self.can.is_connected():
            return False
        self._connected = True
        return True

    def scan_network(self) -> List[Dict]:
        """Discover all devices on NMEA 2000 network via ISO Address Claim (PGN 60928)."""
        devices = []
        # Listen for address claims
        return devices

    def read_engine_data(self) -> Dict:
        """Read engine parameters via PGN 127488 and 127489."""
        return {}

    def read_tank_levels(self) -> List[Dict]:
        """Read fluid levels via PGN 127505 (fuel, water, waste, etc)."""
        return []

    def read_gps(self) -> Optional[Dict]:
        """Read GPS position via PGN 129029."""
        return None


class ISOBUSInterface(DiagnosticInterface):
    """
    ISOBUS (ISO 11783) diagnostic interface for agricultural equipment.
    CAN 250 kbps, 29-bit identifiers.
    """

    def __init__(self, can_bus):
        super().__init__(Protocol.ISOBUS, can_bus=can_bus)

    def connect(self) -> bool:
        if not self.can or not self.can.is_connected():
            return False
        self._connected = True
        return True

    def scan_network(self) -> List[Dict]:
        """Discover ISOBUS devices (tractor ECU, implement ECU, VT, TC)."""
        return []

    def read_implement_data(self) -> Dict:
        """Read implement data (section status, rate, speed, area)."""
        return {}


# ═══════════════════════════════════════════════════════════════════
#  SECTION 9 — UNIVERSAL DIAGNOSTIC ENGINE
# ═══════════════════════════════════════════════════════════════════

class UniversalDiagnosticEngine:
    """
    The main K.A.S.H. diagnostic engine.
    Auto-detects vehicle type and protocol, then provides
    appropriate diagnostic capabilities.
    """

    def __init__(self, can_bus=None, serial_port: str = "/dev/ttyAMA0"):
        self.can = can_bus
        self.serial_port = serial_port
        self.interface: Optional[DiagnosticInterface] = None
        self.vehicle_type: Optional[VehicleType] = None
        self.detected_protocol: Optional[Protocol] = None

    def auto_detect(self) -> Tuple[Optional[VehicleType], Optional[Protocol]]:
        """
        Attempt to detect what we're connected to.
        Tries protocols in order: OBD2 CAN → J1939 → K-Line → NMEA2000.
        """
        log.info("K.A.S.H. Diagnostics — Auto-detecting vehicle...")

        # Try OBD-II CAN (most vehicles 2008+)
        if self.can:
            obd2 = OBD2DiagInterface(self.can)
            if obd2.connect():
                log.info("Detected: OBD-II CAN vehicle")
                self.interface = obd2
                self.vehicle_type = VehicleType.CAR
                self.detected_protocol = Protocol.OBD2_CAN_500
                return self.vehicle_type, self.detected_protocol

            # Try J1939 (heavy-duty)
            j1939 = J1939DiagInterface(self.can)
            if j1939.connect():
                log.info("Detected: J1939 heavy-duty vehicle")
                self.interface = j1939
                self.vehicle_type = VehicleType.HEAVY_TRUCK
                self.detected_protocol = Protocol.J1939
                return self.vehicle_type, self.detected_protocol

            # Try NMEA 2000 (marine)
            nmea = NMEA2000Interface(self.can)
            if nmea.connect():
                log.info("Detected: NMEA 2000 marine vessel")
                self.interface = nmea
                self.vehicle_type = VehicleType.MARINE
                self.detected_protocol = Protocol.NMEA2000
                return self.vehicle_type, self.detected_protocol

        # Try K-Line (motorcycles, older vehicles)
        if self.serial_port:
            for proto, baud, addr in [
                (Protocol.KAWASAKI_KDS, 10400, 0x11),
                (Protocol.HONDA_HDS_MOTO, 10400, 0x01),
                (Protocol.YAMAHA_YDS, 10400, 0x01),
                (Protocol.SUZUKI_SDS, 9600, 0x33),
                (Protocol.OBD2_ISO9141, 10400, 0x33),
                (Protocol.OBD2_KWP2000_FAST, 10400, 0x33),
            ]:
                try:
                    kline = KLineInterface(self.serial_port, baud, proto, addr)
                    if kline.connect():
                        log.info(f"Detected: K-Line vehicle (protocol={proto.name})")
                        self.interface = kline
                        self.vehicle_type = VehicleType.MOTORCYCLE if proto in (
                            Protocol.KAWASAKI_KDS, Protocol.HONDA_HDS_MOTO,
                            Protocol.YAMAHA_YDS, Protocol.SUZUKI_SDS
                        ) else VehicleType.CAR
                        self.detected_protocol = proto
                        return self.vehicle_type, self.detected_protocol
                except Exception:
                    continue

        log.warning("No vehicle detected on any protocol")
        return None, None

    def scan_all(self) -> Dict:
        """Run a complete diagnostic scan."""
        if not self.interface or not self.interface.is_connected:
            return {"error": "Not connected to any vehicle"}

        result = {
            "vehicle_type": self.vehicle_type.name if self.vehicle_type else "Unknown",
            "protocol": self.detected_protocol.name if self.detected_protocol else "Unknown",
            "vin": None,
            "dtcs": [],
            "readiness": {},
            "freeze_frame": None,
        }

        # VIN (OBD2 only)
        if isinstance(self.interface, OBD2DiagInterface):
            result["vin"] = self.interface.read_vin()
            result["readiness"] = self.interface.read_readiness()
            result["freeze_frame"] = self.interface.read_freeze_frame()

        # DTCs
        result["dtcs"] = self.interface.scan_dtcs()

        return result

    def lookup_dtc(self, code: str) -> Optional[Dict]:
        """Look up a DTC in the universal database."""
        code = code.upper().strip()
        if code in GENERIC_DTCS:
            return {"code": code, **GENERIC_DTCS[code]}
        return None

    def get_procedures_for_symptom(self, symptom: str) -> List[DiagProcedure]:
        """Find diagnostic procedures matching a symptom description."""
        symptom_lower = symptom.lower()
        matches = []
        for proc in DIAGNOSTIC_PROCEDURES:
            for s in proc.symptoms:
                if any(word in symptom_lower for word in s.lower().split()):
                    if proc not in matches:
                        matches.append(proc)
                    break
        return matches

    def get_procedures_for_dtc(self, code: str) -> List[DiagProcedure]:
        """Find diagnostic procedures related to a specific DTC."""
        code = code.upper().strip()
        matches = []
        for proc in DIAGNOSTIC_PROCEDURES:
            if code in proc.related_dtcs:
                matches.append(proc)
        return matches

    def get_vehicle_info(self, vehicle_type: VehicleType, make: str = None) -> Optional[Dict]:
        """Look up vehicle database entry."""
        db = VEHICLE_DATABASE.get(vehicle_type)
        if not db:
            return None
        if make:
            return db.get(make)
        return db

    def get_init_procedure(self, proc_id: str) -> Optional[Dict]:
        """Look up a module initialization procedure."""
        return MODULE_INIT_PROCEDURES.get(proc_id)

    def disconnect(self):
        """Disconnect from vehicle."""
        if self.interface:
            self.interface.disconnect()
            self.interface = None
        self.vehicle_type = None
        self.detected_protocol = None


FRAME_PATTERNS = {
    "J1939": re.compile(r"\bJ1939\b|\bPGN\b|\bSPN\b", re.IGNORECASE),
    "KAWASAKI_KDS": re.compile(r"\bKDS\b|\bKAWASAKI\b", re.IGNORECASE),
    "ISO14230_KWP": re.compile(r"\bISO\b|\bKWP\b|\b14230\b", re.IGNORECASE),
}
KV_METRIC_RE = re.compile(r"([A-Za-z][A-Za-z0-9_\-/ ]{0,31})\s*[:=]\s*(-?\d+(?:\.\d+)?)")


class HardwareSupervisor(threading.Thread):
    def __init__(self, port: str, baud_rate: int, reconnect_interval: float) -> None:
        super().__init__(name="kash.hardware", daemon=True)
        self.port = port
        self.baud_rate = baud_rate
        self.reconnect_interval = reconnect_interval
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._last_frame_at = 0.0
        self._reconnect_count = 0
        self._latest_frame: Dict[str, Any] = {
            "status": "NOT_CONNECTED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw_frame": "",
            "frame_type": "UNKNOWN",
            "hardware_state": HARDWARE_NOT_CONNECTED,
            "parsed_metrics": {},
            "gpio_port": self.port,
            "baud_rate": self.baud_rate,
            "reconnect_count": self._reconnect_count,
        }

    def stop(self) -> None:
        self._stop_event.set()
        self._close_serial()

    def _set_latest(self, **updates: Any) -> None:
        with self._lock:
            self._latest_frame.update(updates)
            self._latest_frame["gpio_port"] = self.port
            self._latest_frame["baud_rate"] = self.baud_rate
            self._latest_frame["reconnect_count"] = self._reconnect_count

    def _open_serial(self) -> None:
        service_log.info("Opening hardware bridge on %s @ %s baud", self.port, self.baud_rate)
        self._serial = serial.Serial(
            self.port,
            self.baud_rate,
            timeout=1.0,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        self._last_frame_at = time.monotonic()
        self._set_latest(status="KASH READY", timestamp=datetime.now(timezone.utc).isoformat(), hardware_state=HARDWARE_CONNECTED)
        service_log.info("Hardware bridge connected on %s", self.port)

    def _close_serial(self) -> None:
        serial_conn = self._serial
        self._serial = None
        if serial_conn is not None:
            with suppress(Exception):
                if serial_conn.is_open:
                    serial_conn.close()
                    service_log.info("Hardware bridge closed on %s", self.port)

    def _mark_not_connected(self, exc: BaseException) -> None:
        self._set_latest(
            status="NOT_CONNECTED",
            timestamp=datetime.now(timezone.utc).isoformat(),
            raw_frame="",
            frame_type="UNKNOWN",
            hardware_state=HARDWARE_NOT_CONNECTED,
            parsed_metrics={},
        )
        service_log.error("Hardware supervisor exception: %s", exc, exc_info=True)

    @staticmethod
    def _detect_frame_type(frame: str) -> str:
        frame_upper = frame.strip().upper()
        for frame_type, pattern in FRAME_PATTERNS.items():
            if pattern.search(frame_upper):
                return frame_type
        tokens = re.findall(r"[0-9A-F]+", frame_upper)
        if tokens:
            token = tokens[0]
            if len(token) in {3, 4}:
                return "CAN_11bit"
            if len(token) >= 8:
                return "CAN_29bit"
        if frame_upper.startswith("29"):
            return "CAN_29bit"
        if frame_upper.startswith("11"):
            return "CAN_11bit"
        return "UNKNOWN"

    @staticmethod
    def _parse_metrics(frame: str) -> Dict[str, float]:
        metrics: Dict[str, float] = {}
        for key, value in KV_METRIC_RE.findall(frame):
            normalized = key.strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
            with suppress(ValueError):
                metrics[normalized] = float(value)
        return metrics

    def get_latest_frame(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._latest_frame)

    def run(self) -> None:  # pragma: no cover
        while not self._stop_event.is_set():
            try:
                if self._serial is None or not self._serial.is_open:
                    self._open_serial()
                assert self._serial is not None
                payload = self._serial.readline()
                if payload:
                    raw_frame = payload.decode("utf-8", errors="ignore").strip()
                    if raw_frame:
                        self._last_frame_at = time.monotonic()
                        self._set_latest(
                            status="KASH READY",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            raw_frame=raw_frame,
                            frame_type=self._detect_frame_type(raw_frame),
                            hardware_state=HARDWARE_CONNECTED,
                            parsed_metrics=self._parse_metrics(raw_frame),
                        )
                        continue
                elapsed = time.monotonic() - self._last_frame_at
                if elapsed >= self.reconnect_interval:
                    raise TimeoutError(f"No serial data received for {elapsed:.1f}s on {self.port}")
            except Exception as exc:  # noqa: BLE001
                self._reconnect_count += 1
                self._close_serial()
                self._mark_not_connected(exc)
                self._stop_event.wait(self.reconnect_interval)


class LiveFeedBroker:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Dict[str, Any]]] = set()

    async def subscribe(self) -> asyncio.Queue[Dict[str, Any]]:
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=4)
        self._queues.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        self._queues.discard(queue)

    def publish(self, payload: Dict[str, Any]) -> None:
        for queue in list(self._queues):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def pump(self, supervisor: HardwareSupervisor) -> None:
        while True:
            self.publish(supervisor.get_latest_frame())
            await asyncio.sleep(0.1)


class NullCANBus:
    def is_connected(self) -> bool:
        return False

    def send(self, arbitration_id: int, data: bytes) -> None:
        raise RuntimeError("CAN bus unavailable")


def create_can_bus() -> Optional[Any]:
    if can is None:
        return None
    try:
        detect = getattr(can, "detect_available_configs", None)
        configs = detect() if callable(detect) else []
        if configs:
            config = configs[0]
            interface = config.get("interface") or config.get("bustype")
            channel = config.get("channel")
            return can.Bus(interface=interface, channel=channel)
    except Exception as exc:  # noqa: BLE001
        service_log.warning("CAN auto-discovery failed: %s", exc)
    return None


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {str(to_jsonable(key)): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "name") and hasattr(value, "value"):
        return value.name
    return value


VEHICLE_TYPE_ALIASES = {
    "car": VehicleType.CAR,
    "light_truck": VehicleType.LIGHT_TRUCK,
    "truck": VehicleType.HEAVY_TRUCK,
    "heavy_truck": VehicleType.HEAVY_TRUCK,
    "motorcycle": VehicleType.MOTORCYCLE,
    "moto": VehicleType.MOTORCYCLE,
    "atv": VehicleType.ATV,
    "utv": VehicleType.UTV_SXS,
    "marine": VehicleType.MARINE,
    "ag": VehicleType.AGRICULTURE,
    "agriculture": VehicleType.AGRICULTURE,
    "const": VehicleType.CONSTRUCTION,
    "construction": VehicleType.CONSTRUCTION,
    "snow": VehicleType.SNOWMOBILE,
    "snowmobile": VehicleType.SNOWMOBILE,
    "golf": VehicleType.GOLF_CART,
    "golf_cart": VehicleType.GOLF_CART,
    "fork": VehicleType.FORKLIFT,
    "forklift": VehicleType.FORKLIFT,
    "lawn": VehicleType.LAWN_MOWER,
    "lawn_mower": VehicleType.LAWN_MOWER,
    "ev": VehicleType.ELECTRIC_VEHICLE,
    "electric_vehicle": VehicleType.ELECTRIC_VEHICLE,
    "rv": VehicleType.RV_MOTORHOME,
    "rv_motorhome": VehicleType.RV_MOTORHOME,
    "trailer": VehicleType.TRAILER,
}


def resolve_vehicle_type(name: str) -> VehicleType:
    normalized = name.strip().lower()
    if normalized in VEHICLE_TYPE_ALIASES:
        return VEHICLE_TYPE_ALIASES[normalized]
    try:
        return VehicleType[normalized.upper()]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown vehicle type: {name}") from exc


class ServiceRuntime:
    def __init__(self) -> None:
        self.supervisor = HardwareSupervisor(KASH_GPIO_PORT, KASH_BAUD_RATE, KASH_RECONNECT_INTERVAL)
        self.broker = LiveFeedBroker()
        self.can_bus = create_can_bus()
        self.engine = UniversalDiagnosticEngine(can_bus=self.can_bus, serial_port=KASH_GPIO_PORT)
        self.scan_cache: Dict[str, Any] = {}
        self.broadcast_task: Optional[asyncio.Task[None]] = None
        self.started_at = START_TIME

    async def start(self) -> None:
        self.supervisor.start()
        self.broadcast_task = asyncio.create_task(self.broker.pump(self.supervisor), name="kash-live-broker")
        service_log.info("Unified K.A.S.H. service started")

    async def stop(self) -> None:
        self.supervisor.stop()
        if self.broadcast_task is not None:
            self.broadcast_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.broadcast_task
        self.engine.disconnect()
        service_log.info("Unified K.A.S.H. service stopped")

    def _connect_from_frame_type(self, frame_type: str) -> bool:
        frame_type = frame_type.upper()
        if frame_type == "J1939":
            interface = J1939DiagInterface(self.can_bus or NullCANBus())
            interface._connected = True
            self.engine.interface = interface
            self.engine.vehicle_type = VehicleType.HEAVY_TRUCK
            self.engine.detected_protocol = Protocol.J1939
            return True
        if frame_type == "CAN_29BIT":
            interface = OBD2DiagInterface(self.can_bus or NullCANBus(), protocol=Protocol.OBD2_CAN_500_29)
            interface._connected = True
            self.engine.interface = interface
            self.engine.vehicle_type = VehicleType.CAR
            self.engine.detected_protocol = Protocol.OBD2_CAN_500_29
            return True
        if frame_type == "CAN_11BIT":
            interface = OBD2DiagInterface(self.can_bus or NullCANBus(), protocol=Protocol.OBD2_CAN_500)
            interface._connected = True
            self.engine.interface = interface
            self.engine.vehicle_type = VehicleType.CAR
            self.engine.detected_protocol = Protocol.OBD2_CAN_500
            return True
        if frame_type == "KAWASAKI_KDS":
            interface = KLineInterface(KASH_GPIO_PORT, 10400, Protocol.KAWASAKI_KDS, 0x11)
            interface._connected = True
            self.engine.interface = interface
            self.engine.vehicle_type = VehicleType.MOTORCYCLE
            self.engine.detected_protocol = Protocol.KAWASAKI_KDS
            return True
        if frame_type == "ISO14230_KWP":
            interface = KLineInterface(KASH_GPIO_PORT, 10400, Protocol.OBD2_KWP2000_FAST, 0x33)
            interface._connected = True
            self.engine.interface = interface
            self.engine.vehicle_type = VehicleType.CAR
            self.engine.detected_protocol = Protocol.OBD2_KWP2000_FAST
            return True
        return False

    def connect(self) -> Dict[str, Any]:
        latest = self.supervisor.get_latest_frame()
        self.engine.disconnect()
        if latest["hardware_state"] == HARDWARE_CONNECTED and self._connect_from_frame_type(latest["frame_type"]):
            service_log.info("Protocol detected from live hardware frame: %s", latest["frame_type"])
            return {
                "success": True,
                "vehicle_type": self.engine.vehicle_type.name if self.engine.vehicle_type else None,
                "protocol": self.engine.detected_protocol.name if self.engine.detected_protocol else None,
                "hardware_state": latest["hardware_state"],
                "frame_type": latest["frame_type"],
            }
        vehicle_type, protocol = self.engine.auto_detect()
        if vehicle_type and protocol:
            service_log.info("Protocol auto-detected: %s / %s", vehicle_type.name, protocol.name)
            return {
                "success": True,
                "vehicle_type": vehicle_type.name,
                "protocol": protocol.name,
                "hardware_state": latest["hardware_state"],
                "frame_type": latest["frame_type"],
            }
        return {
            "success": False,
            "error": "No vehicle detected",
            "hardware_state": latest["hardware_state"],
            "frame_type": latest["frame_type"],
        }

    def get_modules(self) -> Dict[str, str]:
        if not self.engine.vehicle_type:
            return {}
        vehicle_info = self.engine.get_vehicle_info(self.engine.vehicle_type) or {}
        modules: List[str] = []
        if isinstance(vehicle_info, dict):
            for info in vehicle_info.values():
                if isinstance(info, dict) and isinstance(info.get("modules"), list):
                    modules.extend(str(module) for module in info["modules"])
        modules = list(dict.fromkeys(modules))
        return {f"{index + 1:02d}": module for index, module in enumerate(modules[:24])}

    def run_scan(self) -> Dict[str, Any]:
        result = self.engine.scan_all()
        if "error" not in result:
            result["modules"] = self.get_modules()
        self.scan_cache = result
        service_log.info("Diagnostic scan result: %s", json.dumps(to_jsonable(result))[:1000])
        return result


runtime = ServiceRuntime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="K.A.S.H. Diagnostics", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=KASH_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def html_file(name: str) -> Path:
    path = BASE_DIR / name
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"Missing asset: {name}")
    return path


def latest_hardware_frame() -> Dict[str, Any]:
    return runtime.supervisor.get_latest_frame()


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(content=html_file("index.html").read_text(encoding="utf-8"))


@app.get("/styles.css")
def styles() -> Response:
    return Response(content=html_file("styles.css").read_text(encoding="utf-8"), media_type="text/css; charset=utf-8")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": VERSION,
        "uptime_s": round(time.monotonic() - runtime.started_at, 3),
        "hardware_state": latest_hardware_frame()["hardware_state"],
    }


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    latest = latest_hardware_frame()
    return {
        "success": True,
        "connected": bool(runtime.engine.interface and runtime.engine.interface.is_connected),
        "vehicle_type": runtime.engine.vehicle_type.name if runtime.engine.vehicle_type else None,
        "protocol": runtime.engine.detected_protocol.name if runtime.engine.detected_protocol else None,
        "hardware_state": latest["hardware_state"],
        "frame_type": latest["frame_type"],
        "port": KASH_GPIO_PORT,
        "baud_rate": KASH_BAUD_RATE,
        "status": latest["status"],
    }


@app.post("/api/connect")
def api_connect() -> Dict[str, Any]:
    return runtime.connect()


@app.post("/api/disconnect")
def api_disconnect() -> Dict[str, Any]:
    runtime.engine.disconnect()
    return {"success": True, "hardware_state": latest_hardware_frame()["hardware_state"]}


@app.get("/api/scan")
def api_scan() -> Dict[str, Any]:
    result = runtime.run_scan()
    return {"success": "error" not in result, **to_jsonable(result)}


@app.post("/api/clear")
def api_clear() -> Dict[str, Any]:
    interface = runtime.engine.interface
    if not interface or not interface.is_connected:
        return {"success": False, "error": "Not connected to any vehicle"}
    with suppress(Exception):
        if interface.clear_dtcs():
            service_log.info("Diagnostic trouble codes cleared")
            return {"success": True}
    return {"success": False, "error": "Unable to clear DTCs"}


@app.get("/api/vin")
def api_vin() -> Dict[str, Any]:
    interface = runtime.engine.interface
    vin = None
    if interface and hasattr(interface, "read_vin"):
        with suppress(Exception):
            vin = interface.read_vin()
    return {"success": vin is not None, "vin": vin}


@app.get("/api/readiness")
def api_readiness() -> Dict[str, Any]:
    interface = runtime.engine.interface
    if not interface or not interface.is_connected:
        return {"success": False, "error": "Not connected to any vehicle"}
    with suppress(Exception):
        return {"success": True, "data": to_jsonable(interface.read_readiness())}
    return {"success": False, "error": "Readiness data unavailable"}


@app.get("/api/freeze")
def api_freeze() -> Dict[str, Any]:
    interface = runtime.engine.interface
    if not interface or not interface.is_connected:
        return {"success": False, "error": "Not connected to any vehicle"}
    with suppress(Exception):
        data = interface.read_freeze_frame()
        return {"success": bool(data), "data": to_jsonable(data)}
    return {"success": False, "error": "Freeze frame unavailable"}


@app.get("/api/modules")
def api_modules() -> Dict[str, Any]:
    return {"success": True, "modules": runtime.get_modules()}


@app.get("/api/dtc/{code}")
def api_dtc(code: str) -> Dict[str, Any]:
    dtc = runtime.engine.lookup_dtc(code)
    if not dtc:
        raise HTTPException(status_code=404, detail=f"DTC '{code.upper()}' not found")
    return {"success": True, **to_jsonable(dtc)}


@app.get("/api/procedures")
def api_procedures() -> Dict[str, Any]:
    procedures = [to_jsonable(procedure) for procedure in DIAGNOSTIC_PROCEDURES]
    return {"success": True, "count": len(procedures), "procedures": procedures}


@app.get("/api/procedures/symptom")
def api_procedures_symptom(q: str = Query(..., min_length=1)) -> Dict[str, Any]:
    procedures = [to_jsonable(proc) for proc in runtime.engine.get_procedures_for_symptom(q)]
    return {"success": True, "symptom": q, "count": len(procedures), "procedures": procedures}


@app.get("/api/vehicles")
def api_vehicles() -> Dict[str, Any]:
    vehicles = {vehicle_type.name: sorted(list(makes.keys())) for vehicle_type, makes in VEHICLE_DATABASE.items()}
    return {"success": True, "vehicles": vehicles}


@app.get("/api/vehicles/{vehicle_type_name}")
def api_vehicle_detail(vehicle_type_name: str) -> Dict[str, Any]:
    vehicle_type = resolve_vehicle_type(vehicle_type_name)
    return {"success": True, "vehicle_type": vehicle_type.name, "makes": to_jsonable(VEHICLE_DATABASE.get(vehicle_type, {}))}


@app.get("/api/init-procedures")
def api_init_procedures() -> Dict[str, Any]:
    return {
        "success": True,
        "count": len(MODULE_INIT_PROCEDURES),
        "procedures": list(MODULE_INIT_PROCEDURES.keys()),
        "details": to_jsonable(MODULE_INIT_PROCEDURES),
    }


@app.get("/api/bridge/status")
def api_bridge_status() -> Dict[str, Any]:
    latest = latest_hardware_frame()
    return {
        "success": True,
        "service": "K.A.S.H. Unified Bridge",
        "status": latest["status"],
        "hardware_state": latest["hardware_state"],
        "gpio_port": KASH_GPIO_PORT,
        "baud_rate": KASH_BAUD_RATE,
        "reconnect_count": latest["reconnect_count"],
        "frame_type": latest["frame_type"],
    }


@app.get("/api/live")
def api_live() -> Dict[str, Any]:
    return {"success": True, **to_jsonable(latest_hardware_frame())}


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = await runtime.broker.subscribe()
    await websocket.send_json(to_jsonable(latest_hardware_frame()))
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(to_jsonable(payload))
    except WebSocketDisconnect:
        service_log.info("WebSocket client disconnected")
    finally:
        await runtime.broker.unsubscribe(queue)


def print_dtc_lookup(code: str) -> int:
    result = runtime.engine.lookup_dtc(code)
    if not result:
        print(json.dumps({"error": f"DTC '{code.upper()}' not found"}, indent=2))
        return 1
    print(json.dumps(to_jsonable(result), indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="K.A.S.H. Diagnostics unified firmware service")
    parser.add_argument("--coverage", action="store_true", help="Print the full vehicle coverage database")
    parser.add_argument("--dtc", metavar="CODE", help="Look up a diagnostic trouble code")
    args = parser.parse_args(argv)
    if args.coverage:
        print_vehicle_database()
        return 0
    if args.dtc:
        return print_dtc_lookup(args.dtc)
    uvicorn.run(app, host=KASH_HOST, port=KASH_PORT, log_level=KASH_LOG_LEVEL.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
