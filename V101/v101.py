#!/usr/bin/env python3
# v100.py - Rilis UI Carbon Final: Fix Sorting & Clean Values
# Deskripsi: Gateway IEC 61850 ke IEC 60870-5-104 dengan sinkronisasi status INVALID.

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

# --- Helper: Ekstraksi Nilai Angka Bersih ---
def find_first_float(data):
    """Mencari angka pertama dalam list/dict MMS yang kompleks."""
    if isinstance(data, (float, int)): return float(data)
    if isinstance(data, list):
        for item in data:
            res = find_first_float(item)
            if res is not None: return res
    if isinstance(data, dict):
        for val in data.values():
            res = find_first_float(val)
            if res is not None: return res
    return None

# --- Helper Function untuk Timestamp ---
def get_timestamp_with_ms(timestamp_ms=None):
    if timestamp_ms is None:
        timestamp_ms = time.time() * 1000
    ts_seconds = timestamp_ms / 1000
    base_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_seconds))
    return "{}.{:03d}".format(base_time, int(timestamp_ms % 1000))

# --- Helper Function untuk Status Deskripsi ---
def get_status_description(data_type, value):
    if value == 'INVALID': return "Link Fail"
    try: val_int = int(value)
    except: return ""
    if data_type == 'DPI':
        return {0: 'Invalid', 1: 'Open', 2: 'Close', 3: 'Intermediate'}.get(val_int, '')
    elif data_type == 'SPI':
        return {0: 'Disappear', 1: 'Appear'}.get(val_int, '')
    return ""

# --- Setup Logging ---
def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(PROCESS_LOG_FILE, mode='a')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False
    evt_handler = logging.FileHandler(EVENT_LOG_FILE, mode='a')
    evt_handler.setFormatter(logging.Formatter('%(message)s'))
    event_logger.addHandler(evt_handler)

# --- Server Web (aiohttp) ---
async def handle_http_get(request):
    try: return web.FileResponse('./index.html')
    except: return web.Response(status=404, text="index.html tidak ditemukan.")

async def start_aiohttp_server(port):
    app = web.Application()
    app.router.add_get('/', handle_http_get)
    app.router.add_get('/download/process', lambda r: web.FileResponse(PROCESS_LOG_FILE))
    app.router.add_get('/download/events', lambda r: web.FileResponse(EVENT_LOG_FILE))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"HTTP server berjalan di http://0.0.0.0:{port}")
    return runner

# --- WebSocket Handler ---
async def websocket_handler(websocket, path):
    websocket_clients.add(websocket)
    async with cache_lock:
        for update in list(connection_status_cache.values()) + list(realtime_data_cache.values()):
            await websocket.send(json.dumps(update))
        if recent_events_cache:
            await websocket.send(json.dumps({'type': 'event_history', 'data': list(recent_events_cache)}))
    try: await websocket.wait_closed()
    finally: websocket_clients.remove(websocket)

async def broadcast_updates_task(queue):
    while not shutdown_event.is_set():
        try:
            update = await asyncio.wait_for(queue.get(), timeout=1.0)
            async with cache_lock:
                if update.get('type') == 'status_update':
                    connection_status_cache[update['ied_id']] = update
                elif update.get('type') in ['data_update', 'invalidation']:
                    realtime_data_cache[update['ioa']] = update
                    if update.get('is_event', True): recent_events_cache.append(update)
            if websocket_clients:
                await asyncio.gather(*[c.send(json.dumps(update)) for c in websocket_clients], return_exceptions=True)
            queue.task_done()
        except asyncio.TimeoutError: continue

# --- Core Processor ---
def process_data_update(ied_id, key, data):
    if not isinstance(data, dict) or 'value' not in data: return
    gateway_timestamp_ms = data.get('timestamp', time.time() * 1000)
    
    mms_path = key
    if "iec61850://" in key: mms_path = urlparse(key).path.lstrip('/')

    # Ekstraksi nilai bersih (Fix image_928a5c.png)
    final_value = find_first_float(data['value'])
    if final_value is None: return

    valid_ioas = ied_to_ioas_map.get(ied_id, [])
    for config_path, ioa in mms_to_ioa_map.items():
        if ioa in valid_ioas and config_path.startswith(mms_path):
            try:
                ioa_type_class = iec104_server.IOA_list.get(ioa, {}).get('type')
                data_type = 'DPI' if "Double" in str(ioa_type_class) else 'SPI' if "Single" in str(ioa_type_class) else 'MEAS'
                is_measurement = (data_type == 'MEAS')

                # Inversion & Normalization
                value_to_send = float(final_value)
                if data_type == 'DPI': value_to_send = {1.0: 1, 2.0: 2}.get(value_to_send, 0)
                elif data_type == 'SPI': value_to_send = 1 if int(value_to_send) != 0 else 0
                if ioa_inversion_map.get(ioa) and data_type == 'DPI':
                    value_to_send = {1: 2, 2: 1}.get(value_to_send, value_to_send)

                formatted_ts = get_timestamp_with_ms(gateway_timestamp_ms)
                
                # Update ke SCADA 104 & Web
                iec104_server.update_ioa(ioa, value_to_send, timestamp=gateway_timestamp_ms)
                
                update_payload = {
                    'type': 'data_update', 'ied_id': ied_id, 'ioa': ioa, 
                    'signal': ioa_to_signal_name_map.get(ioa), 'value': value_to_send, 
                    'timestamp': formatted_ts, 'address': ioa_to_full_address_map.get(ioa),
                    'is_event': not is_measurement, 'data_type': data_type
                }
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)

                if not is_measurement:
                    event_logger.info(f"{formatted_ts} | {ied_id} | IOA: {ioa} | {ioa_to_signal_name_map.get(ioa)} | Val: {value_to_send} | {get_status_description(data_type, value_to_send)}")
            except Exception as e: logging.error(f"Error IOA {ioa}: {e}")

# --- Invalidation Logic (Fix image_935496.png) ---
def do_invalidation(ied_id):
    if ied_id in ied_to_ioas_map:
        ioas = ied_to_ioas_map[ied_id]
        formatted_ts = get_timestamp_with_ms()
        for ioa in ioas:
            # 1. Kirim status INVALID ke 104
            iec104_server.update_ioa(ioa, "INVALID")
            
            # 2. Update Web Dashboard
            ioa_obj = iec104_server.IOA_list.get(ioa, {})
            dt = 'DPI' if "Double" in str(ioa_obj.get('type')) else 'SPI' if "Single" in str(ioa_obj.get('type')) else 'MEAS'
            
            update_payload = {
                'type': 'invalidation', 'ied_id': ied_id, 'ioa': ioa, 'signal': ioa_to_signal_name_map.get(ioa),
                'value': 'INVALID', 'timestamp': formatted_ts, 'address': ioa_to_full_address_map.get(ioa),
                'is_event': (dt != 'MEAS'), 'data_type': dt
            }
            if main_loop and main_loop.is_running():
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, update_payload)

# --- IED Connection Handlers ---
def ied_data_callback(key, data, ied_id):
    if main_loop and processing_queue and main_loop.is_running():
        main_loop.call_soon_threadsafe(processing_queue.put_nowait, {'type': 'process_data', 'ied_id': ied_id, 'key': key, 'data': data})

def broadcast_connection_status(ied_id, status):
    formatted_ts = get_timestamp_with_ms()
    payload = {'type': 'status_update', 'ied_id': ied_id, 'status': status, 'timestamp': formatted_ts}
    if main_loop and main_loop.is_running(): main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, payload)
    event_logger.info(f"{formatted_ts} | {ied_id} | SYSTEM | Connection | {status}")

async def data_processor_task(queue):
    while not shutdown_event.is_set():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
            if item.get('type') == 'process_data': process_data_update(item['ied_id'], item['key'], item['data'])
            elif item.get('type') == 'invalidate': do_invalidation(item['ied_id'])
            queue.task_done()
        except asyncio.TimeoutError: continue

async def ied_handler(ied_id, uris):
    while not shutdown_event.is_set():
        try:
            broadcast_connection_status(ied_id, "CONNECTING")
            client = libiec61850client.iec61850client(readvaluecallback=lambda k,v: ied_data_callback(k,v,ied_id), Rpt_cb=lambda k,v: ied_data_callback(k,v,ied_id))
            if await main_loop.run_in_executor(None, client.getIED, ied_id.split(':')[0], int(ied_id.split(':')[1])) != 0: raise ConnectionError()
            
            broadcast_connection_status(ied_id, "CONNECTED")
            for uri in uris: client.registerReadValue(str(uri))

            while not shutdown_event.is_set():
                con = client.getRegisteredIEDs().get(ied_id, {}).get('con')
                if not con or IedConnection_getState(con) != 2: raise ConnectionError()
                await main_loop.run_in_executor(None, client.poll)
                await asyncio.sleep(0.2)
        except:
            broadcast_connection_status(ied_id, "DISCONNECTED")
            main_loop.call_soon_threadsafe(processing_queue.put_nowait, {'type': 'invalidate', 'ied_id': ied_id})
            await asyncio.sleep(RECONNECT_DELAY)

# --- Main Application ---
async def main_async():
    global iec104_server, main_loop, processing_queue, broadcast_queue
    main_loop = asyncio.get_running_loop()
    processing_queue, broadcast_queue = asyncio.Queue(), asyncio.Queue()
    
    config = configparser.ConfigParser(); config.optionxform = str; config.read('config.local.ini')
    await start_aiohttp_server(HTTP_PORT)
    await websockets.serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT)
    
    iec104_server = libiec60870server.IEC60870_5_104_server()
    ied_groups = {}
    data_types = {'measuredvaluescaled': MeasuredValueScaled, 'measuredvaluefloat': MeasuredValueShort, 'singlepointinformation': SinglePointInformation, 'doublepointinformation': DoublePointInformation}
    
    for sect, mms_t in data_types.items():
        if sect in config:
            for ioa, val in config[sect].items():
                ioa_int = int(ioa)
                sig, addr = val.split(':', 1) if ':' in val else ("N/A", val)
                ioa_to_signal_name_map[ioa_int], ioa_to_full_address_map[ioa_int] = sig.strip(), addr.strip()
                p = urlparse(addr.split('#')[0]); ied_id = f"{p.hostname}:{p.port or 102}"
                ied_to_ioas_map.setdefault(ied_id, []).append(ioa_int)
                mms_to_ioa_map[p.path.lstrip('/')] = ioa_int
                ied_groups.setdefault(ied_id, set()).add(addr.split('#')[0])
                if ':invers=true' in addr: ioa_inversion_map[ioa_int] = True
                iec104_server.add_ioa(ioa_int, mms_t, 0, None, True)

    threading.Thread(target=iec104_server.start, daemon=True).start()
    tasks = [asyncio.create_task(data_processor_task(processing_queue)), asyncio.create_task(broadcast_updates_task(broadcast_queue))]
    tasks.extend([asyncio.create_task(ied_handler(ied, uris)) for ied, uris in ied_groups.items()])
    await asyncio.gather(*tasks)

# --- UI Generation ---
def create_index_html_if_not_exists():
    logging.info("Membuat file index.html UI V55 (Carbon Theme)...")
    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Gateway Monitor Pro v100</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=JetBrains+Mono&display=swap" rel="stylesheet">
    <style>
        :root {{ --bg-body: #121212; --bg-panel: #1e1e1e; --border: #2c2c2c; --text: #e0e0e0; --blue: #2196f3; --red: #f44336; --green: #4caf50; --orange: #ff9800; }}
        body {{ font-family: 'Inter', sans-serif; background: var(--bg-body); color: var(--text); margin: 0; font-size: 13px; }}
        #header {{ background: var(--bg-panel); padding: 15px 25px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); }}
        .tab-button {{ background: none; border: none; color: #a0a0a0; padding: 15px; cursor: pointer; font-weight: 600; border-bottom: 2px solid transparent; }}
        .tab-button.active {{ color: var(--blue); border-bottom-color: var(--blue); }}
        .tab-pane {{ display: none; padding: 25px; }}
        .tab-pane.active {{ display: block; }}
        .table-container {{ background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ background: #252525; padding: 12px; text-align: left; font-size: 11px; text-transform: uppercase; color: #a0a0a0; }}
        td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); }}
        .badge {{ padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
        .badge-open {{ background: rgba(255,255,255,0.1); color: white; }}
        .badge-danger {{ background: rgba(244,67,54,0.1); color: var(--red); }}
        .badge-success {{ background: rgba(76,175,80,0.1); color: var(--green); }}
        .badge-warning {{ background: rgba(255,152,0,0.1); color: var(--orange); }}
        .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
    </style>
</head>
<body>
    <div id="header">
        <div><h1 style="margin:0; font-size:18px;">Gateway Monitor Pro <small style="font-weight:400; opacity:0.6;">v100</small></h1></div>
        <div style="opacity:0.6; font-size:11px;">Created By Ridwan</div>
    </div>
    <div style="border-bottom: 1px solid var(--border); padding: 0 25px;">
        <button class="tab-button active" onclick="openTab(event, 'dashboard-tab')">Dashboard</button>
        <button class="tab-button" onclick="openTab(event, 'event-tab')">Live Events</button>
        <span id="ied-tabs"></span>
    </div>
    <div id="main">
        <div id="dashboard-tab" class="tab-pane active">
            <div class="table-container"><table><thead><tr><th>IED Address</th><th>Status</th><th>Last Update</th></tr></thead><tbody id="dash-tbody"></tbody></table></div>
        </div>
        <div id="event-tab" class="tab-pane">
            <div class="table-container"><table><thead><tr><th>Time</th><th>IED</th><th>IOA</th><th>Signal</th><th>Value</th><th>Status</th></tr></thead><tbody id="evt-tbody"></tbody></table></div>
        </div>
        <div id="ied-panes"></div>
    </div>
    <script>
        const ws = new WebSocket('ws://' + window.location.hostname + ':' + {WEBSOCKET_PORT});
        const iedPanes = {{}};
        function openTab(evt, name) {{
            document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab-button').forEach(b => b.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            if(evt) evt.currentTarget.classList.add('active');
        }}
        ws.onmessage = (e) => {{
            const d = JSON.parse(e.data);
            if(d.type === 'status_update') {{
                let row = document.getElementById('dash-' + d.ied_id);
                if(!row) {{
                    row = document.getElementById('dash-tbody').insertRow(); row.id = 'dash-' + d.ied_id;
                    row.innerHTML = `<td class="font-mono">${{d.ied_id}}</td><td id="st-${{d.ied_id}}"></td><td class="font-mono">${{d.timestamp}}</td>`;
                    const btn = document.createElement('button'); btn.className = 'tab-button'; btn.textContent = d.ied_id;
                    btn.onclick = (ev) => openTab(ev, 'pane-' + d.ied_id.replace(/[:.]/g,'-'));
                    document.getElementById('ied-tabs').appendChild(btn);
                    const p = document.createElement('div'); p.id = 'pane-' + d.ied_id.replace(/[:.]/g,'-'); p.className = 'tab-pane';
                    p.innerHTML = `<div class="table-container"><table><thead><tr><th>IOA</th><th>Signal</th><th>Value</th><th>Time</th></tr></thead><tbody id="tbody-${{d.ied_id}}"></tbody></table></div>`;
                    document.getElementById('ied-panes').appendChild(p);
                }}
                const b = d.status === 'CONNECTED' ? 'badge-success' : 'badge-danger';
                document.getElementById('st-' + d.ied_id).innerHTML = `<span class="badge ${{b}}">${{d.status}}</span>`;
            }}
            if(d.type === 'data_update' || d.type === 'invalidation') {{
                const tb = document.getElementById('tbody-' + d.ied_id);
                if(tb) {{
                    let r = document.getElementById('ioa-' + d.ioa);
                    if(!r) {{ r = tb.insertRow(); r.id = 'ioa-' + d.ioa; r.innerHTML = `<td class="font-mono">${{d.ioa}}</td><td>${{d.signal}}</td><td id="v-${{d.ioa}}" class="font-mono"></td><td class="font-mono">${{d.timestamp.split(' ')[1]}}</td>`; }}
                    const vCell = document.getElementById('v-' + d.ioa);
                    vCell.textContent = d.value; vCell.style.color = d.value === 'INVALID' ? 'var(--red)' : 'var(--blue)';
                }}
                if(d.is_event !== false) {{
                    const er = document.getElementById('evt-tbody').insertRow(0);
                    er.innerHTML = `<td>${{d.timestamp.split(' ')[1]}}</td><td>${{d.ied_id}}</td><td>${{d.ioa}}</td><td>${{d.signal}}</td><td class="font-mono">${{d.value}}</td><td><span class="badge ${{d.value === 'INVALID' ? 'badge-warning' : 'badge-open'}}">${{d.value === 'INVALID' ? 'Link Fail' : d.value}}</span></td>`;
                    if(document.getElementById('evt-tbody').rows.length > 50) document.getElementById('evt-tbody').deleteRow(50);
                }}
            }}
        }};
    </script>
</body>
</html>
    """
    with open("index.html", "w") as f: f.write(html_content)

async def shutdown(sig, loop):
    shutdown_event.set()
    if iec104_server: iec104_server.stop()
    await asyncio.sleep(1)
    loop.stop()

if __name__ == '__main__':
    setup_logging()
    create_index_html_if_not_exists()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s, loop)))
    try: loop.run_until_complete(main_async())
    finally: loop.close()
