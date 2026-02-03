# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, jsonify
import paho.mqtt.client as mqtt
import threading
import time
import sys
import io
import os  # pour self-ping si besoin

# Force UTF-8 sur stdout/stderr sous Windows (utile localement)
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
            # VERSION2 par défaut (supprime le DeprecationWarning)
            client = mqtt.Client(
                client_id=f"flask-kcomat-{int(time.time())}"
            )

            client.on_connect = on_connect
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.tls_set()  # TLS obligatoire pour CloudAMQP port 8883

            print("Tentative de connexion MQTT...")
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_start()

            # Attente max 10s pour CONNACK
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

# Optionnel : self-ping pour éviter le spin-down sur Render Free
# Décommente si tu veux (mais UptimeRobot est mieux)
"""
def keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL", "https://maison-commande.onrender.com")
    while True:
        try:
            requests.get(url, timeout=10)
            print("Self-ping envoyé")
        except Exception as e:
            print(f"Self-ping erreur: {e}")
        time.sleep(600)  # 10 minutes

if os.getenv("RENDER"):
    threading.Thread(target=keep_alive, daemon=True).start()
"""

# Initialisation MQTT au démarrage
init_mqtt_client()

if __name__ == '__main__':
    # Pour développement local seulement (Render utilise gunicorn)
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
