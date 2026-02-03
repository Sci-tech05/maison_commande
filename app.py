# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, jsonify
import paho.mqtt.client as mqtt
import threading
import time
import sys
import io

# Force UTF-8 sur stdout/stderr sous Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

app = Flask(__name__)

# ── Configuration CloudAMQP ──
MQTT_BROKER = "fuji.lmq.cloudamqp.com"
MQTT_PORT   = 8883
MQTT_USER   = "abmejjwc:abmejjwc"
MQTT_PASS   = "uEsqO9J-NpIhNoHnMy9_rSRfUE9oFaGH"

MQTT_TOPIC_DEVANTURE = "maison/devanture/control"
MQTT_TOPIC_SALON     = "maison/salon/control"
MQTT_TOPIC_CHAMBRE   = "maison/chambre/control"

# Variables globales
client = None
client_lock = threading.Lock()
connected_event = threading.Event()

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("MQTT - Connexion réussie (CONNACK reçu)")
        connected_event.set()
    else:
        print(f"Échec connexion MQTT - code rc={rc}")
        connected_event.clear()

def init_mqtt_client():
    global client
    with client_lock:
        if client is not None:
            return

        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"flask-kcomat-{int(time.time())}"
            )
            client.on_connect = on_connect
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.tls_set()  # TLS obligatoire pour CloudAMQP port 8883

            print("Tentative de connexion MQTT...")
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_start()

            if not connected_event.wait(timeout=10):
                print("Timeout : pas de CONNACK reçu après 10s")
                client.loop_stop()
                client.disconnect()
                client = None
                return

            print("Client MQTT initialisé avec succès")

        except Exception as e:
            print(f"Erreur lors de l'initialisation MQTT : {e}")
            client = None

def publish_message(topic, message):
    global client

    if client is None:
        init_mqtt_client()

    if client is None or not connected_event.is_set():
        print("Impossible de publier : client MQTT non connecté")
        return False

    try:
        result = client.publish(topic, message, qos=1)
        result.wait_for_publish(timeout=5.0)

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"Message publié OK sur {topic} : {message}")
            return True
        else:
            print(f"Erreur lors du publish - rc={result.rc}")
            return False

    except Exception as e:
        print(f"Erreur pendant publish : {e}")
        return False

# ── Routes Flask ──
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/control', methods=['POST'])
def control():
    lampe = request.form.get('lampe')
    action = request.form.get('action')

    if not lampe or not action:
        return jsonify({'success': False, 'message': 'Paramètres manquants'}), 400

    if lampe == 'devanture':
        topic = MQTT_TOPIC_DEVANTURE
    elif lampe == 'salon':
        topic = MQTT_TOPIC_SALON
    elif lampe == 'chambre':
        topic = MQTT_TOPIC_CHAMBRE
    else:
        return jsonify({'success': False, 'message': 'Lampe inconnue'}), 400

    success = publish_message(topic, action)

    if success:
        return jsonify({'success': True, 'message': f'{action} envoyé'}), 200
    else:
        return jsonify({'success': False, 'message': 'Échec envoi MQTT'}), 503

# Initialisation au démarrage
init_mqtt_client()

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        print("Arrêt demandé par l'utilisateur")
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()
            print("Client MQTT déconnecté proprement")