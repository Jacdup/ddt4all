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
from typing import List, Optional, Pattern, Set

from ddt4all.plugins.system_diag_lib.chain import (
    OFF, ON, ActiveTest, Check, Condition, RoleDef, Signal, in_range, not_matching,
)


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
    # ---- guided check --------------------------------------------------
    #: what the user must do before / during the check
    instructions: str = ""
    #: ECUs taking part in the chain
    roles: List[RoleDef] = field(default_factory=list)
    #: role whose VIN is the car's VIN (body ECU)
    reference_role: Optional[str] = None
    #: ordered checkpoints, see chain.py
    chain: List[Check] = field(default_factory=list)
    #: optional confirmed direct test of the actuator
    active_test: Optional[ActiveTest] = None

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


# --------------------------------------------------------------------------
# Climate / A/C guided check
#
# Signal names: the variants found across the ECU database (most common
# first): 152/198 climate units use OH_ACCompRequest, 222/400 engine ECUs
# "Air conditioning request", 40/236 body ECUs IH_ACREQUESTNA...
# A checkpoint whose signals are not in the car's files shows "not available".
# --------------------------------------------------------------------------
_HVAC, _ENG, _BCM = "hvac", "engine", "bcm"

_CLIMATE_ROLES = [
    RoleDef(_HVAC, "climate unit", ["29"], r"hvac|clim"),
    RoleDef(_ENG, "engine ECU", ["7A"], r"injection|\bECM\b"),
    RoleDef(_BCM, "body ECU (UCH/BCM)", ["26"], r"\bBCM\b|\bUCH\b"),
]

_CLIMATE_CHAIN = [
    Check("engine_running", "Engine running",
          [Condition([Signal(_HVAC, [r"^IH_EngineRPM$", r"^EngineRPM$"]),
                      Signal(_ENG, [r"^Engine speed$", r"^R[ée]gime moteur$"])], in_range(400))],
          "The engine is not running.", "Start the engine.", precondition=True),
    Check("blower_on", "Blower on",
          [Condition([Signal(_HVAC, [r"^OH_BlowerState$", r"^IH_BlowerState$", r"^OH_BlowerReq$"])], ON)],
          "The blower is off.", "Switch the blower on.", precondition=True),

    Check("hvac_request", "Climate unit asks for the compressor",
          [Condition([Signal(_HVAC, [r"^OH_ACCompRequest$", r"^OH_ACCompClutchRequest$",
                                     r"^ACCompClutchRequest$"]),
                      Signal(_BCM, [r"^IH_ACRequest$", r"ClimateCoolingRequest$"])], ON)],
          "The climate unit is not asking for the compressor.",
          "Make sure the A/C button is on (light lit) and the cabin is warm. If it is, the "
          "climate unit is holding the compressor back: see the problems listed below "
          "(evaporator temperature, pressure, its fault codes)."),
    Check("hvac_allows", "Climate unit allows the compressor",
          [Condition([Signal(_HVAC, [r"^OH_ACCompForbidden$", r"^VL_ACATForLineReq$"])], OFF)],
          "The climate unit itself forbids the compressor.",
          "Look at the climate unit's fault codes and its evaporator and pressure values."),
    Check("network", "Request valid on the network",
          [Condition([Signal(_BCM, [r"^IH_ACREQUESTNA$", r"^IH_ACRequestNA$"])], OFF),
           Condition([Signal(_BCM, [r"^CAN_CLIM_PRESENT$"])], ON),
           Condition([Signal(_BCM, [r"^CLIM_CANHS_FRAME_STATUS$"])], ON)],
          "The climate unit's messages are missing or invalid on the network (seen by the body ECU).",
          "Check the climate unit's power, ground and CAN wiring, and its fault codes."),
    Check("engine_receives", "Engine ECU receives the request",
          [Condition([Signal(_ENG, [r"^Air conditioning request$", r"^A/C request$",
                                    r"_A/C request$", r"^Demande clim\.?$"])], ON)],
          "The engine ECU does not receive the A/C request, although the climate unit sends it.",
          "Check that the engine ECU's software and configuration match this car "
          "(A/C configuration, expected CAN messages).", receiver_role=_ENG),
    Check("engine_allows", "Engine ECU allows the compressor",
          [Condition([Signal(_ENG, [r"^Combination of all engine air conditioning compressor inhibitions$"])], OFF),
           Condition([Signal(_ENG, [r"^Combination of all vehicle air conditioning compressor inhibitions$"])], OFF),
           Condition([Signal(_ENG, [r"AC_Compressor_inhibit", r"AC_Inhib_synthesis"])], OFF)],
          "The engine ECU blocks the compressor (inhibition).",
          "Usual reasons: engine too hot, high engine load or speed, low battery voltage, "
          "refrigerant pressure out of range. Check the engine ECU's fault codes.",
          receiver_role=_ENG),
    Check("relay_on", "Engine ECU switches the compressor on",
          [Condition([Signal(_ENG, [r"air condition compressor relay", r"A/C relay",
                                    r"^Air conditioning state(\.value)?$"])], ON)],
          "The engine ECU accepts the request but does not switch the compressor relay on.",
          "Check the engine ECU's fault codes, then use the direct compressor test.",
          receiver_role=_ENG),
    Check("compressor_engaged", "Compressor engaged",
          [Condition([Signal(_HVAC, [r"^IH_ACCompAuthorized$"])], ON),
           Condition([Signal(_HVAC, [r"^IH_ACCompClutchStatus$"])], ON)],
          "The compressor is commanded but not engaged (clutch off or no authorisation returned).",
          "Check the compressor relay, fuse, clutch coil and wiring; use the direct test."),

    # independent checks: evaluated whatever happens above
    Check("pressure", "Refrigerant pressure plausible",
          [Condition([Signal(_HVAC, [r"^IH_ACHighPressureSensor$", r"^OH_ACHighPressureSensor$"]),
                      Signal(_ENG, [r"^Relative air conditioning pressure$"])], in_range(2, 30)),
           Condition([Signal(_ENG, [r"^Test under pressure of the climatisation circuit$"])],
                     not_matching(r"incorrect"))],
          "The refrigerant pressure or charge is wrong (empty or overfilled system, or a faulty "
          "pressure sensor).",
          "Have the refrigerant charge and the pressure sensor checked.", independent=True),
    Check("evap_sensor", "Evaporator temperature plausible",
          [Condition([Signal(_HVAC, [r"^IH_EvapTemp$"])], in_range(-15, 70))],
          "The evaporator temperature reading is implausible (sensor fault).",
          "Check the evaporator temperature sensor and its wiring.", independent=True),
    Check("voltage", "Battery voltage normal",
          [Condition([Signal(_HVAC, [r"^IH_Vbatt$"]), Signal(_ENG, [r"^Battery voltage$"])],
                     in_range(12.0, 15.5))],
          "The battery/charging voltage is outside the normal range; the energy management can "
          "switch the A/C off.", "Check the battery and the alternator.", independent=True),
    Check("coolant", "Engine temperature allows A/C",
          [Condition([Signal(_ENG, [r"^Engine coolant temperature$"])], in_range(-40, 115))],
          "The engine coolant temperature is out of range (engine very hot, or a sensor fault); "
          "the A/C is cut to protect the engine.",
          "Check the engine cooling system (coolant level, fans) and the coolant temperature "
          "sensor.", independent=True),
]

_CLIMATE_TEST = ActiveTest(
    title="Test compressor directly (10 s)",
    role=_ENG,
    start_patterns=[r"Air conditioning state\.START\.V_ON$",
                    r"COMPRESSOR_CLUTCHING_ORDER_START_ON$",
                    r"OutputControlStatus\.\$0124_A/C relay$"],
    exclude=r"compression|STOP|V_OFF|_OFF$",
    duration_s=10,
    watch=[Condition([Signal(_ENG, [r"^Relative air conditioning pressure$"])], in_range(2, 30),
                     "pressure"),
           Condition([Signal(_ENG, [r"air condition compressor relay", r"A/C relay"])], ON, "relay"),
           Condition([Signal(_ENG, [r"^Electric Cooling Fan 1 command", r"^Motor-fan 1 command state$"])],
                     ON, "fan")],
)

CLIMATE_PROFILE = SystemProfile(
    instructions="Start the engine, switch the blower on and press the A/C button. "
                 "Leave it like that during the check.",
    roles=_CLIMATE_ROLES,
    reference_role=_BCM,
    chain=_CLIMATE_CHAIN,
    active_test=_CLIMATE_TEST,
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
