# -*- coding: utf-8 -*-
"""
System diagnostics plugin (first profile: climate control / A/C, "clim").

Works on the ECUs listed in the main window's *Detected ECUs* panel:

* **Scan (read-only)** - for every checked ECU: identification, fault codes
  (DTC) and every system-related parameter. The ECU owning the system (HVAC,
  address 29) is read entirely; other ECUs (engine, UCH/BCM...) only for the
  requests/parameters whose name matches the profile keywords.
* **Actions** - output controls / routines (start/stop of blower, compressor
  valve, flaps...) and, on demand, configuration writes. Nothing that is not
  a read is ever sent without a confirmation showing the exact bytes; output
  controls are handed back to the ECU (``returnControlToECU``) on request and
  automatically when the dialog closes.

All non-Qt logic lives in ``system_diag_lib`` (see its docstrings).
"""

import datetime
import html
import os
import re
import time

import PyQt5.QtCore as core
import PyQt5.QtGui as qtgui
import PyQt5.QtWidgets as gui

import ddt4all.options as options
from ddt4all.core.ecu.ecu_file import EcuFile
from ddt4all.plugins.system_diag_lib import engine as E
from ddt4all.plugins.system_diag_lib.profiles import PROFILES

_ = options.translator('ddt4all')

plugin_name = _("System diagnostics (A/C - climate)")
category = _("Diagnostics")
need_hw = False

COLOR_FLAG = qtgui.QColor("#f0b429")
COLOR_ACTIVE = qtgui.QColor("#e5534b")
COLOR_STORED = qtgui.QColor("#d29922")
COLOR_MUTED = qtgui.QColor("#8b949e")
#: configuration-like parameter names, compared across ECUs in the summary
CONFIG_PARAM = re.compile(r"config|_CF\b|^C[LH]_|type|archi", re.I)

RISK_TEXT = {
    E.RISK_ACTION: _("ACTUATOR"),
    E.RISK_WRITE: _("WRITE / RESET"),
}
MATCH_HELP = {
    E.MATCH_EXACT: _("database file matches the ECU software"),
    E.MATCH_APPROX: _("APPROXIMATE: ECU software differs from the database file; "
                      "some values may decode wrongly"),
    E.MATCH_UNVERIFIED: _("not verified against the car (loaded from vehicle/catalog)"),
    E.MATCH_UNKNOWN: _("no database file found"),
}


class EcuResult:
    def __init__(self, target, ecufile, primary):
        self.target = target
        self.ecufile = ecufile
        self.primary = primary
        self.idents = []      # (request, param, value)
        self.dtcs = []        # engine.Dtc
        self.dtc_error = ""
        self.params = []      # dict rows
        self.asked = 0
        self.answered = 0
        self.error = ""


class SystemDiagDialog(gui.QDialog):
    def __init__(self, parent=None):
        super(SystemDiagDialog, self).__init__(parent)
        self.setWindowIcon(qtgui.QIcon(":icons/obd.png"))
        self.setWindowTitle(plugin_name)
        self.resize(1200, 800)

        self.transport = E.Transport(options, self.log)
        self.profile = PROFILES[0]
        self.targets = []
        self.ecufiles = {}
        self.results = {}
        self.actions = []     # (target, ecufile, request)
        self.overrides = []   # (target, ecufile, release stream, request name)
        self.cancelled = False
        self.busy = False

        self.keepalive = core.QTimer(self)
        self.keepalive.setInterval(2000)
        self.keepalive.timeout.connect(self._keep_alive)

        self._build_ui()
        self._load_targets()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = gui.QVBoxLayout(self)

        top = gui.QHBoxLayout()
        top.addWidget(gui.QLabel(_("System:")))
        self.profile_combo = gui.QComboBox()
        for p in PROFILES:
            self.profile_combo.addItem(p.name)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        top.addWidget(self.profile_combo)
        self.mode_label = gui.QLabel()
        if self.transport.simulated:
            self.mode_label.setText("<b style='color:#0969da'>" +
                                    _("SIMULATION - database default values, nothing is sent") + "</b>")
        else:
            self.mode_label.setText("<b style='color:#2ea043'>" + _("LIVE - connected to the vehicle") + "</b>")
        top.addWidget(self.mode_label)
        top.addStretch()
        root.addLayout(top)

        split = gui.QSplitter(core.Qt.Horizontal)
        left = gui.QWidget()
        lv = gui.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(gui.QLabel(_("ECUs (from 'Detected ECUs'):")))
        self.ecu_list = gui.QListWidget()
        lv.addWidget(self.ecu_list)
        self.scan_button = gui.QPushButton(_("Scan (read-only)"))
        self.scan_button.clicked.connect(self.scan)
        self.stop_button = gui.QPushButton(_("Stop"))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._cancel)
        self.export_button = gui.QPushButton(_("Export report..."))
        self.export_button.clicked.connect(self.export_report)
        for b in (self.scan_button, self.stop_button, self.export_button):
            lv.addWidget(b)
        self.progress = gui.QProgressBar()
        lv.addWidget(self.progress)
        split.addWidget(left)

        self.tabs = gui.QTabWidget()
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 4)

        # Summary
        self.summary = gui.QTextBrowser()
        self.tabs.addTab(self.summary, _("Summary"))

        # Parameters
        pw = gui.QWidget()
        pl = gui.QVBoxLayout(pw)
        fl = gui.QHBoxLayout()
        self.param_filter = gui.QLineEdit()
        self.param_filter.setPlaceholderText(_("Filter (ECU, parameter, value)..."))
        self.param_filter.textChanged.connect(self._fill_params)
        self.only_flagged = gui.QCheckBox(_("Only flagged"))
        self.only_flagged.toggled.connect(self._fill_params)
        self.hide_unanswered = gui.QCheckBox(_("Hide unanswered"))
        self.hide_unanswered.setChecked(True)
        self.hide_unanswered.toggled.connect(self._fill_params)
        fl.addWidget(self.param_filter)
        fl.addWidget(self.only_flagged)
        fl.addWidget(self.hide_unanswered)
        pl.addLayout(fl)
        self.param_table = self._table([_("ECU"), _("Parameter"), _("Value"), _("Unit"),
                                        _("Request"), _("Note")])
        pl.addWidget(self.param_table)
        self.tabs.addTab(pw, _("Parameters"))

        # Faults
        dw = gui.QWidget()
        dl = gui.QVBoxLayout(dw)
        self.dtc_table = self._table([_("ECU"), _("Code"), _("Description"), _("Status"),
                                      _("System related")])
        dl.addWidget(self.dtc_table)
        clear_button = gui.QPushButton(_("Clear DTCs of the selected ECU(s)..."))
        clear_button.clicked.connect(self.clear_dtcs)
        dl.addWidget(clear_button)
        self.tabs.addTab(dw, _("Faults (DTC)"))

        # Identification
        self.ident_table = self._table([_("ECU"), _("Database file"), _("Match"),
                                        _("Field"), _("Value")])
        self.tabs.addTab(self.ident_table, _("Identification"))

        # Actions
        aw = gui.QWidget()
        al = gui.QVBoxLayout(aw)
        info = gui.QLabel(_("Start/stop commands of the system. Each command is confirmed before "
                            "being sent. Output controls are returned to the ECU with the button "
                            "below and automatically when this window closes."))
        info.setWordWrap(True)
        al.addWidget(info)
        self.show_writes = gui.QCheckBox(_("Also list configuration writes (2E/3B) - experts only"))
        self.show_writes.toggled.connect(self._collect_actions)
        al.addWidget(self.show_writes)
        self.action_table = self._table([_("ECU"), _("Command"), _("Risk"), _("Bytes"),
                                         _("Inputs"), _("Last result")])
        self.action_table.setSelectionMode(gui.QAbstractItemView.ExtendedSelection)
        al.addWidget(self.action_table)
        ab = gui.QHBoxLayout()
        run_button = gui.QPushButton(_("Run selected..."))
        run_button.clicked.connect(self.run_selected_actions)
        self.release_button = gui.QPushButton()
        self.release_button.clicked.connect(self.release_all)
        self.keepalive_box = gui.QCheckBox(_("Keep session alive while outputs are overridden"))
        self.keepalive_box.setChecked(True)
        self.keepalive_box.setToolTip(_("Sends TesterPresent every 2 s to the ECU addressed last. "
                                        "Without it most ECUs end the override after ~5 s."))
        ab.addWidget(run_button)
        ab.addWidget(self.release_button)
        ab.addWidget(self.keepalive_box)
        ab.addStretch()
        al.addLayout(ab)
        self.tabs.addTab(aw, _("Actions (start/stop)"))
        self._update_release_button()

        vsplit = gui.QSplitter(core.Qt.Vertical)
        vsplit.addWidget(split)
        self.logview = gui.QTextEdit()
        self.logview.setReadOnly(True)
        vsplit.addWidget(self.logview)
        vsplit.setStretchFactor(0, 5)
        root.addWidget(vsplit)

    def _table(self, headers):
        t = gui.QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(gui.QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(gui.QAbstractItemView.SelectRows)
        t.horizontalHeader().setStretchLastSection(True)
        t.verticalHeader().setVisible(False)
        t.setSortingEnabled(False)
        return t

    def _add_row(self, table, values, color=None, tooltip=None, background=False):
        r = table.rowCount()
        table.insertRow(r)
        for c, v in enumerate(values):
            it = gui.QTableWidgetItem("" if v is None else str(v))
            if color is not None:
                if background:
                    it.setBackground(color)
                    it.setForeground(qtgui.QColor("black"))
                else:
                    it.setForeground(color)
            if tooltip:
                it.setToolTip(tooltip)
            table.setItem(r, c, it)
        return r

    def log(self, msg, color=None):
        text = html.escape(msg)
        if color:
            text = "<font color='%s'>%s</font>" % (color, text)
        self.logview.append(text)
        if options.main_window is not None and hasattr(options.main_window, "logview"):
            options.main_window.logview.append("[%s] %s" % (_("System diag"), text))

    # ------------------------------------------------------------- targets
    def _load_targets(self):
        self.ecu_list.clear()
        mw = options.main_window
        if mw is None or not hasattr(mw, "treeview_ecu"):
            self.targets = []
        else:
            self.targets = E.detected_ecus(mw, options.ecus_dir)
        for t in self.targets:
            primary = self.profile.is_primary_ecu(t.address, t.group, t.name, t.label)
            text = t.label + "   [" + t.match + "]"
            if primary:
                text = "★ " + text
            item = gui.QListWidgetItem(text)
            item.setFlags(item.flags() | core.Qt.ItemIsUserCheckable)
            item.setCheckState(core.Qt.Checked if t.path else core.Qt.Unchecked)
            item.setToolTip(MATCH_HELP.get(t.match, ""))
            if t.match == E.MATCH_APPROX:
                item.setForeground(qtgui.QColor("#58a6ff"))
            if not t.path:
                item.setFlags(item.flags() & ~core.Qt.ItemIsEnabled)
            self.ecu_list.addItem(item)
        if not self.targets:
            self.summary.setHtml("<h3>" + _("No ECU in the 'Detected ECUs' panel.") + "</h3><p>" +
                                 _("Scan the vehicle or load a saved vehicle first, then reopen this tool.") +
                                 "</p>")
        else:
            self.summary.setHtml("<p>" + _("★ = ECU owning the system (read entirely). "
                                           "Other ECUs are read only for system-related parameters.") +
                                 "</p><p>" + _("Press 'Scan (read-only)' to start.") + "</p>")

    def _checked_targets(self):
        out = []
        for i, t in enumerate(self.targets):
            if self.ecu_list.item(i).checkState() == core.Qt.Checked and t.path:
                out.append(t)
        return out

    def _profile_changed(self, idx):
        self.profile = PROFILES[idx]
        self._load_targets()

    def _ecufile(self, target):
        if target.path not in self.ecufiles:
            ef = EcuFile(target.path, True)
            self.ecufiles[target.path] = ef if ef.requests else None
        return self.ecufiles[target.path]

    def _is_primary(self, target, ecufile):
        return self.profile.is_primary_ecu(target.address or ecufile.funcaddr, ecufile.funcname,
                                           target.group, target.name, target.label)

    # ---------------------------------------------------------------- scan
    def _cancel(self):
        self.cancelled = True

    def _set_busy(self, busy):
        self.busy = busy
        self.scan_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    @staticmethod
    def _canline():
        combo = getattr(options.main_window, "canlinecombo", None)
        return combo.currentIndex() if combo is not None else 0

    def _connect(self, target, ecufile, session=None):
        if not self.transport.connect(ecufile, self._canline()):
            self.log(_("Cannot connect to") + " " + target.label, "red")
            return False
        try:
            self.transport.start_session(ecufile, session)
        except E.UnsafeCommandError as e:
            self.log(str(e), "red")
            return False
        return True

    def _read(self, ecufile, request):
        stream = request.sentbytes
        try:
            resp = self.transport.send(stream, request)
        except E.UnsafeCommandError as e:   # should never happen for reads
            return None, str(e)
        ok, msg = E.check_response(stream, resp)
        return (resp if ok else None), msg

    def scan(self):
        targets = self._checked_targets()
        if not targets:
            self.log(_("No ECU selected"), "orange")
            return
        if self.overrides:
            self.release_all()
        self._set_busy(True)
        self.cancelled = False
        self.results = {}
        try:
            plans = []
            for t in targets:
                ef = self._ecufile(t)
                if ef is None:
                    self.log(_("Cannot load database file for") + " " + t.label, "red")
                    continue
                primary = self._is_primary(t, ef)
                plans.append((t, ef, primary, E.select_ident_reads(ef),
                              E.select_parameter_reads(ef, self.profile, primary)))
            total = sum(len(p[3]) + len(p[4]) + 1 for p in plans) or 1
            self.progress.setRange(0, total)
            done = 0
            for t, ef, primary, idents, reads in plans:
                if self.cancelled:
                    break
                res = EcuResult(t, ef, primary)
                self.results[t.label] = res
                self.log(_("Reading") + " %s (%s, %s)" % (t.label, ef.ecu_protocol, ef.funcaddr), "blue")
                if not self._connect(t, ef):
                    res.error = _("connection failed")
                    done += len(idents) + len(reads) + 1
                    continue

                for sel in idents:
                    resp, _msg = self._read(ef, sel.request)
                    if resp is not None:
                        for k, v in E.decode_values(ef, sel.request, resp, sel.params).items():
                            if v not in (None, ""):
                                res.idents.append((sel.request.name, k, v))
                    done += 1
                    self._tick(done)
                    if self.cancelled:
                        break

                self._read_dtcs(res)
                done += 1

                for sel in reads:
                    if self.cancelled:
                        break
                    res.asked += 1
                    resp, msg = self._read(ef, sel.request)
                    if resp is not None:
                        res.answered += 1
                        values = E.decode_values(ef, sel.request, resp, sel.params)
                    else:
                        values = dict.fromkeys(sel.params)
                    for k in sel.params:
                        v = values.get(k)
                        data = ef.data.get(k)
                        res.params.append({
                            "ecu": t.label, "param": k, "value": v,
                            "unit": data.unit if data is not None and v is not None else "",
                            "request": sel.request.name, "answered": resp is not None,
                            "note": msg if resp is None else "",
                            "flagged": resp is not None and E.looks_abnormal(k, v),
                        })
                    done += 1
                    self._tick(done)
            if self.cancelled:
                self.log(_("Scan stopped by user"), "orange")
            else:
                self.log(_("Scan finished"), "green")
        finally:
            self._set_busy(False)
            self.progress.setValue(0)
            self._fill_all()

    def _tick(self, value):
        self.progress.setValue(value)
        gui.QApplication.processEvents()

    def _read_dtcs(self, res):
        ef = res.ecufile
        req = E.find_dtc_request(ef)
        res.dtcs = []
        if req is None:
            res.dtc_error = _("no DTC request in database file")
            return
        resp, msg = self._read(ef, req)
        if resp is None:
            res.dtc_error = msg
            return
        res.dtc_error = ""
        res.dtcs = E.decode_dtcs(ef, req, resp)

    # -------------------------------------------------------------- tables
    def _fill_all(self):
        self._fill_params()
        self._fill_dtcs()
        self._fill_idents()
        self._collect_actions()
        self._fill_summary()

    def _fill_params(self):
        t = self.param_table
        t.setRowCount(0)
        needle = self.param_filter.text().strip().lower()
        for res in self.results.values():
            for p in res.params:
                if self.only_flagged.isChecked() and not p["flagged"]:
                    continue
                if self.hide_unanswered.isChecked() and not p["answered"]:
                    continue
                row = [p["ecu"], p["param"], "" if p["value"] is None else p["value"],
                       p["unit"], p["request"], p["note"] or (_("check") if p["flagged"] else "")]
                if needle and needle not in " ".join(str(x) for x in row).lower():
                    continue
                if p["flagged"]:
                    self._add_row(t, row, COLOR_FLAG, background=True)
                elif not p["answered"]:
                    self._add_row(t, row, COLOR_MUTED)
                else:
                    self._add_row(t, row)
        t.resizeColumnsToContents()

    def _dtc_related(self, res, dtc):
        return res.primary or self.profile.matches(dtc.description) or any(
            self.profile.matches(str(v)) for v in dtc.fields.values())

    def _fill_dtcs(self):
        t = self.dtc_table
        t.setRowCount(0)
        for res in self.results.values():
            if res.dtc_error:
                self._add_row(t, [res.target.label, "", _("DTC read failed:") + " " + res.dtc_error,
                                  "", ""], COLOR_MUTED)
                continue
            if not res.dtcs:
                self._add_row(t, [res.target.label, "", _("No fault code"), "", ""])
            for d in res.dtcs:
                related = self._dtc_related(res, d)
                color = COLOR_ACTIVE if d.active else COLOR_STORED
                tip = "\n".join("%s: %s" % kv for kv in d.fields.items())
                self._add_row(t, [res.target.label, d.code, d.description,
                                  (_("ACTIVE") + " - " if d.active else "") + d.status,
                                  _("YES") if related else ""],
                              color, tooltip=tip, background=related)
        t.resizeColumnsToContents()

    def _fill_idents(self):
        t = self.ident_table
        t.setRowCount(0)
        for res in self.results.values():
            tg = res.target
            self._add_row(t, [tg.label, tg.name, tg.match, _("(database)"), MATCH_HELP.get(tg.match, "")],
                          qtgui.QColor("#58a6ff") if tg.match == E.MATCH_APPROX else None)
            for req, k, v in res.idents:
                self._add_row(t, [tg.label, "", "", k, v])
        t.resizeColumnsToContents()

    def _collect_actions(self):
        self.actions = []
        targets = [r.target for r in self.results.values()] or [
            t for t in self._checked_targets() if t.path in self.ecufiles]
        for tg in targets:
            ef = self._ecufile(tg)
            if ef is None:
                continue
            primary = self._is_primary(tg, ef)
            for req in E.select_actions(ef, self.profile, primary, self.show_writes.isChecked()):
                self.actions.append((tg, ef, req))
        t = self.action_table
        t.setRowCount(0)
        for tg, ef, req in self.actions:
            risk = E.classify_request(req)
            color = COLOR_ACTIVE if risk == E.RISK_WRITE else None
            self._add_row(t, [tg.label, req.name, RISK_TEXT.get(risk, risk), req.sentbytes,
                              ", ".join(sorted(req.sendbyte_dataitems)), ""], color)
        t.resizeColumnsToContents()

    def _summary_html(self):
        h = ["<h2>%s</h2>" % html.escape(self.profile.name),
             "<p>%s %s</p>" % (_("Generated"), datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))]
        if self.transport.simulated:
            h.append("<p><b>%s</b></p>" % _("SIMULATION: values are database defaults, not the car."))
        h.append("<table border='1' cellspacing='0' cellpadding='4'><tr><th>%s</th><th>%s</th>"
                 "<th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>" % (
                     _("ECU"), _("Database match"), _("Faults (active/stored)"),
                     _("System faults"), _("Flagged values"), _("Requests answered")))
        warnings = []
        for res in self.results.values():
            tg = res.target
            active = sum(1 for d in res.dtcs if d.active)
            related = sum(1 for d in res.dtcs if self._dtc_related(res, d))
            flagged = sum(1 for p in res.params if p["flagged"])
            ratio = "%d / %d" % (res.answered, res.asked)
            h.append("<tr><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                "&#9733; " if res.primary else "", html.escape(tg.label), html.escape(tg.match),
                (res.error or res.dtc_error) and html.escape(res.error or res.dtc_error)
                or "%d / %d" % (active, len(res.dtcs) - active),
                related, flagged, ratio))
            if tg.match == E.MATCH_APPROX:
                warnings.append(_("%s: the ECU software is not an exact match for its database file.")
                                % tg.label)
            if res.asked >= 5 and res.answered < res.asked / 2.0:
                warnings.append(_("%s: fewer than half of the requests were answered - the database "
                                  "file may not match the ECU software version.") % tg.label)
        h.append("</table>")
        if warnings:
            h.append("<h3>%s</h3><ul>%s</ul>" % (_("Version / database warnings"), "".join(
                "<li>%s</li>" % html.escape(w) for w in warnings)))

        cfg = [p for res in self.results.values() for p in res.params
               if p["answered"] and CONFIG_PARAM.search(p["param"])
               and self.profile.matches(p["param"] + " " + p["request"])]
        if cfg:
            h.append("<h3>%s</h3><p>%s</p><table border='1' cellspacing='0' cellpadding='4'>" % (
                _("System configuration across ECUs"),
                _("These values must agree between ECUs (e.g. the A/C type configured in the UCH, "
                  "engine ECU and climate unit).")))
            for p in cfg:
                h.append("<tr><td>%s</td><td>%s</td><td><b>%s</b> %s</td></tr>" % (
                    html.escape(p["ecu"]), html.escape(p["param"]),
                    html.escape(str(p["value"])), html.escape(p["unit"] or "")))
            h.append("</table>")

        flagged = [p for res in self.results.values() for p in res.params if p["flagged"]]
        if flagged:
            h.append("<h3>%s</h3><ul>" % _("Values to check (heuristic)"))
            for p in flagged[:60]:
                h.append("<li>%s - %s: <b>%s</b></li>" % (
                    html.escape(p["ecu"]), html.escape(p["param"]), html.escape(str(p["value"]))))
            h.append("</ul>")

        faults = [(res, d) for res in self.results.values() for d in res.dtcs]
        if faults:
            h.append("<h3>%s</h3><ul>" % _("Fault codes"))
            for res, d in faults:
                h.append("<li>%s%s - %s %s (%s)</li>" % (
                    "<b>[%s]</b> " % _("system") if self._dtc_related(res, d) else "",
                    html.escape(res.target.label), html.escape(d.code), html.escape(d.description),
                    html.escape((_("ACTIVE") + " " if d.active else "") + d.status)))
            h.append("</ul>")
        return "".join(h)

    def _fill_summary(self):
        self.summary.setHtml(self._summary_html())

    # ------------------------------------------------------------- actions
    def _ask_inputs(self, ecufile, request):
        if not request.sendbyte_dataitems:
            return {}
        dlg = gui.QDialog(self)
        dlg.setWindowTitle(request.name)
        form = gui.QFormLayout(dlg)
        editors = {}
        for k in sorted(request.sendbyte_dataitems):
            data = ecufile.data.get(k)
            if data is not None and data.items:
                w = gui.QComboBox()
                w.addItem(_("(database default)"), None)
                for text in sorted(data.items):
                    w.addItem(text, text)
            else:
                w = gui.QLineEdit()
                hint = _("(database default)")
                if data is not None:
                    hint += " - " + (_("decimal") if data.scaled else _("hex"))
                    if data.unit:
                        hint += " " + data.unit
                w.setPlaceholderText(hint)
            editors[k] = w
            form.addRow(k, w)
        bb = gui.QDialogButtonBox(gui.QDialogButtonBox.Ok | gui.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec_() != gui.QDialog.Accepted:
            return None
        values = {}
        for k, w in editors.items():
            if isinstance(w, gui.QComboBox):
                if w.currentData() is not None:
                    values[k] = w.currentData()
            elif w.text().strip():
                values[k] = w.text().strip()
        return values

    def _confirm(self, items):
        """items: (target, request, stream, risk, release)."""
        writes = [i for i in items if i[3] == E.RISK_WRITE]
        lines = []
        for tg, req, stream, risk, release in items:
            lines.append("<li><b>%s</b> - %s<br><code>%s</code> [%s]%s</li>" % (
                html.escape(tg.label), html.escape(req.name), stream, RISK_TEXT.get(risk, risk),
                ("<br>" + _("stop with") + " <code>%s</code>" % release) if release else ""))
        text = "<p>" + _("You are about to send the following command(s) to the vehicle:") + \
               "</p><ul>" + "".join(lines) + "</ul>"
        text += "<p>" + _("ACTUATOR commands drive motors, valves, relays or the compressor until "
                          "control is returned to the ECU. Make sure the engine bay and fan area "
                          "are clear and the vehicle is safe.") + "</p>"
        if writes:
            text += "<p><b><font color='red'>" + _(
                "WRITE / RESET commands change data stored in the ECU (configuration, learning, "
                "counters). A wrong value can disable functions or immobilise the vehicle and may "
                "not be reversible. Note the current values first.") + "</font></b></p>"
        mb = gui.QMessageBox(self)
        mb.setIcon(gui.QMessageBox.Warning)
        mb.setWindowTitle(_("Confirm command"))
        mb.setText(text)
        mb.setStandardButtons(gui.QMessageBox.Yes | gui.QMessageBox.Cancel)
        mb.setDefaultButton(gui.QMessageBox.Cancel)
        if mb.exec_() != gui.QMessageBox.Yes:
            return False
        if writes:
            again = gui.QMessageBox.warning(
                self, _("Confirm ECU write"),
                _("Last check: %d command(s) will MODIFY ECU memory. Continue?") % len(writes),
                gui.QMessageBox.Yes | gui.QMessageBox.No, gui.QMessageBox.No)
            if again != gui.QMessageBox.Yes:
                return False
        return True

    def run_selected_actions(self):
        if self.busy:
            return
        rows = sorted({i.row() for i in self.action_table.selectionModel().selectedRows()})
        if not rows:
            self.log(_("Select one or more commands first"), "orange")
            return
        planned = []
        for r in rows:
            tg, ef, req = self.actions[r]
            values = self._ask_inputs(ef, req)
            if values is None:
                return
            try:
                stream = "".join(req.build_data_stream(values)).upper()
                int(stream, 16)
            except Exception as e:
                self.log(_("Invalid input for") + " %s: %s" % (req.name, e), "red")
                return
            risk = max(E.classify_request(req), E.classify_stream(stream),
                       key=lambda x: [E.RISK_READ, E.RISK_SESSION, E.RISK_ACTION, E.RISK_WRITE].index(x))
            planned.append((tg, req, stream, risk, E.release_command(stream), r, ef))
        if not self._confirm([p[:5] for p in planned]):
            self.log(_("Cancelled by user - nothing sent"))
            return
        self._set_busy(True)
        try:
            for tg, req, stream, risk, release, row, ef in planned:
                if not self._connect(tg, ef):
                    continue
                self.log(_("Sending") + " %s %s (%s)" % (tg.label, req.name, stream), "red")
                resp = self.transport.send(stream, req, allow_unsafe=True)
                ok, msg = E.check_response(stream, resp)
                result = (_("OK") if ok else msg) + " - " + time.strftime("%H:%M:%S")
                self.action_table.setItem(row, 5, gui.QTableWidgetItem(result))
                self.log(_("Response") + ": " + str(resp), "green" if ok else "red")
                if ok and release and release != stream.replace(" ", "") and \
                        not any(o[2] == release and o[0].label == tg.label for o in self.overrides):
                    self.overrides.append((tg, ef, release, req.name))
                elif ok and release == stream.replace(" ", ""):
                    # this was itself a stop / return-control command
                    self.overrides = [o for o in self.overrides
                                      if not (o[2] == release and o[0].label == tg.label)]
                gui.QApplication.processEvents()
                time.sleep(0.2)
        finally:
            self._set_busy(False)
            self._update_release_button()

    def release_all(self):
        if not self.overrides:
            return
        for tg, ef, release, name in list(self.overrides):
            if not self._connect(tg, ef):
                continue
            resp = self.transport.send(release, None, allow_unsafe=True)
            ok, msg = E.check_response(release, resp)
            self.log(_("Return control to ECU") + " %s %s (%s): %s" % (
                tg.label, name, release, _("OK") if ok else msg), "green" if ok else "red")
        self.overrides = []
        self._update_release_button()

    def _update_release_button(self):
        n = len(self.overrides)
        self.release_button.setText(_("Return control to ECU") + " (%d)" % n)
        self.release_button.setEnabled(n > 0)
        if n and self.keepalive_box.isChecked():
            self.keepalive.start()
        else:
            self.keepalive.stop()

    def _keep_alive(self):
        ef = self.transport.current
        if self.busy or ef is None or not self.overrides or not self.keepalive_box.isChecked():
            return
        stream = "3E00" if ef.ecu_protocol.upper() == "CAN" else "3E01"
        for name, req in ef.requests.items():
            if "tester" in name.lower() and "present" in name.lower() and \
                    E.classify_stream(req.sentbytes) == E.RISK_READ:
                stream = req.sentbytes
                break
        self.transport.send(stream)

    # ----------------------------------------------------------- clear DTC
    def clear_dtcs(self):
        rows = sorted({i.row() for i in self.dtc_table.selectionModel().selectedRows()})
        labels = []
        for r in rows:
            it = self.dtc_table.item(r, 0)
            if it and it.text() not in labels:
                labels.append(it.text())
        if not labels:
            self.log(_("Select a fault line of the ECU(s) to clear"), "orange")
            return
        items = []
        for label in labels:
            res = self.results.get(label)
            req = res and E.find_clear_dtc_request(res.ecufile)
            if req is None:
                self.log(_("No clear-DTC request for") + " " + label, "red")
                continue
            items.append((res.target, req, req.sentbytes.upper(), E.RISK_WRITE, None))
        if not items or not self._confirm(items):
            return
        for tg, req, stream, _risk, _rel in items:
            res = self.results[tg.label]
            if not self._connect(tg, res.ecufile):
                continue
            if not self.transport.simulated:
                options.elm.set_can_timeout(1500)
            resp = self.transport.send(stream, req, allow_unsafe=True)
            if not self.transport.simulated:
                options.elm.set_can_timeout(options.cantimeout)
            ok, msg = E.check_response(stream, resp)
            self.log(_("Clear DTC") + " %s: %s" % (tg.label, _("OK") if ok else msg),
                     "green" if ok else "red")
            self._read_dtcs(res)
        self._fill_dtcs()
        self._fill_summary()

    # -------------------------------------------------------------- export
    def export_report(self):
        if not self.results:
            self.log(_("Nothing to export, run a scan first"), "orange")
            return
        try:
            from ddt4all.file_manager import get_logs_dir
            folder = str(get_logs_dir())
        except Exception:
            folder = "."
        default = os.path.join(folder, "system_diag_%s.html" % time.strftime("%Y%m%d_%H%M%S"))
        filename, _sel = gui.QFileDialog.getSaveFileName(self, _("Export report"), default, "*.html")
        if not filename:
            return
        parts = ["<html><head><meta charset='utf-8'><title>%s</title></head><body>" %
                 html.escape(self.profile.name), self._summary_html()]
        parts.append("<h3>%s</h3><table border='1' cellspacing='0' cellpadding='3'>" % _("All parameters"))
        for res in self.results.values():
            for p in res.params:
                parts.append("<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    " style='background:#f0b429'" if p["flagged"] else "",
                    html.escape(p["ecu"]), html.escape(p["param"]),
                    html.escape("" if p["value"] is None else str(p["value"])),
                    html.escape(p["unit"] or ""), html.escape(p["note"] or "")))
        parts.append("</table><h3>%s</h3><table border='1' cellspacing='0' cellpadding='3'>" %
                     _("Identification"))
        for res in self.results.values():
            for req, k, v in res.idents:
                parts.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    html.escape(res.target.label), html.escape(k), html.escape(str(v))))
        parts.append("</table></body></html>")
        with open(filename, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        self.log(_("Report saved to") + " " + filename, "green")

    # --------------------------------------------------------------- close
    def done(self, r):
        if self.overrides:
            self.log(_("Returning overridden outputs to the ECU before closing"), "orange")
            self.release_all()
        self.keepalive.stop()
        super(SystemDiagDialog, self).done(r)


def plugin_entry():
    dlg = SystemDiagDialog()
    dlg.exec_()
