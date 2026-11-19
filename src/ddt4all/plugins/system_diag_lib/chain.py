# -*- coding: utf-8 -*-
"""
Guided check: follows a system's control chain across ECUs and produces one
verdict, plus generic findings that apply to any car and any fault.

Two kinds of evidence are combined:

* the profile's **chain** - ordered checkpoints ("climate unit requests the
  compressor" -> "engine ECU receives the request" -> ...). The first failing
  checkpoint locates the fault; later dependent ones are "not reached".
  Checkpoints marked ``independent`` (refrigerant pressure, battery voltage...)
  are always evaluated and reported on their own.
* **generic findings**, independent of the profile's signal names: ECUs that
  carry another car's VIN, active communication faults (U-codes),
  configuration/programming faults, other active fault codes, fault
  indicators with a non-zero value, database files that do not match.

Signal names are matched with regexes because they differ between ECU
database versions; a checkpoint whose signals do not exist in the car's
database files is reported as "not available", never as a failure.
No Qt in here.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional

from ddt4all.plugins.system_diag_lib import engine as E

OK = "ok"
FAIL = "fail"
NA = "n/a"            # signal not in the database file / not readable
WAIT = "waiting"      # precondition not met
NOT_REACHED = "not reached"

SEV_HIGH, SEV_MED, SEV_LOW = 3, 2, 1

# --------------------------------------------------------------------------
# Value interpretation
# --------------------------------------------------------------------------
_CODE_PREFIX = re.compile(r"^\s*[0-9A-Fa-f]{1,4}\s*[:=-]\s*(?=\D)")
_OFF = re.compile(
    r"^\s*(?:no|non|none|false|off|0+|absent|without|unavailable)\b|\bnot\b|\bpas\b|"
    r"\bno\s|deactiv|d[ée]sactiv|inactiv|\boff\b|absent", re.I)
_ON = re.compile(
    r"^\s*(?:yes|oui|true|on|1|with)\b|request|activ|authori[sz]|present|engaged|"
    r"\bon\b|running|enabled|forbid|inhib|interdi", re.I)


def as_bool(value):
    """'Air Conditioning Requested' -> True, 'ACComp not authorized' -> False,
    '0' -> False, '3' -> True, unknown text -> None."""
    if value is None:
        return None
    text = _CODE_PREFIX.sub("", str(value)).strip()
    if not text:
        return None
    num = as_number(text)
    if num is not None and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return num != 0
    if _OFF.search(text):
        return False
    if _ON.search(text):
        return True
    return None


def as_number(value):
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


@dataclass
class Expect:
    """What a signal must look like for the checkpoint to pass."""
    kind: str                      # "on" | "off" | "range" | "match"
    low: Optional[float] = None
    high: Optional[float] = None
    pattern: Optional[str] = None

    def test(self, value):
        """True / False, or None when the value cannot be interpreted."""
        if self.kind == "on":
            return as_bool(value)
        if self.kind == "off":
            b = as_bool(value)
            return None if b is None else not b
        if self.kind == "range":
            n = as_number(value)
            if n is None:
                return None
            return (self.low is None or n >= self.low) and (self.high is None or n <= self.high)
        if self.kind == "match":
            return bool(re.search(self.pattern, str(value), re.I)) if value is not None else None
        if self.kind == "nomatch":
            return not re.search(self.pattern, str(value), re.I) if value is not None else None
        raise ValueError(self.kind)

    def describe(self):
        if self.kind == "range":
            return "%s..%s" % ("" if self.low is None else "%g" % self.low,
                               "" if self.high is None else "%g" % self.high)
        return self.kind if self.kind != "match" else self.pattern


ON, OFF = Expect("on"), Expect("off")


def in_range(low=None, high=None):
    return Expect("range", low, high)


def matches(pattern):
    return Expect("match", pattern=pattern)


def not_matching(pattern):
    return Expect("nomatch", pattern=pattern)


# --------------------------------------------------------------------------
# Profile building blocks
# --------------------------------------------------------------------------
@dataclass
class Signal:
    """A value of one ECU role, by name patterns (first that exists wins)."""
    role: str
    patterns: List[str]


@dataclass
class Condition:
    #: alternatives: the first signal found in the car's database files is used
    signals: List[Signal]
    expect: Expect
    label: str = ""


@dataclass
class Check:
    id: str
    title: str
    conditions: List[Condition]
    fail: str                       # plain-language statement when it fails
    action: str                     # what to do about it
    precondition: bool = False      # test conditions (engine running...)
    independent: bool = False       # evaluated even after an earlier failure
    receiver_role: Optional[str] = None   # ECU that should have received something


@dataclass
class ActiveTest:
    title: str
    role: str
    start_patterns: List[str]       # request names, first existing wins
    exclude: str
    duration_s: int
    watch: List[Condition]          # read on the same ECU during the test


@dataclass
class RoleDef:
    key: str                        # "hvac", used by Signal.role
    name: str                       # "climate unit", shown to the user
    addresses: List[str]
    name_pattern: Optional[str] = None


# --------------------------------------------------------------------------
# Signal resolution
# --------------------------------------------------------------------------
def _readable_items(ecufile):
    """{data item name: request} for plain reads (no inputs, data services)."""
    out = {}
    for rname in sorted(ecufile.requests):
        req = ecufile.requests[rname]
        if E.sid_of(req.sentbytes) not in E.DATA_SIDS or req.sendbyte_dataitems or req.manualsend:
            continue
        for item in req.dataitems:
            out.setdefault(item, req)
    return out


def resolve(ecufile, patterns, cache=None):
    """(request, item name) of the first pattern that matches a readable item."""
    if ecufile is None:
        return None
    items = cache if cache is not None else _readable_items(ecufile)
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for name in items:
            if rx.search(name):
                return items[name], name
    return None


def resolve_action(ecufile, patterns, exclude):
    if ecufile is None:
        return None
    ex = re.compile(exclude, re.I) if exclude else None
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for name in sorted(ecufile.requests):
            req = ecufile.requests[name]
            if E.sid_of(req.sentbytes) in E.ACTION_SIDS and rx.search(name) \
                    and not (ex and ex.search(name)) and not req.sendbyte_dataitems:
                return req
    return None


# --------------------------------------------------------------------------
# ECU roles and duplicates
# --------------------------------------------------------------------------
def assign_roles(targets, roles, score=None):
    """{role name: target}. Several entries for one address (e.g. the same BCM
    loaded with two database files) are reduced to the best one: exact match
    first, then the highest ``score(target)`` (e.g. resolvable signals).
    Returns (mapping, skipped duplicates as (skipped, kept))."""
    rank = {E.MATCH_EXACT: 3, E.MATCH_APPROX: 2, E.MATCH_UNVERIFIED: 1, E.MATCH_UNKNOWN: 0}
    by_addr = {}
    for t in targets:
        if not t.path:
            continue
        key = (t.address or t.label).upper()
        by_addr.setdefault(key, []).append(t)
    kept, skipped = {}, []
    for key, group in by_addr.items():
        best = max(group, key=lambda t: (rank.get(t.match, 0), score(t) if score else 0,
                                         -group.index(t)))
        kept[key] = best
        skipped += [(t, best) for t in group if t is not best]
    mapping = {}
    for role in roles:          # diagnostic address first, name as fallback
        addrs = [a.upper() for a in role.addresses]
        hit = next((t for key, t in kept.items() if key in addrs), None)
        if hit is None and role.name_pattern:
            hit = next((t for t in kept.values()
                        if t not in mapping.values()
                        and re.search(role.name_pattern, t.group + " " + t.name, re.I)), None)
        if hit is not None:
            mapping[role.key] = hit
    return mapping, kept, skipped


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
@dataclass
class CheckResult:
    check: Check
    state: str
    details: List[str] = field(default_factory=list)   # "OH_ACCompRequest = Requested"
    ecus: List[str] = field(default_factory=list)


def evaluate_check(check, read):
    """``read(signal)`` -> (ecu label, item name, value) or None if unavailable,
    value None if the ECU did not answer."""
    details, verdicts, ecus = [], [], []
    for cond in check.conditions:
        got = None
        for sig in cond.signals:
            got = read(sig)
            if got is not None:
                break
        if got is None:
            continue
        label, item, value = got
        if label not in ecus:
            ecus.append(label)
        details.append("%s = %s" % (item, "no answer" if value is None else value))
        if value is None:
            continue
        verdicts.append(cond.expect.test(value))
    known = [v for v in verdicts if v is not None]
    if not known:
        return CheckResult(check, NA, details, ecus)
    return CheckResult(check, OK if all(known) else FAIL, details, ecus)


def evaluate_chain(checks, read):
    results, failed_dependent, pre_ok = [], False, True
    for chk in checks:
        res = evaluate_check(chk, read)
        if chk.precondition:
            if res.state == FAIL:
                res.state, pre_ok = WAIT, False
        elif failed_dependent and not chk.independent and res.state != NA:
            res.state = NOT_REACHED
        elif res.state == FAIL and not chk.independent:
            failed_dependent = True
        results.append(res)
    if not pre_ok:
        # without the test conditions, chain failures mean nothing yet
        for res in results:
            if not res.check.precondition and not res.check.independent \
                    and res.state in (FAIL, NOT_REACHED):
                res.state = WAIT
    return results, pre_ok


# --------------------------------------------------------------------------
# Generic findings (any system, any car)
# --------------------------------------------------------------------------
@dataclass
class Finding:
    severity: int
    ecu: str
    text: str
    action: str = ""
    kind: str = ""        # vin | network | config | dtc | indicator | database


_VIN_RX = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def clean_vin(value):
    v = re.sub(r"\s", "", str(value or "")).upper()
    return v if _VIN_RX.match(v) and not re.fullmatch(r"(0|F)+", v) else None


def vin_findings(vins, reference_label=None):
    """vins: {ecu label: vin}. The reference is the body ECU's VIN when known,
    otherwise the most common one."""
    vins = {k: v for k, v in vins.items() if v}
    if len(vins) < 2:
        return [], None
    ref = vins.get(reference_label)
    if ref is None:
        (common, count), = Counter(vins.values()).most_common(1)
        if count < 2:
            # no body ECU VIN and no majority: report the disagreement only
            return [Finding(SEV_HIGH, ", ".join(sorted(vins)),
                            "the modules carry different VINs (%s): some come from other vehicles."
                            % ", ".join("%s %s" % (k, v) for k, v in sorted(vins.items())),
                            "Check which VIN is the car's (registration papers / windscreen).",
                            "vin")], None
        ref = common
    out = []
    for label, vin in vins.items():
        if vin != ref:
            out.append(Finding(
                SEV_HIGH, label,
                "carries VIN %s, the car is %s: this module comes from another vehicle." % (vin, ref),
                "A used module usually needs the software and configuration for this car "
                "(dealer reprogramming by VIN), not only the same hardware.", "vin"))
    return out, ref


_NETWORK_TEXT = re.compile(
    r"lost communication|missing message|bus.?off|\bCAN\b|frame|trame|absent|"
    r"invalid serial data|bus signal|message failure", re.I)
_CONFIG_TEXT = re.compile(
    r"not configured|config|programm|calibration|coding|codage|not learn|apprent", re.I)


def is_network_dtc(dtc):
    """U-codes (top bits 11 -> first hex digit C..F) or network wording."""
    return dtc.code[:1].upper() in "CDEF" or bool(_NETWORK_TEXT.search(dtc.description))


def is_config_dtc(dtc):
    ft = dtc.code[4:6].upper() if len(dtc.code) >= 6 else ""
    return ft in ("51", "54", "55", "56") or bool(_CONFIG_TEXT.search(dtc.description))


def dtc_findings(label, dtcs, related, in_chain):
    """related(dtc) -> bool: belongs to the system. in_chain: ECU has a role."""
    out = []
    for d in dtcs:
        confirmed = "confirmedDTC" in d.status
        if not (d.active or confirmed):
            continue          # old / not re-tested: shown in the details only
        text = "%s %s%s" % (d.code, d.description or "(no description)",
                            " - ACTIVE" if d.active else "")
        if is_network_dtc(d) and in_chain:
            out.append(Finding(SEV_HIGH if d.active else SEV_MED, label,
                               "communication fault: " + text,
                               "This module misses or rejects messages from another module "
                               "(wiring, missing module, or software expecting another vehicle setup).",
                               "network"))
        elif is_config_dtc(d) and (in_chain or related(d)):
            out.append(Finding(SEV_HIGH if d.active or confirmed else SEV_MED, label,
                               "configuration fault: " + text,
                               "The module's configuration does not match the car. Compare its "
                               "configuration with the correct one for this VIN.", "config"))
        elif related(d):
            out.append(Finding(SEV_HIGH if d.active else SEV_MED, label, "fault: " + text,
                               "Check the component named in the fault.", "dtc"))
        elif d.active and in_chain:
            out.append(Finding(SEV_LOW, label, "other active fault: " + text, "", "dtc"))
    return out


_INDICATOR_NAME = re.compile(
    r"diag_?(?:oc|sc|ov|uv)|default$|default_|defect|fault|failure|d[ée]faut|inhib|"
    r"_na$|notavailable|absent", re.I)
#: configuration, thresholds and *requests to other modules* are not faults
_INDICATOR_SKIP = re.compile(
    r"^C[HL]_|_CF$|config|threshold|seuil|enable|GADE|DiagEnable|req(uest)?$|need$|counter|"
    r"compteur|memor|stored|eeprom", re.I)


def indicator_items(ecufile):
    """Readable fault indicators of an ECU (``VL_BlowerControlDiag_OCFM``...)."""
    return {n: r for n, r in _readable_items(ecufile).items()
            if _INDICATOR_NAME.search(n) and not _INDICATOR_SKIP.search(n)}


def indicator_abnormal(name, value):
    if value is None:
        return False
    b = as_bool(value)
    if re.search(r"absent|_na$|notavailable|inhib|default|defect|fault|failure|"
                 r"d[ée]faut|diag", name, re.I):
        return bool(b) if b is not None else E.looks_abnormal(name, value)
    return E.looks_abnormal(name, value)


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
@dataclass
class Verdict:
    headline: str
    explanation: str
    action: str
    level: str              # "fault" | "ok" | "waiting" | "unknown"
    findings: List[Finding]


def build_verdict(results, pre_ok, findings, role_labels, instructions=""):
    """role_labels: {role: ecu label}. Findings are generic (VIN, faults...)."""
    findings = sorted(findings, key=lambda f: -f.severity)
    first_fail = next((r for r in results if r.state == FAIL and not r.check.independent), None)
    independent_fails = [r for r in results if r.state == FAIL and r.check.independent]
    for r in independent_fails:
        findings.insert(0, Finding(SEV_HIGH, ", ".join(r.ecus), r.check.fail,
                                   r.check.action, "check"))
    if not pre_ok:
        waiting = [r.check.title for r in results if r.state == WAIT]
        return Verdict("Waiting for test conditions",
                       "Not met: " + ", ".join(waiting) + ". " + instructions,
                       "", "waiting", findings)
    if first_fail is None:
        if not any(r.state == OK for r in results if not r.check.precondition):
            return Verdict("Not enough data",
                           "None of the checkpoints could be read from this car's ECUs.",
                           "Use 'Details' for a full scan.", "unknown", findings)
        if findings:
            return Verdict("Control chain OK - other problems found",
                           "The request goes through every checkpoint. See the problems below.",
                           findings[0].action, "fault", findings)
        return Verdict("No fault found",
                       "The request goes through every checkpoint and no fault is stored.",
                       "If it still does not work, use the direct test to check the hardware.",
                       "ok", findings)
    chk = first_fail.check
    explanation = chk.fail
    receiver = role_labels.get(chk.receiver_role) if chk.receiver_role else None
    action = chk.action
    if receiver:
        phrases = {"vin": "carries another car's VIN",
                   "network": "reports communication faults",
                   "config": "reports configuration faults"}
        kinds = [k for k in ("vin", "network", "config")
                 if any(f.ecu == receiver and f.kind == k for f in findings)]
        if kinds:
            explanation += " %s also %s. Likely cause: its software or configuration does not " \
                           "match this car." % (receiver, " and ".join(phrases[k] for k in kinds))
            action = "Have %s reprogrammed with the software for this car's VIN (dealer), " \
                     "then recheck." % receiver
    return Verdict(chk.title + " - FAILED", explanation, action, "fault", findings)
