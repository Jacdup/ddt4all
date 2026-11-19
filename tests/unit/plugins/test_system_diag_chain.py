# -*- coding: utf-8 -*-
"""Guided check: value interpretation, chain evaluation, verdicts, and
end-to-end scenarios on fake ECUs (no hardware, no Qt)."""
import json
from types import SimpleNamespace

import pytest

from ddt4all.core.ecu.ecu_file import EcuFile
from ddt4all.core.ecu.ecu_request import EcuRequest
from ddt4all.plugins.system_diag_lib import chain as C
from ddt4all.plugins.system_diag_lib import engine as E
from ddt4all.plugins.system_diag_lib.guided import GuidedRun
from ddt4all.plugins.system_diag_lib.profiles import CLIMATE_PROFILE as P


# ---------------------------------------------------------------- values
@pytest.mark.parametrize("value,expected", [
    ("Air Conditioning Requested", True), ("No Air Conditioning Requested", False),
    ("ACComp authorized", True), ("ACComp not authorized", False),
    ("Clutch ON", True), ("Clutch OFF", False), ("yes", True), ("no", False),
    ("true", True), ("false", False), (" activated", True), ("Deactivated", False),
    ("Not activated", False), ("FRAME_STATUS_PRESENT", True), ("ABSENT_AND_EXPECTED", False),
    ("AC compressor activation forbidden", True), ("AC compressor activation not forbidden", False),
    ("0", False), ("1", True), ("14", True), ("0.0", False), ("with", True), ("without", False),
    ("0d - Not configured", False), ("Engine fan stopped", None), (None, None), ("", None),
])
def test_as_bool(value, expected):
    assert C.as_bool(value) is expected


def test_expectations():
    assert C.in_range(400).test("900.75") is True
    assert C.in_range(400).test("0") is False
    assert C.in_range(2, 30).test("abc") is None
    assert C.OFF.test("no") is True and C.OFF.test("yes") is False
    assert C.not_matching("incorrect").test("test conditions satisfied and correct cooling gas load")
    assert not C.not_matching("incorrect").test("test conditions satisfied and incorrect cooling gas load")


# ----------------------------------------------------------------- chain
def _chain():
    sig = lambda name: C.Signal("r", [name])
    return [
        C.Check("pre", "Engine running", [C.Condition([sig("rpm")], C.in_range(400))],
                "off", "start", precondition=True),
        C.Check("a", "A", [C.Condition([sig("a")], C.ON)], "A failed", "fix A"),
        C.Check("b", "B", [C.Condition([sig("b")], C.ON)], "B failed", "fix B", receiver_role="r"),
        C.Check("c", "C", [C.Condition([sig("c")], C.ON)], "C failed", "fix C"),
        C.Check("x", "X", [C.Condition([sig("x")], C.ON)], "X failed", "fix X", independent=True),
    ]


def _reader(values):
    def read(sig):
        name = sig.patterns[0]
        if name not in values:
            return None
        return ("ECU", name, values[name])
    return read


def test_first_failure_wins_and_later_steps_not_reached():
    res, pre = C.evaluate_chain(_chain(), _reader({"rpm": 900, "a": "yes", "b": "no", "c": "no", "x": "no"}))
    states = {r.check.id: r.state for r in res}
    assert pre and states == {"pre": C.OK, "a": C.OK, "b": C.FAIL, "c": C.NOT_REACHED, "x": C.FAIL}
    v = C.build_verdict(res, pre, [], {"r": "ECU"})
    assert v.headline.startswith("B") and v.level == "fault"
    assert any(f.text == "X failed" for f in v.findings)       # independent failure listed


def test_missing_signal_is_not_a_failure():
    res, pre = C.evaluate_chain(_chain(), _reader({"rpm": 900, "a": "yes", "c": "yes"}))
    states = {r.check.id: r.state for r in res}
    assert states["b"] == C.NA and states["x"] == C.NA
    assert C.build_verdict(res, pre, [], {}).level == "ok"


def test_precondition_waits():
    res, pre = C.evaluate_chain(_chain(), _reader({"rpm": 0, "a": "no"}))
    assert not pre
    v = C.build_verdict(res, pre, [], {}, "Start the engine.")
    assert v.level == "waiting" and "Engine running" in v.explanation


def test_receiver_problems_explain_the_failure():
    res, pre = C.evaluate_chain(_chain(), _reader({"rpm": 900, "a": "yes", "b": "no"}))
    findings = [C.Finding(C.SEV_HIGH, "ECU", "carries VIN X", kind="vin"),
                C.Finding(C.SEV_HIGH, "ECU", "communication fault: U0101", kind="network")]
    v = C.build_verdict(res, pre, findings, {"r": "ECU"})
    assert "another car's VIN" in v.explanation and "communication faults" in v.explanation
    assert "reprogrammed" in v.action


def test_ok_chain_with_other_problems():
    res, pre = C.evaluate_chain(_chain(), _reader({"rpm": 900, "a": "1", "b": "1", "c": "1", "x": "1"}))
    v = C.build_verdict(res, pre, [C.Finding(C.SEV_MED, "HVAC", "fault: blower")], {})
    assert v.level == "fault" and "other problems" in v.headline


# -------------------------------------------------------------- findings
def test_vin_findings_use_body_ecu_as_reference():
    f, ref = C.vin_findings({"BCM": "VF1HJD40364862159", "ECM": "VF1HSRCEP58556200",
                             "HVAC": "VF1HJD40165390697", "EMM": "VF1HJD40364862159"}, "BCM")
    assert ref == "VF1HJD40364862159"
    assert sorted(x.ecu for x in f) == ["ECM", "HVAC"]
    assert C.clean_vin("00000000000000000") is None and C.clean_vin(" VF1HJD40364862159 ")


def _dtc(code, desc, status="confirmedDTC", active=False):
    return E.Dtc(code=code, description=desc, status=status, active=active)


def test_dtc_classification():
    u = _dtc("C10187", "Lost Communication with Transmission Control Module / missing message",
             "confirmedDTC, testFailed", True)
    cfg = _dtc("A4A255", "Config")
    old = _dtc("950414", "EXTERNAL_ANTENNA / short", "testNotCompletedSinceLastClear")
    blower = _dtc("A4F593", "Blower / no operation", "confirmedDTC, testFailed", True)
    assert C.is_network_dtc(u) and C.is_config_dtc(cfg) and not C.is_network_dtc(blower)
    f = C.dtc_findings("ECM", [u, old], lambda d: False, in_chain=True)
    assert [x.kind for x in f] == ["network"]                  # old stored code ignored
    f = C.dtc_findings("HVAC", [cfg, blower], lambda d: True, in_chain=True)
    assert [x.kind for x in f] == ["config", "dtc"]
    assert C.dtc_findings("ABS", [u], lambda d: False, in_chain=False) == []


@pytest.mark.parametrize("name,value,bad", [
    ("VL_BlowerControlDiag_OCFM", "1", True), ("VL_BlowerControlDiag_OCFM", "0", False),
    ("IH_ACCompClutchDefault", "No clutch failure", False),
    ("IH_ACCompClutchDefault", "Clutch failure", True),
    ("IH_HVPTCElectronicDefect", "No defect", False),
    ("CAN_CLIM_ABSENT", "true", True), ("IH_ACREQUESTNA", "false", False),
])
def test_indicators(name, value, bad):
    assert C.indicator_abnormal(name, value) is bad


def test_indicator_selection_skips_requests_and_config():
    ef = SimpleNamespace(requests={
        "r1": SimpleNamespace(sentbytes="224001", sendbyte_dataitems={}, manualsend=False,
                              dataitems={"VL_BlowerControlDiag_OCFM": 1, "OH_AC_StopAutoForbidden": 1,
                                         "E_B_ESMINHIBIT_CF": 1, "VH_FrontDeMistingRecyInhibReq": 1,
                                         "IH_ACCompClutchDefault": 1})})
    assert sorted(C.indicator_items(ef)) == ["IH_ACCompClutchDefault", "VL_BlowerControlDiag_OCFM"]


def test_assign_roles_dedupes_by_address():
    T = lambda label, addr, match, group="": E.EcuTarget(label, label, label + ".json", match, addr, group)
    targets = [T("[UCH] old", "26", E.MATCH_UNVERIFIED), T("[BCM] new", "26", E.MATCH_UNVERIFIED),
               T("[HVAC] clim", "29", E.MATCH_UNVERIFIED), T("[ECM_29] nissan", "15", E.MATCH_EXACT, "ECM_29"),
               T("[Injection] ecm", "7A", E.MATCH_UNVERIFIED, "Injection")]
    score = {"[UCH] old": 3, "[BCM] new": 9}
    roles, kept, skipped = C.assign_roles(targets, P.roles, score=lambda t: score.get(t.label, 0))
    assert roles["bcm"].label == "[BCM] new"
    assert roles["engine"].label == "[Injection] ecm"           # address wins over name
    assert roles["hvac"].label == "[HVAC] clim"
    assert [(s.label, k.label) for s, k in skipped] == [("[UCH] old", "[BCM] new")]


# -------------------------------------------------- end-to-end scenarios
DTC_DATA = {
    "DTCDeviceIdentifier": {"bitscount": 16, "bytescount": 2, "lists": {"49409": "Lost Communication with TCM"}},
    "DTCFailureType": {"bitscount": 8, "lists": {"135": "missing message"}},
    "StatusOfDTC": {"bitscount": 8},
    "DTCStatus.testFailed": {"bitscount": 1, "lists": {"0": "No", "1": "Yes"}},
    "DTCStatus.confirmedDTC": {"bitscount": 1, "lists": {"0": "No", "1": "Yes"}},
}
DTC_REQ = {"name": "ReadDTCInformation.ReportDTC", "sentbytes": "1902FF", "shiftbytescount": 4,
           "manualsend": True, "receivebyte_dataitems": {
               "DTCDeviceIdentifier": {"firstbyte": 4}, "DTCFailureType": {"firstbyte": 6},
               "StatusOfDTC": {"firstbyte": 7},
               "DTCStatus.testFailed": {"firstbyte": 7, "bitoffset": 7},
               "DTCStatus.confirmedDTC": {"firstbyte": 7, "bitoffset": 4}}}


def make_ecu(tmp_path, name, addr, items):
    """items: {data name: (did, lists or None, unit)} -> one 1-byte DID each,
    plus VIN (F190) and a UDS DTC request."""
    data, reqs = dict(DTC_DATA), [dict(DTC_REQ)]
    data["VIN"] = {"bytescount": 17, "bitscount": 136, "bytesascii": True}
    reqs.append({"name": "DataRead.VIN", "sentbytes": "22F190", "receivebyte_dataitems": {"VIN": {"firstbyte": 4}}})
    for item, (did, lists, unit) in items.items():
        d = {"bitscount": 8}
        if lists:
            d["lists"] = lists
        if unit:
            d.update(unit)
        data[item] = d
        reqs.append({"name": "DataRead." + item, "sentbytes": "22" + did,
                     "receivebyte_dataitems": {item: {"firstbyte": 4}}})
    doc = {"ecuname": name, "endian": "Big", "devices": [], "requests": reqs, "data": data,
           "obd": {"protocol": "CAN", "send_id": "7E0", "recv_id": "7E8", "baudrate": 500000,
                   "funcaddr": addr, "funcname": name}}
    path = tmp_path / (name + ".json")
    path.write_text(json.dumps(doc), encoding="utf-8")
    ef = EcuFile(str(path), True)
    return ef, {item: did for item, (did, _l, _u) in items.items()}


YESNO = {"0": "no", "1": "yes"}
SCALE_RPM = {"scaled": True, "step": 50, "unit": "rpm"}

HVAC_ITEMS = {"IH_EngineRPM": ("4001", None, SCALE_RPM),
              "OH_BlowerState": ("4002", None, None),
              "OH_ACCompRequest": ("4003", {"0": "No Air Conditioning Requested", "1": "Air Conditioning Requested"}, None),
              "OH_ACCompForbidden": ("4004", {"0": "AC compressor activation not forbidden",
                                              "1": "AC compressor activation forbidden"}, None),
              "IH_ACCompAuthorized": ("4005", {"0": "ACComp not authorized", "1": "ACComp authorized"}, None),
              "IH_ACCompClutchStatus": ("4006", {"0": "Clutch OFF", "1": "Clutch ON"}, None),
              "IH_ACHighPressureSensor": ("4007", None, {"scaled": True, "unit": "bar"}),
              "VL_BlowerControlDiag_OCFM": ("4008", None, None)}
ENG_ITEMS = {"Air conditioning request": ("2092", YESNO, None),
             "Combination of all engine air conditioning compressor inhibitions": ("2095", YESNO, None),
             "Combination of all vehicle air conditioning compressor inhibitions": ("2094", YESNO, None),
             "air condition compressor relay (OFF/ON)": ("FDBD", None, None),
             "Relative air conditioning pressure": ("222A", None, {"scaled": True, "unit": "bar"})}
BCM_ITEMS = {"IH_ACREQUESTNA": ("6001", {"0": "false", "1": "true"}, None),
             "CAN_CLIM_PRESENT": ("6002", {"0": "false", "1": "true"}, None)}

CAR_VIN = "VF1HJD40364862159"


class FakeTransport(E.Transport):
    """Real safety checks, fake bus: answers per ECU from ``state``."""

    def __init__(self, ecus, state):
        self.ecus, self.state, self.sent = ecus, state, []
        super(FakeTransport, self).__init__(
            SimpleNamespace(simulation_mode=False, cantimeout=0,
                            elm=SimpleNamespace(request=self._request, set_can_timeout=lambda v: None)))

    def connect(self, ecufile, canline=0):
        self.current = ecufile
        return True

    def start_session(self, ecufile, stream=None):
        return True

    def _request(self, spaced, cache=True, **kw):
        raw = spaced.replace(" ", "")
        name = self.current.ecuname
        self.sent.append((name, raw))
        st = self.state[name]
        if raw == "22F190":
            return "62 F1 90 " + " ".join("%02X" % ord(c) for c in st.get("vin", CAR_VIN))
        if raw == "1902FF":
            return "59 02 FF" + "".join(" " + b for b in st.get("dtc", []))
        if raw[:2] == "22":
            item = next((i for i, d in self.ecus[name].items() if d == raw[2:]), None)
            if item is None or item not in st:
                return "7F 22 31"
            return "62 %s %s %02X" % (raw[2:4], raw[4:6], st[item])
        if raw[:2] in ("2F", "3E"):
            hook = st.get("on_action")
            if hook:
                hook(raw)
            return "%02X %s" % (int(raw[:2], 16) + 0x40, " ".join(raw[i:i + 2] for i in range(2, len(raw), 2)))
        return "7F %s 11" % raw[:2]


def build_car(tmp_path, hvac, eng, bcm, eng_vin=CAR_VIN, eng_dtc=(), with_test=False):
    files = {}
    hv, hv_dids = make_ecu(tmp_path, "HVAC", "29", HVAC_ITEMS)
    items = dict(ENG_ITEMS)
    if with_test:
        items = dict(items)
    en, en_dids = make_ecu(tmp_path, "ECM", "7A", items)
    if with_test:
        en.requests["IOControl.Air conditioning state.START.V_ON"] = EcuRequest(
            {"name": "IOControl.Air conditioning state.START.V_ON", "sentbytes": "2F201E0301"}, en)
        en.requests["Compression test Strt"] = EcuRequest(
            {"name": "Compression test Strt", "sentbytes": "316400"}, en)
    bc, bc_dids = make_ecu(tmp_path, "BCM", "26", BCM_ITEMS)
    for ef in (hv, en, bc):
        files[ef.ecuname + ".json"] = ef
    targets = [E.EcuTarget("[HVAC] clim", "clim", "HVAC.json", E.MATCH_EXACT, "29", "HVAC"),
               E.EcuTarget("[ECM] engine", "eng", "ECM.json", E.MATCH_EXACT, "7A", "Injection"),
               E.EcuTarget("[BCM] body", "bcm", "BCM.json", E.MATCH_EXACT, "26", "BCM")]
    state = {"HVAC": dict(hvac), "ECM": dict(eng, vin=eng_vin, dtc=list(eng_dtc)), "BCM": dict(bcm)}
    tr = FakeTransport({"HVAC": hv_dids, "ECM": en_dids, "BCM": bc_dids}, state)
    run = GuidedRun(P, targets, lambda t: files.get(t.path), tr)
    return run, tr, state


GOOD_HVAC = {"IH_EngineRPM": 18, "OH_BlowerState": 1, "OH_ACCompRequest": 1, "OH_ACCompForbidden": 0,
             "IH_ACCompAuthorized": 1, "IH_ACCompClutchStatus": 1, "IH_ACHighPressureSensor": 12,
             "VL_BlowerControlDiag_OCFM": 0}
GOOD_ENG = {"Air conditioning request": 1,
            "Combination of all engine air conditioning compressor inhibitions": 0,
            "Combination of all vehicle air conditioning compressor inhibitions": 0,
            "air condition compressor relay (OFF/ON)": 1, "Relative air conditioning pressure": 12}
GOOD_BCM = {"IH_ACREQUESTNA": 0, "CAN_CLIM_PRESENT": 1}
U0101_ACTIVE = ["C1", "01", "87", "99"]   # testFailed + confirmed


def run_once(run):
    run.baseline()
    run.cycle()
    return run.verdict, {r.check.id: r.state for r in run.results}


def test_scenario_working_system(tmp_path):
    run, tr, _ = build_car(tmp_path, GOOD_HVAC, GOOD_ENG, GOOD_BCM)
    v, states = run_once(run)
    assert v.level == "ok", v
    assert states["engine_receives"] == C.OK and states["compressor_engaged"] == C.OK
    assert all(raw[:2] in ("22", "19") for _n, raw in tr.sent)   # reads only


def test_scenario_user_case_ecu_software(tmp_path):
    eng = dict(GOOD_ENG, **{"Air conditioning request": 0, "air condition compressor relay (OFF/ON)": 0})
    hvac = dict(GOOD_HVAC, IH_ACCompAuthorized=0, IH_ACCompClutchStatus=0)
    run, _tr, _ = build_car(tmp_path, hvac, eng, GOOD_BCM, eng_vin="VF1HSRCEP58556200",
                            eng_dtc=U0101_ACTIVE)
    v, states = run_once(run)
    assert states["network"] == C.OK and states["engine_receives"] == C.FAIL
    assert states["relay_on"] == C.NOT_REACHED
    assert v.headline.startswith("Engine ECU receives the request")
    assert "another car's VIN" in v.explanation and "communication faults" in v.explanation
    assert "reprogrammed" in v.action


def test_scenario_engine_inhibits(tmp_path):
    eng = dict(GOOD_ENG, **{"Combination of all engine air conditioning compressor inhibitions": 1,
                            "air condition compressor relay (OFF/ON)": 0})
    run, _tr, _ = build_car(tmp_path, dict(GOOD_HVAC, IH_ACCompClutchStatus=0), eng, GOOD_BCM)
    v, states = run_once(run)
    assert states["engine_receives"] == C.OK and states["engine_allows"] == C.FAIL
    assert v.headline.startswith("Engine ECU allows the compressor")


def test_scenario_climate_messages_missing(tmp_path):
    run, _tr, _ = build_car(tmp_path, GOOD_HVAC, dict(GOOD_ENG, **{"Air conditioning request": 0}),
                            {"IH_ACREQUESTNA": 1, "CAN_CLIM_PRESENT": 0})
    v, states = run_once(run)
    assert states["network"] == C.FAIL and v.headline.startswith("Request valid on the network")


def test_scenario_clutch_hardware(tmp_path):
    run, _tr, _ = build_car(tmp_path, dict(GOOD_HVAC, IH_ACCompClutchStatus=0), GOOD_ENG, GOOD_BCM)
    v, _states = run_once(run)
    assert v.headline.startswith("Compressor engaged") and "clutch" in v.action


def test_scenario_empty_refrigerant_and_blower_fault(tmp_path):
    hvac = dict(GOOD_HVAC, IH_ACHighPressureSensor=0, VL_BlowerControlDiag_OCFM=1)
    run, _tr, _ = build_car(tmp_path, hvac, dict(GOOD_ENG, **{"Relative air conditioning pressure": 0}), GOOD_BCM)
    v, states = run_once(run)
    assert states["pressure"] == C.FAIL
    texts = " ".join(f.text for f in v.findings)
    assert "refrigerant" in texts and "VL_BlowerControlDiag_OCFM = 01" in texts


def test_scenario_waits_for_engine(tmp_path):
    run, _tr, _ = build_car(tmp_path, dict(GOOD_HVAC, IH_EngineRPM=0), GOOD_ENG, GOOD_BCM)
    v, _ = run_once(run)
    assert v.level == "waiting"


def test_direct_test_reports_working_compressor_and_releases(tmp_path):
    run, tr, state = build_car(tmp_path, GOOD_HVAC, dict(GOOD_ENG, **{
        "air condition compressor relay (OFF/ON)": 0, "Relative air conditioning pressure": 6}),
        GOOD_BCM, with_test=True)
    eng = state["ECM"]

    def on_action(raw):
        if raw == "2F201E0301":
            eng["air condition compressor relay (OFF/ON)"] = 1
            eng["Relative air conditioning pressure"] = 14
    eng["on_action"] = on_action
    plan = run.prepare_active_test()
    assert plan[1].name == "IOControl.Air conditioning state.START.V_ON"   # not the compression test
    assert plan[4] == "2F201E00"
    lines = run.run_active_test(plan, sleep=lambda s: None)
    assert any("compressor works" in l for l in lines)
    assert ("ECM", "2F201E00") in tr.sent and tr.sent[-1] == ("ECM", "2F201E00")


def test_direct_test_aborts_on_high_pressure(tmp_path):
    run, tr, state = build_car(tmp_path, GOOD_HVAC, GOOD_ENG, GOOD_BCM, with_test=True)
    state["ECM"]["Relative air conditioning pressure"] = 30
    lines = run.run_active_test(run.prepare_active_test(), sleep=lambda s: None)
    assert any("Stopped early" in l for l in lines)
    starts = [r for _n, r in tr.sent if r == "2F201E0301"]
    assert len(starts) == 1 and tr.sent[-1][1] == "2F201E00"


def test_vin_without_reference_or_majority_does_not_guess():
    f, ref = C.vin_findings({"ECM": "VF1HSRCEP58556200", "HVAC": "VF1HJD40165390697",
                             "BCM": "VF1HJD40364862159"}, reference_label="UCH-not-read")
    assert ref is None and len(f) == 1 and "different VINs" in f[0].text


def test_duplicate_module_keeps_the_file_that_answers(tmp_path):
    run, tr, state = build_car(tmp_path, GOOD_HVAC, GOOD_ENG, GOOD_BCM)
    # an older database file for the same body ECU, listed first: its DIDs don't answer
    old_ef, old_dids = make_ecu(tmp_path, "UCHOLD", "26", {
        "IH_ACREQUESTNA": ("7001", {"0": "false", "1": "true"}, None),
        "CAN_CLIM_PRESENT": ("7002", {"0": "false", "1": "true"}, None)})
    tr.ecus["UCHOLD"], state["UCHOLD"] = old_dids, {}
    files = {"HVAC.json": run.load(run.kept["29"]), "ECM.json": run.load(run.kept["7A"]),
             "BCM.json": run.load(run.kept["26"]), "UCHOLD.json": old_ef}
    targets = [E.EcuTarget("[UCH] old", "old", "UCHOLD.json", E.MATCH_EXACT, "26", "UCH")] + \
              [run.kept[a] for a in ("29", "7A", "26")]
    run = GuidedRun(P, targets, lambda t: files.get(t.path), tr)
    assert run.roles["bcm"].label == "[UCH] old"           # static tie: first entry
    v, states = run_once(run)
    assert run.roles["bcm"].label == "[BCM] body"           # probing picked the answering file
    assert [(s.label, k.label) for s, k in run.skipped] == [("[UCH] old", "[BCM] body")]
    assert states["network"] == C.OK and v.level == "ok"
