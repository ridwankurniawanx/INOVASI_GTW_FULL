#!/usr/bin/env python3
# v42.py - Rilis dengan Download Log Terpisah
# Deskripsi: Gateway IEC 61850 ke IEC 60870-5-104.
# Fitur v42:
# - Menambahkan fitur pencatatan ke file (Logging to File).
# - Memisahkan "Process Log" (System) dan "Event Log" (Data Changes).
# - Menambahkan tombol Download Log di Web UI.
# - Tetap mempertahankan fitur Timestamp ms (v41).

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
cache_lock = asyncio.Lock()
shutdown_event = asyncio.Event()

# Logger khusus untuk Event (Perubahan Data)
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

# --- Setup Logging (Process & Event) ---
def setup_logging():
    # 1. Setup Root Logger (Process Log + Console)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s')

    # Handler Console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Handler File Process Log
    file_handler = logging.FileHandler(PROCESS_LOG_FILE, mode='a')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 2. Setup Event Logger (Hanya Data Changes, File Only)
    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False # Jangan kirim ke root logger (supaya tidak duplikat di console/process log)

    evt_handler = logging.FileHandler(EVENT_LOG_FILE, mode='a')
    # Format Event Log lebih bersih: Timestamp | IED | IOA | Signal | Value
    evt_formatter = logging.Formatter('%(message)s')
    evt_handler.setFormatter(evt_formatter)
    event_logger.addHandler(evt_handler)

# --- Fungsi-fungsi untuk Server Web (aiohttp) ---
async def handle_http_get(request):
    try:
        return web.FileResponse('./index.html')
    except Exception as e:
        logging.error(f"Gagal menyajikan index.html: {e}")
        return web.Response(status=404, text="index.html tidak ditemukan. Restart script untuk membuatnya.")

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
    # Route untuk download logs
    app.router.add_get('/download/process', handle_download_process)
    app.router.add_get('/download/events', handle_download_events)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"HTTP server (aiohttp) berjalan di http://0.0.0.0:{port}")
    return runner

# --- WebSocket & Fungsi Utilitas ---
async def websocket_handler(websocket, path):
    logging.info(f"WebSocket client terhubung: {websocket.remote_address}")
    async with cache_lock:
        if connection_status_cache: await asyncio.gather(*[websocket.send(json.dumps(status)) for status in connection_status_cache.values()], return_exceptions=True)
        if realtime_data_cache: await asyncio.gather(*[websocket.send(json.dumps(data)) for data in realtime_data_cache.values()], return_exceptions=True)
    websocket_clients.add(websocket)
    try:
        await websocket.wait_closed()
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"Koneksi WebSocket ditutup dari sisi client: {websocket.remote_address}")
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
            result = find_first_float(val)
            if result is not None: return result
    if isinstance(data, list):
        for item in data:
            result = find_first_float(item)
            if result is not None: return result
    return None

def get_value_by_path(data_dict, path_str):
    keys = path_str.split('.')
    current_level = data_dict
    try:
        for key in keys:
            current_level = current_level[key]
        if isinstance(current_level, (int, float)):
            return float(current_level)
        return None
    except (KeyError, TypeError):
        return None

def process_data_update(ied_id, key, data):
    if not isinstance(data, dict) or 'value' not in data: return

    gateway_timestamp_ms = data.get('timestamp', time.time() * 1000)

    reported_key, value_to_update = key, data['value']
    mms_path_from_key = reported_key
    if "iec61850://" in reported_key:
        try:
            parsed_uri = urlparse(reported_key)
            mms_path_from_key = parsed_uri.path.lstrip('/')
        except Exception:
            logging.warning(f"Tidak dapat mem-parsing URI key: {reported_key}")
            return
    valid_ioas_for_ied = set(ied_to_ioas_map.get(ied_id, []))
    if not valid_ioas_for_ied: return
    for config_path, ioa in mms_to_ioa_map.items():
        if ioa in valid_ioas_for_ied and config_path.startswith(mms_path_from_key):
            value_path = mms_to_value_path_map.get(config_path)
            final_value = get_value_by_path(value_to_update, value_path) if value_path else find_first_float(value_to_update)
            if final_value is None: continue
            try:
                ioa_type_class = iec104_server.IOA_list.get(ioa, {}).get('type')
                value_to_send = float(final_value)
                if "DoublePointInformation" in str(ioa_type_class): value_to_send = {1.0: 1, 2.0: 2}.get(value_to_send, 0)
                elif "SinglePointInformation" in str(ioa_type_class): value_to_send = 1 if int(value_to_send) != 0 else 0
                if ioa_inversion_map.get(ioa, False): value_to_send = {1: 2, 2: 1}.get(value_to_send, value_to_send)

                formatted_timestamp = get_timestamp_with_ms(gateway_timestamp_ms)
                signal_name = ioa_to_signal_name_map.get(ioa, "N/A")

                update_payload = {
                    'type': 'data_update',
                    'ied_id': ied_id,
                    'ioa': ioa,
                    'signal': signal_name,
                    'value': value_to_send,
                    'timestamp': formatted_timestamp,
                    'address': ioa_to_full_address_map.get(ioa, "N/A")
                }

                if main_loop and main_loop.is_running(): main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)

                iec104_server.update_ioa(ioa, value_to_send, timestamp=gateway_timestamp_ms)

                # --- LOGGING: PROCESS LOG (Console/File) ---
                logging.info(f"[{ied_id}] Update IOA {ioa} ({signal_name}): {value_to_send}")

                # --- LOGGING: EVENT LOG (File Only - events.txt) ---
                # Format: Timestamp | IED | IOA | Signal Name | Value
                event_log_msg = f"{formatted_timestamp} | {ied_id} | IOA: {ioa} | {signal_name} | Val: {value_to_send}"
                event_logger.info(event_log_msg)

            except Exception as e:
                logging.error(f"Error saat memproses update untuk IOA {ioa}: {e}", exc_info=True)
            return

def do_invalidation(ied_id):
    if ied_id in ied_to_ioas_map:
        ioas_to_invalidate = ied_to_ioas_map[ied_id]
        logging.warning(f"Membuat invalid {len(ioas_to_invalidate)} titik data untuk {ied_id}.")

        formatted_timestamp = get_timestamp_with_ms()

        for ioa in ioas_to_invalidate:
            signal_name = ioa_to_signal_name_map.get(ioa, "N/A")
            update_payload = {
                'type': 'invalidation',
                'ied_id': ied_id,
                'ioa': ioa,
                'signal': signal_name,
                'value': 'INVALID',
                'timestamp': formatted_timestamp,
                'address': ioa_to_full_address_map.get(ioa, "N/A")
            }
            if main_loop and main_loop.is_running():
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)

            # Catat invalidation ke Event Log
            event_log_msg = f"{formatted_timestamp} | {ied_id} | IOA: {ioa} | {signal_name} | Val: INVALID"
            event_logger.info(event_log_msg)

def ied_data_callback(key, data, ied_id):
    if main_loop and processing_queue and main_loop.is_running():
        update_item = {'type': 'process_data', 'ied_id': ied_id, 'key': key, 'data': data}
        main_loop.call_soon_threadsafe(processing_queue.put_nowait, update_item)

def invalidate_ied_points(ied_id):
    if main_loop and processing_queue and main_loop.is_running():
        update_item = {'type': 'invalidate', 'ied_id': ied_id}
        main_loop.call_soon_threadsafe(processing_queue.put_nowait, update_item)

def broadcast_connection_status(ied_id, status):
    if main_loop and broadcast_queue and main_loop.is_running():
        formatted_timestamp = get_timestamp_with_ms()
        status_payload = {'type': 'status_update', 'ied_id': ied_id, 'status': status, 'timestamp': formatted_timestamp}
        main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, status_payload)

        # Status koneksi masuk ke Process Log (sudah via logging.info di caller)
        # Jika ingin masuk Event Log juga:
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
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
    logging.info("Data processor task dihentikan.")

async def ied_handler(ied_id, uris):
    logging.info(f"[{ied_id}] IED handler task dimulai.")
    client = None
    ied_locks[ied_id] = threading.Lock()
    def polling_entry_point(key, data): ied_data_callback(key, data, ied_id)
    def report_entry_point(key, data): ied_data_callback(key, data, ied_id)
    def locked_check_state():
        with ied_locks[ied_id]:
            if not client or not client.getRegisteredIEDs().get(ied_id, {}).get('con'): return False
            conn_info = client.getRegisteredIEDs()[ied_id]
            if conn_info.get('con'): return IedConnection_getState(conn_info['con']) == 2
            return False
    def locked_register_values():
        with ied_locks[ied_id]:
            for uri in uris: client.registerReadValue(str(uri))
            return len(client.polling)

    while not shutdown_event.is_set():
        try:
            logging.info(f"[{ied_id}] Mencoba menghubungkan...")
            broadcast_connection_status(ied_id, "CONNECTING")
            with ied_locks[ied_id]:
                client = libiec61850client.iec61850client(readvaluecallback=polling_entry_point, loggerRef=logging, cmdTerm_cb=None, Rpt_cb=report_entry_point)

            res = await main_loop.run_in_executor(None, client.getIED, ied_id.split(':')[0], int(ied_id.split(':')[1]))
            if res != 0: raise ConnectionError("getIED gagal")

            with clients_dict_lock: ied_clients[ied_id] = client
            logging.info(f"[{ied_id}] Koneksi berhasil.")
            broadcast_connection_status(ied_id, "CONNECTED")

            polling_item_count = await main_loop.run_in_executor(None, locked_register_values)
            active_polling_interval = HEARTBEAT_POLLING_INTERVAL if polling_item_count == 0 else FALLBACK_POLLING_INTERVAL
            logging.info(f"[{ied_id}] Mode: {'Heartbeat' if active_polling_interval == HEARTBEAT_POLLING_INTERVAL else 'Fallback'}. Interval: {active_polling_interval} detik.")

            while not shutdown_event.is_set():
                if not await main_loop.run_in_executor(None, locked_check_state):
                    raise ConnectionError("Koneksi terputus")
                await main_loop.run_in_executor(None, client.poll)
                await asyncio.sleep(0.1)
        except (ConnectionError, Exception) as e:
            if not shutdown_event.is_set(): logging.error(f"[{ied_id}] Handler error: {e}.")
        finally:
            if not shutdown_event.is_set():
                broadcast_connection_status(ied_id, "DISCONNECTED")
                with clients_dict_lock:
                    if ied_id in ied_clients: del ied_clients[ied_id]
                invalidate_ied_points(ied_id)
                logging.info(f"[{ied_id}] Menghubungkan ulang dalam {RECONNECT_DELAY} detik.")
                try:
                    await asyncio.sleep(RECONNECT_DELAY)
                except asyncio.CancelledError:
                    break
    logging.info(f"[{ied_id}] IED handler task berhenti.")

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
                signal_name = "N/A"
                full_address = config_line.strip()
                if ':' in full_address and '://' in full_address:
                    parts = full_address.split(':', 1)
                    if '://' in parts[1]:
                        signal_name = parts[0]
                        full_address = parts[1]

                ioa_int = int(ioa)
                ioa_to_signal_name_map[ioa_int] = signal_name.strip()
                ioa_to_full_address_map[ioa_int] = full_address

                uri_and_options = full_address
                uri_part = uri_and_options.split('#')[0]
                if ':invers=true' in uri_part:
                    uri_part = uri_part.replace(':invers=true', '')

                parsed = urlparse(uri_part)
                ied_id = f"{parsed.hostname}:{parsed.port or 102}"

                if ied_id not in ied_to_ioas_map: ied_to_ioas_map[ied_id] = []
                ied_to_ioas_map[ied_id].append(ioa_int)

                if section in data_types:
                    mms_path = parsed.path.lstrip('/')
                    mms_to_ioa_map[mms_path] = ioa_int
                    if '#' in uri_and_options:
                        mms_to_value_path_map[mms_path] = uri_and_options.split('#', 1)[1]
                    if ied_id not in ied_data_groups: ied_data_groups[ied_id] = []
                    if uri_part not in ied_data_groups[ied_id]:
                        ied_data_groups[ied_id].append(uri_part)

                if ':invers=true' in uri_and_options: ioa_inversion_map[ioa_int] = True
                if section in command_types: ioa_to_mms_config[ioa_int] = uri_and_options

    logging.info(f"Menemukan {len(ied_data_groups)} IED unik untuk dimonitor.")

    # --- BLOK VALIDASI KONFIGURASI ---
    logging.info("Memvalidasi konfigurasi terhadap model data IED...")
    client_validator = libiec61850client.iec61850client(loggerRef=logging)
    for ied_id in ied_data_groups.keys():
        host, port_str = ied_id.split(':')
        port = int(port_str)
        if client_validator.getIED(host, port) == 0:
            logging.info(f"Berhasil terhubung ke {ied_id} untuk validasi.")
            datamodel = client_validator.getDatamodel(hostname=host, port=port)
            if not datamodel:
                logging.error(f"Gagal mendapatkan model data dari {ied_id}, validasi dilewati.")
                continue

            mms_paths_in_config = [path for path, ioa in mms_to_ioa_map.items() if ioa in ied_to_ioas_map.get(ied_id, [])]
            for mms_path in mms_paths_in_config:
                submodel, _ = libiec61850client.iec61850client.parseRef(datamodel, mms_path)
                if not submodel:
                    logging.warning(f"[VALIDASI GAGAL] Alamat '{mms_path}' TIDAK DITEMUKAN di IED {ied_id}.")
        else:
            logging.error(f"Gagal terhubung ke {ied_id} untuk validasi.")
    logging.info("Validasi konfigurasi selesai.")

    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]: iec104_server.add_ioa(int(item), mms_type, 0, None, True)
    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]: iec104_server.add_ioa(int(item), mms_type, 0, None, False)

    server_thread = threading.Thread(target=iec104_server.start, name="IEC104ServerThread", daemon=True)
    server_thread.start()

    all_tasks = [
        asyncio.create_task(data_processor_task(processing_queue)),
        asyncio.create_task(broadcast_updates_task(broadcast_queue)),
    ]
    all_tasks.extend([asyncio.create_task(ied_handler(ied_id, uris)) for ied_id, uris in ied_data_groups.items()])

    logging.info(f"Gateway v42 dimulai. Tekan Ctrl+C untuk berhenti.")
    await asyncio.gather(*all_tasks)

async def shutdown(sig, loop):
    logging.info(f"Diterima sinyal {sig.name}, memulai shutdown...")
    shutdown_event.set()
    await asyncio.sleep(1.5)

    if 'http_runner' in globals() and http_runner:
        await http_runner.cleanup()
        logging.info("Server HTTP berhenti.")
    if 'websocket_server' in globals() and websocket_server:
        websocket_server.close()
        await websocket_server.wait_closed()
        logging.info("Server WebSocket berhenti.")
    if iec104_server:
        iec104_server.stop()
        logging.info("Server IEC 104 berhenti.")

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    logging.info("Semua task asyncio selesai.")
    loop.stop()

def create_index_html_if_not_exists():
    if not os.path.exists("index.html"):
        logging.info("Membuat file index.html untuk v42...")
        html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gateway Status v42 (Logs)</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; background-color: #f4f7f6; color: #333; }
        h1 { text-align: center; color: #1a252f; padding: 20px; background-color: #fff; margin: 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }

        .controls { max-width: 1200px; margin: 20px auto; text-align: right; padding: 0 20px; }
        .btn { display: inline-block; padding: 10px 20px; color: #fff; text-decoration: none; border-radius: 5px; font-size: 14px; font-weight: bold; margin-left: 10px; transition: background 0.3s; }
        .btn-process { background-color: #007bff; }
        .btn-process:hover { background-color: #0056b3; }
        .btn-event { background-color: #28a745; }
        .btn-event:hover { background-color: #1e7e34; }

        #ied-container { padding: 20px; max-width: 1200px; margin: 0 auto; }
        .ied-section { background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 25px; overflow: hidden; }
        .ied-header { display: flex; align-items: center; justify-content: space-between; padding: 15px 20px; background-color: #e9ecef; border-bottom: 1px solid #dee2e6; }
        h2 { margin: 0; font-size: 1.5em; color: #1a252f; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #dee2e6; }
        thead tr { background-color: #343a40; color: #fff; }
        tbody tr:nth-child(even) { background-color: #f8f9fa; }
        tbody tr:hover { background-color: #e9ecef; }
        .status { padding: 5px 10px; color: white; border-radius: 15px; font-weight: bold; text-transform: uppercase; font-size: 0.9em; }
        .status.connected { background-color: #28a745; }
        .status.connecting { background-color: #ffc107; color: #333; }
        .status.disconnected { background-color: #dc3545; }
        .value-invalid { color: #dc3545; font-weight: bold; }
        .value-valid { color: #28a745; }
    </style>
</head>
<body>
    <h1>Gateway Monitoring Status v42 (Log Download)</h1>

    <div class="controls">
        <a href="/download/process" class="btn btn-process" target="_blank">Download Process Log</a>
        <a href="/download/events" class="btn btn-event" target="_blank">Download Event Log</a>
    </div>

    <div id="ied-container"></div>
    <script>
        const iedContainer = document.getElementById('ied-container');
        const ws = new WebSocket(`ws://${window.location.hostname}:""" + str(WEBSOCKET_PORT) + """`);

        ws.onopen = () => console.log('WebSocket connection established');
        ws.onclose = () => console.log('WebSocket connection closed');
        ws.onerror = (error) => console.error('WebSocket error:', error);

        ws.onmessage = function(event) {
            const data = JSON.parse(event.data);
            const iedId = data.ied_id;

            if (!iedId && data.type !== 'status_update') {
                const address = data.address || 'N/A';
                const match = address.match(/(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}:\\d+)/);
                if (!match) return;
                data.ied_id = match[0];
            }

            let iedSection = document.getElementById(`ied-${data.ied_id}`);
            if (!iedSection) {
                iedSection = document.createElement('div');
                iedSection.id = `ied-${data.ied_id}`;
                iedSection.className = 'ied-section';
                iedSection.innerHTML = `
                    <div class="ied-header">
                        <h2>IED: ${data.ied_id}</h2>
                        <span id="status-${data.ied_id}" class="status">UNKNOWN</span>
                    </div>
                    <table>
                        <thead>
                            <tr>
                                <th>IOA</th>
                                <th>Signal Name</th>
                                <th>IEC 61850 Address</th>
                                <th>Value</th>
                                <th>Last Update</th>
                            </tr>
                        </thead>
                        <tbody id="tbody-${data.ied_id}"></tbody>
                    </table>
                `;
                iedContainer.appendChild(iedSection);
            }

            if (data.type === 'status_update') {
                const statusBadge = document.getElementById(`status-${data.ied_id}`);
                statusBadge.textContent = data.status;
                statusBadge.className = `status ${data.status.toLowerCase()}`;
            } else if (data.type === 'data_update' || data.type === 'invalidation') {
                const tbody = document.getElementById(`tbody-${data.ied_id}`);
                let row = document.getElementById(`row-${data.ioa}`);

                if (!row) {
                    row = tbody.insertRow();
                    row.id = `row-${data.ioa}`;
                    row.innerHTML = `
                        <td>${data.ioa}</td>
                        <td id="signal-${data.ioa}"></td>
                        <td id="address-${data.ioa}" style="word-break: break-all;"></td>
                        <td id="value-${data.ioa}"></td>
                        <td id="time-${data.ioa}"></td>
                    `;
                }

                document.getElementById(`signal-${data.ioa}`).textContent = data.signal || 'N/A';
                document.getElementById(`address-${data.ioa}`).textContent = data.address;
                const valueCell = document.getElementById(`value-${data.ioa}`);
                valueCell.textContent = (typeof data.value === 'number') ? data.value.toFixed(3) : data.value;
                valueCell.className = data.value === 'INVALID' ? 'value-invalid' : 'value-valid';
                document.getElementById(`time-${data.ioa}`).textContent = data.timestamp;
            }
        };
    </script>
</body>
</html>
        """
        with open("index.html", "w") as f:
            f.write(html_content)

if __name__ == '__main__':
    # Konfigurasi Logging (File + Console)
    setup_logging()
    create_index_html_if_not_exists()

    loop = asyncio.get_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s, loop)))

    try:
        loop.create_task(main_async())
        loop.run_forever()
    finally:
        logging.info("Event loop ditutup.")
        loop.close()
