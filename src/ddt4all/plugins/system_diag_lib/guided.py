# -*- coding: utf-8 -*-
"""
Guided check runner (no Qt): reads what the profile's chain needs from the
car, plus generic evidence (VINs, fault codes, fault indicators), and builds
the verdict. The UI calls ``baseline()`` once and ``cycle()`` repeatedly.
"""

import time

from ddt4all.plugins.system_diag_lib import chain as C
from ddt4all.plugins.system_diag_lib import engine as E

VIN_PATTERNS = [r"^VIN$", r"^V\.I\.N\. in ASCII$", r"^Vin In Ascii$", r"^n° VIN$"]
#: abort the direct test above this refrigerant pressure (bar)
MAX_TEST_PRESSURE = 28.0


def tester_present_stream(ecufile):
    for name, req in sorted(ecufile.requests.items()):
        if "tester" in name.lower() and "present" in name.lower() \
                and E.classify_stream(req.sentbytes) == E.RISK_READ:
            return req.sentbytes
    return "3E00" if ecufile.ecu_protocol.upper() == "CAN" else "3E01"


class GuidedRun:
    def __init__(self, profile, targets, load_ecufile, transport, log=None, canline=0):
        self.profile = profile
        self.transport = transport
        self.load = load_ecufile
        self.log = log or (lambda m, c=None: None)
        self.canline = canline
        self.cancelled = False

        self._items = {}            # label -> {item: request} cache
        self._resolved = {}         # (label, patterns) -> (request, item) | None
        self.roles, self.kept, self.skipped = C.assign_roles(
            targets, profile.roles, score=self._score)
        self.role_labels = {k: t.label for k, t in self.roles.items()}
        self.role_names = {r.key: r.name for r in profile.roles}

        self.vins, self.dtcs, self.dtc_errors = {}, {}, {}
        self.indicators = {}        # label -> {item: value}
        self.values = {}            # (label, item) -> value | None (no answer)
        self.asked, self.answered = {}, {}
        self.results, self.pre_ok, self.verdict = [], True, None

    # ---------------------------------------------------------- resolution
    def ecufile(self, target):
        return self.load(target) if target is not None else None

    def _readable(self, target):
        if target.label not in self._items:
            ef = self.ecufile(target)
            self._items[target.label] = C._readable_items(ef) if ef is not None else {}
        return self._items[target.label]

    def resolve(self, target, patterns):
        key = (target.label, tuple(patterns))
        if key not in self._resolved:
            self._resolved[key] = C.resolve(self.ecufile(target), patterns,
                                            self._readable(target))
        return self._resolved[key]

    def _score(self, target):
        """Signals of the chain this database file can read (picks the best
        file when one ECU is listed twice)."""
        if self.load(target) is None:
            return -1
        n = 0
        for chk in self.profile.chain:
            for cond in chk.conditions:
                for sig in cond.signals:
                    if self.resolve(target, sig.patterns):
                        n += 1
        return n

    def chain_reads(self):
        """{label: (target, {request name: (request, [items])})} for one cycle."""
        plan = {}
        for chk in self.profile.chain:
            for cond in chk.conditions:
                for sig in cond.signals:
                    tgt = self.roles.get(sig.role)
                    hit = tgt and self.resolve(tgt, sig.patterns)
                    if not hit:
                        continue
                    req, item = hit
                    reqs = plan.setdefault(tgt.label, (tgt, {}))[1]
                    reqs.setdefault(req.name, (req, []))
                    if item not in reqs[req.name][1]:
                        reqs[req.name][1].append(item)
        return plan

    # -------------------------------------------------------------- bus I/O
    def _connect(self, target):
        ef = self.ecufile(target)
        if ef is None or not self.transport.connect(ef, self.canline):
            return None
        try:
            self.transport.start_session(ef)
        except E.UnsafeCommandError:
            return None
        return ef

    def _read(self, ef, req, items):
        resp = self.transport.send(req.sentbytes, req)
        ok, msg = E.check_response(req.sentbytes, resp)
        if not ok:
            return None, msg
        return E.decode_values(ef, req, resp, items), ""

    def _read_ecu(self, target, reads, tick=None):
        """reads: {name: (request, [items])}. Values go to self.values."""
        ef = self._connect(target)
        out = {}
        for req, items in reads.values():
            if self.cancelled:
                break
            vals = None
            if ef is not None:
                vals, _ = self._read(ef, req, items)
            self.asked[target.label] = self.asked.get(target.label, 0) + 1
            if vals is not None:
                self.answered[target.label] = self.answered.get(target.label, 0) + 1
            for it in items:
                v = vals.get(it) if vals else None
                self.values[(target.label, it)] = v
                out[it] = v
            if tick:
                tick()
        return out

    # ------------------------------------------------------- duplicates
    def _probe_reads(self, target):
        """Chain signals and VIN this target's database file can read."""
        reads = {}
        pattern_sets = [sig.patterns for chk in self.profile.chain
                        for cond in chk.conditions for sig in cond.signals] + [VIN_PATTERNS]
        for patterns in pattern_sets:
            hit = self.resolve(target, patterns)
            if hit:
                req, item = hit
                entry = reads.setdefault(req.name, (req, []))
                if item not in entry[1]:
                    entry[1].append(item)
        return reads

    def probe_duplicates(self, tick=None):
        """One module listed with several database files (e.g. an old UCH file
        and a newer BCM file): keep the file the module actually answers best.
        The database contents alone cannot decide this."""
        groups = {}
        for t in list(self.kept.values()) + [s for s, _k in self.skipped]:
            groups.setdefault((t.address or t.label).upper(), []).append(t)
        for key, cands in groups.items():
            if len(cands) < 2 or self.cancelled:
                continue
            scores = {}
            for t in cands:
                ef, n = self._connect(t), 0
                for req, items in self._probe_reads(t).values():
                    if ef is not None and self._read(ef, req, items)[0] is not None:
                        n += 1
                    if tick:
                        tick()
                scores[t.label] = n
            old = self.kept[key]
            best = max(cands, key=lambda t: (scores[t.label], t is old))
            self.log("Same module, several files: %s" % ", ".join(
                "%s answered %d" % (lbl, n) for lbl, n in scores.items()))
            if best is not old:
                self.kept[key] = best
                for role, t in list(self.roles.items()):
                    if t is old:
                        self.roles[role] = best
            self.skipped = [(s, k) for s, k in self.skipped
                            if (k.address or k.label).upper() != key]
            self.skipped += [(t, best) for t in cands if t is not best]
        self.role_labels = {k: t.label for k, t in self.roles.items()}

    # ------------------------------------------------------------- baseline
    def baseline(self, tick=None):
        """Once per run: VIN and fault codes of every ECU, fault indicators of
        the ECUs owning the system."""
        self.probe_duplicates(tick)
        for target in self.kept.values():
            if self.cancelled:
                return
            self.log("Reading %s" % target.label)
            ef = self._connect(target)
            if ef is None:
                self.dtc_errors[target.label] = "no connection"
                continue
            hit = self.resolve(target, VIN_PATTERNS)
            if hit:
                vals, _ = self._read(ef, hit[0], [hit[1]])
                if vals:
                    self.vins[target.label] = C.clean_vin(vals.get(hit[1]))
            dreq = E.find_dtc_request(ef)
            if dreq is None:
                self.dtc_errors[target.label] = "no fault-code request in database file"
            else:
                resp = self.transport.send(dreq.sentbytes, dreq)
                ok, msg = E.check_response(dreq.sentbytes, resp)
                if ok:
                    self.dtcs[target.label] = E.decode_dtcs(ef, dreq, resp)
                else:
                    self.dtc_errors[target.label] = msg
            if tick:
                tick()
            if self._is_primary(target):
                planned = self.chain_reads().get(target.label, (None, {}))[1]
                chain_items = {it for _req, items in planned.values() for it in items}
                reads = {}
                for item, req in C.indicator_items(ef).items():
                    if item in chain_items:
                        continue
                    reads.setdefault(req.name, (req, []))[1].append(item)
                vals = self._read_ecu(target, reads, tick)
                self.indicators[target.label] = vals

    def _is_primary(self, target):
        return self.profile.is_primary_ecu(target.address, target.group, target.name)

    # ---------------------------------------------------------------- cycle
    def cycle(self, tick=None):
        for label, (target, reads) in self.chain_reads().items():
            if self.cancelled:
                return
            self._read_ecu(target, reads, tick)
        self.evaluate()

    def read_signal(self, sig):
        tgt = self.roles.get(sig.role)
        hit = tgt and self.resolve(tgt, sig.patterns)
        if not hit or (tgt.label, hit[1]) not in self.values:
            return None
        return (tgt.label, hit[1], self.values[(tgt.label, hit[1])])

    def findings(self):
        out = []
        f, ref = C.vin_findings(self.vins, self.role_labels.get(self.profile.reference_role))
        out += f
        role_set = set(self.role_labels.values())
        for label, dtcs in self.dtcs.items():
            tgt = next((t for t in self.kept.values() if t.label == label), None)
            primary = tgt is not None and self._is_primary(tgt)

            def related(d, primary=primary):
                return primary or self.profile.matches(d.description)
            out += C.dtc_findings(label, dtcs, related, label in role_set)
        for label, vals in self.indicators.items():
            bad = ["%s = %s" % (k, v) for k, v in sorted(vals.items()) if C.indicator_abnormal(k, v)]
            if bad:
                out.append(C.Finding(C.SEV_MED, label,
                                     "fault indicators set: " + "; ".join(bad[:6]) +
                                     (" (+%d more)" % (len(bad) - 6) if len(bad) > 6 else ""),
                                     "These are the module's own fault flags; check the "
                                     "components named.", "indicator"))
        for label in role_set:
            asked, answered = self.asked.get(label, 0), self.answered.get(label, 0)
            if asked >= 3 and answered < asked / 2.0:
                out.append(C.Finding(C.SEV_MED, label,
                                     "answered only %d of %d requests: its database file "
                                     "may not match its software." % (answered, asked),
                                     "Rescan the car so ddt4all identifies the exact "
                                     "ECU version.", "database"))
        for t in self.kept.values():
            if t.label in role_set and t.match == E.MATCH_APPROX:
                out.append(C.Finding(C.SEV_LOW, t.label,
                                     "software only approximately matches its database file.",
                                     "Some values may be decoded wrongly.", "database"))
        return out

    def evaluate(self):
        self.results, self.pre_ok = C.evaluate_chain(self.profile.chain, self.read_signal)
        self.verdict = C.build_verdict(self.results, self.pre_ok, self.findings(),
                                       self.role_labels, self.profile.instructions)
        return self.verdict

    def missing_roles(self):
        return [r.name for r in self.profile.roles if r.key not in self.roles]

    # ---------------------------------------------------------- direct test
    def prepare_active_test(self):
        """(target, request, stream, risk, release) or None."""
        test = self.profile.active_test
        tgt = test and self.roles.get(test.role)
        if tgt is None:
            return None
        req = C.resolve_action(self.ecufile(tgt), test.start_patterns, test.exclude)
        if req is None:
            return None
        stream = req.sentbytes.upper()
        return tgt, req, stream, E.classify_request(req), E.release_command(stream)

    def run_active_test(self, plan, tick=None, sleep=time.sleep):
        """Runs a confirmed direct test; the release command is always sent.
        Returns plain-language result lines."""
        test = self.profile.active_test
        tgt, req, stream, _risk, release = plan
        ef = self._connect(tgt)
        if ef is None:
            return ["Cannot connect to %s." % tgt.label]
        tp = tester_present_stream(ef)
        watch = [(c.label, self.resolve(tgt, c.signals[0].patterns), c) for c in test.watch]
        watch = [(lbl, hit, c) for lbl, hit, c in watch if hit]

        def sample():
            row = {}
            for lbl, (wreq, item), _c in watch:
                vals, _ = self._read(ef, wreq, [item])
                row[lbl] = vals.get(item) if vals else None
            return row

        before = sample()
        samples, lines, aborted = [], [], False
        resp = self.transport.send(stream, req, allow_unsafe=True)
        ok, msg = E.check_response(stream, resp)
        try:
            if not ok:
                return ["%s refused the test command (%s)." % (tgt.label, msg)]
            for _ in range(test.duration_s):
                sleep(1.0)
                self.transport.send(tp)
                row = sample()
                samples.append(row)
                if tick:
                    tick()
                p = C.as_number(row.get("pressure"))
                if p is not None and p > MAX_TEST_PRESSURE:
                    aborted = True
                    lines.append("Stopped early: pressure reached %.1f bar." % p)
                    break
                if self.cancelled:
                    break
        finally:
            if release:
                rresp = self.transport.send(release, None, allow_unsafe=True)
                rok, rmsg = E.check_response(release, rresp)
                lines.append("Control returned to %s: %s." % (tgt.label, "OK" if rok else rmsg))
        return self._judge_test(before, samples, aborted) + lines

    @staticmethod
    def _judge_test(before, samples, aborted):
        out = []
        relay_on = any(C.as_bool(s.get("relay")) for s in samples)
        fan_on = any(C.as_bool(s.get("fan")) for s in samples)
        p0 = C.as_number(before.get("pressure"))
        ps = [C.as_number(s.get("pressure")) for s in samples]
        ps = [p for p in ps if p is not None]
        if "relay" in before or any("relay" in s for s in samples):
            out.append("Compressor relay: %s." % ("switched on" if relay_on else "stayed off"))
        if p0 is not None and ps:
            rise = max(ps) - p0
            out.append("Refrigerant pressure: %.1f -> max %.1f bar." % (p0, max(ps)))
            if rise >= 1.5:
                out.append("RESULT: the compressor works. The hardware is fine; the fault is in "
                           "the control chain (see the verdict).")
            elif relay_on or not any("relay" in s for s in samples):
                out.append("RESULT: commanded, but the pressure did not rise. Check the compressor "
                           "clutch, its relay and fuse, the wiring and the refrigerant charge.")
            else:
                out.append("RESULT: the engine ECU did not switch its compressor output on.")
        else:
            out.append("RESULT: no pressure reading available; check by ear/feel whether the "
                       "compressor clutch engaged and the air got colder.")
        if samples and "fan" in samples[0]:
            out.append("Cooling fan during test: %s." % ("ran" if fan_on else "did not run"))
            if not fan_on and not aborted:
                out.append("Note: without the fan, do not run the compressor for long.")
        return out
