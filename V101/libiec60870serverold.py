#!/usr/bin/env python3
from lib60870 import *
import time

class IEC60870_5_104_server:

    def printCP56Time2a(self, time):
        print("%02i:%02i:%02i %02i/%02i/%04i" % ( CP56Time2a_getHour(time),
                                        CP56Time2a_getMinute(time),
                                        CP56Time2a_getSecond(time),
                                        CP56Time2a_getDayOfMonth(time),
                                        CP56Time2a_getMonth(time),
                                        CP56Time2a_getYear(time) + 2000) )

    def clock(self, param, con, asdu, newTime):
        return True

    def GI_h(self, param, connection, asdu, qoi):
        if (qoi == 20):
            alParams = IMasterConnection_getApplicationLayerParameters(connection)
            IMasterConnection_sendACT_CON(connection, asdu, False)
            all_types = [MeasuredValueScaled, MeasuredValueShort, SinglePointInformation, DoublePointInformation]

            for data_type in all_types:
                newAsdu = CS101_ASDU_create(alParams, False, CS101_COT_INTERROGATED_BY_STATION, 0, 1, False, False)
                io = None
                has_data = False
                for ioa in self.IOA_list:
                    if self.IOA_list[ioa]['type'] == data_type:
                        has_data = True
                        value = self.IOA_list[ioa]['data']
                        # MODIFIKASI: Gunakan quality asli yang tersimpan (Invalid/Good)
                        quality = self.IOA_list[ioa].get('quality', IEC60870_QUALITY_GOOD)

                        if data_type == MeasuredValueScaled: creator = MeasuredValueScaled_create
                        elif data_type == MeasuredValueShort: creator = MeasuredValueShort_create
                        elif data_type == SinglePointInformation: creator = SinglePointInformation_create
                        elif data_type == DoublePointInformation: creator = DoublePointInformation_create

                        if io is None:
                            io = cast(creator(None, ioa, value, quality), InformationObject)
                            CS101_ASDU_addInformationObject(newAsdu, io)
                        else:
                            if data_type == MeasuredValueScaled: cast_type = MeasuredValueScaled
                            elif data_type == MeasuredValueShort: cast_type = MeasuredValueShort
                            elif data_type == SinglePointInformation: cast_type = SinglePointInformation
                            elif data_type == DoublePointInformation: cast_type = DoublePointInformation
                            CS101_ASDU_addInformationObject(newAsdu, cast(creator(cast(io, cast_type), ioa, value, quality), InformationObject))

                if has_data: IMasterConnection_sendASDU(connection, newAsdu)
                if io is not None: InformationObject_destroy(io)
                CS101_ASDU_destroy(newAsdu)
            IMasterConnection_sendACT_TERM(connection, asdu)
        else:
            IMasterConnection_sendACT_CON(connection, asdu, True)

    def ASDU_h(self, param, connection, asdu):
        cot = CS101_ASDU_getCOT(asdu)
        if cot == CS101_COT_ACTIVATION:
            io = CS101_ASDU_getElement(asdu, 0)
            ioa = InformationObject_getObjectAddress(io)
            if ioa in self.IOA_list:
                ioa_object = self.IOA_list[ioa]
                if (CS101_ASDU_getTypeID(asdu) == C_SC_NA_1):
                    sc = cast(io, SingleCommand)
                    ioa_object['data'] = SingleCommand_getState(sc)
                    if ioa_object['callback']: ioa_object['callback'](ioa, ioa_object, self, SingleCommand_isSelect(sc))
                elif (CS101_ASDU_getTypeID(asdu) == C_DC_NA_1):
                    sc = cast(io, DoubleCommand)
                    ioa_object['data'] = DoubleCommand_getState(sc)
                    if ioa_object['callback']: ioa_object['callback'](ioa, ioa_object, self, DoubleCommand_isSelect(sc))
            InformationObject_destroy(io)
        IMasterConnection_sendASDU(connection, asdu)
        return True

    def __init__(self, ip = "0.0.0.0"):
        self.slave = CS104_Slave_create(100, 100)
        CS104_Slave_setLocalAddress(self.slave, ip)
        CS104_Slave_setServerMode(self.slave, CS104_MODE_SINGLE_REDUNDANCY_GROUP)
        self.alParams = CS104_Slave_getAppLayerParameters(self.slave)
        CS104_Slave_setClockSyncHandler(self.slave, CS101_ClockSynchronizationHandler(self.clock), None)
        CS104_Slave_setInterrogationHandler(self.slave, CS101_InterrogationHandler(self.GI_h), None)
        CS104_Slave_setASDUHandler(self.slave, CS101_ASDUHandler(self.ASDU_h), None)
        self.IOA_list = {}

    def add_ioa(self, number, type = MeasuredValueScaled, data = 0, callback = None, event = False):
        if not number in self.IOA_list:
            # Tambahkan quality ke struktur data
            self.IOA_list[int(number)] = { 'type': type, 'data': data, 'callback': callback, 'event': event, 'quality': IEC60870_QUALITY_GOOD }
            return 0
        return -1

    def update_ioa(self, ioa, data, timestamp=None):
        if ioa not in self.IOA_list: return -1
        io_type = self.IOA_list[ioa]['type']

        # LOGIKA PERBAIKAN: Deteksi status INVALID
        quality = IEC60870_QUALITY_GOOD
        if data == "INVALID":
            quality = IEC60870_QUALITY_INVALID # Bit 7 (128)
            value = self.IOA_list[ioa]['data'] # Kirim nilai terakhir dengan flag Invalid
        else:
            try:
                value = float(data)
                if io_type != MeasuredValueShort: value = int(value)
            except: return -1

        # Kirim jika data berubah ATAU jika status kualitas berubah (menjadi Invalid)
        if value != self.IOA_list[ioa]['data'] or quality != self.IOA_list[ioa].get('quality'):
            self.IOA_list[ioa]['data'] = value
            self.IOA_list[ioa]['quality'] = quality

            if self.IOA_list[ioa]['event']:
                is_sp = (io_type == SinglePointInformation)
                is_dp = (io_type == DoublePointInformation)
                use_ts = (timestamp is not None) and (is_sp or is_dp)

                type_id = {True: M_SP_TB_1 if is_sp else M_DP_TB_1, False: M_SP_NA_1 if is_sp else M_DP_NA_1 if is_dp else M_ME_NA_1 if io_type == MeasuredValueScaled else M_ME_ND_1}[use_ts]

                newAsdu = CS101_ASDU_create(self.alParams, False, CS101_COT_SPONTANEOUS, 0, 1, False, False)
                CS101_ASDU_setTypeID(newAsdu, type_id)

                if use_ts:
                    ts = struct_sCP56Time2a()
                    CP56Time2a_setFromMsTimestamp(byref(ts), int(timestamp))
                    io = cast(SinglePointWithCP56Time2a_create(None, ioa, value, quality, byref(ts)) if is_sp else DoublePointWithCP56Time2a_create(None, ioa, value, quality, byref(ts)), InformationObject)
                else:
                    if io_type == MeasuredValueScaled: io = cast(MeasuredValueScaled_create(None, ioa, int(value), quality), InformationObject)
                    elif io_type == MeasuredValueShort: io = cast(MeasuredValueShort_create(None, ioa, value, quality), InformationObject)
                    elif is_sp: io = cast(SinglePointInformation_create(None, ioa, bool(value), quality), InformationObject)
                    elif is_dp: io = cast(DoublePointInformation_create(None, ioa, int(value), quality), InformationObject)

                CS101_ASDU_addInformationObject(newAsdu, io)
                InformationObject_destroy(io)
                CS104_Slave_enqueueASDU(self.slave, newAsdu)
                CS101_ASDU_destroy(newAsdu)
        return 0

    def start(self):
        CS104_Slave_start(self.slave)
        return 0 if CS104_Slave_isRunning(self.slave) else -1

    def stop(self):
        CS104_Slave_stop(self.slave)
        CS104_Slave_destroy(self.slave)
