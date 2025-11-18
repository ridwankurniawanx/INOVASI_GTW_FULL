#!/usr/bin/env python3
# v47.py - Rilis dengan Status Deskripsi di Event Log File
# Deskripsi: Gateway IEC 61850 ke IEC 60870-5-104.
# Fitur v47:
# - Menambahkan deskripsi status (Open/Close/etc) ke dalam file log events.txt.
# - Backend Python kini memiliki logika mapping status yang sama dengan Frontend.
# - Fitur v46 (Web UI Status Column) tetap ada.
# - Fitur v44 (Filter Measured Value) tetap ada.

import asyncio
import json
import logging
import configparser
import threading
import sys
import os
import time
from urllib.parse import urlparse
import websockets
from aiohttp import web
import signal
from collections import deque

try:
    import libiec61850client_cached as libiec61850client
    import libiec60870server
    from lib60870 import *
    from lib61850 import IedConnection_getState
except ImportError as e:
    print(f"Error: Gagal mengimpor library yang dibutuhkan. Pesan: {e}")
    sys.exit(1)

# --- Konfigurasi Nama File Log ---
PROCESS_LOG_FILE = 'process.txt'
EVENT_LOG_FILE = 'events.txt'

# --- Definisikan konstanta & Variabel Global ---
FALLBACK_POLLING_INTERVAL, HEARTBEAT_POLLING_INTERVAL, RECONNECT_DELAY = 10, 60, 15
HTTP_PORT, WEBSOCKET_PORT = 8000, 8001

clients_dict_lock = threading.Lock()
ied_locks, ied_clients = {}, {}
ied_to_ioas_map, mms_to_ioa_map, ioa_inversion_map, ioa_to_mms_config, mms_to_value_path_map, ioa_to_full_address_map, ioa_to_signal_name_map = {}, {}, {}, {}, {}, {}, {}
processing_queue, broadcast_queue, iec104_server, main_loop = None, None, None, None
websocket_clients = set()
realtime_data_cache, connection_status_cache = {}, {}

# Cache history event
recent_events_cache = deque(maxlen=20) 

cache_lock = asyncio.Lock()
shutdown_event = asyncio.Event()

# Logger khusus untuk Event
event_logger = logging.getLogger("event_logger")

# --- Helper Function untuk Timestamp dengan ms ---
def get_timestamp_with_ms(timestamp_ms=None):
    """Mengembalikan string waktu format YYYY-MM-DD HH:MM:SS.mmm"""
    if timestamp_ms is None:
        timestamp_ms = time.time() * 1000
    
    ts_seconds = timestamp_ms / 1000
    ts_ms_part = int(timestamp_ms % 1000)
    base_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_seconds))
    return "{}.{:03d}".format(base_time, ts_ms_part)

# --- Helper Function untuk Status Deskripsi (Backend Logic) ---
def get_status_description(data_type, value):
    """Mengembalikan string deskripsi status berdasarkan tipe data dan nilai."""
    if value == 'INVALID':
        return "Link Fail"
    
    try:
        val_int = int(value)
    except (ValueError, TypeError):
        return ""

    if data_type == 'DPI':
        # Mapping Double Point: 0=Invalid, 1=Open, 2=Close, 3=Intermediate
        return {0: 'Invalid', 1: 'Open', 2: 'Close', 3: 'Intermediate'}.get(val_int, '')
    elif data_type == 'SPI':
        # Mapping Single Point: 0=Disappear, 1=Appear
        return {0: 'Disappear', 1: 'Appear'}.get(val_int, '')
    
    return ""

# --- Setup Logging ---
def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s')

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(PROCESS_LOG_FILE, mode='a')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False
    
    evt_handler = logging.FileHandler(EVENT_LOG_FILE, mode='a')
    evt_formatter = logging.Formatter('%(message)s') 
    evt_handler.setFormatter(evt_formatter)
    event_logger.addHandler(evt_handler)

# --- Server Web (aiohttp) ---
async def handle_http_get(request):
    try:
        return web.FileResponse('./index.html')
    except Exception as e:
        logging.error(f"Gagal menyajikan index.html: {e}")
        return web.Response(status=404, text="index.html tidak ditemukan. Restart script.")

async def handle_download_process(request):
    if os.path.exists(PROCESS_LOG_FILE):
        return web.FileResponse(PROCESS_LOG_FILE)
    return web.Response(status=404, text="Process log belum tersedia.")

async def handle_download_events(request):
    if os.path.exists(EVENT_LOG_FILE):
        return web.FileResponse(EVENT_LOG_FILE)
    return web.Response(status=404, text="Event log belum tersedia.")

async def start_aiohttp_server(port):
    app = web.Application()
    app.router.add_get('/', handle_http_get)
    app.router.add_get('/download/process', handle_download_process)
    app.router.add_get('/download/events', handle_download_events)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"HTTP server berjalan di http://0.0.0.0:{port}")
    return runner

# --- WebSocket & Logic ---
async def websocket_handler(websocket, path):
    logging.info(f"WebSocket client terhubung: {websocket.remote_address}")
    async with cache_lock:
        if connection_status_cache: 
            await asyncio.gather(*[websocket.send(json.dumps(status)) for status in connection_status_cache.values()], return_exceptions=True)
        if realtime_data_cache: 
            await asyncio.gather(*[websocket.send(json.dumps(data)) for data in realtime_data_cache.values()], return_exceptions=True)
        
        if recent_events_cache:
            history_payload = {
                'type': 'event_history',
                'data': list(recent_events_cache)
            }
            await websocket.send(json.dumps(history_payload))

    websocket_clients.add(websocket)
    try:
        await websocket.wait_closed()
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        logging.info(f"WebSocket client terputus: {websocket.remote_address}")
        websocket_clients.remove(websocket)

async def broadcast_updates_task(queue):
    while not shutdown_event.is_set():
        try:
            update = await asyncio.wait_for(queue.get(), timeout=1.0)
            async with cache_lock:
                if update.get('type') == 'status_update':
                    connection_status_cache[update['ied_id']] = update
                else:
                    realtime_data_cache[update['ioa']] = update
                    if update.get('is_event', True):
                        recent_events_cache.append(update)

            if websocket_clients:
                await asyncio.gather(*[client.send(json.dumps(update)) for client in websocket_clients], return_exceptions=True)
            queue.task_done()
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
    logging.info("Broadcast task dihentikan.")

def find_first_float(data):
    if isinstance(data, (float, int)): return float(data)
    if isinstance(data, dict):
        for val in data.values():
            res = find_first_float(val)
            if res is not None: return res
    if isinstance(data, list):
        for item in data:
            res = find_first_float(item)
            if res is not None: return res
    return None

def get_value_by_path(data_dict, path_str):
    keys = path_str.split('.')
    cur = data_dict
    try:
        for k in keys: cur = cur[k]
        if isinstance(cur, (int, float)): return float(cur)
        return None
    except: return None

def process_data_update(ied_id, key, data):
    if not isinstance(data, dict) or 'value' not in data: return
    
    gateway_timestamp_ms = data.get('timestamp', time.time() * 1000)
    reported_key, value_to_update = key, data['value']
    
    mms_path_from_key = reported_key
    if "iec61850://" in reported_key:
        try:
            mms_path_from_key = urlparse(reported_key).path.lstrip('/')
        except: return

    valid_ioas = set(ied_to_ioas_map.get(ied_id, []))
    if not valid_ioas: return

    for config_path, ioa in mms_to_ioa_map.items():
        if ioa in valid_ioas and config_path.startswith(mms_path_from_key):
            value_path = mms_to_value_path_map.get(config_path)
            final_value = get_value_by_path(value_to_update, value_path) if value_path else find_first_float(value_to_update)
            if final_value is None: continue
            
            try:
                ioa_type_class = iec104_server.IOA_list.get(ioa, {}).get('type')
                
                # Deteksi Tipe Data
                data_type = 'MEAS'
                is_measurement = True
                
                if "DoublePointInformation" in str(ioa_type_class):
                    data_type = 'DPI'
                    is_measurement = False
                elif "SinglePointInformation" in str(ioa_type_class):
                    data_type = 'SPI'
                    is_measurement = False

                value_to_send = float(final_value)
                
                # Normalisasi Nilai
                if data_type == 'DPI':
                    value_to_send = {1.0: 1, 2.0: 2}.get(value_to_send, 0)
                elif data_type == 'SPI':
                    value_to_send = 1 if int(value_to_send) != 0 else 0
                
                if ioa_inversion_map.get(ioa, False): 
                    value_to_send = {1: 2, 2: 1}.get(value_to_send, value_to_send)
                
                formatted_timestamp = get_timestamp_with_ms(gateway_timestamp_ms)
                signal_name = ioa_to_signal_name_map.get(ioa, "N/A")

                update_payload = {
                    'type': 'data_update',
                    'ied_id': ied_id,
                    'ioa': ioa,
                    'signal': signal_name,
                    'value': value_to_send,
                    'timestamp': formatted_timestamp,
                    'address': ioa_to_full_address_map.get(ioa, "N/A"),
                    'is_event': not is_measurement,
                    'data_type': data_type
                }

                if main_loop and main_loop.is_running(): 
                    main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)
                
                iec104_server.update_ioa(ioa, value_to_send, timestamp=gateway_timestamp_ms)
                
                logging.info(f"[{ied_id}] Update IOA {ioa} ({signal_name}): {value_to_send}")
                
                # LOGIC V47: Tambahkan Deskripsi Status ke Event Log
                if not is_measurement:
                    status_desc = get_status_description(data_type, value_to_send)
                    event_log_msg = f"{formatted_timestamp} | {ied_id} | IOA: {ioa} | {signal_name} | Val: {value_to_send} | {status_desc}"
                    event_logger.info(event_log_msg)

            except Exception as e:
                logging.error(f"Error processing IOA {ioa}: {e}")
            return

def do_invalidation(ied_id):
    if ied_id in ied_to_ioas_map:
        ioas = ied_to_ioas_map[ied_id]
        formatted_timestamp = get_timestamp_with_ms()
        for ioa in ioas:
            signal_name = ioa_to_signal_name_map.get(ioa, "N/A")
            
            ioa_type_class = iec104_server.IOA_list.get(ioa, {}).get('type')
            data_type = 'MEAS'
            if "DoublePointInformation" in str(ioa_type_class): data_type = 'DPI'
            elif "SinglePointInformation" in str(ioa_type_class): data_type = 'SPI'

            update_payload = {
                'type': 'invalidation',
                'ied_id': ied_id,
                'ioa': ioa,
                'signal': signal_name,
                'value': 'INVALID',
                'timestamp': formatted_timestamp,
                'address': ioa_to_full_address_map.get(ioa, "N/A"),
                'is_event': True,
                'data_type': data_type
            }
            if main_loop and main_loop.is_running():
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)
            
            # LOGIC V47: Tambahkan Deskripsi Status ke Event Log
            status_desc = get_status_description(data_type, 'INVALID')
            event_log_msg = f"{formatted_timestamp} | {ied_id} | IOA: {ioa} | {signal_name} | Val: INVALID | {status_desc}"
            event_logger.info(event_log_msg)

def ied_data_callback(key, data, ied_id):
    if main_loop and processing_queue and main_loop.is_running():
        update_item = {'type': 'process_data', 'ied_id': ied_id, 'key': key, 'data': data}
        main_loop.call_soon_threadsafe(processing_queue.put_nowait, update_item)

def invalidate_ied_points(ied_id):
    if main_loop and processing_queue and main_loop.is_running():
        main_loop.call_soon_threadsafe(processing_queue.put_nowait, {'type': 'invalidate', 'ied_id': ied_id})

def broadcast_connection_status(ied_id, status):
    if main_loop and broadcast_queue and main_loop.is_running():
        formatted_timestamp = get_timestamp_with_ms()
        payload = {'type': 'status_update', 'ied_id': ied_id, 'status': status, 'timestamp': formatted_timestamp}
        main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, payload)
        event_logger.info(f"{formatted_timestamp} | {ied_id} | SYSTEM | Connection Status | {status}")

async def data_processor_task(queue):
    while not shutdown_event.is_set():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
            if item.get('type') == 'process_data':
                await main_loop.run_in_executor(None, process_data_update, item['ied_id'], item['key'], item['data'])
            elif item.get('type') == 'invalidate':
                await main_loop.run_in_executor(None, do_invalidation, item['ied_id'])
            queue.task_done()
        except asyncio.TimeoutError: continue
        except asyncio.CancelledError: break
    logging.info("Data processor task dihentikan.")

async def ied_handler(ied_id, uris):
    logging.info(f"[{ied_id}] IED handler task dimulai.")
    client = None
    ied_locks[ied_id] = threading.Lock()
    def polling_cb(key, data): ied_data_callback(key, data, ied_id)
    def report_cb(key, data): ied_data_callback(key, data, ied_id)
    def locked_check_state():
        with ied_locks[ied_id]:
            if not client or not client.getRegisteredIEDs().get(ied_id, {}).get('con'): return False
            return IedConnection_getState(client.getRegisteredIEDs()[ied_id]['con']) == 2
    def locked_register_values():
        with ied_locks[ied_id]:
            for uri in uris: client.registerReadValue(str(uri))
            return len(client.polling)
            
    while not shutdown_event.is_set():
        try:
            broadcast_connection_status(ied_id, "CONNECTING")
            with ied_locks[ied_id]:
                client = libiec61850client.iec61850client(readvaluecallback=polling_cb, loggerRef=logging, cmdTerm_cb=None, Rpt_cb=report_cb)
            
            if await main_loop.run_in_executor(None, client.getIED, ied_id.split(':')[0], int(ied_id.split(':')[1])) != 0:
                raise ConnectionError("getIED gagal")
            
            with clients_dict_lock: ied_clients[ied_id] = client
            broadcast_connection_status(ied_id, "CONNECTED")
            
            count = await main_loop.run_in_executor(None, locked_register_values)
            interval = HEARTBEAT_POLLING_INTERVAL if count == 0 else FALLBACK_POLLING_INTERVAL
            logging.info(f"[{ied_id}] Interval: {interval}s")
            
            while not shutdown_event.is_set():
                if not await main_loop.run_in_executor(None, locked_check_state): raise ConnectionError("Terputus")
                await main_loop.run_in_executor(None, client.poll)
                await asyncio.sleep(0.1)
        except Exception as e:
            if not shutdown_event.is_set(): logging.error(f"[{ied_id}] Error: {e}")
        finally:
            if not shutdown_event.is_set():
                broadcast_connection_status(ied_id, "DISCONNECTED")
                with clients_dict_lock: 
                    if ied_id in ied_clients: del ied_clients[ied_id]
                invalidate_ied_points(ied_id)
                await asyncio.sleep(RECONNECT_DELAY)

async def main_async():
    global iec104_server, main_loop, processing_queue, broadcast_queue, ioa_to_signal_name_map
    main_loop = asyncio.get_running_loop()
    processing_queue, broadcast_queue = asyncio.Queue(), asyncio.Queue()

    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(sys.argv[1] if len(sys.argv) > 1 else 'config.local.ini')
    
    http_runner = await start_aiohttp_server(HTTP_PORT)
    websocket_server = await websockets.serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT)
    
    iec104_server = libiec60870server.IEC60870_5_104_server()
    
    data_types = {
        'measuredvaluescaled': MeasuredValueScaled, 'measuredvaluefloat': MeasuredValueShort,
        'singlepointinformation': SinglePointInformation, 'doublepointinformation': DoublePointInformation
    }
    command_types = {'singlepointcommand': SingleCommand, 'doublepointcommand': DoubleCommand}
    ied_data_groups = {}

    for section in list(data_types.keys()) + list(command_types.keys()):
        if section in config:
            for ioa, config_line in config[section].items():
                signal, full_addr = "N/A", config_line.strip()
                if ':' in full_addr and '://' in full_addr:
                    parts = full_addr.split(':', 1)
                    if '://' in parts[1]: signal, full_addr = parts[0], parts[1]

                ioa_int = int(ioa)
                ioa_to_signal_name_map[ioa_int] = signal.strip()
                ioa_to_full_address_map[ioa_int] = full_addr

                uri_part = full_addr.split('#')[0].replace(':invers=true', '')
                parsed = urlparse(uri_part)
                ied_id = f"{parsed.hostname}:{parsed.port or 102}"

                if ied_id not in ied_to_ioas_map: ied_to_ioas_map[ied_id] = []
                ied_to_ioas_map[ied_id].append(ioa_int)

                if section in data_types:
                    mms_path = parsed.path.lstrip('/')
                    mms_to_ioa_map[mms_path] = ioa_int
                    if '#' in full_addr: mms_to_value_path_map[mms_path] = full_addr.split('#', 1)[1]
                    if ied_id not in ied_data_groups: ied_data_groups[ied_id] = []
                    if uri_part not in ied_data_groups[ied_id]: ied_data_groups[ied_id].append(uri_part)
                
                if ':invers=true' in full_addr: ioa_inversion_map[ioa_int] = True
                if section in command_types: ioa_to_mms_config[ioa_int] = full_addr

    logging.info(f"Menemukan {len(ied_data_groups)} IED.")

    # Validasi (Simplified)
    client_val = libiec61850client.iec61850client(loggerRef=logging)
    for ied_id in ied_data_groups:
        h, p = ied_id.split(':')
        if client_val.getIED(h, int(p)) == 0:
            logging.info(f"Validasi koneksi ke {ied_id} OK.")
        else:
            logging.error(f"Gagal validasi koneksi ke {ied_id}.")

    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]: iec104_server.add_ioa(int(item), mms_type, 0, None, True)
    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]: iec104_server.add_ioa(int(item), mms_type, 0, None, False)
    
    threading.Thread(target=iec104_server.start, name="IEC104ServerThread", daemon=True).start()
    
    tasks = [asyncio.create_task(data_processor_task(processing_queue)), asyncio.create_task(broadcast_updates_task(broadcast_queue))]
    tasks.extend([asyncio.create_task(ied_handler(ied_id, uris)) for ied_id, uris in ied_data_groups.items()])
    
    logging.info("Gateway v47 Berjalan. Tekan Ctrl+C untuk berhenti.")
    await asyncio.gather(*tasks)

async def shutdown(sig, loop):
    logging.info(f"Shutdown signal {sig.name} received.")
    shutdown_event.set()
    await asyncio.sleep(1)
    if 'http_runner' in globals() and http_runner: await http_runner.cleanup()
    if 'websocket_server' in globals() and websocket_server: 
        websocket_server.close()
        await websocket_server.wait_closed()
    if iec104_server: iec104_server.stop()
    
    for task in asyncio.all_tasks(): 
        if task is not asyncio.current_task(): task.cancel()
    loop.stop()

def create_index_html_if_not_exists():
    if not os.path.exists("index.html"):
        logging.info("Membuat file index.html untuk v47...")
        html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gateway Status v47 (Status Logged)</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; background-color: #f4f7f6; color: #333; }
        h1 { text-align: center; color: #1a252f; padding: 20px; background-color: #fff; margin: 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .controls { max-width: 1200px; margin: 20px auto; text-align: right; padding: 0 20px; }
        .btn { display: inline-block; padding: 10px 20px; color: #fff; text-decoration: none; border-radius: 5px; font-size: 14px; font-weight: bold; margin-left: 10px; }
        .btn-process { background-color: #007bff; } .btn-event { background-color: #28a745; }
        #ied-container, #event-log-container { padding: 20px; max-width: 1200px; margin: 0 auto; }
        .ied-section { background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 25px; overflow: hidden; }
        .ied-header { display: flex; align-items: center; justify-content: space-between; padding: 15px 20px; background-color: #e9ecef; border-bottom: 1px solid #dee2e6; }
        h2 { margin: 0; font-size: 1.5em; color: #1a252f; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #dee2e6; }
        thead tr { background-color: #343a40; color: #fff; }
        tbody tr:nth-child(even) { background-color: #f8f9fa; }
        .status { padding: 5px 10px; color: white; border-radius: 15px; font-weight: bold; font-size: 0.9em; }
        .status.connected { background-color: #28a745; } .status.connecting { background-color: #ffc107; color: #333; } .status.disconnected { background-color: #dc3545; }
        .value-invalid { color: #dc3545; font-weight: bold; } .value-valid { color: #28a745; }
        .status-desc { font-weight: bold; color: #555; }
        .event-row-enter { animation: highlight 1s ease-out; }
        @keyframes highlight { 0% { background-color: #d4edda; } 100% { background-color: transparent; } }
    </style>
</head>
<body>
    <h1>Gateway Monitoring Status v47</h1>
    <div class="controls">
        <a href="/download/process" class="btn btn-process" target="_blank">Process Log (.txt)</a>
        <a href="/download/events" class="btn btn-event" target="_blank">Event Log (.txt)</a>
    </div>
    <div id="ied-container"></div>
    <div id="event-log-container">
        <div class="ied-section">
            <div class="ied-header"><h2>Recent Events (Binary Signals)</h2></div>
            <table class="event-table">
                <thead><tr><th>Timestamp</th><th>IED</th><th>IOA</th><th>Signal Name</th><th>Value</th><th>Status</th></tr></thead>
                <tbody id="event-tbody"></tbody>
            </table>
        </div>
    </div>
    <script>
        // --- KONFIGURASI LABEL STATUS (Harus sinkron dengan Backend Python) ---
        const STATUS_LABELS = {
            'DPI': { 0: 'Invalid', 1: 'Open', 2: 'Close', 3: 'Intermediate' },
            'SPI': { 0: 'Disappear', 1: 'Appear' }
        };
        // -----------------------------------------------------------------------

        const iedContainer = document.getElementById('ied-container');
        const eventTbody = document.getElementById('event-tbody');
        const ws = new WebSocket(`ws://${window.location.hostname}:""" + str(WEBSOCKET_PORT) + """`);
        
        function getStatusDesc(dataType, value) {
            if (value === 'INVALID') return 'Link Fail';
            if (STATUS_LABELS[dataType] && STATUS_LABELS[dataType][value] !== undefined) {
                return STATUS_LABELS[dataType][value];
            }
            return '';
        }

        function addEventToTable(data) {
            const row = eventTbody.insertRow(0);
            row.className = 'event-row-enter';
            const statusDesc = getStatusDesc(data.data_type, data.value);
            row.innerHTML = `<td>${data.timestamp}</td><td>${data.ied_id}</td><td>${data.ioa}</td><td>${data.signal}</td><td class="${data.value === 'INVALID'?'value-invalid':'value-valid'}">${(typeof data.value==='number')?data.value.toFixed(3):data.value}</td><td class="status-desc">${statusDesc}</td>`;
            if (eventTbody.rows.length > 20) eventTbody.deleteRow(-1);
        }

        ws.onmessage = function(event) {
            const data = JSON.parse(event.data);
            if (data.type === 'event_history') { data.data.forEach(evt => addEventToTable(evt)); return; }
            
            const iedId = data.ied_id || (data.address && data.address.match(/(\\d+\\.\\d+\\.\\d+\\.\\d+:\\d+)/)?.[0]);
            if (!iedId && data.type !== 'status_update') return;

            let iedSection = document.getElementById(`ied-${iedId}`);
            if (!iedSection && iedId) {
                iedSection = document.createElement('div'); iedSection.id = `ied-${iedId}`; iedSection.className = 'ied-section';
                iedSection.innerHTML = `<div class="ied-header"><h2>IED: ${iedId}</h2><span id="status-${iedId}" class="status">UNKNOWN</span></div><table><thead><tr><th>IOA</th><th>Signal Name</th><th>Address</th><th>Value</th><th>Status</th><th>Time</th></tr></thead><tbody id="tbody-${iedId}"></tbody></table>`;
                iedContainer.appendChild(iedSection);
            }

            if (data.type === 'status_update') {
                const b = document.getElementById(`status-${data.ied_id}`);
                if(b) { b.textContent = data.status; b.className = `status ${data.status.toLowerCase()}`; }
            } else if (data.type === 'data_update' || data.type === 'invalidation') {
                const tbody = document.getElementById(`tbody-${data.ied_id}`);
                let row = document.getElementById(`row-${data.ioa}`);
                const statusDesc = getStatusDesc(data.data_type, data.value);
                
                if (!row) {
                    row = tbody.insertRow(); row.id = `row-${data.ioa}`;
                    row.innerHTML = `<td>${data.ioa}</td><td id="signal-${data.ioa}"></td><td id="address-${data.ioa}" style="word-break:break-all;"></td><td id="value-${data.ioa}"></td><td id="status-txt-${data.ioa}" class="status-desc"></td><td id="time-${data.ioa}"></td>`;
                }
                document.getElementById(`signal-${data.ioa}`).textContent = data.signal;
                document.getElementById(`address-${data.ioa}`).textContent = data.address;
                const v = document.getElementById(`value-${data.ioa}`);
                v.textContent = (typeof data.value==='number')?data.value.toFixed(3):data.value;
                v.className = data.value==='INVALID'?'value-invalid':'value-valid';
                document.getElementById(`status-txt-${data.ioa}`).textContent = statusDesc;
                document.getElementById(`time-${data.ioa}`).textContent = data.timestamp;
                
                if (data.is_event !== false) addEventToTable(data);
            }
        };
    </script>
</body>
</html>
        """
        with open("index.html", "w") as f:
            f.write(html_content)

if __name__ == '__main__':
    setup_logging()
    create_index_html_if_not_exists()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s, loop)))
    try:
        loop.create_task(main_async())
        loop.run_forever()
    finally:
        loop.close()
