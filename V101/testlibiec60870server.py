#!/usr/bin/env python3
from lib60870 import *
import threading

class IEC60870_5_104_server:
    def __init__(self, ip="0.0.0.0"):
        self.slave = CS104_Slave_create(100, 100)
        CS104_Slave_setLocalAddress(self.slave, ip)
        CS104_Slave_setServerMode(self.slave, CS104_MODE_SINGLE_REDUNDANCY_GROUP)
        self.alParams = CS104_Slave_getAppLayerParameters(self.slave)
        
        # Lock untuk sinkronisasi thread C dan Python
        self.lock = threading.Lock()
        self.IOA_list = {}

        # Set Handlers
        CS104_Slave_setClockSyncHandler(self.slave, CS101_ClockSynchronizationHandler(self.clock), None)
        CS104_Slave_setInterrogationHandler(self.slave, CS101_InterrogationHandler(self.GI_h), None)
        CS104_Slave_setASDUHandler(self.slave, CS101_ASDUHandler(self.ASDU_h), None)

    def GI_h(self, param, connection, asdu, qoi):
        """Handler untuk General Interrogation (GI)"""
        if qoi == 20:
            alParams = IMasterConnection_getApplicationLayerParameters(connection)
            IMasterConnection_sendACT_CON(connection, asdu, False)
            
            # Ambil snapshot data dengan LOCK agar iterasi aman dari update simultan
            with self.lock:
                snapshot_ioa = list(self.IOA_list.items())

            all_types = [MeasuredValueScaled, MeasuredValueShort, SinglePointInformation, DoublePointInformation]

            for data_type in all_types:
                newAsdu = CS101_ASDU_create(alParams, False, CS101_COT_INTERROGATED_BY_STATION, 0, 1, False, False)
                io = None
                has_data = False
                
                for ioa, config in snapshot_ioa:
                    if config['type'] == data_type:
                        has_data = True
                        value = config['data']
                        quality = config.get('quality', IEC60870_QUALITY_GOOD)

                        if data_type == MeasuredValueScaled: creator = MeasuredValueScaled_create
                        elif data_type == MeasuredValueShort: creator = MeasuredValueShort_create
                        elif data_type == SinglePointInformation: creator = SinglePointInformation_create
                        elif data_type == DoublePointInformation: creator = DoublePointInformation_create

                        if io is None:
                            io = cast(creator(None, ioa, value, quality), InformationObject)
                            CS101_ASDU_addInformationObject(newAsdu, io)
                        else:
                            # Masukkan ke chain ASDU
                            if data_type == MeasuredValueScaled: cast_t = MeasuredValueScaled
                            elif data_type == MeasuredValueShort: cast_t = MeasuredValueShort
                            elif data_type == SinglePointInformation: cast_t = SinglePointInformation
                            elif data_type == DoublePointInformation: cast_t = DoublePointInformation
                            CS101_ASDU_addInformationObject(newAsdu, cast(creator(cast(io, cast_t), ioa, value, quality), InformationObject))

                if has_data:
                    IMasterConnection_sendASDU(connection, newAsdu)
                
                # Cukup destroy ASDU, objek di dalamnya otomatis dibersihkan
                CS101_ASDU_destroy(newAsdu)

            IMasterConnection_sendACT_TERM(connection, asdu)
            return True
        else:
            IMasterConnection_sendACT_CON(connection, asdu, True)
            return True

    def add_ioa(self, number, type=MeasuredValueScaled, data=0, callback=None, event=False):
        with self.lock:
            self.IOA_list[int(number)] = {
                'type': type, 'data': data, 'callback': callback, 
                'event': event, 'quality': IEC60870_QUALITY_GOOD
            }
        return 0

    def update_ioa(self, ioa, data, timestamp=None):
        with self.lock:
            if ioa not in self.IOA_list: return -1
            config = self.IOA_list[ioa]
            io_type = config['type']

            quality = IEC60870_QUALITY_GOOD
            if data == "INVALID":
                quality = IEC60870_QUALITY_INVALID
                value = config['data']
            else:
                try:
                    value = float(data)
                    if io_type != MeasuredValueShort: value = int(value)
                except: return -1

            # Kirim spontaneity jika ada perubahan nilai atau status
            if value != config['data'] or quality != config.get('quality'):
                config['data'] = value
                config['quality'] = quality

                if config['event']:
                    is_sp = (io_type == SinglePointInformation)
                    is_dp = (io_type == DoublePointInformation)
                    use_ts = (timestamp is not None) and (is_sp or is_dp)

                    type_id = {
                        True: M_SP_TB_1 if is_sp else M_DP_TB_1,
                        False: M_SP_NA_1 if is_sp else M_DP_NA_1 if is_dp else M_ME_NA_1 if io_type == MeasuredValueScaled else M_ME_ND_1
                    }[use_ts]

                    newAsdu = CS101_ASDU_create(self.alParams, False, CS101_COT_SPONTANEOUS, 0, 1, False, False)
                    CS101_ASDU_setTypeID(newAsdu, type_id)

                    if use_ts:
                        ts = struct_sCP56Time2a()
                        CP56Time2a_setFromMsTimestamp(byref(ts), int(timestamp))
                        io = cast(SinglePointWithCP56Time2a_create(None, ioa, bool(value), quality, byref(ts)) if is_sp else DoublePointWithCP56Time2a_create(None, ioa, int(value), quality, byref(ts)), InformationObject)
                    else:
                        if io_type == MeasuredValueScaled: io = cast(MeasuredValueScaled_create(None, ioa, int(value), quality), InformationObject)
                        elif io_type == MeasuredValueShort: io = cast(MeasuredValueShort_create(None, ioa, value, quality), InformationObject)
                        elif is_sp: io = cast(SinglePointInformation_create(None, ioa, bool(value), quality), InformationObject)
                        elif is_dp: io = cast(DoublePointInformation_create(None, ioa, int(value), quality), InformationObject)

                    CS101_ASDU_addInformationObject(newAsdu, io)
                    CS104_Slave_enqueueASDU(self.slave, newAsdu)
                    CS101_ASDU_destroy(newAsdu)
        return 0

    def clock(self, param, con, asdu, newTime): return True
    def ASDU_h(self, param, connection, asdu): return True
    def start(self):
        CS104_Slave_start(self.slave)
        return 0 if CS104_Slave_isRunning(self.slave) else -1
    def stop(self):
        CS104_Slave_stop(self.slave)
        CS104_Slave_destroy(self.slave)
