# -*- coding: utf-8 -*-
import base64
import io
import json
import os
import sqlite3
import sys
import threading
import time
import zipfile
import cv2
import numpy as np
from imageio import get_writer
import tempfile
import subprocess
from io import BytesIO
from flask import send_file
from datetime import datetime, timezone
from functools import wraps

import paho.mqtt.client as mqtt
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO
from werkzeug.security import check_password_hash, generate_password_hash

# Force UTF-8 sur stdout/stderr sous Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "change-me-in-production-2026")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Base SQLite
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "db.sqlite3")

# Identifiants par defaut
DEFAULT_ADMIN_USERNAME = "ADMIN"
DEFAULT_ADMIN_PASSWORD = "ENSET2026"

# Configuration CloudAMQP
MQTT_BROKER = "fuji.lmq.cloudamqp.com"
MQTT_PORT = 8883
MQTT_USER = "abmejjwc:abmejjwc"
MQTT_PASS = "uEsqO9J-NpIhNoHnMy9_rSRfUE9oFaGH"

# Topics MQTT - Automatisation maison
MQTT_TOPIC_DEVANTURE = "maison/devanture/control"
MQTT_TOPIC_SALON = "maison/salon/control"
MQTT_TOPIC_CHAMBRE = "maison/chambre/control"
MQTT_TOPIC_PRISE_SALON = "maison/salon/prise/control"
MQTT_TOPIC_PRISE_CHAMBRE = "maison/chambre/prise/control"

# Topics MQTT - ESP32-CAM
MQTT_TOPIC_ESP32CAM_FRAME = "maison/esp32cam/frame"
MQTT_TOPIC_ESP32CAM_STATUS = "maison/esp32cam/status"
ESP32CAM_SOURCE_LABEL = f"mqtt://{MQTT_TOPIC_ESP32CAM_FRAME}"

# Variables MQTT globales
client = None
client_lock = threading.Lock()
connected_event = threading.Event()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def get_db():
    if "db" not in g:
        g.db = get_db_connection()
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def table_has_column(conn, table_name, column_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def ensure_column(conn, table_name, column_sql):
    column_name = column_sql.split()[0]
    if not table_has_column(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")

# ====================== NOUVELLE ROUTE HTTP POUR ESP32-CAM ======================
@app.route("/upload_frame", methods=["POST"])
def upload_frame():
    device_id = request.headers.get("X-Device-ID", "esp32cam-1")
    
    if not request.data or len(request.data) < 100:
        return jsonify({"success": False, "message": "Frame vide"}), 400

    try:
        cam_stream_state.push_frame(request.data, device_id, int(time.time()))
        cam_recorder.record_frame(request.data)

        socketio.emit("cam_status", {
            "device_id": device_id,
            "last_frame_size": len(request.data),
            "received_at": utc_now_iso(),
        })

        return jsonify({"success": True, "size": len(request.data)}), 200

    except Exception as e:
        print(f"Erreur upload_frame: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

def init_db():
    conn = get_db_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS esp32cam_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                stream_url TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT 'esp32cam-1',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS camera_recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_url TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                frame_count INTEGER NOT NULL DEFAULT 0,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS camera_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id INTEGER NOT NULL,
                frame_index INTEGER NOT NULL,
                captured_at TEXT NOT NULL,
                frame_data BLOB NOT NULL,
                FOREIGN KEY(recording_id) REFERENCES camera_recordings(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_camera_frames_recording
                ON camera_frames(recording_id, frame_index);

            CREATE TABLE IF NOT EXISTS camera_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                frame_data BLOB NOT NULL,
                source_url TEXT NOT NULL
            );
            """
        )

        ensure_column(conn, "esp32cam_config", "device_id TEXT NOT NULL DEFAULT 'esp32cam-1'")
        ensure_column(conn, "esp32cam_config", "stream_url TEXT NOT NULL DEFAULT ''")

        now = utc_now_iso()

        user_row = conn.execute(
            "SELECT id FROM users WHERE UPPER(username) = UPPER(?)",
            (DEFAULT_ADMIN_USERNAME,),
        ).fetchone()
        if user_row is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (
                    DEFAULT_ADMIN_USERNAME,
                    generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                    now,
                ),
            )

        default_device_id = os.getenv("ESP32CAM_DEVICE_ID", "esp32cam-1")
        config_row = conn.execute("SELECT id FROM esp32cam_config WHERE id = 1").fetchone()
        if config_row is None:
            conn.execute(
                "INSERT INTO esp32cam_config (id, stream_url, device_id, updated_at) VALUES (1, ?, ?, ?)",
                ("", default_device_id, now),
            )
        else:
            conn.execute(
                "UPDATE esp32cam_config SET device_id = COALESCE(NULLIF(device_id, ''), ?), updated_at = ? WHERE id = 1",
                (default_device_id, now),
            )

        conn.commit()
    finally:
        conn.close()


def get_camera_config():
    row = get_db().execute(
        "SELECT device_id, updated_at FROM esp32cam_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {
            "device_id": "esp32cam-1",
            "updated_at": utc_now_iso(),
        }
    return dict(row)


def update_camera_device_id(device_id):
    now = utc_now_iso()
    db = get_db()
    db.execute(
        "UPDATE esp32cam_config SET device_id = ?, updated_at = ? WHERE id = 1",
        (device_id.strip(), now),
    )
    db.commit()


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


def api_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"success": False, "message": "Non authentifie"}), 401
        return view_func(*args, **kwargs)

    return wrapped


class ESP32CamRecorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.db_conn = None
        self.recording_id = None
        self.frame_count = 0
        self.last_error = None

    def start(self, source_label):
        with self.lock:
            if self.recording_id is not None:
                return False, "Enregistrement deja en cours", self.recording_id

            conn = get_db_connection()
            now = utc_now_iso()
            cursor = conn.execute(
                """
                INSERT INTO camera_recordings (stream_url, status, started_at, frame_count)
                VALUES (?, 'recording', ?, 0)
                """,
                (source_label, now),
            )
            conn.commit()

            self.db_conn = conn
            self.recording_id = cursor.lastrowid
            self.frame_count = 0
            self.last_error = None
            return True, "Enregistrement demarre", self.recording_id

    def record_frame(self, frame_bytes):
        with self.lock:
            if self.recording_id is None or self.db_conn is None:
                return

            self.frame_count += 1
            self.db_conn.execute(
                """
                INSERT INTO camera_frames (recording_id, frame_index, captured_at, frame_data)
                VALUES (?, ?, ?, ?)
                """,
                (
                    self.recording_id,
                    self.frame_count,
                    utc_now_iso(),
                    frame_bytes,
                ),
            )

            if self.frame_count % 10 == 0:
                self.db_conn.execute(
                    "UPDATE camera_recordings SET frame_count = ? WHERE id = ?",
                    (self.frame_count, self.recording_id),
                )
                self.db_conn.commit()

    def stop(self):
        with self.lock:
            if self.recording_id is None or self.db_conn is None:
                return False, "Aucun enregistrement en cours", None

            current_id = self.recording_id
            self.db_conn.execute(
                """
                UPDATE camera_recordings
                SET status = 'completed', ended_at = ?, frame_count = ?
                WHERE id = ?
                """,
                (utc_now_iso(), self.frame_count, current_id),
            )
            self.db_conn.commit()
            self.db_conn.close()
            self.db_conn = None
            self.recording_id = None
            return True, "Enregistrement arrete", current_id

    def status_payload(self):
        with self.lock:
            return {
                "is_recording": self.recording_id is not None,
                "recording_id": self.recording_id,
                "frame_count": self.frame_count,
                "last_error": self.last_error,
            }


class ESP32CamStreamState:
    def __init__(self):
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.sequence = 0
        self.latest_frame = None
        self.latest_device_id = None
        self.latest_sent_ts = None
        self.latest_received_iso = None
        self.latest_frame_size = 0

    def push_frame(self, frame_bytes, device_id, sent_ts):
        with self.condition:
            self.sequence += 1
            self.latest_frame = frame_bytes
            self.latest_device_id = device_id
            self.latest_sent_ts = sent_ts
            self.latest_received_iso = utc_now_iso()
            self.latest_frame_size = len(frame_bytes)
            self.condition.notify_all()

    def get_latest_frame(self):
        with self.lock:
            return self.latest_frame

    def wait_next_frame(self, last_sequence, timeout=15):
        with self.condition:
            if self.sequence <= last_sequence:
                self.condition.wait(timeout=timeout)
            if self.sequence <= last_sequence or self.latest_frame is None:
                return None, last_sequence
            return self.latest_frame, self.sequence

    def status_payload(self):
        with self.lock:
            return {
                "sequence": self.sequence,
                "last_device_id": self.latest_device_id,
                "last_sent_ts": self.latest_sent_ts,
                "last_received_at": self.latest_received_iso,
                "last_frame_size": self.latest_frame_size,
                "has_frame": self.latest_frame is not None,
            }


cam_recorder = ESP32CamRecorder()
cam_stream_state = ESP32CamStreamState()


def handle_esp32cam_frame_message(raw_payload):
    try:
        payload_text = raw_payload.decode("utf-8", errors="ignore").strip()
        if not payload_text:
            return

        message = json.loads(payload_text)
        if isinstance(message, dict):
            frame_b64 = message.get("frame")
            device_id = str(message.get("device_id") or message.get("device") or "esp32cam-1")
            sent_ts = message.get("ts")
        else:
            frame_b64 = None
            device_id = "esp32cam-1"
            sent_ts = None

        if not frame_b64:
            return

        frame_bytes = base64.b64decode(frame_b64, validate=True)
        if len(frame_bytes) < 8:
            return

        cam_stream_state.push_frame(frame_bytes, device_id, sent_ts)
        cam_recorder.record_frame(frame_bytes)

        socketio.emit(
            "cam_status",
            {
                "device_id": device_id,
                "last_frame_size": len(frame_bytes),
                "received_at": utc_now_iso(),
            },
        )
    except Exception as exc:
        print(f"Erreur decodage frame ESP32-CAM MQTT: {exc}")


# MQTT Callbacks
def on_connect(mqtt_client, _userdata, _flags, reason_code, _properties):
    rc_value = getattr(reason_code, "value", reason_code)
    is_success = (rc_value == 0) or (str(reason_code).strip().lower() == "success")
    if is_success:
        print("MQTT - Connexion reussie (CONNACK recu)")
        connected_event.set()
        mqtt_client.subscribe(MQTT_TOPIC_ESP32CAM_FRAME, qos=0)
        mqtt_client.subscribe(MQTT_TOPIC_ESP32CAM_STATUS, qos=0)
        print(f"MQTT - Subscribe camera: {MQTT_TOPIC_ESP32CAM_FRAME}")
    else:
        print(f"Echec connexion MQTT - code rc={reason_code}")
        connected_event.clear()


def on_message(_mqtt_client, _userdata, msg):
    if msg.topic == MQTT_TOPIC_ESP32CAM_FRAME:
        handle_esp32cam_frame_message(msg.payload)
        return

    if msg.topic == MQTT_TOPIC_ESP32CAM_STATUS:
        try:
            payload_text = msg.payload.decode("utf-8", errors="ignore")
            payload_json = json.loads(payload_text) if payload_text else {}
        except Exception:
            payload_json = {"raw": msg.payload.decode("utf-8", errors="ignore")}

        socketio.emit("cam_device_status", payload_json)


def init_mqtt_client():
    global client
    with client_lock:
        if client is not None:
            return
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"flask-kcomat-{int(time.time())}",
            )
            client.on_connect = on_connect
            client.on_message = on_message
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.tls_set()
            print("Tentative de connexion MQTT...")
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_start()
            if not connected_event.wait(timeout=10):
                print("Timeout : pas de CONNACK recu apres 10s")
                client.loop_stop()
                client.disconnect()
                client = None
                return
            print("Client MQTT initialise avec succes")
        except Exception as exc:
            print(f"Erreur lors de l'initialisation MQTT : {exc}")
            client = None


def publish_message(topic, message):
    global client
    if client is None:
        init_mqtt_client()
    if client is None or not connected_event.is_set():
        print("Impossible de publier : client MQTT non connecte")
        return False
    try:
        result = client.publish(topic, message, qos=1)
        result.wait_for_publish(timeout=5.0)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"Message publie OK sur {topic} : {message}")
            return True
        print(f"Erreur lors du publish - rc={result.rc}")
        return False
    except Exception as exc:
        print(f"Erreur pendant publish : {exc}")
        return False

def convert_frames_to_video(frames_data, output_path, fps=10):
    """
    Convertit une liste de frames JPEG en vidéo MP4
    frames_data: liste de bytes (contenu JPEG)
    output_path: chemin de sortie pour la vidéo
    fps: images par seconde
    """
    if not frames_data:
        return False
    
    # Décoder la première frame pour obtenir les dimensions
    first_frame = cv2.imdecode(np.frombuffer(frames_data[0], np.uint8), cv2.IMREAD_COLOR)
    if first_frame is None:
        return False
    
    height, width = first_frame.shape[:2]
    
    # Utiliser imageio avec ffmpeg pour créer la vidéo
    writer = get_writer(output_path, fps=fps, format='mp4', codec='libx264')
    
    for frame_data in frames_data:
        # Décoder l'image JPEG
        img = cv2.imdecode(np.frombuffer(frame_data, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            # Convertir BGR (OpenCV) en RGB (imageio)
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            writer.append_data(img_rgb)
    
    writer.close()
    return True


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            flash("Veuillez remplir tous les champs.", "error")
            return render_template("login.html")

        user = get_db().execute(
            "SELECT id, username, password_hash FROM users WHERE UPPER(username) = UPPER(?)",
            (username,),
        ).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Identifiants invalides.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/account/password", methods=["POST"])
@api_login_required
def change_password():
    payload = request.get_json(silent=True) or {}
    current_password = payload.get("current_password") or ""
    new_password = payload.get("new_password") or ""
    confirm_password = payload.get("confirm_password") or ""

    if not current_password or not new_password or not confirm_password:
        return jsonify({"success": False, "message": "Tous les champs mot de passe sont requis."}), 400

    if new_password != confirm_password:
        return jsonify({"success": False, "message": "La confirmation du nouveau mot de passe ne correspond pas."}), 400

    if len(new_password) < 8:
        return jsonify({"success": False, "message": "Le nouveau mot de passe doit contenir au moins 8 caracteres."}), 400

    if new_password == current_password:
        return jsonify({"success": False, "message": "Le nouveau mot de passe doit etre different de l'ancien."}), 400

    user_id = session.get("user_id")
    db = get_db()
    user = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return jsonify({"success": False, "message": "Utilisateur introuvable."}), 404

    if not check_password_hash(user["password_hash"], current_password):
        return jsonify({"success": False, "message": "Mot de passe actuel incorrect."}), 403

    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    db.commit()
    return jsonify({"success": True, "message": "Mot de passe modifie avec succes."})


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username", "ADMIN"))


@app.route("/control", methods=["POST"])
@api_login_required
def control():
    lampe = request.form.get("lampe")
    action = request.form.get("action")

    if not lampe or not action:
        return jsonify({"success": False, "message": "Parametres manquants"}), 400

    if lampe == "devanture":
        topic = MQTT_TOPIC_DEVANTURE
    elif lampe == "salon":
        topic = MQTT_TOPIC_SALON
    elif lampe == "chambre":
        topic = MQTT_TOPIC_CHAMBRE
    elif lampe == "prise_salon":
        topic = MQTT_TOPIC_PRISE_SALON
    elif lampe == "prise_chambre":
        topic = MQTT_TOPIC_PRISE_CHAMBRE
    else:
        return jsonify({"success": False, "message": "Appareil inconnu"}), 400

    success = publish_message(topic, action)

    if success:
        socketio.emit("lamp_update", {"lampe": lampe, "action": action})
        return jsonify({"success": True, "message": f"{action} envoye"}), 200
    return jsonify({"success": False, "message": "Echec envoi MQTT"}), 503


@app.route("/api/esp32cam/config", methods=["GET", "POST"])
@api_login_required
def esp32cam_config():
    if request.method == "GET":
        cfg = get_camera_config()
        return jsonify(
            {
                "success": True,
                "device_id": cfg["device_id"],
                "updated_at": cfg["updated_at"],
                "frame_topic": MQTT_TOPIC_ESP32CAM_FRAME,
                "status_topic": MQTT_TOPIC_ESP32CAM_STATUS,
            }
        )

    payload = request.get_json(silent=True) or {}
    device_id = (payload.get("device_id") or "").strip()

    if not device_id:
        return jsonify({"success": False, "message": "device_id requis"}), 400

    update_camera_device_id(device_id)
    return jsonify({"success": True, "message": "Device ID ESP32-CAM mis a jour"})


@app.route("/api/esp32cam/status", methods=["GET"])
@api_login_required
def esp32cam_status():
    stream_info = cam_stream_state.status_payload()
    recorder_info = cam_recorder.status_payload()
    return jsonify({"success": True, "stream": stream_info, "recorder": recorder_info})


@app.route("/api/esp32cam/record/start", methods=["POST"])
@api_login_required
def esp32cam_record_start():
    if not cam_stream_state.status_payload()["has_frame"]:
        return jsonify({"success": False, "message": "Aucune frame recue. Verifiez la carte ESP32-CAM."}), 409

    ok, message, rid = cam_recorder.start(ESP32CAM_SOURCE_LABEL)
    status_code = 200 if ok else 409
    return jsonify({"success": ok, "message": message, "recording_id": rid}), status_code


@app.route("/api/esp32cam/record/stop", methods=["POST"])
@api_login_required
def esp32cam_record_stop():
    ok, message, rid = cam_recorder.stop()
    status_code = 200 if ok else 409
    return jsonify({"success": ok, "message": message, "recording_id": rid}), status_code


@app.route("/api/esp32cam/recordings", methods=["GET"])
@api_login_required
def esp32cam_recordings():
    rows = get_db().execute(
        """
        SELECT id, status, stream_url, started_at, ended_at, frame_count, error_message
        FROM camera_recordings
        ORDER BY id DESC
        LIMIT 25
        """
    ).fetchall()
    return jsonify({"success": True, "recordings": [dict(row) for row in rows]})


@app.route("/api/esp32cam/recordings/cleanup", methods=["POST"])
@api_login_required
def esp32cam_recordings_cleanup():
    payload = request.get_json(silent=True) or {}
    keep_last = payload.get("keep_last", 3)
    try:
        keep_last = max(0, int(keep_last))
    except (TypeError, ValueError):
        keep_last = 3

    db = get_db()
    rows = db.execute(
        """
        SELECT id
        FROM camera_recordings
        WHERE status != 'recording'
        ORDER BY id DESC
        LIMIT -1 OFFSET ?
        """,
        (keep_last,),
    ).fetchall()
    ids_to_delete = [row["id"] for row in rows]

    if not ids_to_delete:
        return jsonify(
            {
                "success": True,
                "deleted_recordings": 0,
                "deleted_frames": 0,
                "message": "Aucun ancien enregistrement a supprimer.",
            }
        )

    placeholders = ",".join("?" for _ in ids_to_delete)
    deleted_frames = db.execute(
        f"SELECT COUNT(*) AS n FROM camera_frames WHERE recording_id IN ({placeholders})",
        ids_to_delete,
    ).fetchone()["n"]
    db.execute(
        f"DELETE FROM camera_recordings WHERE id IN ({placeholders})",
        ids_to_delete,
    )
    db.commit()

    return jsonify(
        {
            "success": True,
            "deleted_recordings": len(ids_to_delete),
            "deleted_frames": deleted_frames,
            "message": f"{len(ids_to_delete)} enregistrement(s) supprime(s).",
        }
    )

@app.route("/api/esp32cam/snapshots/cleanup", methods=["POST"])
@api_login_required
def esp32cam_snapshots_cleanup():
    payload = request.get_json(silent=True) or {}
    keep_last = payload.get("keep_last", 3)
    try:
        keep_last = max(0, int(keep_last))
    except (TypeError, ValueError):
        keep_last = 3

    db = get_db()
    rows = db.execute(
        """
        SELECT id
        FROM camera_snapshots
        ORDER BY id DESC
        LIMIT -1 OFFSET ?
        """,
        (keep_last,),
    ).fetchall()
    ids_to_delete = [row["id"] for row in rows]

    if not ids_to_delete:
        return jsonify({
            "success": True,
            "deleted_snapshots": 0,
            "message": "Aucune ancienne capture a supprimer.",
        })

    placeholders = ",".join("?" for _ in ids_to_delete)
    db.execute(
        f"DELETE FROM camera_snapshots WHERE id IN ({placeholders})",
        ids_to_delete,
    )
    db.commit()

    return jsonify({
        "success": True,
        "deleted_snapshots": len(ids_to_delete),
        "message": f"{len(ids_to_delete)} capture(s) supprimee(s).",
    })

@app.route("/api/esp32cam/snapshots/<int:snapshot_id>/download", methods=["GET"])
@login_required
def esp32cam_snapshot_download(snapshot_id):
    """Télécharger une photo individuelle"""
    row = get_db().execute(
        "SELECT frame_data, captured_at FROM camera_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    
    if row is None:
        return jsonify({"success": False, "message": "Capture introuvable"}), 404
    
    # Créer un nom de fichier avec la date
    filename = f"snapshot_{snapshot_id}_{row['captured_at'].replace(':', '-').replace('.', '-')}.jpg"
    
    return send_file(
        BytesIO(row["frame_data"]),
        mimetype="image/jpeg",
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/esp32cam/snapshots/download/all", methods=["GET"])
@login_required
def esp32cam_snapshots_download_all():
    """Télécharger toutes les photos dans un fichier ZIP"""
    rows = get_db().execute(
        "SELECT id, frame_data, captured_at FROM camera_snapshots ORDER BY id DESC"
    ).fetchall()
    
    if not rows:
        return jsonify({"success": False, "message": "Aucune capture disponible"}), 404
    
    # Créer le fichier ZIP en mémoire
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for row in rows:
            filename = f"snapshot_{row['id']}_{row['captured_at'].replace(':', '-').replace('.', '-')}.jpg"
            zip_file.writestr(filename, row["frame_data"])
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"snapshots_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    )


@app.route("/api/esp32cam/recordings/<int:recording_id>/download", methods=["GET"])
@login_required
def esp32cam_recording_download(recording_id):
    """Télécharger une vidéo complète au format MP4"""
    # Récupérer les infos de l'enregistrement
    recording = get_db().execute(
        "SELECT id, started_at, frame_count, status FROM camera_recordings WHERE id = ?",
        (recording_id,),
    ).fetchone()
    
    if recording is None:
        return jsonify({"success": False, "message": "Enregistrement introuvable"}), 404
    
    # Récupérer toutes les frames
    frames = get_db().execute(
        "SELECT frame_index, frame_data FROM camera_frames WHERE recording_id = ? ORDER BY frame_index ASC",
        (recording_id,),
    ).fetchall()
    
    if not frames:
        return jsonify({"success": False, "message": "Aucune frame trouvée"}), 404
    
    # Créer un fichier temporaire pour la vidéo
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
        temp_video_path = tmp_file.name
    
    try:
        # Convertir les frames en vidéo
        frames_data = [frame["frame_data"] for frame in frames]
        fps = 10  # 10 images par seconde (ajustable)
        
        success = convert_frames_to_video(frames_data, temp_video_path, fps)
        
        if not success:
            return jsonify({"success": False, "message": "Erreur lors de la création de la vidéo"}), 500
        
        # Envoyer le fichier vidéo
        timestamp = recording['started_at'].replace(':', '-').replace('.', '-')
        filename = f"recording_{recording_id}_{timestamp}.mp4"
        
        return send_file(
            temp_video_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=filename
        )
    
    finally:
        # Nettoyer le fichier temporaire après l'envoi
        def cleanup():
            try:
                os.unlink(temp_video_path)
            except:
                pass
        # Planifier le nettoyage (Flask le fera après l'envoi)
        if hasattr(request, 'after_request'):
            request.after_request(lambda response: cleanup() or response)


@app.route("/api/esp32cam/recordings/download/all", methods=["GET"])
@login_required
def esp32cam_recordings_download_all():
    """Télécharger toutes les vidéos dans un fichier ZIP (chaque vidéo en MP4)"""
    recordings = get_db().execute(
        "SELECT id, started_at, frame_count FROM camera_recordings WHERE status = 'completed' ORDER BY id DESC"
    ).fetchall()
    
    if not recordings:
        return jsonify({"success": False, "message": "Aucun enregistrement disponible"}), 404
    
    zip_buffer = BytesIO()
    temp_videos = []
    
    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for recording in recordings:
                # Récupérer les frames
                frames = get_db().execute(
                    "SELECT frame_data FROM camera_frames WHERE recording_id = ? ORDER BY frame_index ASC",
                    (recording['id'],),
                ).fetchall()
                
                if not frames:
                    continue
                
                # Créer un fichier temporaire pour la vidéo
                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
                    temp_video_path = tmp_file.name
                    temp_videos.append(temp_video_path)
                
                # Convertir en vidéo
                frames_data = [frame["frame_data"] for frame in frames]
                success = convert_frames_to_video(frames_data, temp_video_path, fps=10)
                
                if success:
                    # Ajouter la vidéo au ZIP
                    video_filename = f"recording_{recording['id']}_{recording['started_at'].replace(':', '-').replace('.', '-')}.mp4"
                    with open(temp_video_path, 'rb') as video_file:
                        zip_file.writestr(video_filename, video_file.read())
        
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"all_recordings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
    
    finally:
        # Nettoyer les fichiers temporaires
        for temp_path in temp_videos:
            try:
                os.unlink(temp_path)
            except:
                pass

@app.route("/api/esp32cam/recordings/<int:recording_id>/stream", methods=["GET"])
@login_required
def esp32cam_recording_stream(recording_id):
    fps = max(1, min(int(request.args.get("fps", 8)), 30))
    delay = 1.0 / fps

    def generate():
        conn = get_db_connection()
        try:
            cursor = conn.execute(
                "SELECT frame_data FROM camera_frames WHERE recording_id = ? ORDER BY frame_index ASC",
                (recording_id,),
            )
            for row in cursor:
                frame = row["frame_data"]
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                time.sleep(delay)
        finally:
            conn.close()

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/esp32cam/recordings/<int:recording_id>/snapshot", methods=["GET"])
@login_required
def esp32cam_recording_snapshot(recording_id):
    row = get_db().execute(
        """
        SELECT frame_data
        FROM camera_frames
        WHERE recording_id = ?
        ORDER BY frame_index DESC
        LIMIT 1
        """,
        (recording_id,),
    ).fetchone()

    if row is None:
        return jsonify({"success": False, "message": "Aucune image pour cet enregistrement"}), 404

    return Response(row["frame_data"], mimetype="image/jpeg")


@app.route("/api/esp32cam/recordings/config", methods=["GET", "POST"])
@api_login_required
def esp32cam_recordings_config():
    """Configurer les paramètres d'enregistrement vidéo"""
    if request.method == "GET":
        # Lire la configuration
        config = get_db().execute(
            "SELECT value FROM app_config WHERE key = 'video_fps'"
        ).fetchone()
        fps = int(config["value"]) if config else 10
        
        return jsonify({
            "success": True,
            "fps": fps
        })
    
    else:
        # Mettre à jour la configuration
        data = request.get_json(silent=True) or {}
        fps = data.get("fps", 10)
        
        get_db().execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES ('video_fps', ?)",
            (str(fps),)
        )
        get_db().commit()
        
        return jsonify({"success": True, "message": "Configuration mise à jour"})


@app.route("/api/esp32cam/snapshot", methods=["POST"])
@api_login_required
def esp32cam_capture_snapshot():
    frame = cam_stream_state.get_latest_frame()
    if frame is None:
        return jsonify({"success": False, "message": "Aucune image recue depuis MQTT"}), 409

    db = get_db()
    db.execute(
        "INSERT INTO camera_snapshots (captured_at, frame_data, source_url) VALUES (?, ?, ?)",
        (utc_now_iso(), frame, ESP32CAM_SOURCE_LABEL),
    )
    db.commit()
    return jsonify({"success": True, "message": "Capture enregistree en base"})


@app.route("/api/esp32cam/snapshots", methods=["GET"])
@api_login_required
def esp32cam_snapshots():
    rows = get_db().execute(
        """
        SELECT id, captured_at, source_url
        FROM camera_snapshots
        ORDER BY id DESC
        LIMIT 30
        """
    ).fetchall()
    return jsonify({"success": True, "snapshots": [dict(row) for row in rows]})


@app.route("/api/esp32cam/snapshots/<int:snapshot_id>", methods=["GET"])
@login_required
def esp32cam_snapshot_image(snapshot_id):
    row = get_db().execute(
        "SELECT frame_data FROM camera_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return jsonify({"success": False, "message": "Capture introuvable"}), 404
    return Response(row["frame_data"], mimetype="image/jpeg")


@app.route("/esp32cam/live", methods=["GET"])
@login_required
def esp32cam_live_proxy():
    def generate():
        seq = 0
        while True:
            frame, seq = cam_stream_state.wait_next_frame(seq, timeout=15)
            if frame is None:
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# Initialisation au demarrage
init_db()
init_mqtt_client()

if __name__ == "__main__":
    debug_enabled = os.getenv("FLASK_DEBUG", "0") == "1"
    port = int(os.getenv("PORT", "5000"))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=debug_enabled,
        allow_unsafe_werkzeug=True,
    )
