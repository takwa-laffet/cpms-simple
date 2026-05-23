import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'ocpp-simulator-secret-key-change-in-production'
CORS(app)  # Enable CORS for all routes

# Global state for the simulator
simulator_state = {
    'connected': False,
    'cp_id': None,
    'ws_url': None,
    'websocket': None,
    'simulating': False,
    'messages': [],
    'meter_values': [],
    'transaction_id': None,
    'id_tag': None,
    'connector_id': 1,
    'meter_start': 0
}

# In-memory message store for demo
ocpp_messages = []

def add_message(direction, message_type, payload=None):
    """Add a message to the message log"""
    msg = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'direction': direction,  # 'sent' or 'received'
        'type': message_type,
        'payload': payload or {}
    }
    ocpp_messages.append(msg)
    # Keep only last 100 messages
    if len(ocpp_messages) > 100:
        ocpp_messages.pop(0)
    logger.info(f"OCPP {direction}: {message_type}")

def simulate_ocpp_messages(cp_id, ws_url):
    """Simulate OCPP message exchange"""
    global simulator_state
    
    simulator_state['cp_id'] = cp_id
    simulator_state['ws_url'] = ws_url
    simulator_state['connected'] = True
    simulator_state['id_tag'] = f"SIM_{uuid.uuid4().hex[:8].upper()}"
    simulator_state['meter_start'] = 0
    
    add_message('sent', 'BootNotification', {
        'charge_point_model': 'OCPP Simulator Model',
        'charge_point_vendor': 'OCPP Simulator Vendor',
        'firmware_version': '1.0'
    })
    
    # Simulate accepting BootNotification
    time.sleep(0.1)
    add_message('received', 'BootNotification', {
        'current_time': datetime.now(timezone.utc).isoformat(),
        'interval': 10,
        'status': 'Accepted'
    })
    
    add_message('sent', 'Heartbeat')
    time.sleep(0.1)
    add_message('received', 'Heartbeat', {
        'current_time': datetime.now(timezone.utc).isoformat()
    })
    
    # Simulate status changes
    add_message('sent', 'StatusNotification', {
        'connector_id': simulator_state['connector_id'],
        'error_code': 'NoError',
        'status': 'Available',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    
    simulator_state['simulating'] = True
    
    # Simulate a charging session
    time.sleep(2)
    add_message('sent', 'StatusNotification', {
        'connector_id': simulator_state['connector_id'],
        'error_code': 'NoError',
        'status': 'Preparing',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    
    time.sleep(1)
    add_message('sent', 'StatusNotification', {
        'connector_id': simulator_state['connector_id'],
        'error_code': 'NoError',
        'status': 'Charging',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    
    # Start transaction
    add_message('sent', 'StartTransaction', {
        'connector_id': simulator_state['connector_id'],
        'id_tag': simulator_state['id_tag'],
        'meter_start': simulator_state['meter_start'],
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    
    simulator_state['transaction_id'] = 12345
    time.sleep(0.1)
    add_message('received', 'StartTransaction', {
        'transaction_id': simulator_state['transaction_id'],
        'id_tag_info': {'status': 'Accepted'}
    })
    
    # Simulate meter values during charging
    for i in range(5):
        if not simulator_state['simulating']:
            break
        energy_wh = simulator_state['meter_start'] + (i + 1) * 1000
        add_message('sent', 'MeterValues', {
            'connector_id': simulator_state['connector_id'],
            'meter_value': [{
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'sampled_value': [
                    {'value': str(energy_wh), 'unit': 'Wh'}
                ]
            }]
        })
        time.sleep(3)
    
    # Stop transaction
    add_message('sent', 'StopTransaction', {
        'meter_stop': simulator_state['meter_start'] + 5000,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'transaction_id': simulator_state['transaction_id']
    })
    
    time.sleep(0.1)
    add_message('received', 'StopTransaction', {
        'id_tag_info': {'status': 'Accepted'}
    })
    
    simulator_state['transaction_id'] = None
    
    # Finish simulation
    time.sleep(1)
    add_message('sent', 'StatusNotification', {
        'connector_id': simulator_state['connector_id'],
        'error_code': 'NoError',
        'status': 'Available',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    
    simulator_state['simulating'] = False
    add_message('simulation_end', 'Simulation Complete', {})

@app.route('/')
def index():
    """Serve the frontend"""
    return render_template('index.html')

@app.route('/api/ocpp/connect', methods=['POST'])
def connect():
    """Start OCPP simulation"""
    global simulator_state
    
    if simulator_state['connected']:
        return jsonify({'error': 'Already connected'}), 400
    
    data = request.get_json()
    cp_id = data.get('cp_id')
    ws_url = data.get('ws_url', 'ws://localhost:5000')
    
    if not cp_id:
        return jsonify({'error': 'cp_id is required'}), 400
    
    # Reset state
    simulator_state.update({
        'connected': False,
        'cp_id': None,
        'ws_url': None,
        'simulating': False,
        'transaction_id': None,
        'messages': []
    })
    ocpp_messages.clear()
    
    # Start simulation in background thread
    thread = threading.Thread(target=simulate_ocpp_messages, args=(cp_id, ws_url))
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'connected', 'cp_id': cp_id})

@app.route('/api/ocpp/disconnect', methods=['POST'])
def disconnect():
    """Stop OCPP simulation"""
    global simulator_state
    simulator_state['connected'] = False
    simulator_state['simulating'] = False
    simulator_state['cp_id'] = None
    simulator_state['ws_url'] = None
    simulator_state['transaction_id'] = None
    return jsonify({'status': 'disconnected'})

@app.route('/api/ocpp/messages', methods=['GET'])
def get_messages():
    """Get OCPP message log"""
    return jsonify({
        'messages': ocpp_messages,
        'state': simulator_state
    })

@app.route('/api/ocpp/send', methods=['POST'])
def send_custom_message():
    """Send a custom OCPP message"""
    data = request.get_json()
    message_type = data.get('type')
    payload = data.get('payload', {})
    
    if not message_type:
        return jsonify({'error': 'Message type is required'}), 400
    
    add_message('sent', message_type, payload)
    
    # Simulate response for certain message types
    if message_type == 'Heartbeat':
        time.sleep(0.1)
        add_message('received', 'Heartbeat', {
            'current_time': datetime.now(timezone.utc).isoformat()
        })
    elif message_type == 'BootNotification':
        time.sleep(0.1)
        add_message('received', 'BootNotification', {
            'current_time': datetime.now(timezone.utc).isoformat(),
            'interval': 10,
            'status': 'Accepted'
        })
    
    return jsonify({'status': 'sent'})

@app.route('/api/ocpp/reset', methods=['POST'])
def reset():
    """Reset simulator state"""
    global simulator_state, ocpp_messages
    simulator_state.update({
        'connected': False,
        'cp_id': None,
        'ws_url': None,
        'simulating': False,
        'websocket': None,
        'messages': [],
        'meter_values': [],
        'transaction_id': None,
        'id_tag': None,
        'connector_id': 1,
        'meter_start': 0
    })
    ocpp_messages.clear()
    return jsonify({'status': 'reset'})

@app.route('/api/ocpp/status', methods=['GET'])
def get_status():
    """Get current simulator status"""
    return jsonify(simulator_state)

if __name__ == '__main__':
    logger.info("Starting OCPP Simulator Flask App")
    logger.info("Access the simulator at: http://localhost:5001")
    logger.info("Frontend: http://localhost:5001/")
    logger.info("API: http://localhost:5001/api/ocpp/*")
    app.run(host='0.0.0.0', port=5001, debug=True, threaded=True)