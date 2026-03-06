#!/usr/bin/env python3
# v101.py - Professional Edition: Carbon UI + Event Filter + State Tracking
# Deskripsi: Gateway IEC 61850 ke 104 dengan proteksi sinkronisasi thread untuk Master asli.

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
from logging.handlers import RotatingFileHandler

# --- Import Library Vendor ---
try:
    import libiec61850client_cached as libiec61850client
    import libiec60870server
    from lib60870 import *
    from lib61850 import IedConnection_getState
except ImportError as e:
    print(f"Error: Gagal mengimpor library. Pesan: {e}")
    sys.exit(1)

# --- Konfigurasi File & Konstanta ---
PROCESS_LOG_FILE = 'process.txt'
EVENT_LOG_FILE = 'events.txt'
RECONNECT_DELAY = 15
HTTP_PORT, WEBSOCKET_PORT = 8000, 8001

# --- Variabel Global ---
ied_to_ioas_map, mms_to_ioa_map = {}, {}
ioa_to_full_address_map, ioa_to_signal_name_map = {}, {}
ied_last_state = {} 
websocket_clients = set()
realtime_data_cache, connection_status_cache = {}, {}
recent_events_cache = deque(maxlen=50)

processing_queue, broadcast_queue, iec104_server, main_loop = None, None, None, None
cache_lock = asyncio.Lock()
shutdown_event = asyncio.Event()
event_logger = logging.getLogger("event_logger")

# --- [LOGIC] Helper: Ekstraksi & Format ---
def find_first_float(data):
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

def get_timestamp_with_ms(timestamp_ms=None):
    if timestamp_ms is None: timestamp_ms = time.time() * 1000
    ts_seconds = timestamp_ms / 1000
    base_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_seconds))
    return "{}.{:03d}".format(base_time, int(timestamp_ms % 1000))

def get_status_description(data_type, value):
    if value == 'INVALID': return "Link Fail"
    try: val_int = int(value)
    except: return ""
    if data_type == 'DPI':
        return {0: 'Invalid', 1: 'Open', 2: 'Close', 3: 'Intermediate'}.get(val_int, '')
    elif data_type == 'SPI':
        return {0: 'Disappear', 1: 'Appear'}.get(val_int, '')
    return ""

# --- [SYSTEM] Setup Logging ---
def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    file_handler = RotatingFileHandler(PROCESS_LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False
    evt_handler = RotatingFileHandler(EVENT_LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    evt_handler.setFormatter(logging.Formatter('%(message)s'))
    event_logger.addHandler(evt_handler)

# --- [NETWORK] Server Web & WebSocket ---
async def start_aiohttp_server(port):
    app = web.Application()
    app.router.add_get('/', lambda r: web.FileResponse('./index.html'))
    app.router.add_get('/download/process', lambda r: web.FileResponse(PROCESS_LOG_FILE))
    app.router.add_get('/download/events', lambda r: web.FileResponse(EVENT_LOG_FILE))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', port).start()
    logging.info(f"UI Gateway aktif di http://0.0.0.0:{port}")

async def websocket_handler(websocket, path=None):
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

# --- [CORE] Data Processing ---
def process_data_update(ied_id, key, data, mode="Poll"):
    if not isinstance(data, dict) or 'value' not in data: return
    gw_ts_ms = data.get('timestamp', time.time() * 1000)
    mms_path = urlparse(key).path.lstrip('/') if "iec61850://" in key else key
    final_value = find_first_float(data['value'])
    if final_value is None: return

    valid_ioas = ied_to_ioas_map.get(ied_id, [])
    for config_path, ioa in mms_to_ioa_map.items():
        if ioa in valid_ioas and config_path.startswith(mms_path):
            try:
                # Proteksi akses ke IOA_list dengan Lock dari server
                with iec104_server.lock:
                    ioa_conf = iec104_server.IOA_list.get(ioa)
                    if not ioa_conf: continue
                    ioa_type_class = ioa_conf['type']

                data_type = 'DPI' if "Double" in str(ioa_type_class) else 'SPI' if "Single" in str(ioa_type_class) else 'MEAS'
                is_meas = (data_type == 'MEAS')
                
                val_to_send = float(final_value)
                if data_type == 'DPI': val_to_send = {1.0: 1, 2.0: 2}.get(val_to_send, 0)
                elif data_type == 'SPI': val_to_send = 1 if int(val_to_send) != 0 else 0
                
                # Update ke library 104 (fungsi ini internalnya harus pakai lock)
                iec104_server.update_ioa(ioa, val_to_send, timestamp=gw_ts_ms)
                
                fmt_ts = get_timestamp_with_ms(gw_ts_ms)
                payload = {
                    'type': 'data_update', 'ied_id': ied_id, 'ioa': ioa, 
                    'signal': ioa_to_signal_name_map.get(ioa), 'value': val_to_send, 
                    'timestamp': fmt_ts, 'address': ioa_to_full_address_map.get(ioa),
                    'is_event': not is_meas, 'data_type': data_type, 'mode': mode
                }
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, payload)
                if not is_meas:
                    event_logger.info(f"{fmt_ts} | {ied_id} | IOA: {ioa} | {mode} | Val: {val_to_send} | {get_status_description(data_type, val_to_send)}")
            except Exception as e: logging.error(f"Error IOA {ioa}: {e}")

def do_invalidation(ied_id):
    if ied_id in ied_to_ioas_map:
        ioas = ied_to_ioas_map[ied_id]
        fmt_ts = get_timestamp_with_ms()
        for ioa in ioas:
            iec104_server.update_ioa(ioa, "INVALID")
            with iec104_server.lock:
                ioa_obj = iec104_server.IOA_list.get(ioa, {})
                ioa_type = ioa_obj.get('type')
            
            dt = 'DPI' if "Double" in str(ioa_type) else 'SPI' if "Single" in str(ioa_type) else 'MEAS'
            payload = {
                'type': 'invalidation', 'ied_id': ied_id, 'ioa': ioa, 'signal': ioa_to_signal_name_map.get(ioa),
                'value': 'INVALID', 'timestamp': fmt_ts, 'address': ioa_to_full_address_map.get(ioa),
                'is_event': (dt != 'MEAS'), 'data_type': dt, 'mode': 'System'
            }
            if main_loop and main_loop.is_running():
                main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, payload)

# --- [HANDLER] IED Handlers ---
def ied_data_callback(key, data, ied_id, mode):
    if main_loop and processing_queue and main_loop.is_running():
        main_loop.call_soon_threadsafe(processing_queue.put_nowait, {'type': 'process_data', 'ied_id': ied_id, 'key': key, 'data': data, 'mode': mode})

def broadcast_connection_status(ied_id, status, silent=False):
    fmt_ts = get_timestamp_with_ms()
    payload = {'type': 'status_update', 'ied_id': ied_id, 'status': status, 'timestamp': fmt_ts}
    if main_loop and main_loop.is_running(): 
        main_loop.call_soon_threadsafe(broadcast_queue.put_nowait, payload)
    if not silent:
        event_logger.info(f"{fmt_ts} | {ied_id} | SYSTEM | Connection | {status}")

async def data_processor_task(queue):
    while not shutdown_event.is_set():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
            if item.get('type') == 'process_data': process_data_update(item['ied_id'], item['key'], item['data'], item['mode'])
            elif item.get('type') == 'invalidate': do_invalidation(item['ied_id'])
            queue.task_done()
        except asyncio.TimeoutError: continue

async def ied_handler(ied_id, uris):
    global ied_last_state
    while not shutdown_event.is_set():
        try:
            is_new_attempt = ied_last_state.get(ied_id) not in ["CONNECTING", "DISCONNECTED"]
            broadcast_connection_status(ied_id, "CONNECTING", silent=not is_new_attempt)
            
            client = libiec61850client.iec61850client(
                readvaluecallback=lambda k,v: ied_data_callback(k,v,ied_id, mode="Polling"), 
                Rpt_cb=lambda k,v: ied_data_callback(k,v,ied_id, mode="Report")
            )
            
            host, port = ied_id.split(':')
            if await main_loop.run_in_executor(None, client.getIED, host, int(port)) != 0: 
                raise ConnectionError("Discovery Failed")
            
            broadcast_connection_status(ied_id, "CONNECTED")
            ied_last_state[ied_id] = "CONNECTED"
            for uri in uris: client.registerReadValue(str(uri))

            while not shutdown_event.is_set():
                # Polling IED via executor agar tidak blocking event loop
                await main_loop.run_in_executor(None, client.poll)
                
                # Cek status koneksi secara berkala
                con = client.getRegisteredIEDs().get(ied_id, {}).get('con')
                if not con or IedConnection_getState(con) != 2: 
                    raise ConnectionError("Link Lost")
                    
                await asyncio.sleep(0.5)
        except Exception as e:
            if ied_last_state.get(ied_id) != "DISCONNECTED":
                broadcast_connection_status(ied_id, "DISCONNECTED")
                main_loop.call_soon_threadsafe(processing_queue.put_nowait, {'type': 'invalidate', 'ied_id': ied_id})
                ied_last_state[ied_id] = "DISCONNECTED"
            else:
                broadcast_connection_status(ied_id, "DISCONNECTED", silent=True)
            await asyncio.sleep(RECONNECT_DELAY)

# --- [MAIN] Application Entry ---
async def main_async():
    global iec104_server, main_loop, processing_queue, broadcast_queue
    main_loop = asyncio.get_running_loop()
    processing_queue, broadcast_queue = asyncio.Queue(), asyncio.Queue()
    
    config = configparser.ConfigParser(); config.optionxform = str; config.read('config.local.ini')
    await start_aiohttp_server(HTTP_PORT)
    await websockets.serve(websocket_handler, "0.0.0.0", WEBSOCKET_PORT)
    
    iec104_server = libiec60870server.IEC60870_5_104_server()
    ied_groups = {}
    data_types = {
        'measuredvaluescaled': MeasuredValueScaled, 
        'measuredvaluefloat': MeasuredValueShort, 
        'singlepointinformation': SinglePointInformation, 
        'doublepointinformation': DoublePointInformation
    }
    
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
                iec104_server.add_ioa(ioa_int, mms_t, 0, None, True)

    threading.Thread(target=iec104_server.start, daemon=True).start()
    
    tasks = [
        asyncio.create_task(data_processor_task(processing_queue)), 
        asyncio.create_task(broadcast_updates_task(broadcast_queue))
    ]
    tasks.extend([asyncio.create_task(ied_handler(ied, uris)) for ied, uris in ied_groups.items()])
    await asyncio.gather(*tasks)

def create_index_html():
    logging.info("Generating UI Index...")
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Gateway Monitor Pro v101</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono&display=swap" rel="stylesheet">
    <style>
        :root { --bg-body: #121212; --bg-panel: #1e1e1e; --border: #2c2c2c; --text: #e0e0e0; --blue: #2196f3; --red: #f44336; --green: #4caf50; --orange: #ff9800; }
        body { font-family: 'Inter', sans-serif; background: var(--bg-body); color: var(--text); margin: 0; font-size: 13px; }
        #header { background: var(--bg-panel); padding: 15px 25px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); }
        .system-health { display: flex; align-items: center; gap: 15px; background: rgba(0,0,0,0.2); padding: 5px 15px; border-radius: 20px; border: 1px solid var(--border); }
        .heartbeat-icon { color: var(--red); font-size: 14px; animation: beat 1s infinite; }
        @keyframes beat { 0% { transform: scale(1); } 50% { transform: scale(1.2); } 100% { transform: scale(1); } }
        .clock-display { font-family: 'JetBrains Mono', monospace; font-size: 14px; color: var(--blue); min-width: 200px; text-align: right; }
        .tab-bar { display: flex; gap: 20px; padding: 0 25px; background: var(--bg-panel); border-bottom: 1px solid var(--border); overflow-x: auto; }
        .tab-button { background: none; border: none; color: #a0a0a0; padding: 15px 5px; cursor: pointer; font-weight: 500; position: relative; white-space: nowrap; display: flex; align-items: center; }
        .tab-button.active { color: var(--blue); }
        .tab-button.active::after { content: ''; position: absolute; bottom: -1px; left: 0; width: 100%; height: 2px; background: var(--blue); }
        .tab-pane { display: none; padding: 25px; }
        .tab-pane.active { display: block; }
        .table-container { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
        table { width: 100%; border-collapse: collapse; }
        th { background: #252525; padding: 12px 16px; text-align: left; font-size: 11px; text-transform: uppercase; color: #a0a0a0; border-bottom: 1px solid var(--border); }
        td { padding: 10px 16px; border-bottom: 1px solid var(--border); }
        .badge { padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
        .badge-success { background: rgba(76,175,80,0.1); color: var(--green); }
        .badge-danger { background: rgba(244,67,54,0.1); color: var(--red); }
        .badge-warning { background: rgba(255,152,0,0.1); color: var(--orange); }
        .badge-blue { background: rgba(33,150,243,0.1); color: var(--blue); }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 10px; }
        .connected { background: var(--green); box-shadow: 0 0 8px var(--green); }
        .disconnected { background: var(--red); }
        .connecting { background: var(--orange); animation: pulse 1s infinite; }
        @keyframes pulse { 50% { opacity: 0.3; } }
        .btn { padding: 6px 12px; background: #252525; border: 1px solid var(--border); color: #eee; text-decoration: none; border-radius: 4px; font-size: 11px; }
    </style>
</head>
<body>
    <div id="header">
        <div><h1 style="margin:0; font-size:18px;">Gateway Monitor Pro <small style="font-weight:400; opacity:0.5;">v101</small></h1></div>
        <div style="display:flex; gap:20px; align-items:center;">
            <div class="system-health">
                <span class="heartbeat-icon">❤</span>
                <span id="sys-clock" class="clock-display">0000-00-00 00:00:00.000</span>
            </div>
            <a href="/download/process" class="btn">Process Log</a>
            <a href="/download/events" class="btn" style="background:var(--blue); border-color:var(--blue);">Event Log</a>
        </div>
    </div>
    <div class="tab-bar">
        <button class="tab-button active" onclick="openTab(event, 'dash-tab')">Dashboard</button>
        <button class="tab-button" onclick="openTab(event, 'event-tab')">Live Events</button>
        <span id="ied-nav" style="display:flex; gap:20px;"></span>
    </div>
    <div id="dash-tab" class="tab-pane active">
        <div class="table-container"><table><thead><tr><th>IED Address</th><th>Status</th><th>Last Update</th></tr></thead><tbody id="dash-tbody"></tbody></table></div>
    </div>
    <div id="event-tab" class="tab-pane">
        <div class="table-container"><table><thead><tr><th>Time</th><th>IED</th><th>IOA</th><th>Signal</th><th>Mode</th><th>Value</th><th>Status</th></tr></thead><tbody id="evt-tbody"></tbody></table></div>
    </div>
    <div id="ied-panes"></div>

    <script>
        const ws = new WebSocket('ws://' + window.location.hostname + ':8001');
        const STATUS_MAP = { 'DPI': { 0:'Invalid', 1:'Open', 2:'Close', 3:'Intermediate' }, 'SPI': { 0:'Disappear', 1:'Appear' } };
        const iedTabs = {};

        function updateClock() {
            const now = new Date();
            document.getElementById('sys-clock').textContent = now.toLocaleString() + '.' + now.getMilliseconds().toString().padStart(3, '0');
        }
        setInterval(updateClock, 50);

        function openTab(evt, name) {
            document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab-button').forEach(b => b.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            evt.currentTarget.classList.add('active');
        }

        function getStatusDesc(type, val) {
            if (val === 'INVALID') return 'Link Fail';
            return (STATUS_MAP[type] && STATUS_MAP[type][val] !== undefined) ? STATUS_MAP[type][val] : '';
        }

        function getBadgeClass(status) {
            const s = status.toLowerCase();
            if (s.includes('connected') || s.includes('open') || s.includes('appear')) return 'badge-success';
            if (s.includes('disconnected') || s.includes('close') || s.includes('disappear')) return 'badge-danger';
            return 'badge-warning';
        }

        ws.onmessage = (e) => {
            const d = JSON.parse(e.data);
            if(d.type === 'status_update') {
                let row = document.getElementById('dash-' + d.ied_id);
                if(!row) {
                    row = document.getElementById('dash-tbody').insertRow(); row.id = 'dash-' + d.ied_id;
                    row.innerHTML = `<td style="font-family:monospace">${d.ied_id}</td><td id="st-${d.ied_id}"></td><td id="dash-t-${d.ied_id}" style="opacity:0.6">${d.timestamp}</td>`;
                    const btn = document.createElement('button'); btn.className = 'tab-button';
                    btn.onclick = (ev) => openTab(ev, 'pane-' + d.ied_id.replace(/[:.]/g,'-'));
                    btn.innerHTML = `<span class="status-dot"></span>${d.ied_id}`;
                    document.getElementById('ied-nav').appendChild(btn);
                    const p = document.createElement('div'); p.id = 'pane-' + d.ied_id.replace(/[:.]/g,'-'); p.className = 'tab-pane';
                    p.innerHTML = `<div class="table-container"><table><thead><tr><th>IOA</th><th>Signal Name</th><th>Mode</th><th>Value</th><th>Status</th><th>Time</th></tr></thead><tbody id="tbody-${d.ied_id}"></tbody></table></div>`;
                    document.getElementById('ied-panes').appendChild(p);
                    iedTabs[d.ied_id] = btn;
                }
                document.getElementById('st-' + d.ied_id).innerHTML = `<span class="badge ${getBadgeClass(d.status)}">${d.status}</span>`;
                iedTabs[d.ied_id].querySelector('.status-dot').className = `status-dot ${d.status.toLowerCase()}`;
            }
            if(d.type === 'data_update' || d.type === 'invalidation') {
                const tb = document.getElementById('tbody-' + d.ied_id);
                if(tb) {
                    let r = document.getElementById('ioa-' + d.ioa);
                    if(!r) { 
                        r = tb.insertRow(); r.id = 'ioa-' + d.ioa; 
                        r.innerHTML = `<td>${d.ioa}</td><td>${d.signal}</td><td><span class="badge" id="m-${d.ioa}"></span></td><td id="v-${d.ioa}"></td><td id="stcol-${d.ioa}"></td><td id="tcol-${d.ioa}" style="opacity:0.6"></td>`; 
                    }
                    document.getElementById('v-' + d.ioa).textContent = d.value;
                    document.getElementById('m-' + d.ioa).textContent = d.mode;
                    document.getElementById('m-' + d.ioa).className = 'badge ' + (d.mode === 'Report' ? 'badge-blue' : 'badge-neutral');
                    const stText = getStatusDesc(d.data_type, d.value);
                    document.getElementById('stcol-' + d.ioa).innerHTML = stText ? `<span class="badge ${getBadgeClass(stText)}">${stText}</span>` : '';
                    document.getElementById('tcol-' + d.ioa).textContent = d.timestamp.split(' ')[1];
                }
                if(d.is_event !== false) {
                    const er = document.getElementById('evt-tbody').insertRow(0);
                    const st = getStatusDesc(d.data_type, d.value);
                    er.innerHTML = `<td>${d.timestamp.split(' ')[1]}</td><td>${d.ied_id}</td><td>${d.ioa}</td><td>${d.signal}</td><td>${d.mode}</td><td>${d.value}</td><td><span class="badge ${getBadgeClass(st)}">${st}</span></td>`;
                    if(document.getElementById('evt-tbody').rows.length > 50) document.getElementById('evt-tbody').deleteRow(50);
                }
            }
        };
    </script>
</body>
</html>
    """
    with open("index.html", "w") as f: f.write(html_content)

async def shutdown(loop):
    shutdown_event.set()
    if iec104_server: iec104_server.stop()
    await asyncio.sleep(1)
    loop.stop()

if __name__ == '__main__':
    setup_logging()
    create_index_html()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(loop)))
    try: loop.run_until_complete(main_async())
    finally: loop.close()
