#!/usr/bin/env python3
# v48.py - Rilis UI Carbon Final: Fix Sorting Order
# Deskripsi: Gateway IEC 61850 ke IEC 60870-5-104.


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
recent_events_cache = deque(maxlen=50)

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
        updates = []
        if connection_status_cache: updates.extend(list(connection_status_cache.values()))
        if realtime_data_cache: updates.extend(list(realtime_data_cache.values()))
        
        # Kirim event history
        if recent_events_cache:
            updates.append({
                'type': 'event_history',
                'data': list(recent_events_cache)
            })
            
        if updates: await asyncio.gather(*[websocket.send(json.dumps(update)) for update in updates], return_exceptions=True)

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
                elif update.get('type') in ['data_update', 'invalidation']:
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
        except Exception as e:
            logging.error(f"Error saat broadcasting: {e}", exc_info=True)
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
                    'is_event': not is_measurement, # Hanya binary signals yang dianggap event
                    'data_type': data_type
                }

                if main_loop and main_loop.is_running(): 
                    main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)
                
                iec104_server.update_ioa(ioa, value_to_send, timestamp=gateway_timestamp_ms)
                
                logging.info(f"[{ied_id}] Update IOA {ioa} ({signal_name}): {value_to_send}")
                
                # LOGIC V55: Tambahkan Deskripsi Status ke Event Log
                if not is_measurement:
                    status_desc = get_status_description(data_type, value_to_send)
                    event_log_msg = f"{formatted_timestamp} | {ied_id} | IOA: {ioa} | {signal_name} | Val: {value_to_send} | {status_desc}"
                    event_logger.info(event_log_msg)

            except Exception as e:
                logging.error(f"Error processing IOA {ioa}: {e}", exc_info=True)
            return

def do_invalidation(ied_id):
    if ied_id in ied_to_ioas_map:
        ioas = ied_to_ioas_map[ied_id]
        formatted_timestamp = get_timestamp_with_ms()
        for ioa in ioas:
            signal_name = ioa_to_signal_name_map.get(ioa, "N/A")
            
            ioa_type_class = iec104_server.IOA_list.get(ioa, {}).get('type')
            data_type = 'MEAS'
            is_measurement = True
            if "DoublePointInformation" in str(ioa_type_class): 
                data_type = 'DPI'; is_measurement = False
            elif "SinglePointInformation" in str(ioa_type_class): 
                data_type = 'SPI'; is_measurement = False

            update_payload = {
                'type': 'invalidation',
                'ied_id': ied_id,
                'ioa': ioa,
                'signal': signal_name,
                'value': 'INVALID',
                'timestamp': formatted_timestamp,
                'address': ioa_to_full_address_map.get(ioa, "N/A"),
                'is_event': not is_measurement, # Binary signals juga di-invalidate
                'data_type': data_type
            }
            if main_loop and main_loop.is_running():
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)
            
            # LOGIC V55: Tambahkan Deskripsi Status ke Event Log
            if not is_measurement:
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
        # Log status koneksi ke events.txt
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

    # Validasi (Simplified/Kept from V47)
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
    
    logging.info("Gateway v55 Berjalan. Tekan Ctrl+C untuk berhenti.")
    await asyncio.gather(*tasks)

async def shutdown(sig, loop):
    logging.info(f"Shutdown signal {sig.name} received.")
    shutdown_event.set()
    await asyncio.sleep(1)
    if 'http_runner' in globals() and http_runner: 
        try: await http_runner.cleanup()
        except NameError: pass
    if 'websocket_server' in globals() and websocket_server: 
        websocket_server.close()
        await websocket_server.wait_closed()
    if iec104_server: iec104_server.stop()
    
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks: task.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

def create_index_html_if_not_exists():
    logging.info("Membuat file index.html UI V55 (Fix Sorting)...")
    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gateway Monitor Pro</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    
    <style>
        :root {{
            /* --- CARBON / ONYX THEME (Professional Industrial) --- */
            --bg-body: #121212;       /* True Black/Dark Grey */
            --bg-panel: #1e1e1e;      /* Surface */
            --bg-header: #1e1e1e;
            --border-color: #2c2c2c;  /* Subtle separation */
            --bg-row-even: rgba(255, 255, 255, 0.03); 
            --bg-hover: rgba(255, 255, 255, 0.06);

            --text-primary: #e0e0e0;  
            --text-secondary: #a0a0a0;
            
            /* Status Colors */
            --color-success: #4caf50; /* Green */
            --color-danger: #f44336;  /* Red */
            --color-warning: #ff9800; /* Orange */
            --color-info: #2196f3;    /* Blue */
            --color-white: #ffffff;   /* Open/Neutral */
            
            --font-ui: 'Inter', sans-serif;
            --font-mono: 'JetBrains Mono', monospace;
        }}

        * {{ box-sizing: border-box; outline: none; }}

        body {{ 
            font-family: var(--font-ui); margin: 0; background-color: var(--bg-body);
            color: var(--text-primary); font-size: 13px; line-height: 1.5;
        }}

        /* Header */
        #header {{ 
            background-color: var(--bg-header); border-bottom: 1px solid var(--border-color);
            padding: 0 24px; height: 70px; display: flex; justify-content: space-between; align-items: center;
            position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }}
        .header-left, .header-right {{ display: flex; align-items: center; gap: 20px; }}
        
        .brand {{ display: flex; align-items: center; gap: 12px; }}
        .logo-icon {{ 
            width: 28px; height: 28px; fill: var(--color-info); 
            filter: drop-shadow(0 0 4px rgba(33, 150, 243, 0.4));
        }}
        
        .brand-text {{ display: flex; flex-direction: column; justify-content: center; }}
        .brand h1 {{ margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.5px; color: var(--text-primary); line-height: 1.2; }}
        .brand-subtitle {{ font-size: 11px; color: var(--text-secondary); font-weight: 400; letter-spacing: 0.3px; }}

        .creator-tag {{
            font-size: 11px; font-weight: 500; color: var(--text-secondary); opacity: 0.7; margin-right: 10px;
        }}

        /* Controls */
        .controls {{ display: flex; gap: 10px; }}
        .btn {{ 
            display: inline-flex; align-items: center; padding: 6px 16px;
            color: var(--text-primary); text-decoration: none; border-radius: 4px;
            font-size: 12px; font-weight: 500; border: 1px solid var(--border-color); 
            background: #252525; transition: all 0.2s;
        }}
        .btn:hover {{ background: #333; border-color: #555; }}
        .btn-primary {{ background: var(--color-info); border-color: var(--color-info); color: white; }}
        .btn-primary:hover {{ background: #1976d2; }}

        /* Tabs */
        #tabs-header {{ padding: 20px 24px 0; display: flex; gap: 20px; border-bottom: 1px solid var(--border-color); }}
        .tab-button {{
            background: transparent; border: none; padding: 10px 4px; cursor: pointer;
            font-size: 13px; font-weight: 500; color: var(--text-secondary); 
            position: relative; font-family: var(--font-ui);
        }}
        .tab-button:hover {{ color: var(--text-primary); }}
        .tab-button.active {{ color: var(--color-info); }}
        .tab-button.active::after {{
            content: ''; position: absolute; bottom: -1px; left: 0; width: 100%; height: 2px; background: var(--color-info);
        }}

        /* Layout */
        #main-container {{ padding: 20px 24px; max-width: 100%; }}
        .tab-pane {{ display: none; animation: fadeIn 0.2s ease; }}
        .tab-pane.active {{ display: block; }}
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: translateY(0); }} }}

        .pane-header {{ margin-bottom: 16px; }}
        .pane-header h2 {{ margin: 0; font-size: 15px; font-weight: 600; }}
        .pane-header p {{ margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }}

        /* Tables */
        .table-container {{
            border: 1px solid var(--border-color); border-radius: 6px; overflow: hidden; background: var(--bg-panel);
        }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        thead th {{ 
            background-color: #252525; color: var(--text-secondary); font-weight: 600; 
            text-align: left; padding: 12px 16px; border-bottom: 1px solid var(--border-color);
            text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px;
            position: sticky; top: 0; z-index: 10;
        }}
        tbody td {{ padding: 10px 16px; border-bottom: 1px solid var(--border-color); color: var(--text-primary); }}
        tbody tr:nth-child(even) {{ background-color: var(--bg-row-even); }}
        tbody tr:hover {{ background-color: var(--bg-hover); }}

        /* Badges */
        .badge {{
            display: inline-flex; align-items: center; padding: 3px 10px;
            border-radius: 4px; font-size: 11px; font-weight: 600; 
            line-height: 1; text-transform: uppercase;
        }}
        .badge-open {{ background: rgba(255,255,255,0.1); color: var(--color-white); border: 1px solid rgba(255,255,255,0.2); }}
        .badge-danger {{ background: rgba(244, 67, 54, 0.1); color: var(--color-danger); border: 1px solid rgba(244, 67, 54, 0.2); }}
        .badge-success {{ background: rgba(76, 175, 80, 0.1); color: var(--color-success); border: 1px solid rgba(76, 175, 80, 0.2); }}
        .badge-warning {{ background: rgba(255, 152, 0, 0.1); color: var(--color-warning); border: 1px solid rgba(255, 152, 0, 0.2); }}
        .badge-neutral {{ background: rgba(255, 255, 255, 0.05); color: var(--text-secondary); border: 1px solid rgba(255, 255, 255, 0.1); }}

        /* Typography & Utilities */
        .font-mono {{ font-family: var(--font-mono); font-size: 12px; }}
        .text-dim {{ color: var(--text-secondary); }}
        .val-cell {{ font-family: var(--font-mono); font-weight: 500; letter-spacing: -0.5px; }}
        
        /* Status Dot */
        .status-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 8px; }}
        .status-dot.connected {{ background: var(--color-success); box-shadow: 0 0 6px rgba(76, 175, 80, 0.4); }}
        .status-dot.disconnected {{ background: var(--color-danger); }}
        .status-dot.connecting {{ background: var(--color-warning); animation: pulse 1s infinite; }}
        @keyframes pulse {{ 50% {{ opacity: 0.5; }} }}

        /* New Row Animation */
        .event-row-new {{ animation: highlight 1.5s ease-out; }}
        @keyframes highlight {{ from {{ background-color: rgba(33, 150, 243, 0.2); }} to {{ background-color: transparent; }} }}

    </style>
</head>
<body>
    <div id="header">
        <div class="header-left">
            <div class="brand">
                <svg class="logo-icon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                    <path d="M13 2L3 14H12L11 22L21 10H12L13 2Z"/>
                </svg>
                <div class="brand-text">
                    <h1>Gateway Monitor Pro</h1>
                    <span class="brand-subtitle">Gateway IEC 61850 ke IEC 60870-5-104</span>
                </div>
            </div>
        </div>
        <div class="header-right">
            <span class="creator-tag">Created By Ridwan</span>
            <div class="controls">
                <a href="/download/process" class="btn" target="_blank">Process Log</a>
                <a href="/download/events" class="btn btn-primary" target="_blank">Event Log</a>
            </div>
        </div>
    </div>

    <div id="tabs-header">
        <button class="tab-button active" id="btn-dashboard-tab" onclick="openTab(event, 'dashboard-tab')">Dashboard</button>
        <button class="tab-button" id="btn-event-log-tab" onclick="openTab(event, 'event-log-tab')">Live Events</button>
    </div>

    <div id="main-container">
        <div id="dashboard-tab" class="tab-pane active">
            <div class="pane-header">
                <h2>IED Connection Status</h2>
                <p>Real-time monitoring of connected devices.</p>
            </div>
            <div class="table-container">
                <table id="dashboard-table">
                    <thead>
                        <tr>
                            <th style="width: 40%">IED Address</th>
                            <th style="width: 30%">Status</th>
                            <th style="width: 30%">Last Activity</th>
                        </tr>
                    </thead>
                    <tbody id="dashboard-tbody"></tbody>
                </table>
            </div>
        </div>
        
        <div id="event-log-tab" class="tab-pane">
            <div class="pane-header">
                <h2>Event Log</h2>
                <p>Recent binary events and system alerts.</p>
            </div>
            <div class="table-container">
                <table class="event-log-table">
                    <thead>
                        <tr>
                            <th style="width: 140px">Time</th>
                            <th style="width: 150px">IED</th>
                            <th style="width: 70px">IOA</th>
                            <th style="width: 200px">Address (MMS)</th>
                            <th>Signal Name</th>
                            <th style="width: 90px">Value</th>
                            <th style="width: 120px">Status</th>
                        </tr>
                    </thead>
                    <tbody id="event-tbody"></tbody>
                </table>
            </div>
        </div>

        <div id="tab-content"></div>
    </div>

    <script>
        const WEBSOCKET_PORT = """ + str(WEBSOCKET_PORT) + """;
        const STATUS_LABELS = {
            'DPI': { 0: 'Invalid', 1: 'Open', 2: 'Close', 3: 'Intermediate' },
            'SPI': { 0: 'Disappear', 1: 'Appear' }
        };
        
        const tabContent = document.getElementById('tab-content');
        const eventTbody = document.getElementById('event-tbody');
        const dashboardTbody = document.getElementById('dashboard-tbody');
        const tabsHeader = document.getElementById('tabs-header');
        const iedTabs = {};
        const ws = new WebSocket(`ws://${window.location.hostname}:${WEBSOCKET_PORT}`);

        function openTab(evt, tabName) {
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
            document.getElementById(tabName).classList.add('active');
            evt.currentTarget.classList.add('active');
        }

        function getStatusDesc(dataType, value) {
            if (value === 'INVALID') return 'Link Fail';
            if (STATUS_LABELS[dataType] && STATUS_LABELS[dataType][value] !== undefined) {
                return STATUS_LABELS[dataType][value];
            }
            return '';
        }

        function getBadgeClass(statusDesc) {
            const s = statusDesc.toLowerCase();
            if (s.includes('open') || s.includes('appear')) return 'badge-open';
            if (s.includes('close') || s.includes('disappear')) return 'badge-danger';
            if (s.includes('fail') || s.includes('invalid')) return 'badge-warning';
            if (s.includes('connect')) return 'badge-success';
            return 'badge-neutral';
        }

        function ensureIedTabExists(iedId) {
            const tabId = `ied-tab-${iedId.replace(/[^a-zA-Z0-9]/g, '-')}`;
            if (!iedTabs[iedId]) {
                const btn = document.createElement('button');
                btn.className = 'tab-button';
                btn.setAttribute('onclick', `openTab(event, '${tabId}')`);
                btn.innerHTML = `<span class="status-dot"></span> ${iedId}`;
                tabsHeader.insertBefore(btn, document.getElementById('btn-event-log-tab'));

                const pane = document.createElement('div');
                pane.id = tabId;
                pane.className = 'tab-pane';
                pane.innerHTML = `
                    <div class="pane-header"><h2>${iedId} Data Points</h2></div>
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>
                                    <th style="width:80px">IOA</th>
                                    <th>Signal Name</th>
                                    <th>MMS Addr</th>
                                    <th>Value</th>
                                    <th>Status</th>
                                    <th>Time</th>
                                </tr>
                            </thead>
                            <tbody id="tbody-${tabId}"></tbody>
                        </table>
                    </div>`;
                tabContent.appendChild(pane);
                iedTabs[iedId] = { button: btn, tbody: document.getElementById(`tbody-${tabId}`) };
            }
            return iedTabs[iedId];
        }

        function updateIedStatus(iedId, status, timestamp) {
            const ied = ensureIedTabExists(iedId);
            ied.button.querySelector('.status-dot').className = `status-dot ${status.toLowerCase()}`;
            
            let row = document.getElementById(`dash-row-${iedId}`);
            if (!row) {
                row = dashboardTbody.insertRow();
                row.id = `dash-row-${iedId}`;
                row.innerHTML = `<td class="font-mono" style="font-weight:500">${iedId}</td><td id="dst-${iedId}"></td><td id="dtm-${iedId}" class="font-mono text-dim"></td>`;
            }
            
            let badge = 'badge-neutral';
            if (status === 'CONNECTED') badge = 'badge-success';
            else if (status === 'DISCONNECTED') badge = 'badge-danger';
            else if (status === 'CONNECTING') badge = 'badge-warning';
            
            document.getElementById(`dst-${iedId}`).innerHTML = `<span class="badge ${badge}">${status}</span>`;
            document.getElementById(`dtm-${iedId}`).textContent = timestamp;
            
            addEventToTable({timestamp, ied_id: iedId, ioa: '-', address: '-', signal: 'Connection Status', value: status, data_type: 'SYSTEM'}, true);
        }

        function addEventToTable(data, isNewUpdate) {
            const row = eventTbody.insertRow(0);
            if (isNewUpdate) {
                row.className = 'event-row-new';
            }
            
            let statusDesc = data.value, badgeClass = 'badge-neutral';
            
            if (data.data_type !== 'SYSTEM') {
                statusDesc = getStatusDesc(data.data_type, data.value);
            }
            badgeClass = getBadgeClass(statusDesc);
            if(data.data_type === 'SYSTEM') badgeClass = (data.value === 'CONNECTED') ? 'badge-success' : 'badge-danger';

            row.innerHTML = `
                <td class="font-mono text-dim">${data.timestamp.split(' ')[1]}</td>
                <td style="font-weight:500">${data.ied_id}</td>
                <td class="font-mono">${data.ioa}</td>
                <td class="font-mono text-dim" style="font-size:11px">${data.address || '-'}</td>
                <td>${data.signal}</td>
                <td class="val-cell">${(typeof data.value === 'number') ? data.value.toFixed(3) : '-'}</td>
                <td><span class="badge ${badgeClass}">${statusDesc}</span></td>
            `;
            
            if (eventTbody.rows.length > 50) eventTbody.deleteRow(50);
        }

        ws.onmessage = function(e) {
            const data = JSON.parse(e.data);
            
            if (data.type === 'event_history') {
                while (eventTbody.firstChild) eventTbody.removeChild(eventTbody.firstChild);
                // FIX: DO NOT REVERSE HERE. 
                // Data comes as [Oldest, ..., Newest].
                // We iterate sequentially:
                // 1. Take Oldest -> Insert at Top -> [Oldest]
                // 2. Take Newest -> Insert at Top -> [Newest, Oldest]
                // Result: Newest at Top (Correct)
                data.data.forEach(evt => addEventToTable(evt, false)); 
                return;
            }
            
            if (!data.ied_id) return;
            
            if (data.type === 'status_update') {
                updateIedStatus(data.ied_id, data.status, data.timestamp);
            } else if (data.type === 'data_update' || data.type === 'invalidation') {
                const ied = ensureIedTabExists(data.ied_id);
                let row = document.getElementById(`row-${data.ioa}`);
                const statusDesc = getStatusDesc(data.data_type, data.value);
                const badgeClass = getBadgeClass(statusDesc);

                if (!row) {
                    row = ied.tbody.insertRow();
                    row.id = `row-${data.ioa}`;
                    row.innerHTML = `<td class="font-mono">${data.ioa}</td><td id="s-${data.ioa}"></td><td id="a-${data.ioa}" class="font-mono text-dim"></td><td id="v-${data.ioa}" class="val-cell"></td><td id="st-${data.ioa}"></td><td id="t-${data.ioa}" class="font-mono text-dim"></td>`;
                }
                
                document.getElementById(`s-${data.ioa}`).textContent = data.signal || '-';
                document.getElementById(`a-${data.ioa}`).textContent = data.address;
                
                const vCell = document.getElementById(`v-${data.ioa}`);
                vCell.textContent = (typeof data.value === 'number') ? data.value.toFixed(3) : data.value;
                vCell.style.color = (data.value === 'INVALID') ? 'var(--color-danger)' : 'var(--color-info)';
                
                document.getElementById(`st-${data.ioa}`).innerHTML = statusDesc ? `<span class="badge ${badgeClass}">${statusDesc}</span>` : '';
                document.getElementById(`t-${data.ioa}`).textContent = data.timestamp.split(' ')[1];
                
                if (data.is_event !== false) addEventToTable(data, true); 
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
