"""
Drainage Monitoring System — pure-Python dashboard.

Every piece of logic that would normally live in client-side JavaScript
(status classification, gauge drawing, sparklines, alert evaluation,
the "live" polling loop) is implemented here in Python and rendered to
plain HTML/CSS by Flask + Jinja2. The page refreshes itself with a
<meta http-equiv="refresh"> tag — no <script> tags anywhere.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""

import json
import os
import random
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "drainage-safety-session-secret-key-2026")

# ---------------------------------------------------------------------------
# Config — mirrors backend/config.py from the original project
# ---------------------------------------------------------------------------
WATER_LEVEL_WARNING = 70          # % full before a warning alert
WATER_LEVEL_CRITICAL = 90         # % full before a critical alert
FLOW_RATE_LOW = 2.0               # L/min considered "abnormally low"
BLOCKAGE_WATER_LEVEL_MIN = 40     # level above which low flow suggests blockage
DEVICE_OFFLINE_SECONDS = 60       # no data for this long -> offline
API_KEY = "drain-iot-key-2026"    # required in X-API-KEY header for real sensors
HISTORY_LEN = 26                  # points kept for sparklines / trend charts
REFRESH_SECONDS = 5               # how often the page reloads itself

# ---------------------------------------------------------------------------
# In-memory store. Swap this section for real SQLAlchemy models
# (Device / SensorReading / Alert) if you're wiring this into the full
# backend described in the README — the route logic below stays the same.
# ---------------------------------------------------------------------------
devices = {}                                   # device_id -> dict
history = defaultdict(lambda: {"level": deque(maxlen=HISTORY_LEN),
                                "flow": deque(maxlen=HISTORY_LEN)})
net_history = {"level": deque(maxlen=HISTORY_LEN), "flow": deque(maxlen=HISTORY_LEN)}
alerts = {}                                    # alert_id -> dict
_alert_seq = 0
_lock = threading.Lock()
demo_mode = True                               # auto-simulates until real data arrives


def _now():
    return datetime.now(timezone.utc)


SAFETY_FILE = os.path.join(os.path.dirname(__file__), "safety_contacts.json")


def _load_safety_contacts():
    if os.path.exists(SAFETY_FILE):
        try:
            with open(SAFETY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    default_contacts = [
        {
            "id": "sc-init-01",
            "name": "Municipal Emergency Dispatch",
            "email": "safety-control@citydrainage.gov",
            "phone": "+91 94421 88390",
            "zone": "All Zones",
            "role": "Safety Officer",
            "alert_level": "Critical & Warnings",
            "triggers": ["Flood Overflow", "Drainage Blockage"],
            "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }
    ]
    _save_safety_contacts(default_contacts)
    return default_contacts


def _save_safety_contacts(contacts):
    try:
        with open(SAFETY_FILE, "w", encoding="utf-8") as f:
            json.dump(contacts, f, indent=2)
    except Exception as e:
        print(f"Error saving safety contacts: {e}")


def _next_alert_id():
    global _alert_seq
    _alert_seq += 1
    return str(_alert_seq)


# ---------------------------------------------------------------------------
# Ingestion + alert evaluation
# ---------------------------------------------------------------------------
KNOWN_LOCATIONS = {
    "DRAIN-001": "MG Road Junction",
    "DRAIN-002": "RS Puram Outfall",
    "DRAIN-003": "Gandhipuram Culvert",
    "DRAIN-004": "Market Culvert",
    "DRAIN-005": "Bus Stand",
}


def _ingest(device_id, water_level, flow_rate, location=None):
    dev = devices.setdefault(
        device_id,
        {"device_id": device_id, "location": location or KNOWN_LOCATIONS.get(device_id, f"Node {device_id}")}
    )
    dev["water_level"] = round(max(0.0, min(100.0, float(water_level))), 1)
    dev["flow_rate"] = round(max(0.0, float(flow_rate)), 1)
    dev["last_seen"] = _now()
    dev["online"] = True
    if location:
        dev["location"] = location

    h = history[device_id]
    h["level"].append(dev["water_level"])
    h["flow"].append(dev["flow_rate"])

    _evaluate_device_alerts(dev)


def _set_alert(key, device_id, severity, message):
    existing = next((a for a in alerts.values() if a["key"] == key and not a["resolved"]), None)
    if existing:
        existing["message"] = message
        return
    aid = _next_alert_id()
    alerts[aid] = {
        "id": aid, "key": key, "device_id": device_id, "severity": severity,
        "message": message, "created_at": _now(), "resolved": False,
    }


def _clear_alert(key):
    for a in alerts.values():
        if a["key"] == key and not a["resolved"]:
            a["resolved"] = True


def _evaluate_device_alerts(dev):
    device_id = dev["device_id"]
    level, flow = dev["water_level"], dev["flow_rate"]

    if level >= WATER_LEVEL_CRITICAL:
        _set_alert(f"{device_id}:level", device_id, "critical",
                    f"Water level critical at {level:.0f}% — overflow risk.")
    elif level >= WATER_LEVEL_WARNING:
        _set_alert(f"{device_id}:level", device_id, "warning",
                    f"Water level high at {level:.0f}%.")
    else:
        _clear_alert(f"{device_id}:level")

    if level >= BLOCKAGE_WATER_LEVEL_MIN and flow <= FLOW_RATE_LOW:
        _set_alert(f"{device_id}:blockage", device_id, "warning",
                    "Level rising with abnormally low flow — possible blockage.")
    else:
        _clear_alert(f"{device_id}:blockage")


def _refresh_online_status():
    now = _now()
    for dev in devices.values():
        device_id = dev["device_id"]
        last = dev.get("last_seen")
        online = bool(last) and (now - last).total_seconds() <= DEVICE_OFFLINE_SECONDS
        dev["online"] = online
        key = f"{device_id}:offline"
        if online:
            _clear_alert(key)
        else:
            _set_alert(key, device_id, "offline", "No data received recently — node may be offline.")


def _demo_tick():
    """Gently random-walks demo values so the dashboard looks alive before
    real sensors are connected. Stops as soon as real data is posted."""
    if not devices:
        seed = [
            ("DRAIN-001", "Ketti Junction", 42, 11.4),
            ("DRAIN-002", "Coonoor Road", 71, 6.2),
            ("DRAIN-003", "College Gate", 88, 2.4),
            ("DRAIN-004", "Market Culvert", 95, 14.2),
            ("DRAIN-005", "Bus Stand", 20, 4.0),
        ]
        for device_id, loc, level, flow in seed:
            _ingest(device_id, level, flow, location=loc)
        return

    for device_id, dev in list(devices.items()):
        level = dev["water_level"] + random.uniform(-3, 3)
        flow = dev["flow_rate"] + random.uniform(-1, 1)
        _ingest(device_id, level, flow)


# ---------------------------------------------------------------------------
# Presentation helpers — status classification + inline SVG generation
# (all computed server-side; the template just drops the markup in)
# ---------------------------------------------------------------------------
def device_status(dev):
    if not dev.get("online", True):
        return "offline", "OFFLINE"
    level, flow = dev["water_level"], dev["flow_rate"]
    if level >= WATER_LEVEL_CRITICAL:
        return "critical", "CRITICAL"
    if level >= WATER_LEVEL_WARNING or (level >= BLOCKAGE_WATER_LEVEL_MIN and flow <= FLOW_RATE_LOW):
        return "warning", "WARNING"
    return "normal", "LIVE"


def time_ago(dt):
    if not dt:
        return "—"
    diff = max(0, (_now() - dt).total_seconds())
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    return f"{int(diff // 3600)}h ago"


def tank_svg(level_pct):
    w, h = 42, 56
    clamped = max(0.0, min(100.0, level_pct))
    fill_top = 10 + (h - 20) * (1 - clamped / 100)
    return f'''<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <linearGradient id="mtFluid{int(clamped*10)}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#5CC8FF"/><stop offset="100%" stop-color="#1657FF"/>
    </linearGradient>
    <clipPath id="mtClip{int(clamped*10)}"><rect x="5" y="9" width="{w-10}" height="{h-18}" rx="4"/></clipPath>
  </defs>
  <rect x="4" y="8" width="{w-8}" height="{h-16}" rx="7" fill="#DCE6F5" stroke="#B9C9E4" stroke-width="1.2"/>
  <g clip-path="url(#mtClip{int(clamped*10)})">
    <rect x="5" y="{fill_top:.1f}" width="{w-10}" height="{h}" fill="url(#mtFluid{int(clamped*10)})"/>
  </g>
  <ellipse cx="{w/2}" cy="9" rx="{w/2-5}" ry="3.4" fill="none" stroke="#B9C9E4" stroke-width="1"/>
</svg>'''


def gauge_svg(pct):
    size, r, stroke = 150, 62, 14
    circumference = 2 * 3.14159265 * r
    clamped = max(0.0, min(100.0, pct))
    offset = circumference * (1 - clamped / 100)
    return f'''<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <defs>
    <linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#9FE0FF"/><stop offset="100%" stop-color="#1657FF"/>
    </linearGradient>
  </defs>
  <circle cx="{size/2}" cy="{size/2}" r="{r}" stroke-width="{stroke}" fill="none" stroke="rgba(255,255,255,.06)"/>
  <circle cx="{size/2}" cy="{size/2}" r="{r}" stroke-width="{stroke}" fill="none"
          stroke="url(#gaugeGrad)" stroke-linecap="round"
          stroke-dasharray="{circumference:.2f}" stroke-dashoffset="{offset:.2f}"
          transform="rotate(-90 {size/2} {size/2})" class="gauge-ring-fill"/>
  <text x="{size/2}" y="{size/2 - 4}" text-anchor="middle" class="gauge-value-text">{clamped:.0f}%</text>
  <text x="{size/2}" y="{size/2 + 18}" text-anchor="middle" class="gauge-unit-text">avg level</text>
</svg>'''


def sparkline_svg(values, color, width=120, height=34):
    if len(values) < 2:
        return f'<svg class="sparkline" viewBox="0 0 {width} {height}"></svg>'
    pad = 3
    lo, hi = min(values), max(values)
    span = max(hi - lo, 0.01)
    step = (width - pad * 2) / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = height - pad - ((v - lo) / span) * (height - pad * 2)
        pts.append(f"{x:.1f},{y:.1f}")
    last_x, last_y = pts[-1].split(",")
    return (f'<svg class="sparkline" viewBox="0 0 {width} {height}">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="2.6" fill="{color}"/></svg>')


def flow_chart_svg(values):
    w, h, pad = 320, 110, 6
    if len(values) < 2:
        return f'<svg class="flow-chart" viewBox="0 0 {w} {h}"></svg>'
    lo, hi = min(values), max(values)
    span = max(hi - lo, 0.01)
    step = (w - pad * 2) / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = h - pad - ((v - lo) / span) * (h - pad * 2 - 10)
        pts.append((x, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pad},{h} {line} {w-pad},{h}"
    lx, ly = pts[-1]
    return f'''<svg class="flow-chart" viewBox="0 0 {w} {h}" preserveAspectRatio="none">
  <defs>
    <linearGradient id="flowFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#5CC8FF" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="#5CC8FF" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <polygon class="flow-area" points="{area}"/>
  <polyline class="flow-line" points="{line}"/>
  <circle class="flow-pulse" cx="{lx:.1f}" cy="{ly:.1f}" r="4"/>
  <circle class="flow-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="4"/>
</svg>'''


# ---------------------------------------------------------------------------
# Mandatory Safety Login Gatekeeper
# The dashboard / monitoring screen CANNOT run without entering:
# Your Name, Email Address (Mail ID), Phone Number, and Preferred Drainage Zone
# ---------------------------------------------------------------------------
@app.before_request
def require_safety_login():
    allowed_endpoints = {"login", "static"}
    if request.endpoint in allowed_endpoints or request.path.startswith("/api/"):
        return
    user = session.get("user")
    if not user or not user.get("email") or not user.get("name") or not user.get("phone") or not user.get("zone"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        zone = request.form.get("zone", "").strip()

        if not name or not email or not phone or not zone:
            error_msg = "All 4 fields (Your Name, Email Address, Phone Number, and Preferred Drainage Zone) must be entered to access the system."
            return render_template(
                "login.html",
                error_msg=error_msg,
                known_locations=KNOWN_LOCATIONS,
                prev_name=name,
                prev_email=email,
                prev_phone=phone,
            )

        # Save to session so the next window can run
        user_data = {
            "name": name,
            "email": email,
            "phone": phone,
            "zone": zone,
            "login_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        session["user"] = user_data

        # Also register into safety_contacts.json for emergency broadcasts
        contacts = _load_safety_contacts()
        existing = next((c for c in contacts if c.get("email", "").lower() == email.lower()), None)
        if existing:
            existing["name"] = name
            existing["phone"] = phone
            existing["zone"] = zone
            existing["updated_at"] = user_data["login_at"]
        else:
            contacts.append({
                "id": f"sc-{uuid.uuid4().hex[:8]}",
                "name": name,
                "email": email,
                "phone": phone,
                "zone": zone,
                "role": "Verified User",
                "alert_level": "Critical & Warnings",
                "triggers": ["Flood Overflow", "Drainage Blockage"],
                "registered_at": user_data["login_at"],
            })
        _save_safety_contacts(contacts)

        return redirect(url_for("index"))

    # GET request: If already logged in, proceed directly to dashboard
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html", known_locations=KNOWN_LOCATIONS)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Page route — computes everything in Python, hands finished markup to Jinja2
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    with _lock:
        if demo_mode:
            _demo_tick()
        _refresh_online_status()

        device_list = sorted(devices.values(), key=lambda d: d["device_id"])
        active_alerts = sorted(
            (a for a in alerts.values() if not a["resolved"]),
            key=lambda a: a["created_at"], reverse=True,
        )

        levels = [d["water_level"] for d in device_list] or [0.0]
        flows = [d["flow_rate"] for d in device_list] or [0.0]
        avg_level = sum(levels) / len(levels)
        avg_flow = sum(flows) / len(flows)
        net_history["level"].append(avg_level)
        net_history["flow"].append(avg_flow)

        device_rows = []
        for d in device_list:
            cls, label = device_status(d)
            device_rows.append({
                "device_id": d["device_id"],
                "location": d["location"],
                "level": d["water_level"],
                "flow": d["flow_rate"],
                "status_class": cls,
                "status_label": label,
                "last_seen": time_ago(d.get("last_seen")),
                "tank_svg": tank_svg(d["water_level"]),
            })

        alert_rows = [{
            "id": a["id"], "device_id": a["device_id"], "severity": a["severity"],
            "message": a["message"], "time_ago": time_ago(a["created_at"]),
        } for a in active_alerts]

        context = {
            "devices": device_rows,
            "alerts": alert_rows,
            "alert_count": len(alert_rows),
            "online_count": sum(1 for d in device_list if d["online"]),
            "total_count": len(device_list),
            "avg_level": avg_level,
            "avg_flow": avg_flow,
            "highest_level": max(levels),
            "lowest_level": min(levels),
            "gauge_svg": gauge_svg(avg_level),
            "flow_chart_svg": flow_chart_svg(list(net_history["flow"])),
            "level_trend_svg": sparkline_svg(list(net_history["level"]), "#5CC8FF", 280, 90),
            "flow_trend_svg": sparkline_svg(list(net_history["flow"]), "#3D78FF", 280, 90),
            "demo_mode": demo_mode,
            "refresh_seconds": REFRESH_SECONDS,
            "now": _now().strftime("%H:%M:%S"),
            "safety_count": len(_load_safety_contacts()),
            "current_user": session.get("user"),
        }
    return render_template("index.html", **context)


@app.route("/alerts/<alert_id>/resolve", methods=["POST"])
def resolve_alert(alert_id):
    with _lock:
        a = alerts.get(alert_id)
        if a:
            a["resolved"] = True
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Background & Safety Routes
# ---------------------------------------------------------------------------
@app.route("/home")
@app.route("/background")
def home():
    with _lock:
        if demo_mode and not devices:
            _demo_tick()
        _refresh_online_status()
        device_list = list(devices.values())
        active_alerts = [a for a in alerts.values() if not a["resolved"]]
        levels = [d["water_level"] for d in device_list] or [0.0]
        flows = [d["flow_rate"] for d in device_list] or [0.0]
        avg_level = sum(levels) / len(levels)
        avg_flow = sum(flows) / len(flows)

    contacts = _load_safety_contacts()
    status_msg = request.args.get("status_msg")
    test_alert_msg = request.args.get("test_alert_msg")

    return render_template(
        "home.html",
        total_devices=len(device_list),
        online_devices=sum(1 for d in device_list if d.get("online", True)),
        avg_level=avg_level,
        avg_flow=avg_flow,
        alert_count=len(active_alerts),
        contacts=contacts,
        known_locations=KNOWN_LOCATIONS,
        demo_mode=demo_mode,
        status_msg=status_msg,
        test_alert_msg=test_alert_msg,
        current_user=session.get("user"),
    )


@app.route("/safety/register", methods=["POST"])
def safety_register():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    zone = request.form.get("zone", "All Zones").strip()
    role = request.form.get("role", "Local Resident").strip()
    alert_level = request.form.get("alert_level", "Critical & Warnings").strip()
    triggers = request.form.getlist("alert_triggers") or ["Flood Overflow", "Drainage Blockage"]

    if not email or not name:
        return redirect(url_for("home", status_msg="Please provide your name and a valid email address.") + "#safety-form")

    contacts = _load_safety_contacts()
    
    # Check if email is already registered; update if so, else append
    existing = next((c for c in contacts if c.get("email", "").lower() == email.lower()), None)
    if existing:
        existing["name"] = name
        existing["phone"] = phone
        existing["zone"] = zone
        existing["role"] = role
        existing["alert_level"] = alert_level
        existing["triggers"] = triggers
        existing["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"Safety contact details updated successfully for {email}!"
    else:
        new_contact = {
            "id": f"sc-{uuid.uuid4().hex[:8]}",
            "name": name,
            "email": email,
            "phone": phone,
            "zone": zone,
            "role": role,
            "alert_level": alert_level,
            "triggers": triggers,
            "registered_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }
        contacts.append(new_contact)
        msg = f"Registration successful! Safety alerts will now be dispatched to {email}."

    _save_safety_contacts(contacts)
    return redirect(url_for("home", status_msg=msg) + "#contacts-list")


@app.route("/safety/delete/<contact_id>", methods=["POST"])
def safety_delete(contact_id):
    contacts = _load_safety_contacts()
    filtered = [c for c in contacts if c.get("id") != contact_id]
    _save_safety_contacts(filtered)
    return redirect(url_for("home", status_msg="Safety recipient unsubscribed successfully.") + "#contacts-list")


@app.route("/safety/test-alert", methods=["POST"])
def safety_test_alert():
    email = request.form.get("email", "Subscriber")
    name = request.form.get("name", "Resident")
    zone = request.form.get("zone", "All Zones")
    
    with _lock:
        active_alerts = [a for a in alerts.values() if not a["resolved"]]
        sample_loc = zone if zone != "All Zones" else (active_alerts[0]["device_id"] if active_alerts else "Market Culvert")
        msg_text = active_alerts[0]["message"] if active_alerts else "Water level exceeded 90.0% threshold (Critical overflow risk)."
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    formatted_preview = (
        f"<b>To:</b> {name} &lt;{email}&gt;<br>"
        f"<b>Subject:</b> 🚨 URGENT SAFETY ALERT: Drainage Surge Alert at {sample_loc}<br>"
        f"<b>Timestamp:</b> {timestamp}<br>"
        f"<b>Status:</b> CRITICAL SURGE DETECTED<br>"
        f"<b>Details:</b> {msg_text}<br>"
        f"<b>Emergency Action Advice:</b> Avoid low-lying culvert crossings and pedestrian subways. Municipal drainage team deployed."
    )
    return redirect(url_for("home", test_alert_msg=formatted_preview) + "#contacts-list")


# ---------------------------------------------------------------------------
# JSON API — unchanged contract from the README, for real ESP32 nodes /
# simulate_sensors.py to POST readings into the same store the page reads
# ---------------------------------------------------------------------------
def _serialize_device(d):
    return {**{k: v for k, v in d.items() if k != "last_seen"},
            "last_seen": d["last_seen"].isoformat() if d.get("last_seen") else None}


def _serialize_alert(a):
    return {**{k: v for k, v in a.items() if k not in ("created_at", "key")},
            "created_at": a["created_at"].isoformat()}


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-KEY"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/sensor-data", methods=["POST"])
def api_sensor_data():
    global demo_mode
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    with _lock:
        demo_mode = False
        _ingest(device_id, data.get("water_level", 0), data.get("flow_rate", 0), location=data.get("location"))
    return jsonify({"status": "ok"}), 201


@app.route("/api/devices", methods=["GET"])
def api_devices():
    with _lock:
        _refresh_online_status()
        return jsonify([_serialize_device(d) for d in devices.values()])


@app.route("/api/devices/<device_id>/history", methods=["GET"])
def api_device_history(device_id):
    limit = int(request.args.get("limit", 50))
    h = history.get(device_id, {"level": [], "flow": []})
    return jsonify({"level": list(h["level"])[-limit:], "flow": list(h["flow"])[-limit:]})


@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    resolved_param = request.args.get("resolved")
    with _lock:
        items = list(alerts.values())
    if resolved_param is not None:
        want = resolved_param.lower() == "true"
        items = [a for a in items if a["resolved"] == want]
    return jsonify([_serialize_alert(a) for a in items])


@app.route("/api/alerts/<alert_id>/resolve", methods=["POST"])
def api_resolve_alert(alert_id):
    with _lock:
        a = alerts.get(alert_id)
        if a:
            a["resolved"] = True
    return jsonify({"status": "ok"})


@app.route("/api/summary", methods=["GET"])
def api_summary():
    with _lock:
        _refresh_online_status()
        levels = [d["water_level"] for d in devices.values()] or [0.0]
        flows = [d["flow_rate"] for d in devices.values()] or [0.0]
        return jsonify({
            "total_devices": len(devices),
            "online_devices": sum(1 for d in devices.values() if d["online"]),
            "active_alerts": sum(1 for a in alerts.values() if not a["resolved"]),
            "avg_water_level": round(sum(levels) / len(levels), 1),
            "avg_flow_rate": round(sum(flows) / len(flows), 1),
        })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
