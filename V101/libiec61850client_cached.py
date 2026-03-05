#!/usr/bin/env python3

import os
import sys
import ctypes
import time
import lib61850
import logging
import json

from urllib.parse import urlparse
from enum import Enum

# --- Nama file untuk menyimpan cache ---
CACHE_FILE = "ied_model_cache.json"

class AddCause(Enum):
    ADD_CAUSE_UNKNOWN = 0
    ADD_CAUSE_NOT_SUPPORTED = 1
    ADD_CAUSE_BLOCKED_BY_SWITCHING_HIERARCHY = 2
    ADD_CAUSE_SELECT_FAILED = 3
    ADD_CAUSE_INVALID_POSITION = 4
    ADD_CAUSE_POSITION_REACHED = 5
    ADD_CAUSE_PARAMETER_CHANGE_IN_EXECUTION = 6
    ADD_CAUSE_STEP_LIMIT = 7
    ADD_CAUSE_BLOCKED_BY_MODE = 8
    ADD_CAUSE_BLOCKED_BY_PROCESS = 9
    ADD_CAUSE_BLOCKED_BY_INTERLOCKING = 10
    ADD_CAUSE_BLOCKED_BY_SYNCHROCHECK = 11
    ADD_CAUSE_COMMAND_ALREADY_IN_EXECUTION = 12
    ADD_CAUSE_BLOCKED_BY_HEALTH = 13
    ADD_CAUSE_1_OF_N_CONTROL = 14
    ADD_CAUSE_ABORTION_BY_CANCEL = 15
    ADD_CAUSE_TIME_LIMIT_OVER = 16
    ADD_CAUSE_ABORTION_BY_TRIP = 17
    ADD_CAUSE_OBJECT_NOT_SELECTED = 18
    ADD_CAUSE_OBJECT_ALREADY_SELECTED = 19
    ADD_CAUSE_NO_ACCESS_AUTHORITY = 20
    ADD_CAUSE_ENDED_WITH_OVERSHOOT = 21
    ADD_CAUSE_ABORTION_DUE_TO_DEVIATION = 22
    ADD_CAUSE_ABORTION_BY_COMMUNICATION_LOSS = 23
    ADD_CAUSE_ABORTION_BY_COMMAND = 24
    ADD_CAUSE_NONE = 25
    ADD_CAUSE_INCONSISTENT_PARAMETERS = 26
    ADD_CAUSE_LOCKED_BY_OTHER_CLIENT = 27

class IedClientError(Enum):
    IED_ERROR_OK = 0
    IED_ERROR_NOT_CONNECTED = 1
    IED_ERROR_ALREADY_CONNECTED = 2
    IED_ERROR_CONNECTION_LOST = 3
    IED_ERROR_SERVICE_NOT_SUPPORTED = 4
    IED_ERROR_CONNECTION_REJECTED = 5
    IED_ERROR_OUTSTANDING_CALL_LIMIT_REACHED = 6
    IED_ERROR_USER_PROVIDED_INVALID_ARGUMENT = 10
    IED_ERROR_ENABLE_REPORT_FAILED_DATASET_MISMATCH = 11
    IED_ERROR_OBJECT_REFERENCE_INVALID = 12
    IED_ERROR_UNEXPECTED_VALUE_RECEIVED = 13
    IED_ERROR_TIMEOUT = 20
    IED_ERROR_ACCESS_DENIED = 21
    IED_ERROR_OBJECT_DOES_NOT_EXIST = 22
    IED_ERROR_OBJECT_EXISTS = 23
    IED_ERROR_OBJECT_ACCESS_UNSUPPORTED = 24
    IED_ERROR_TYPE_INCONSISTENT = 25
    IED_ERROR_TEMPORARILY_UNAVAILABLE = 26
    IED_ERROR_OBJECT_UNDEFINED = 27
    IED_ERROR_INVALID_ADDRESS = 28
    IED_ERROR_HARDWARE_FAULT = 29
    IED_ERROR_TYPE_UNSUPPORTED = 30
    IED_ERROR_OBJECT_ATTRIBUTE_INCONSISTENT = 31
    IED_ERROR_OBJECT_VALUE_INVALID = 32
    IED_ERROR_OBJECT_INVALIDATED = 33
    IED_ERROR_MALFORMED_MESSAGE = 34
    IED_ERROR_SERVICE_NOT_IMPLEMENTED = 98
    IED_ERROR_UNKNOWN = 99

logger = logging.getLogger(__name__)

class iec61850client():

    def __init__(self, readvaluecallback = None, loggerRef = None, cmdTerm_cb = None, Rpt_cb = None):
        global logger
        if loggerRef != None:
            logger = loggerRef

        self.polling = {}
        self.last_poll_times = {}
        self.previous_values = {} # Menyimpan nilai terakhir untuk filter perubahan
        self.connections = {}
        self.readvaluecallback = readvaluecallback
        self.cmdTerm_cb = cmdTerm_cb
        self.Rpt_cb = Rpt_cb
        self.cb_refs = []
        self.reporting = {}
        self.model_cache = self._load_cache()

    @staticmethod
    def _load_cache():
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    logger.info(f"Memuat model IED dari cache: {CACHE_FILE}")
                    return json.load(f)
            except (IOError, json.JSONDecodeError) as e:
                logger.warning(f"Gagal memuat cache, akan melakukan discovery ulang. Error: {e}")
                return {}
        else:
            logger.info("File cache tidak ditemukan, akan dibuat setelah discovery.")
            return {}

    def _save_cache(self):
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.model_cache, f, indent=4)
        except IOError as e:
            logger.error(f"Gagal menyimpan cache. Error: {e}")

    @staticmethod
    def printValue(value):
        _type = str(lib61850.MmsValue_getTypeString(value))
        if _type == "boolean": return lib61850.MmsValue_getBoolean(value), _type
        if _type == "array": return "arr", _type
        if _type == "bcd": return "bcd", _type
        if _type == "binary-time": return lib61850.MmsValue_getBinaryTimeAsUtcMs(value), _type
        if _type == "bit-string": return lib61850.MmsValue_getBitStringAsInteger(value), _type
        if _type == "access-error": return "ACCESS ERROR", _type
        if _type == "float": return lib61850.MmsValue_toFloat(value), _type
        if _type == "generalized-time": return lib61850.MmsValue_toUnixTimestamp(value), _type
        if _type == "integer": return lib61850.MmsValue_toInt64(value), _type
        if _type == "oid": return "OID ERROR", _type
        if _type == "mms-string": return lib61850.MmsValue_toString(value).decode("utf-8"), _type
        if _type == "structure":
            sub_list = []
            size = lib61850.MmsValue_getArraySize(value)
            for i in range(size):
                component = lib61850.MmsValue_getElement(value, i)
                if component:
                    component_value, _ = iec61850client.printValue(component)
                    sub_list.append(component_value)
            return sub_list, _type
        if _type == "octet-string":
            len_val = lib61850.MmsValue_getOctetStringSize(value)
            buf = lib61850.MmsValue_getOctetStringBuffer(value)
            res = bytearray(len_val)
            ctypes.memmove((ctypes.c_char * len_val).from_buffer(res), buf, len_val)
            return (''.join(format(x, '02x') for x in res)), _type
        if _type == "unsigned": return lib61850.MmsValue_toUint32(value), _type
        if _type == "utc-time": return lib61850.MmsValue_getUtcTimeInMs(value), _type
        if _type == "visible-string": return lib61850.MmsValue_toString(value).decode("utf-8"), _type
        if _type == "unknown(error)": return "UNKNOWN ERROR", _type
        return "CANNOT FIND TYPE", _type

    @staticmethod
    def printDataDirectory(con, doRef):
        tmodel = {}
        error = lib61850.IedClientError()
        dataAttributes = lib61850.IedConnection_getDataDirectoryFC(con, ctypes.byref(error), doRef.encode('utf-8'))
        if error.value == 0 and dataAttributes:
            dataAttribute = lib61850.LinkedList_getNext(dataAttributes)
            while dataAttribute:
                daName = ctypes.cast(lib61850.LinkedList_getData(dataAttribute),ctypes.c_char_p).value.decode("utf-8")
                daRef = doRef+"."+daName[:-4]
                fcName = daName[-3:-1]
                submodel = iec61850client.printDataDirectory(con,daRef)
                if submodel: tmodel[daName[:-4]] = submodel
                else:
                    tmodel[daName[:-4]] = {'reftype': "DA", 'FC': fcName, 'value': "UNKNOWN"}
                    fc = lib61850.FunctionalConstraint_fromString(fcName)
                    value = lib61850.IedConnection_readObject(con, ctypes.byref(error), daRef.encode('utf-8'), fc)
                    if error.value == 0:
                        tmodel[daName[:-4]]['value'], tmodel[daName[:-4]]['type'] = iec61850client.printValue(value)
                        lib61850.MmsValue_delete(value)
                dataAttribute = lib61850.LinkedList_getNext(dataAttribute)
            lib61850.LinkedList_destroy(dataAttributes)
        return tmodel

    @staticmethod
    def discovery(con):
        tmodel = {}
        error = lib61850.IedClientError()
        deviceList = lib61850.IedConnection_getLogicalDeviceList(con, ctypes.byref(error))
        if error.value != 0 or not deviceList: return {}
        ld_names = []
        device_item = lib61850.LinkedList_getNext(deviceList)
        while device_item:
            ld_names.append(ctypes.cast(lib61850.LinkedList_getData(device_item), ctypes.c_char_p).value.decode("utf-8"))
            device_item = lib61850.LinkedList_getNext(device_item)
        lib61850.LinkedList_destroy(deviceList)
        for LD_name in ld_names:
            tmodel[LD_name] = {}
            logicalNodes_c = lib61850.IedConnection_getLogicalDeviceDirectory(con, ctypes.byref(error), LD_name.encode('utf-8'))
            if error.value == 0 and logicalNodes_c:
                ln_item = lib61850.LinkedList_getNext(logicalNodes_c)
                while ln_item:
                    LN_name = ctypes.cast(lib61850.LinkedList_getData(ln_item), ctypes.c_char_p).value.decode("utf-8")
                    tmodel[LD_name][LN_name] = {}
                    LNobjects = lib61850.IedConnection_getLogicalNodeDirectory(con, ctypes.byref(error), (LD_name+"/"+LN_name).encode('utf-8'),lib61850.ACSI_CLASS_DATA_OBJECT)
                    if LNobjects:
                        LNobject = lib61850.LinkedList_getNext(LNobjects)
                        while LNobject:
                            Do = ctypes.cast(lib61850.LinkedList_getData(LNobject),ctypes.c_char_p).value.decode("utf-8")
                            tmodel[LD_name][LN_name][Do] = iec61850client.printDataDirectory(con, LD_name+"/"+LN_name+"."+Do)
                            LNobject = lib61850.LinkedList_getNext(LNobject)
                        lib61850.LinkedList_destroy(LNobjects)
                    # Dataset/URCB/BRCB logic
                    for cls in [lib61850.ACSI_CLASS_DATA_SET, lib61850.ACSI_CLASS_URCB, lib61850.ACSI_CLASS_BRCB]:
                        objs = lib61850.IedConnection_getLogicalNodeDirectory(con, ctypes.byref(error), (LD_name+"/"+LN_name).encode('utf-8'), cls)
                        if objs:
                            obj = lib61850.LinkedList_getNext(objs)
                            while obj:
                                name = ctypes.cast(lib61850.LinkedList_getData(obj),ctypes.c_char_p).value.decode("utf-8")
                                if cls == lib61850.ACSI_CLASS_DATA_SET:
                                    isDel = ctypes.c_bool(False)
                                    tmodel[LD_name][LN_name][name] = {}
                                    mems = lib61850.IedConnection_getDataSetDirectory(con, ctypes.byref(error), (LD_name+"/"+LN_name+"."+name).encode('utf-8'), ctypes.byref(isDel))
                                    if mems:
                                        mem = lib61850.LinkedList_getNext(mems)
                                        i = 0
                                        while mem:
                                            dsRef = ctypes.cast(lib61850.LinkedList_getData(mem),ctypes.c_char_p).value.decode("utf-8")
                                            tmodel[LD_name][LN_name][name][str(i)] = {'reftype': "DX", 'type': "reference", 'value': dsRef[:-4], 'FC': dsRef[-3:-1]}
                                            mem, i = lib61850.LinkedList_getNext(mem), i + 1
                                        lib61850.LinkedList_destroy(mems)
                                else:
                                    tmodel[LD_name][LN_name][name] = iec61850client.printDataDirectory(con, LD_name+"/"+LN_name+"."+name)
                                obj = lib61850.LinkedList_getNext(obj)
                            lib61850.LinkedList_destroy(objs)
                    ln_item = lib61850.LinkedList_getNext(ln_item)
                lib61850.LinkedList_destroy(logicalNodes_c)
        return tmodel

    @staticmethod
    def updateValueInModel(con, model, ref):
        val, path = iec61850client.parseRef(model,ref)
        def update_recurse(con, submodel, path):
            err = -1
            if len(path) == 1:
                if submodel[ path[0] ] and submodel[ path[0] ].get('reftype') == 'DA':
                    fc = lib61850.FunctionalConstraint_fromString(submodel[ path[0] ]['FC'])
                    error = lib61850.IedClientError()
                    value = lib61850.IedConnection_readObject(con, ctypes.byref(error), ref.encode('utf-8'), fc)
                    if error.value == 0:
                        submodel[ path[0] ]['value'], submodel[ path[0] ]['type'] = iec61850client.printValue(value)
                        lib61850.MmsValue_delete(value)
                        err = 0
            else: submodel[ path[0] ], err = update_recurse(con, submodel[ path[0] ], path[1:])
            return submodel, err
        return update_recurse(con, model, path)

    @staticmethod
    def parseRef(model,ref):
        path = []
        if not ref: return model, path
        _ref = ref.split("/")
        if len(_ref) == 1:
            path.append(ref)
            return (model.get(ref), path) if ref in model else ({}, [])
        LD = _ref[0]
        path.append(LD)
        mm = model.get(LD)
        if not mm: return {}, []
        _ref_parts = _ref[1].split(".")
        for part in _ref_parts:
            path.append(part)
            mm = mm.get(part)
            if not mm: return {},[]
        return mm, path

    def getIED(self, host, port):
        tupl = f"{host}:{port}"
        if tupl in self.connections and self.connections[tupl].get("con"): return 0
        if tupl in self.model_cache: self.connections[tupl] = {"con": None, "model": self.model_cache[tupl]}
        else: self.connections[tupl] = {"con": None, "model": {}}
        con = lib61850.IedConnection_create()
        error = lib61850.IedClientError()
        lib61850.IedConnection_connect(con,ctypes.byref(error), host.encode('utf-8'), port)
        if error.value == 0:
            self.connections[tupl]["con"] = con
            if not self.connections[tupl]["model"]:
                model = iec61850client.discovery(con)
                if model:
                    self.connections[tupl]["model"] = model
                    self.model_cache[tupl] = model
                    self._save_cache()
            return 0
        return -1

    def getRegisteredIEDs(self):
        return self.connections

    # --- FILTER PERUBAHAN ---
    def _trigger_callback_if_changed(self, ref, submodel, source="POLL"):
        new_val = submodel.get("value")
        # Hanya kirim ke callback/log jika nilai berubah
        if new_val != self.previous_values.get(ref):
            self.previous_values[ref] = new_val
            if source == "RPT":
                if self.Rpt_cb: self.Rpt_cb(ref, submodel)
            else:
                if self.readvaluecallback: self.readvaluecallback(ref, submodel)
            logger.info(f"CHANGE [{source}] | {ref} = {new_val}")

    def poll(self):
        now = time.time()
        for key in list(self.polling.keys()):
            if now - self.last_poll_times.get(key, 0) < 5: continue
            u = urlparse(key)
            tupl = f"{u.hostname}:{u.port or 102}"
            if self.getIED(u.hostname, u.port or 102) == 0:
                con, model = self.connections[tupl]['con'], self.connections[tupl]['model']
                if con and model:
                    self.last_poll_times[key] = now
                    _, err = iec61850client.updateValueInModel(con, model, u.path[1:])
                    if err == 0:
                        sm, _ = iec61850client.parseRef(model, u.path[1:])
                        self._trigger_callback_if_changed(key, sm, "POLL")

    # --- LOGIKA REPORTING (KODE MURNI USER + FILTER) ---
    def ReportHandler_cb(self, param, report):
        refdata = ctypes.cast(param, ctypes.py_object).value
        key, tupl, LD, LN, DSRef = refdata
        dataSetValues = lib61850.ClientReport_getDataSetValues(report)
        if not dataSetValues: return

        report_timestamp = None
        if lib61850.ClientReport_hasTimestamp(report):
            report_timestamp = lib61850.ClientReport_getTimestamp(report)

        dataset = self.connections[tupl]['model'][LD][LN][DSRef]
        for index_str in dataset:
            index = int(index_str)
            reason = lib61850.ClientReport_getReasonForInclusion(report, index)
            if reason != lib61850.IEC61850_REASON_NOT_INCLUDED:
                mmsval = lib61850.MmsValue_getElement(dataSetValues, index)
                if mmsval:
                    DaRef = dataset[index_str]['value']
                    val, _ = iec61850client.printValue(mmsval)
                    sm, _ = iec61850client.parseRef(self.connections[tupl]['model'], DaRef)
                    if sm:
                        sm["value"] = val
                        if report_timestamp: sm["timestamp"] = report_timestamp
                        # Masukkan ke filter COS agar terminal bersih
                        full_ref = f"iec61850://{tupl}/{DaRef}"
                        self._trigger_callback_if_changed(full_ref, sm, "RPT")

    def registerForReporting(self, key, tupl, ref_path):
        con = self.connections[tupl]['con']
        model = self.connections[tupl]['model']
        found_dataset, target_ds_ref, target_ld, target_ln, target_ds_name = False, "", "", "", ""

        for LD_name, lns in model.items():
            if found_dataset: break
            for LN_name, dos in lns.items():
                if found_dataset: break
                for DSname, ds_content in dos.items():
                    if isinstance(ds_content, dict) and ds_content.get("0", {}).get('reftype') == "DX":
                        for index, dx_info in ds_content.items():
                            if ref_path.startswith(dx_info['value']):
                                target_ds_ref, target_ld, target_ln, target_ds_name, found_dataset = f"{LD_name}/{LN_name}${DSname}", LD_name, LN_name, DSname, True
                                break

        if not found_dataset: return False

        candidates = []
        for RP_name, rp_content in model[target_ld][target_ln].items():
            if isinstance(rp_content, dict) and rp_content.get("DatSet", {}).get("value") == target_ds_ref:
                fc = rp_content.get("DatSet", {}).get("FC", "RP")
                candidates.append(f"{target_ld}/{target_ln}.{fc}.{RP_name}")

        if not candidates: return False

        for RPT_path in candidates:
            if RPT_path in self.cb_refs: return True
            error = lib61850.IedClientError()
            rcb = lib61850.IedConnection_getRCBValues(con, ctypes.byref(error), RPT_path.encode('utf-8'), None)
            if error.value != 0 or lib61850.ClientReportControlBlock_getRptEna(rcb):
                if rcb: lib61850.ClientReportControlBlock_destroy(rcb)
                continue

            RptId = lib61850.ClientReportControlBlock_getRptId(rcb)
            cbh = lib61850.ReportCallbackFunction(self.ReportHandler_cb)

            # Sangat Penting: Simpan ref_data dalam variabel agar tidak di-GC
            ref_payload = [key, tupl, target_ld, target_ln, target_ds_name]

            lib61850.IedConnection_installReportHandler(con, RPT_path.encode('utf-8'), RptId, cbh, id(ref_payload))
            lib61850.ClientReportControlBlock_setRptEna(rcb, True)
            lib61850.ClientReportControlBlock_setGI(rcb, True)
            lib61850.IedConnection_setRCBValues(con, ctypes.byref(error), rcb, lib61850.RCB_ELEMENT_RPT_ENA | lib61850.RCB_ELEMENT_GI, True)

            if error.value == 0:
                self.cb_refs.append(RPT_path)
                if tupl not in self.reporting: self.reporting[tupl] = []
                self.reporting[tupl].append({"rcb": rcb, "cbh": cbh, "RPT": RPT_path, "refdata": ref_payload})
                return True
        return False

    def registerReadValue(self,ref):
        u = urlparse(ref)
        tupl = f"{u.hostname}:{u.port or 102}"
        if self.getIED(u.hostname, u.port or 102) == 0:
            model = self.connections[tupl]['model']
            sm, _ = iec61850client.parseRef(model, u.path[1:])
            if sm:
                if not self.registerForReporting(ref, tupl, u.path[1:]):
                    self.polling[ref] = 1
                return 0
        return -1

    def getDatamodel(self, ref=None, hostname="localhost", port=102):
        if ref:
            u = urlparse(ref)
            hostname, port = u.hostname, u.port
        if self.getIED(hostname, port or 102) == 0:
            return self.connections[f"{hostname}:{port or 102}"]['model']
        return {}

    # --- OPERATE & SELECT (ORISINAL) ---
    def get_controlObject(self, tupl, uri_ref):
        con = self.connections[tupl]['con']
        self.connections[tupl].setdefault('control', {})
        ref_path = uri_ref.path[1:]
        if ref_path not in self.connections[tupl]['control']:
            control = lib61850.ControlObjectClient_create(ref_path.encode('utf-8'), con)
            self.connections[tupl]['control'][ref_path] = control
        return self.connections[tupl]['control'][ref_path]

    def operate(self, ref, value):
        u = urlparse(ref)
        if self.getIED(u.hostname, u.port or 102) == 0:
            tupl = f"{u.hostname}:{u.port or 102}"
            ctrl = self.get_controlObject(tupl, u)
            mmsT = lib61850.ControlObjectClient_getCtlValType(ctrl)
            ctlV = iec61850client.getMMsValue("", value, 0, mmsT)
            err = lib61850.ControlObjectClient_operate(ctrl, ctlV, 0)
            lib61850.MmsValue_delete(ctlV)
            return err, ""
        return -1, ""

if __name__=="__main__":
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO)
    cl = iec61850client()

    # Contoh penggunaan loop
    # while True:
    #     cl.poll()
    #     time.sleep(0.1)
