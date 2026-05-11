import os
import time
import random
from collections import deque
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ============================================================================
# QoS / metrics state (in-memory, single-process — suffisant pour l'atelier)
# ============================================================================
WINDOW = deque(maxlen=2000)
TOKENS_PER_SEC = 5
BURST = 10
tokens = BURST
last_refill = time.time()


def now_ms():
    return int(time.time() * 1000)


def refill_tokens():
    global tokens, last_refill
    t = time.time()
    elapsed = t - last_refill
    add = int(elapsed * TOKENS_PER_SEC)
    if add > 0:
        tokens = min(BURST, tokens + add)
        last_refill = t


def qos_admit():
    """Token-bucket : retourne (admis, retry_after_seconds)."""
    global tokens
    refill_tokens()
    if tokens > 0:
        tokens -= 1
        return True, 0
    return False, 1


def record(endpoint, duration_ms, status):
    WINDOW.append((time.time(), endpoint, duration_ms, status))


def compute_metrics():
    data = list(WINDOW)
    if not data:
        return {
            "count": 0,
            "error_rate": 0,
            "rps_last_60s": 0,
            "latency_ms": {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0},
            "jitter_ms_avg_absdiff": 0,
            "qos_policy": {"token_bucket": {"tokens_per_sec": TOKENS_PER_SEC, "burst": BURST}},
        }

    durations = sorted(d[2] for d in data)
    errors = sum(1 for d in data if d[3] >= 400)
    total = len(data)
    error_rate = errors / total

    cutoff = time.time() - 60
    last60 = [d for d in data if d[0] >= cutoff]
    rps = len(last60) / 60 if last60 else 0

    def pct(p):
        idx = int(round((p / 100) * (len(durations) - 1)))
        return durations[max(0, min(idx, len(durations) - 1))]

    diffs = [abs(durations[i] - durations[i - 1]) for i in range(1, len(durations))]
    jitter = sum(diffs) / len(diffs) if diffs else 0

    return {
        "count": total,
        "error_rate": round(error_rate, 4),
        "rps_last_60s": round(rps, 3),
        "latency_ms": {
            "p50": pct(50),
            "p90": pct(90),
            "p95": pct(95),
            "p99": pct(99),
            "max": durations[-1],
        },
        "jitter_ms_avg_absdiff": round(jitter, 2),
        "qos_policy": {"token_bucket": {"tokens_per_sec": TOKENS_PER_SEC, "burst": BURST}},
    }


@app.before_request
def start_timer():
    request._t0 = time.time()


@app.after_request
def end_timer(response):
    duration_ms = int((time.time() - getattr(request, "_t0", time.time())) * 1000)
    record(request.path, duration_ms, response.status_code)
    response.headers["X-Service-Name"] = "network-lab"
    response.headers["X-Service-Version"] = "2.0"
    response.headers["X-Request-Id"] = f"{now_ms()}-{random.randint(1000, 9999)}"
    return response


# ============================================================================
# Négociation de contenu : HTML par défaut, JSON sur demande explicite.
# Règle : `?format=json` ou `?format=html` priment. Sinon, on regarde Accept :
# un navigateur envoie toujours "text/html,..." → HTML ; curl par défaut
# envoie "*/*" → JSON (compatibilité avec l'exercice `curl /metrics` du README).
# ============================================================================
def wants_json():
    fmt = request.args.get("format")
    if fmt == "json":
        return True
    if fmt == "html":
        return False
    accept = request.headers.get("Accept", "")
    return "text/html" not in accept


# ============================================================================
# Contenu pédagogique
# ============================================================================
OSI_LAYERS = [
    {"num": 7, "slug": "l7", "name": "Application",
     "description": "Interaction directe avec l'application (HTTP, DNS, SMTP, SSH…).",
     "exemples": "HTTP · HTTPS · DNS · FTP · SMTP · SSH",
     "pdu": "Données / Message"},
    {"num": 6, "slug": "l6", "name": "Présentation",
     "description": "Format des données : encodage, compression, chiffrement (TLS).",
     "exemples": "TLS · MIME · UTF-8 · JPEG",
     "pdu": "Données"},
    {"num": 5, "slug": "l5", "name": "Session",
     "description": "Ouverture, maintien et fermeture des dialogues entre applications.",
     "exemples": "Cookies HTTP · NetBIOS · RPC",
     "pdu": "Données"},
    {"num": 4, "slug": "l4", "name": "Transport",
     "description": "Communication de bout en bout. TCP fiable, UDP non fiable.",
     "exemples": "TCP · UDP · ports source/destination",
     "pdu": "Segment (TCP) / Datagramme (UDP)"},
    {"num": 3, "slug": "l3", "name": "Réseau",
     "description": "Adressage logique et routage entre réseaux distincts.",
     "exemples": "IPv4 · IPv6 · ICMP · ARP (frontière L2/L3)",
     "pdu": "Paquet"},
    {"num": 2, "slug": "l2", "name": "Liaison de données",
     "description": "Transmission des trames sur le lien local, adressage physique.",
     "exemples": "Ethernet (802.3) · Wi-Fi (802.11) · MAC",
     "pdu": "Trame"},
    {"num": 1, "slug": "l1", "name": "Physique",
     "description": "Transmission des bits sur le support physique.",
     "exemples": "Cuivre RJ45 · Fibre optique · Ondes radio",
     "pdu": "Bit"},
]


def osi_observed():
    return {
        "method": request.method,
        "path": request.path,
        "host": request.headers.get("Host"),
        "user_agent": request.headers.get("User-Agent"),
        "accept": request.headers.get("Accept"),
        "remote_addr": request.remote_addr,
    }


DHCP_DORA = [
    {"step": 1, "name": "DISCOVER", "side": "client", "from_": "Client", "to": "Broadcast",
     "src": "0.0.0.0:68", "dst": "255.255.255.255:67",
     "description": "Le client n'a pas d'IP, il cherche un serveur DHCP en broadcast L2 et L3."},
    {"step": 2, "name": "OFFER", "side": "server", "from_": "Serveur", "to": "Client",
     "src": "serveur:67", "dst": "broadcast:68",
     "description": "Le serveur propose une IP, un masque, une passerelle, des DNS et un bail."},
    {"step": 3, "name": "REQUEST", "side": "client", "from_": "Client", "to": "Broadcast",
     "src": "0.0.0.0:68", "dst": "255.255.255.255:67",
     "description": "Le client accepte une offre. Diffusion pour notifier les autres serveurs DHCP."},
    {"step": 4, "name": "ACK", "side": "server", "from_": "Serveur", "to": "Client",
     "src": "serveur:67", "dst": "client:68",
     "description": "Confirmation. Le bail est attribué, le client peut utiliser sa configuration."},
]

DHCP_OPTIONS = [
    {"code": 1,  "nom": "Subnet mask",  "exemple": "255.255.255.0"},
    {"code": 3,  "nom": "Router",       "exemple": "172.20.1.254"},
    {"code": 6,  "nom": "DNS",          "exemple": "1.1.1.1, 8.8.8.8"},
    {"code": 15, "nom": "Domain name",  "exemple": "lab.local"},
    {"code": 51, "nom": "Lease time",   "exemple": "43200 s (12 h)"},
    {"code": 53, "nom": "Message type", "exemple": "DISCOVER / OFFER / REQUEST / ACK"},
    {"code": 54, "nom": "Server ID",    "exemple": "172.20.1.2"},
]

DHCP_RISQUES = [
    "Rogue DHCP : un faux serveur peut imposer une mauvaise passerelle ou un DNS malveillant.",
    "DHCP starvation : un attaquant épuise le pool pour préparer un MITM.",
    "Plage mal calibrée : conflits si le pool recouvre des adresses statiques (serveurs, imprimantes…).",
    "Point central : la panne du serveur DHCP coupe l'arrivée de nouveaux clients sur le LAN.",
]

DHCP_PORTS = {"client": 68, "serveur": 67, "transport": "UDP"}


NAT_TYPES = [
    {"nom": "NAT statique", "courant": False,
     "description": "Une IP privée mappée 1:1 vers une IP publique fixe. Sert à exposer un serveur interne."},
    {"nom": "NAT dynamique", "courant": False,
     "description": "Mapping temporaire depuis un pool d'IP publiques. Rare en pratique aujourd'hui."},
    {"nom": "PAT (NAT overload)", "courant": True,
     "description": "Plusieurs IP privées partagent une seule IP publique ; le port source est traduit pour démultiplexer les flux en retour. C'est le NAT de votre box."},
]

NAT_EXEMPLE = {
    "avant": {"ip": "192.168.1.10", "port": 51514, "dst": "142.250.179.14:443"},
    "apres": {"ip": "203.0.113.5",  "port": 40001, "dst": "142.250.179.14:443"},
}

NAT_TABLE = [
    {"interne": "192.168.1.10:51514", "publique": "203.0.113.5:40001",
     "destination": "142.250.179.14:443", "protocole": "TCP"},
    {"interne": "192.168.1.11:51515", "publique": "203.0.113.5:40002",
     "destination": "1.1.1.1:53", "protocole": "UDP"},
    {"interne": "192.168.1.12:62330", "publique": "203.0.113.5:40003",
     "destination": "9.9.9.9:443", "protocole": "TCP"},
]

NAT_RFC1918 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

NAT_LIMITES = [
    "Casse certains protocoles (FTP actif, SIP, IPsec) qui transportent l'IP en couche applicative.",
    "Complique les connexions entrantes (P2P, hébergement). Solutions : DNAT, STUN/TURN, UPnP.",
    "N'est pas un mécanisme de sécurité : sans pare-feu, une connexion sortante établit une fenêtre de retour.",
    "Disparaît en IPv6 : l'espace d'adressage rend le partage inutile (NAT66 reste marginal).",
]


# ============================================================================
# Routes
# ============================================================================
@app.get("/")
def index():
    if wants_json():
        return jsonify({
            "service": "network-lab", "version": "2.0",
            "endpoints": ["/", "/osi", "/dhcp", "/nat", "/metrics"],
            "note": "Ajoutez ?format=html dans le navigateur pour la version visuelle.",
        })
    return render_template("index.html", active="index")


@app.get("/osi")
def osi():
    observed = osi_observed()
    if wants_json():
        return jsonify({"layers": OSI_LAYERS, "observed": observed})
    return render_template("osi.html", active="osi", layers=OSI_LAYERS, observed=observed)


@app.get("/dhcp")
def dhcp():
    if wants_json():
        return jsonify({
            "dora": DHCP_DORA, "options": DHCP_OPTIONS,
            "risques": DHCP_RISQUES, "ports": DHCP_PORTS,
        })
    return render_template(
        "dhcp.html", active="dhcp",
        dora=DHCP_DORA, options=DHCP_OPTIONS, risques=DHCP_RISQUES, ports=DHCP_PORTS,
    )


@app.get("/nat")
def nat():
    if wants_json():
        return jsonify({
            "types": NAT_TYPES, "exemple": NAT_EXEMPLE, "table": NAT_TABLE,
            "rfc1918": NAT_RFC1918, "limites": NAT_LIMITES,
        })
    return render_template(
        "nat.html", active="nat",
        types=NAT_TYPES, exemple=NAT_EXEMPLE, table=NAT_TABLE,
        rfc1918=NAT_RFC1918, limites=NAT_LIMITES,
    )


@app.get("/metrics")
def metrics():
    # La page-dashboard elle-même est toujours servie. Le token-bucket
    # s'applique au fetch JSON (c'est *lui* l'objet de la démo QoS).
    if not wants_json():
        return render_template("metrics.html", active="metrics")

    allowed, retry_after = qos_admit()
    if not allowed:
        resp = jsonify({"error": "rate_limited", "retry_after_seconds": retry_after})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    return jsonify(compute_metrics())


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
