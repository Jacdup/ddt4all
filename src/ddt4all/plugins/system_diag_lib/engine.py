# -*- coding: utf-8 -*-
"""
Engine of the system diagnostics plugin: no Qt in here.

Safety model
------------
Every request is classified from its **service ID** (first byte sent), never
from its name: some database entries are misnamed (``DataRead.WriteAllConfig1``
is really a ``2E`` write).

* ``read``    - 19/17/18 (DTC), 21/22 (data), 1A (ident), 23 (memory), 3E.
                Sent freely.
* ``action``  - 2F/30 (output control), 31/32/33 (routines): drives actuators.
* ``write``   - everything else (2E/3B config writes, 11 reset, 14 clear DTC,
                27 security, 34-37 download...): changes ECU memory/state.

``Transport.send`` refuses anything that is not ``read`` (or a non-programming
diagnostic session) unless the caller passes ``allow_unsafe=True``; the UI
only does so after the user confirmed the exact command.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

READ_SIDS = {"17", "18", "19", "1A", "21", "22", "23", "3E"}
ACTION_SIDS = {"2F", "30", "31", "32", "33"}
#: service IDs of the configuration writes listed (on demand) in the actions tab
CONFIG_WRITE_SIDS = {"2E", "3B"}
#: diagnostic sessions that must never be opened without confirmation
PROGRAMMING_SESSIONS = {"1002", "1085"}
DATA_SIDS = {"21", "22"}
DTC_SIDS = {"17", "18", "19"}

RISK_READ = "read"
RISK_SESSION = "session"
RISK_ACTION = "action"
RISK_WRITE = "write"

MATCH_EXACT = "exact"
MATCH_APPROX = "approximate"
MATCH_UNVERIFIED = "not verified"
MATCH_UNKNOWN = "unknown"

_IDENT_NAME = re.compile(
    r"identification|ident\b|\.ident\.|software|soft[\s_]?(?:version|number)|hardware|"
    r"supplier|fournisseur|part.?number|\bVIN\b|diag(?:nostic)?.?version|serial|"
    r"calibration[\s_.-]?(?:number|id|verification)", re.I)
_NOT_IDENT_NAME = re.compile(r"supported|threshold|history", re.I)
_SKIP_ACTION_NAME = re.compile(r"support(?:ed)?[\s_.]?check|supported", re.I)
#: actions that alter stored state (learning, resets...) are rated as writes
_STATEFUL_ACTION = re.compile(
    r"reset|erase|clear|config|learn|apprent|init|program|virgin|calibrat|"
    r"index|code|write|save|store|blank", re.I)

_FAULT_VALUE = re.compile(
    r"not\s*ok|\bnok\b|fault|failure|d[ée]faut|panne|error|erreur|short|"
    r"court[\s-]?circuit|open[\s-]?circuit|circuit[\s-]?ouvert|inhib|invalid|"
    r"blocked|bloqu|out of range|hors plage|incoh", re.I)
_NEGATED_VALUE = re.compile(
    r"^\s*(?:no|non|none|aucune?|pas|sans|without|false|inactive?|inactif|ok)\b|"
    r"\bno\b[^.;]*\b(?:fault|failure|error|inhib|d[ée]faut|panne)|pas\s+de|absence", re.I)
_FAULTY_NAME = re.compile(r"inhib|fault|failure|d[ée]faut|panne", re.I)
#: leading raw code of list values, e.g. "0: ...", "0d - ...", "3 = ..."
_CODE_PREFIX = re.compile(r"^\s*[0-9A-Fa-f]{1,4}\s*[:=-]\s*")
_HAS_WORDS = re.compile(r"[A-Za-zÀ-ſ]{3,}")
_NEUTRAL_VALUE = re.compile(
    r"^\s*(?:0+(?:\.0+)?|no|non|none|aucune?|false|inactive?|inactif|ok|"
    r"not active|off|absent|pas d.*|no .*)\s*$", re.I)


class UnsafeCommandError(Exception):
    """Raised when a non-read command is sent without explicit permission."""


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def sid_of(stream):
    return stream.replace(" ", "")[:2].upper()


def classify_stream(stream):
    raw = stream.replace(" ", "").upper()
    sid = raw[:2]
    if sid in READ_SIDS:
        return RISK_READ
    if sid == "10":
        return RISK_WRITE if raw[:4] in PROGRAMMING_SESSIONS else RISK_SESSION
    if sid in ACTION_SIDS:
        return RISK_ACTION
    return RISK_WRITE


def classify_request(request):
    """Risk of a database request: the stream class, raised to ``write`` for
    routines/output controls whose name says they alter stored state."""
    risk = classify_stream(request.sentbytes)
    if risk == RISK_ACTION and _STATEFUL_ACTION.search(request.name):
        return RISK_WRITE
    return risk


def is_template_action(request):
    """Generic 'OutputControl' entries whose identifier is a 00 placeholder."""
    raw = request.sentbytes.upper()
    sid = raw[:2]
    return (sid == "2F" and raw[2:6] == "0000") or (sid == "30" and raw[2:4] == "00")


def release_command(stream):
    """Command that ends an output control / routine started by ``stream``.

    UDS 2F <DID> xx -> 2F <DID> 00 (returnControlToECU)
    KWP 30 <LID> xx -> 30 <LID> 00 (returnControlToECU)
    UDS 31 01 <RID> -> 31 02 <RID> (stopRoutine)
    KWP 31 <LID>    -> 32 <LID>    (stopRoutineByLocalIdentifier)
    """
    raw = stream.replace(" ", "").upper()
    sid = raw[:2]
    if sid == "2F" and len(raw) >= 6:
        return "2F" + raw[2:6] + "00"
    if sid == "30" and len(raw) >= 4:
        return "30" + raw[2:4] + "00"
    if sid == "31":
        if raw[2:4] == "01" and len(raw) >= 8:
            return "3102" + raw[4:8]
        if len(raw) >= 4 and raw[2:4] not in ("02", "03"):
            return "32" + raw[2:4]
    return None


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------
def response_bytes(response):
    if not response:
        return []
    return response.strip().upper().split()


_PENDING = re.compile(r"NR:\s*78\b|(?:^|\s):?78:|^7F\s*[0-9A-F]{2}\s*78\b", re.I)


def is_pending(response):
    """NRC 0x78 'response pending': the real answer comes later."""
    return bool(response) and bool(_PENDING.search(response.strip()))


def _echo_len(sent_stream):
    """Bytes after the service byte that the ECU echoes back (DID, LID, sub-function)."""
    raw = sent_stream.replace(" ", "").upper()
    sid = raw[:2]
    if sid == "22":
        return 2
    if sid in ("21", "19", "1A", "2F", "30", "31", "10"):
        return 1
    return 0


def check_response(sent_stream, response):
    """Return (ok, message) for an ELM response.

    Besides the positive service byte, the echoed identifier must match the
    request: a delayed answer to an earlier request is rejected instead of
    being decoded as this request's value.
    """
    if response is None:
        return False, "no response"
    text = response.strip().upper()
    if not text:
        return False, "no response"
    if is_pending(text):
        return False, "no final answer (ECU still busy, NRC 78)"
    for bad in ("WRONG", "NO DATA", "BLOCKED", "CAN ERROR", "ERROR", "?", "STOPPED", "NR:"):
        if text.startswith(bad):
            return False, text
    rb = response_bytes(text)
    sid = sid_of(sent_stream)
    try:
        expected = "%02X" % (int(sid, 16) + 0x40)
    except ValueError:
        return False, "invalid request"
    if rb[0] == "7F":
        if len(rb) > 1 and rb[1] != sid:
            return False, "out-of-sync response " + " ".join(rb[:4])
        nrc = rb[2] if len(rb) > 2 else "??"
        return False, "negative response 7F (NRC %s)" % nrc
    if rb[0] != expected:
        if re.fullmatch(r"[4-7][0-9A-F]", rb[0]):   # positive answer to another service
            return False, "out-of-sync response " + " ".join(rb[:4])
        return False, "unexpected response " + " ".join(rb[:4])
    raw = sent_stream.replace(" ", "").upper()
    n = _echo_len(raw)
    sent_echo = [raw[2 + 2 * i: 4 + 2 * i] for i in range(n)]
    if n and len(raw) >= 2 + 2 * n and rb[1:1 + n] != sent_echo:
        return False, "out-of-sync response " + " ".join(rb[:4])
    return True, ""


# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------
def looks_abnormal(param_name, value):
    """Heuristic: does this decoded value deserve the user's attention?

    Text values are judged on their wording ("short circuit", "inhibited"...);
    numeric values only when the parameter is an inhibition/fault indicator
    and the value is not 0.
    """
    if value is None:
        return False
    value = str(value).strip()
    if not value:
        return False
    text = _CODE_PREFIX.sub("", value)
    if _FAULTY_NAME.search(param_name or "") and text.lower() in _YES:
        return True    # e.g. "compressor inhibitions = yes"
    if _HAS_WORDS.search(text):
        return bool(_FAULT_VALUE.search(text)) and not _NEGATED_VALUE.search(text)
    return bool(_FAULTY_NAME.search(param_name or "")) and not _NEUTRAL_VALUE.match(value)


# --------------------------------------------------------------------------
# Request selection
# --------------------------------------------------------------------------
def _request_inputs(request):
    return list(request.sendbyte_dataitems.keys())


def is_standard_ident_request(request):
    raw = request.sentbytes.upper()
    sid = raw[:2]
    if _NOT_IDENT_NAME.search(request.name):
        return False
    return (sid == "1A"
            # standard UDS identification DIDs F180-F19F
            or (sid == "22" and raw[2:4] == "F1" and raw[4:5] in ("8", "9"))
            # Renault KWP identification local IDs
            or (sid == "21" and raw[2:4] in ("80", "81")))


def is_ident_request(request):
    if is_standard_ident_request(request):
        return True
    if _NOT_IDENT_NAME.search(request.name):
        return False
    return sid_of(request.sentbytes) in DATA_SIDS and bool(_IDENT_NAME.search(request.name))


def _plain_read(request):
    """A read we can send as-is: data service, no inputs, not manual-only."""
    return (sid_of(request.sentbytes) in DATA_SIDS
            and not request.sendbyte_dataitems
            and not request.manualsend
            and len(request.dataitems) > 0)


@dataclass
class ReadSelection:
    request: object
    #: parameter names to display (subset of request.dataitems)
    params: List[str]


def select_parameter_reads(ecufile, profile, primary):
    out = []
    for name in sorted(ecufile.requests):
        req = ecufile.requests[name]
        if not _plain_read(req) or is_ident_request(req):
            continue
        if primary or profile.matches(name):
            out.append(ReadSelection(req, sorted(req.dataitems.keys())))
            continue
        params = sorted(k for k in req.dataitems if profile.matches(k))
        if params:
            out.append(ReadSelection(req, params))
    return out


def select_ident_reads(ecufile, limit=40):
    """Standard identification requests; name-matched ones only as fallback."""
    reads = [ecufile.requests[n] for n in sorted(ecufile.requests)
             if _plain_read(ecufile.requests[n])]
    chosen = [r for r in reads if is_standard_ident_request(r)] or \
             [r for r in reads if is_ident_request(r)]
    return [ReadSelection(r, sorted(r.dataitems.keys())) for r in chosen[:limit]]


def select_actions(ecufile, profile, primary, include_writes=False):
    out = []
    for name in sorted(ecufile.requests):
        req = ecufile.requests[name]
        sid = sid_of(req.sentbytes)
        if sid in ACTION_SIDS:
            if _SKIP_ACTION_NAME.search(name) or is_template_action(req):
                continue
        elif not (include_writes and sid in CONFIG_WRITE_SIDS):
            continue
        relevant = primary or profile.matches(name) or any(
            profile.matches(k) for k in req.sendbyte_dataitems)
        if relevant:
            out.append(req)
    return out


def find_dtc_request(ecufile):
    reqs = ecufile.requests
    for cand in ("ReadDTCInformation.ReportDTC", "ReadDTC", "Read_DTC",
                 "ReadDTCInformation", "ReportDTC"):
        if cand in reqs and sid_of(reqs[cand].sentbytes) in DTC_SIDS:
            return reqs[cand]
    for name, req in sorted(reqs.items()):
        low = name.lower()
        if sid_of(req.sentbytes) in DTC_SIDS and "dtc" in low and "read" in low \
                and "extended" not in low and "snapshot" not in low:
            return req
    return None


def find_clear_dtc_request(ecufile):
    for name in ("ClearDiagnosticInformation.All", "ClearDTC",
                 "Clear Diagnostic Information"):
        if name in ecufile.requests:
            return ecufile.requests[name]
    for name, req in sorted(ecufile.requests.items()):
        if sid_of(req.sentbytes) in ("14", "04"):
            return req
    return None


def pick_session(ecufile):
    """Diagnostic session used for this ECU, mirroring the main window."""
    if ecufile.ecu_protocol.upper() == "ISO8" or ecufile.funcaddr == "00":
        return None
    for name, req in sorted(ecufile.requests.items()):
        up = name.upper()
        if "START" in up and "DIAG" in up and "SESSION" in up and "EXTENDED" in up \
                and not req.sendbyte_dataitems:
            stream = req.sentbytes.upper()
            if classify_stream(stream) == RISK_SESSION:
                return stream
    return "10C0"


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------
def decode_values(ecufile, request, response, params):
    """Decode ``params`` of ``request`` from a positive response."""
    values = {}
    for k in params:
        di = request.dataitems.get(k)
        data = ecufile.data.get(k)
        if di is None or data is None:
            values[k] = None
            continue
        try:
            values[k] = data.getDisplayValue(response, di, ecufile.endianness)
        except Exception:
            values[k] = None
    return values


@dataclass
class Dtc:
    code: str
    description: str
    status: str
    active: bool
    fields: Dict[str, str] = field(default_factory=dict)


_STATUS_ITEM = re.compile(r"^DTCStatus\.|^Status", re.I)
_SKIP_DTC_ITEM = re.compile(r"^(NDTC|DTCStatusAvailabilityMask|StatusOfDTC)$", re.I)
_YES = {"yes", "oui", "1", "true", "active", "actif", "activated", "activé"}


def decode_dtcs(ecufile, request, response):
    """Split a DTC response into records using the request's own layout."""
    rb = response_bytes(response)
    shift = int(request.shiftbytescount or 0)
    if not rb or shift <= 0:
        return []

    last_byte = 0
    for k, di in request.dataitems.items():
        data = ecufile.data.get(k)
        nbytes = max(1, int(getattr(data, "bytescount", 1) or 1)) if data else 1
        last_byte = max(last_byte, int(di.firstbyte) + nbytes - 1)
    header = max(1, last_byte - shift)   # bytes before the first record
    record_items = {k: di for k, di in request.dataitems.items()
                    if int(di.firstbyte) > header}

    out = []
    count = (len(rb) - header) // shift
    for i in range(min(count, 255)):
        record = rb[header + i * shift: header + (i + 1) * shift]
        if all(b == "00" for b in record) or all(b == "FF" for b in record):
            continue
        stream = " ".join(rb[:header] + record)
        fields, desc, flags = {}, [], []
        for k in sorted(record_items, key=lambda n: (int(record_items[n].firstbyte), n)):
            data = ecufile.data.get(k)
            if data is None:
                continue
            try:
                val = data.getDisplayValue(stream, record_items[k], ecufile.endianness)
            except Exception:
                val = None
            if val is None:
                continue
            fields[k] = val
            if _SKIP_DTC_ITEM.match(k):
                continue
            if _STATUS_ITEM.match(k):
                if str(val).strip().lower() in _YES:
                    flags.append(k.split(".", 1)[-1])
            elif data.lists and val in data.lists.values():
                desc.append(str(val))
        active = any(f.lower() in ("testfailed", "testfailedthismonitoringcycle")
                     for f in flags)
        code = "".join(record[:min(3, max(1, shift - 1))])
        out.append(Dtc(code=code, description=" / ".join(dict.fromkeys(desc)),
                       status=", ".join(flags), active=active, fields=fields))
    return out


# --------------------------------------------------------------------------
# ECUs from the "Detected ECUs" panel
# --------------------------------------------------------------------------
@dataclass
class EcuTarget:
    label: str
    name: str
    path: Optional[str]
    match: str
    address: str = ""
    group: str = ""


def detected_ecus(main_window, ecus_dir="ecus/"):
    """Resolve the entries of the Detected ECUs list like ``changeECU`` does."""
    out = []
    scan = main_window.ecu_scan
    namemap = getattr(main_window, "ecunamemap", {}) or {}
    lw = main_window.treeview_ecu
    for i in range(lw.count()):
        label = lw.item(i).text()
        ecu, match = None, MATCH_UNKNOWN
        if label in scan.ecus:
            ecu, match = scan.ecus[label], MATCH_EXACT
        elif label in scan.approximate_ecus:
            ecu, match = scan.approximate_ecus[label], MATCH_APPROX
        elif label in namemap:
            ecu, match = scan.ecu_database.getTarget(namemap[label]), MATCH_UNVERIFIED
        else:
            ecu, match = scan.ecu_database.getTarget(label), MATCH_UNVERIFIED
        if ecu is None:
            path, match = (label if label.endswith(".json") else None), MATCH_UNKNOWN
            out.append(EcuTarget(label, label, path, match))
            continue
        out.append(EcuTarget(label, ecu.name, ecus_dir + ecu.href, match,
                             str(ecu.addr), ecu.group))
    return out


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
class Transport:
    """Thin wrapper over the ELM link that enforces the safety model."""

    def __init__(self, options, log=None):
        self.options = options
        self.log = log or (lambda msg: None)
        self.current = None

    @property
    def simulated(self):
        return bool(self.options.simulation_mode) or self.options.elm is None

    def connect(self, ecufile, canline=0):
        self.current = ecufile
        if self.simulated:
            return True
        return bool(ecufile.connect_to_hardware(canline))

    def start_session(self, ecufile, stream=None):
        stream = stream if stream is not None else pick_session(ecufile)
        if not stream:
            return True
        if classify_stream(stream) != RISK_SESSION:
            raise UnsafeCommandError("session %s not allowed" % stream)
        if self.simulated:
            return True
        proto = ecufile.ecu_protocol.upper()
        if proto == "CAN":
            return self.options.elm.start_session_can(stream)
        if proto == "KWP2000":
            return self.options.elm.start_session_iso(stream)
        return True

    def send(self, stream, request=None, allow_unsafe=False):
        stream = stream.replace(" ", "").upper()
        risk = classify_stream(stream)
        if risk not in (RISK_READ, RISK_SESSION) and not allow_unsafe:
            raise UnsafeCommandError(
                "%s command %s requires user confirmation" % (risk, stream))
        spaced = " ".join(stream[i:i + 2] for i in range(0, len(stream), 2))
        if self.simulated:
            if request is not None and request.replybytes:
                rb = request.replybytes.replace(" ", "")
                return " ".join(rb[i:i + 2] for i in range(0, len(rb), 2))
            if risk == RISK_READ:
                return "7F %s 11" % stream[:2]
            return "%02X" % ((int(stream[:2], 16) + 0x40) & 0xFF) + " " + spaced[3:]
        # never use the ELM cache: it is keyed by request only, not by ECU
        resp = self.options.elm.request(spaced, cache=False)
        if risk == RISK_READ:
            resp = self._settle_read(spaced, resp)
        return resp

    def _settle_read(self, spaced, resp, retries=3, timeout_ms=2000):
        """Re-send a read whose answer is pending (NRC 78) or belongs to an
        earlier request, with a longer timeout. Only reads are retried."""
        def unsettled(r):
            if is_pending(r):
                return True
            return check_response(spaced, r)[1].startswith("out-of-sync")

        if not unsettled(resp):
            return resp
        elm = self.options.elm
        self.log("ECU busy or out of sync on %s - retrying" % spaced.replace(" ", ""))
        try:
            elm.set_can_timeout(timeout_ms)
        except Exception:
            pass
        try:
            for _ in range(retries):
                resp = elm.request(spaced, cache=False)
                if not unsettled(resp):
                    break
        finally:
            try:
                elm.set_can_timeout(self.options.cantimeout)
            except Exception:
                pass
        return resp
