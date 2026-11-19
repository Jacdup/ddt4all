# -*- coding: utf-8 -*-
"""Unit tests for the system diagnostics plugin engine (no Qt, no hardware)."""
import json
from types import SimpleNamespace

import pytest

from ddt4all.core.ecu.ecu_file import EcuFile
from ddt4all.plugins.system_diag_lib import engine as E
from ddt4all.plugins.system_diag_lib.profiles import CLIMATE_PROFILE as P


def _req(name, sent, rx=None, tx=None, **extra):
    r = {"name": name, "sentbytes": sent, "deny_sds": []}
    if rx:
        r["receivebyte_dataitems"] = rx
    if tx:
        r["sendbyte_dataitems"] = tx
    r.update(extra)
    return r


@pytest.fixture
def bcm(tmp_path):
    """A tiny UDS BCM with A/C and non-A/C data, a DTC request and actions."""
    data = {
        "CLIM_TYPE": {"bitscount": 8, "lists": {"0": "No clim", "1": "Manual", "2": "Auto"}},
        "LIGHT_CF": {"bitscount": 8},
        "E_DU_AC_LHM_CF": {"bitscount": 8},
        "TURBO_COMPRESSOR_BYPASS": {"bitscount": 8},
        "Compressor inhibitions": {"bitscount": 8},
        "VIN": {"bytescount": 3, "bytesascii": True},
        "DTCStatusAvailabilityMask": {"bitscount": 8},
        "DTCDeviceIdentifier": {"bitscount": 16, "bytescount": 2,
                                "lists": {"16912": "AC compressor clutch", "4660": "Wiper"}},
        "DTCFailureType": {"bitscount": 8, "lists": {"17": "SC to ground"}},
        "StatusOfDTC": {"bitscount": 8},
        "DTCStatus.testFailed": {"bitscount": 1, "lists": {"0": "No", "1": "Yes"}},
        "DTCStatus.confirmedDTC": {"bitscount": 1, "lists": {"0": "No", "1": "Yes"}},
        "OH_BlowerReq": {"bitscount": 8},
    }
    reqs = [
        _req("DataRead.CLIM_TYPE", "2200FD", {"CLIM_TYPE": {"firstbyte": 4}}, replybytes="6200FD02"),
        _req("DataRead.ReadAllConfig1", "221001",
             {"LIGHT_CF": {"firstbyte": 4}, "E_DU_AC_LHM_CF": {"firstbyte": 5}}),
        _req("DataRead.Turbo", "222530", {"TURBO_COMPRESSOR_BYPASS": {"firstbyte": 4}}),
        _req("DataRead.Light", "221042", {"LIGHT_CF": {"firstbyte": 4}}),
        _req("DataRead.Compressor inhibitions", "222094", {"Compressor inhibitions": {"firstbyte": 4}}),
        _req("DataRead.VIN", "22F190", {"VIN": {"firstbyte": 4}}),
        # misnamed write: must be classified by its SID, not its name
        _req("DataRead.WriteAllConfig1", "2E100100", tx={"LIGHT_CF": {"firstbyte": 4}}),
        _req("DataWrite.CLIM_TYPE", "2E00FD00", tx={"CLIM_TYPE": {"firstbyte": 4}}),
        _req("ReadDTCInformation.ReportDTC", "1902FF", {
            "DTCStatusAvailabilityMask": {"firstbyte": 3},
            "DTCDeviceIdentifier": {"firstbyte": 4},
            "DTCFailureType": {"firstbyte": 6},
            "StatusOfDTC": {"firstbyte": 7},
            "DTCStatus.testFailed": {"firstbyte": 7, "bitoffset": 7},
            "DTCStatus.confirmedDTC": {"firstbyte": 7, "bitoffset": 4},
        }, shiftbytescount=4, manualsend=True),
        _req("ClearDiagnosticInformation.All", "14FFFFFF"),
        _req("OutputControl.Start.FrontBlower", "2F422F031E", tx={"OH_BlowerReq": {"firstbyte": 5}}),
        _req("OutputControl.Start.Wiper", "2F500103"),
        _req("OutputControl", "2F0000000000"),
        _req("RoutineControl.Clim_ResetConfiguration", "31010C02"),
        _req("StartDiagnosticSession.ExtendedDiagnosticSession", "1003"),
        _req("StartDiagnosticSession.ProgrammingSession", "1002"),
    ]
    doc = {"ecuname": "TEST_BCM", "endian": "Big",
           "obd": {"protocol": "CAN", "send_id": "745", "recv_id": "765", "baudrate": 500000,
                   "funcaddr": "26", "funcname": "BCM"},
           "devices": [], "requests": reqs, "data": data}
    path = tmp_path / "test_bcm.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    ef = EcuFile(str(path), True)
    assert ef.requests, "fixture failed to load"
    return ef


# ------------------------------------------------------------ classification
@pytest.mark.parametrize("stream,risk", [
    ("22F190", E.RISK_READ), ("1902FF", E.RISK_READ), ("2101", E.RISK_READ),
    ("3E00", E.RISK_READ), ("1003", E.RISK_SESSION), ("10C0", E.RISK_SESSION),
    ("1002", E.RISK_WRITE), ("1085", E.RISK_WRITE), ("2F422F031E", E.RISK_ACTION),
    ("30120700", E.RISK_ACTION), ("31010C06", E.RISK_ACTION), ("2E00FD80", E.RISK_WRITE),
    ("3B8001", E.RISK_WRITE), ("14FFFFFF", E.RISK_WRITE), ("1101", E.RISK_WRITE),
    ("2701", E.RISK_WRITE), ("3401", E.RISK_WRITE), ("22 F1 90", E.RISK_READ),
])
def test_classify_stream(stream, risk):
    assert E.classify_stream(stream) == risk


def test_misnamed_write_is_classified_by_sid(bcm):
    assert E.classify_request(bcm.requests["DataRead.WriteAllConfig1"]) == E.RISK_WRITE


def test_stateful_routine_is_rated_as_write(bcm):
    assert E.classify_request(bcm.requests["RoutineControl.Clim_ResetConfiguration"]) == E.RISK_WRITE
    assert E.classify_request(bcm.requests["OutputControl.Start.FrontBlower"]) == E.RISK_ACTION


@pytest.mark.parametrize("stream,release", [
    ("2F422F031E", "2F422F00"), ("2F 42 2F 03 1E", "2F422F00"), ("30120701", "301200"),
    ("31010C06", "31020C06"), ("3112", "3212"), ("31030C06", None), ("22F190", None),
])
def test_release_command(stream, release):
    assert E.release_command(stream) == release


# ------------------------------------------------------------------ profile
@pytest.mark.parametrize("text,expected", [
    ("CLIM_TYPE", True), ("E_DU_AC_LHM_CF", True), ("Air conditioning request", True),
    ("A/C compressor relay", True), ("Freon pressure", True), ("Final PTC level", True),
    ("Motor-fans requests", True), ("BACKLIGHT", False), ("ACCELERATOR", False),
    ("IH_ACREQUESTNA", True), ("IH_ACCompAuthorized", True), ("TRACREQ", False),
    ("BACKCOMPAT", False),
    ("Turbocharger (Compressor) ByPass Valve", False), ("Evaporation monitoring", False),
    ("Exhaust Gas Recirculation Temperature", False), ("Vehicle speed", False),
])
def test_climate_keywords(text, expected):
    assert P.matches(text) is expected


def test_primary_ecu_detection():
    assert P.is_primary_ecu("29")
    assert P.is_primary_ecu("", "HVAC")
    assert P.is_primary_ecu("72", "ClimBox")
    assert not P.is_primary_ecu("26", "BCM", "T4_VS_BCM")


# ---------------------------------------------------------------- selection
def test_parameter_reads_on_secondary_ecu(bcm):
    sel = {s.request.name: s.params for s in E.select_parameter_reads(bcm, P, primary=False)}
    assert sel["DataRead.CLIM_TYPE"] == ["CLIM_TYPE"]
    # only the A/C parameter of a mixed request
    assert sel["DataRead.ReadAllConfig1"] == ["E_DU_AC_LHM_CF"]
    assert "DataRead.Light" not in sel
    assert "DataRead.Turbo" not in sel                  # excluded false positive
    assert "DataRead.VIN" not in sel                    # identification, not parameter
    assert "ReadDTCInformation.ReportDTC" not in sel    # DTC handled separately
    assert not any(n.startswith("DataRead.Write") for n in sel)


def test_parameter_reads_on_primary_ecu_take_everything(bcm):
    names = {s.request.name for s in E.select_parameter_reads(bcm, P, primary=True)}
    assert {"DataRead.Light", "DataRead.Turbo", "DataRead.CLIM_TYPE"} <= names


def test_ident_reads(bcm):
    assert [s.request.name for s in E.select_ident_reads(bcm)] == ["DataRead.VIN"]


def test_select_actions(bcm):
    names = [r.name for r in E.select_actions(bcm, P, primary=False)]
    assert "OutputControl.Start.FrontBlower" in names       # blower = A/C
    assert "RoutineControl.Clim_ResetConfiguration" in names
    assert "OutputControl.Start.Wiper" not in names         # unrelated
    assert "OutputControl" not in names                     # 2F0000 template
    assert "DataWrite.CLIM_TYPE" not in names               # writes hidden by default
    with_writes = [r.name for r in E.select_actions(bcm, P, primary=False, include_writes=True)]
    assert "DataWrite.CLIM_TYPE" in with_writes
    assert "DataRead.WriteAllConfig1" not in with_writes    # not A/C related


def test_pick_session_prefers_extended(bcm):
    assert E.pick_session(bcm) == "1003"


def test_find_dtc_requests(bcm):
    assert E.find_dtc_request(bcm).name == "ReadDTCInformation.ReportDTC"
    assert E.find_clear_dtc_request(bcm).name == "ClearDiagnosticInformation.All"


# ------------------------------------------------------------------ decoding
def test_check_response():
    assert E.check_response("2200FD", "62 00 FD 02") == (True, "")
    ok, msg = E.check_response("2200FD", "7F 22 31")
    assert not ok and "31" in msg
    assert not E.check_response("2200FD", "NO DATA")[0]
    assert not E.check_response("2200FD", "")[0]
    assert not E.check_response("2200FD", "50 C0")[0]


def test_check_response_rejects_out_of_sync_and_pending():
    # delayed DTC answer arriving as the reply to a DID read
    ok, msg = E.check_response("222098", "59 02 FF C1")
    assert not ok
    # positive service byte but another DID's data
    ok, msg = E.check_response("222098", "62 20 97 01")
    assert not ok and msg.startswith("out-of-sync")
    assert E.check_response("1902FF", "59 02 FF 42 10 11 89")[0]
    for pending in (":78:NR: REQUEST CORRECTLY RECEIVED-RESPONSE PENDING",
                    "NR:78:REQUEST CORRECTLY RECEIVED-RESPONSE PENDING", "7F 19 78"):
        assert E.is_pending(pending)
        assert not E.check_response("1902AF", pending)[0]
    assert not E.is_pending("62 20 78 01")


def test_transport_retries_pending_read_with_longer_timeout():
    answers = [":78:NR: REQUEST CORRECTLY RECEIVED-RESPONSE PENDING", "59 02 FF C1 23 45 08"]
    calls, timeouts = [], []
    elm = SimpleNamespace(
        request=lambda req, cache=True, **k: calls.append(req) or answers.pop(0),
        set_can_timeout=timeouts.append)
    t = E.Transport(SimpleNamespace(simulation_mode=False, elm=elm, cantimeout=0))
    assert t.send("1902AF") == "59 02 FF C1 23 45 08"
    assert calls == ["19 02 AF", "19 02 AF"]
    assert timeouts == [2000, 0]          # raised, then restored


def test_transport_resyncs_after_stale_answer():
    answers = ["59 02 FF C1 23 45 08", "62 20 98 00"]
    elm = SimpleNamespace(request=lambda req, cache=True, **k: answers.pop(0),
                          set_can_timeout=lambda v: None)
    t = E.Transport(SimpleNamespace(simulation_mode=False, elm=elm, cantimeout=0))
    assert t.send("222098") == "62 20 98 00"


def test_transport_never_retries_actions():
    calls = []
    elm = SimpleNamespace(request=lambda req, cache=True, **k: calls.append(req) or "7F 2F 78",
                          set_can_timeout=lambda v: None)
    t = E.Transport(SimpleNamespace(simulation_mode=False, elm=elm, cantimeout=0))
    t.send("2F201E0301", allow_unsafe=True)
    assert len(calls) == 1


def test_decode_values(bcm):
    req = bcm.requests["DataRead.CLIM_TYPE"]
    assert E.decode_values(bcm, req, "62 00 FD 02", ["CLIM_TYPE"]) == {"CLIM_TYPE": "Auto"}


def test_decode_dtcs_uds(bcm):
    req = bcm.requests["ReadDTCInformation.ReportDTC"]
    # header 59 02 FF, then 2 records of 4 bytes + one all-zero padding record
    resp = "59 02 FF 42 10 11 89 12 34 11 08 00 00 00 00"
    dtcs = E.decode_dtcs(bcm, req, resp)
    assert len(dtcs) == 2
    first, second = dtcs
    assert first.code == "421011"
    assert "AC compressor clutch" in first.description
    assert "SC to ground" in first.description
    assert first.active is True                      # testFailed bit 7 set
    assert "confirmedDTC" in first.status
    assert second.code == "123411"
    assert second.active is False
    assert P.matches(first.description) and not P.matches(second.description)


def test_decode_dtcs_empty(bcm):
    req = bcm.requests["ReadDTCInformation.ReportDTC"]
    assert E.decode_dtcs(bcm, req, "59 02 FF") == []


@pytest.mark.parametrize("name,value,expected", [
    ("Compressor inhibitions", "Inhibited by engine", True),
    ("Compressor inhibitions", "0", False),
    ("Compressor inhibitions", "No inhibition", False),
    ("Compressor inhibitions", "3", True),
    ("A/C state", "Short circuit to ground", True),
    ("A/C state", "No failure", False),
    ("A/C state", "Not OK", True),
    ("A/C state", "Active", False),
    ("Freon pressure", "12.5", False),
    ("anything", None, False),
    ("Motor fan failure status", "0: No motor fan failure on the CAN", False),
    ("Motor fan failure status", "1: Motor fan failure on the CAN", True),
    ("Stop auto inhibition", "Automatic air conditionning authorizes StopAuto", False),
    ("KH_AltLoadErrorManaged_IV", "-100", False),
    ("Combination of all engine air conditioning compressor inhibitions", "yes", True),
    ("Combination of all engine air conditioning compressor inhibitions", "no", False),
])
def test_looks_abnormal(name, value, expected):
    assert E.looks_abnormal(name, value) is expected


# ---------------------------------------------------------------- transport
def _options(sim=True, elm=None):
    return SimpleNamespace(simulation_mode=sim, elm=elm)


def test_transport_blocks_unconfirmed_writes_and_actions(bcm):
    elm = SimpleNamespace(request=lambda *a, **k: pytest.fail("must not reach the ELM"))
    t = E.Transport(_options(sim=False, elm=elm))
    for stream in ("2E00FD02", "2F422F031E", "31010C02", "14FFFFFF", "1101", "1002"):
        with pytest.raises(E.UnsafeCommandError):
            t.send(stream)


def test_transport_sends_reads_uncached():
    calls = []

    def request(req, positive='', cache=True, serviceDelay="0"):
        calls.append((req, cache))
        return "62 F1 90 41 42 43"

    t = E.Transport(_options(sim=False, elm=SimpleNamespace(request=request)))
    assert t.send("22F190") == "62 F1 90 41 42 43"
    assert calls == [("22 F1 90", False)]


def test_transport_sends_confirmed_action():
    calls = []
    elm = SimpleNamespace(request=lambda req, cache=True, **k: calls.append(req) or "6F 42 2F 03")
    t = E.Transport(_options(sim=False, elm=elm))
    t.send("2F422F031E", allow_unsafe=True)
    assert calls == ["2F 42 2F 03 1E"]


def test_transport_refuses_programming_session(bcm):
    t = E.Transport(_options(sim=True))
    with pytest.raises(E.UnsafeCommandError):
        t.start_session(bcm, "1002")
    assert t.start_session(bcm) is True


def test_transport_simulation_uses_database_reply(bcm):
    t = E.Transport(_options(sim=True))
    req = bcm.requests["DataRead.CLIM_TYPE"]
    assert t.send(req.sentbytes, req) == "62 00 FD 02"
    assert t.send("221042").startswith("7F 22")


# ------------------------------------------------------------ detected ECUs
def test_detected_ecus_resolution():
    def ident(name, href, addr, group):
        return SimpleNamespace(name=name, href=href, addr=addr, group=group)

    exact = ident("ClimBox", "ClimBox.json", "29", "HVAC")
    approx = ident("BCM_X", "BCM_X.json", "26", "BCM")
    catalog = ident("ECM_Y", "ECM_Y.json", "7A", "ECM")
    db = SimpleNamespace(getTarget=lambda n: {"ECM_Y": catalog}.get(n))
    scan = SimpleNamespace(ecus={"[ HVAC ] ClimBox": exact},
                           approximate_ecus={"[ BCM ] BCM_X": approx}, ecu_database=db)
    labels = ["[ HVAC ] ClimBox", "[ BCM ] BCM_X", "[ ECM ] ECM_Y", "mine.json", "ghost"]
    items = [SimpleNamespace(text=(lambda s=s: s)) for s in labels]
    lw = SimpleNamespace(count=lambda: len(items), item=lambda i: items[i])
    mw = SimpleNamespace(ecu_scan=scan, ecunamemap={"[ ECM ] ECM_Y": "ECM_Y"}, treeview_ecu=lw)

    out = E.detected_ecus(mw, "ecus/")
    assert [(t.match, t.path) for t in out] == [
        (E.MATCH_EXACT, "ecus/ClimBox.json"),
        (E.MATCH_APPROX, "ecus/BCM_X.json"),
        (E.MATCH_UNVERIFIED, "ecus/ECM_Y.json"),
        (E.MATCH_UNKNOWN, "mine.json"),
        (E.MATCH_UNKNOWN, None),
    ]
    assert out[0].address == "29"
