# -*- coding: utf-8 -*-
"""
Functional-system profiles for the system diagnostics plugin.

A profile says which ECUs "own" a system (every read/action of those ECUs is
relevant) and which keywords make a request or parameter of any *other* ECU
relevant (e.g. the A/C request seen by the engine ECU or the BCM/UCH).

To support another system (lighting, cruise control...), add a profile to
``PROFILES``; no other code needs to change.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Pattern, Set


@dataclass
class SystemProfile:
    name: str
    #: diagnostic addresses (hex, upper case) of the ECUs owning the system
    primary_addresses: Set[str] = field(default_factory=set)
    #: matched against the ECU function name / group / file name
    primary_names: Optional[Pattern] = None
    #: case-insensitive keywords for requests / parameters of other ECUs
    keywords: Optional[Pattern] = None
    #: case-sensitive keywords (abbreviations such as "AC" or "PTC")
    keywords_cs: Optional[Pattern] = None
    #: anything matching this is never considered relevant (false positives)
    exclude: Optional[Pattern] = None

    def is_primary_ecu(self, address, *names):
        if address and address.upper() in self.primary_addresses:
            return True
        if self.primary_names is None:
            return False
        return any(n and self.primary_names.search(n) for n in names)

    def matches(self, text):
        if not text:
            return False
        if self.exclude is not None and self.exclude.search(text):
            return False
        if self.keywords is not None and self.keywords.search(text):
            return True
        if self.keywords_cs is not None and self.keywords_cs.search(text):
            return True
        return False


CLIMATE_PROFILE = SystemProfile(
    name="Climate control / A/C (clim)",
    # 0x29 is the HVAC / Climatisation / ClimBox address in the ECU database
    primary_addresses={"29"},
    primary_names=re.compile(r"hvac|clim", re.I),
    keywords=re.compile(
        r"clim"                                  # clim, climatisation, CLIM_TYPE
        r"|air[\s_-]?cond"                       # air conditioning
        r"|compress(?:or|eur)"                   # A/C compressor
        r"|freon|refrig|r134|r1234"              # refrigerant / pressure sensor
        r"|evaporat(?:or|eur)|evap[\s_]?(?:temp|sensor)"
        r"|blower|pulseur"                       # cabin blower
        r"|hvac|condens"
        r"|motor[\s-]?fans?|moto[\s-]?ventil|cooling[\s_-]?fan|coolfan"
        r"|enginefan|fan[\s_]?speed"             # condenser / engine fan
        r"|recycl|recirc|pressostat"
        r"|(?<![a-z])ac_?(?:req|comp|clutch)",  # IH_ACREQUESTNA, IH_ACCompAuthorized
        re.I),
    keywords_cs=re.compile(
        r"(?<![A-Za-z0-9])A/?C(?![A-Za-z0-9])"  # AC, A/C, E_DU_AC_LHM_CF
        r"|PTC|GMV"),                            # PTC heater, motor-fan unit
    exclude=re.compile(
        r"turbo|by-?pass|boost|evaporation|purge|canister|\bCSF\b|EGR|particul|exhaust",
        re.I),
)

PROFILES = [CLIMATE_PROFILE]
