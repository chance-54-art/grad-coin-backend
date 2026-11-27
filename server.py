"""GRAD-COIN - backend de l'application Android (authentification par jeton).

Un seul processus : serveur HTTP aiohttp + stockage SQLite.
Le solde et le minage sont calcules ICI, jamais dans le telephone.

Regles produit :
- Bienvenue : +1000 GRAD, une seule fois a la creation du compte.
- Minage : 10 GRAD par heure, jusqu'a 240 GRAD par cycle de 24 h.
- Pas d'energie qui expire.
- Recompenses quotidiennes, ligues, ameliorations, combo/cipher du jour,
  serie de connexion (streak), paliers de parrainage, taches, portefeuille BNB.

Lancement (sur Render ou un hebergeur similaire) :
    pip install -r requirements.txt
    python server.py
    -> le serveur ecoute sur PORT (defaut 10000)

Aucun secret n'est lu ni expose ici. L'application Android cree un compte
avec un nom d'utilisateur et un identifiant d'appareil, puis le serveur
delivre un jeton d'acces. Les mots de passe ne sont jamais ecrits en clair
(empreinte SHA-256 salee par utilisateur, usage local uniquement).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import time
from pathlib import Path

from aiohttp import web

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("gradcoin")

# ---------------------------------------------------------------------------
# Configuration (variables d'environnement de l'hebergeur)
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "gradcoin.db")
BASE_URL = (
    os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL") or ""
).rstrip("/")

APP_NAME = "GRAD-COIN"
CURRENCY = "GRAD"
APP_VERSION = "1.0.0"
MIN_APP_VERSION = "1.0.0"  # version Android minimale acceptee par l'API

# ---------------------------------------------------------------------------
# Regles produit
# ---------------------------------------------------------------------------
WELCOME_BONUS = 1000          # une seule fois, a la creation du compte
MINING_RATE_PER_HOUR = 10     # GRAD par heure
MINING_CYCLE_SECONDS = 86400  # 24 h
MINING_MAX_PER_CYCLE = 240    # 10 x 24 - plafond du cycle

DAILY_BASE = 100              # jour 1 = 100 GRAD, +50 par jour
DAILY_STEP = 50
DAILY_DAYS = 7                # jour 7 = 400 GRAD, puis le cycle reprend
DAILY_COOLDOWN = 86400

REFERRAL_BONUS = 500          # pour le parrain, a chaque inscription
REFERRED_WELCOME = 250        # pour l'ami invite

LEAGUES = [
    (0, "Bronze"),
    (1000, "Argent"),
    (5000, "Or"),
    (20000, "Platine"),
    (100000, "Diamant"),
]

UPGRADES = [
    {"id": "gpu", "title": "GPU", "base": 200},
    {"id": "ferme", "title": "Ferme", "base": 300},
    {"id": "noeud", "title": "Noeud", "base": 450},
    {"id": "serveur", "title": "Serveur", "base": 650},
    {"id": "pool", "title": "Pool", "base": 900},
    {"id": "usine", "title": "Usine", "base": 1200},
]
UPGRADE_BY_ID = {u["id"]: u for u in UPGRADES}
UPGRADE_STEP = 0.10           # +10 % du taux par niveau
UPGRADE_COST_MULT = 1.5

COMBO_SIZE = 3
COMBO_BONUS = 500
CIPHER_BONUS = 250
CIPHER_WORDS = ["GRAD", "MINE", "LUNE", "ETOILE", "BLOC", "CRYPTO", "BOURSE", "ROCHE"]
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..",
}

STREAK_STEP = 10              # 10 GRAD x jours consecutifs
STREAK_CAP = 150
FRIEND_TIERS = [(5, 2000), (10, 5000), (25, 15000)]

TASKS = [
    {"id": "invite_3", "title": "Inviter 3 amis", "reward": 1500, "kind": "invites", "goal": 3},
    {"id": "mines_7", "title": "Faire 7 minages", "reward": 700, "kind": "mines", "goal": 7},
    {"id": "wallet", "title": "Ajouter ton portefeuille", "reward": 200, "kind": "wallet"},
]
TASK_BY_ID = {t["id"]: t for t in TASKS}

DAY_S = 86400
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
BNB_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DEFAULT_ROLE = "user"
VALID_ROLES = {"user", "admin"}

# ---------------------------------------------------------------------------
# Base de donnees SQLite
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    device_id TEXT UNIQUE NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    welcomed_at REAL,
    mining_started_at REAL,
    mining_credited INTEGER NOT NULL DEFAULT 0,
    mines_count INTEGER NOT NULL DEFAULT 0,
    daily_last_claim REAL NOT NULL DEFAULT 0,
    daily_day INTEGER NOT NULL DEFAULT 0,
    daily_claims_count INTEGER NOT NULL DEFAULT 0,
    combo_claimed_day TEXT,
    cipher_claimed_day TEXT,
    streak INTEGER NOT NULL DEFAULT 0,
    streak_last_day TEXT,
    streak_bonus INTEGER NOT NULL DEFAULT 0,
    wallet TEXT,
    referred_by INTEGER,
    role TEXT NOT NULL DEFAULT 'user',
    password_hash TEXT,
    password_salt TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_upgrades (
    user_id INTEGER NOT NULL,
    upgrade_id TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, upgrade_id)
);
CREATE TABLE IF NOT EXISTS user_tasks (
    user_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
CREATE TABLE IF NOT EXISTS user_daily_claims (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, day)
);
CREATE TABLE IF NOT EXISTS friend_tiers (
    user_id INTEGER NOT NULL,
    tier_goal INTEGER NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (user_id, tier_goal)
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    user_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC);
CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referred_by);
"""


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


DB = open_db()
DB_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def now() -> float:
    return time.time()


def today_str(ts: float | None = None) -> str:
    ts = now() if ts is None else ts
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    DB.execute(
        "INSERT INTO tokens (token_hash, user_id, created_at) VALUES (?, ?, ?)",
        (hash_token(token), user_id, now()),
    )
    DB.commit()
    return token


def user_from_token(token: str | None):
    if not token:
        return None
    row = DB.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    return row


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


def is_admin(user) -> bool:
    return user is not None and user["role"] == "admin"


# ---------------------------------------------------------------------------
# Regles metier (le serveur tranche, jamais le telephone)
# ---------------------------------------------------------------------------
def mining_multiplier(user_id: int) -> float:
    total = 0
    for r in DB.execute(
        "SELECT level FROM user_upgrades WHERE user_id = ?", (user_id,)
    ):
        total += int(r["level"] or 0)
    return round(1.0 + UPGRADE_STEP * total, 4)


def mining_rate_per_hour(user_id: int) -> float:
    return MINING_RATE_PER_HOUR * mining_multiplier(user_id)


def cycle_accrued(started_at, credited: int, user_id: int) -> int:
    """GRAD dus depuis le debut du cycle, plafonnes a 240."""
    if not started_at:
        return credited
    elapsed_h = max(0.0, (now() - started_at) / 3600.0)
    due = int(math.floor(mining_rate_per_hour(user_id) * elapsed_h))
    return min(due, MINING_MAX_PER_CYCLE)


def cycle_remaining_seconds(started_at) -> int:
    if not started_at:
        return 0
    return max(0, int(MINING_CYCLE_SECONDS - (now() - started_at)))


def settle_mining(user_id: int) -> None:
    """Verse au solde ce que le cycle a produit, sans depasser 240."""
    row = DB.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None or not row["mining_started_at"]:
        return
    accrued = cycle_accrued(row["mining_started_at"], row["mining_credited"], user_id)
    to_credit = accrued - int(row["mining_credited"] or 0)
    if to_credit > 0:
        DB.execute(
            "UPDATE users SET balance = balance + ?, mining_credited = ? WHERE id = ?",
            (to_credit, accrued, user_id),
        )
        DB.commit()
    elif accrued >= MINING_MAX_PER_CYCLE and int(row["mining_credited"] or 0) >= MINING_MAX_PER_CYCLE:
        # Cycle termine : on repart un nouveau cycle automatiquement.
        DB.execute(
            "UPDATE users SET mining_started_at = ?, mining_credited = 0 WHERE id = ?",
            (now(), user_id),
        )
        DB.commit()


def daily_bonus(day: int) -> int:
    return DAILY_BASE + DAILY_STEP * (day - 1)


def daily_next_day(row, ts: float) -> int:
    last = float(row["daily_last_claim"] or 0)
    # Si plus de 2 jours sans claim, la serie repart a 1.
    if last and (ts - last) > 2 * DAY_S:
        return 1
    return (int(row["daily_day"] or 0) % DAILY_DAYS) + 1


def daily_state(row, ts: float) -> dict:
    last = float(row["daily_last_claim"] or 0)
    nxt = daily_next_day(row, ts)
    available = (ts - last) >= DAILY_COOLDOWN
    return {
        "day": int(row["daily_day"] or 0),
        "next_day": nxt,
        "next_bonus": daily_bonus(nxt),
        "available": available,
        "bonuses": [daily_bonus(d) for d in range(1, DAILY_DAYS + 1)],
        "claims_count": int(row["daily_claims_count"] or 0),
    }


def league_for(balance: int) -> dict:
    idx = 0
    for i, (threshold, _) in enumerate(LEAGUES):
        if balance >= threshold:
            idx = i
    name = LEAGUES[idx][1]
    nxt = LEAGUES[idx + 1] if idx + 1 < len(LEAGUES) else None
    return {
        "name": name,
        "floor": LEAGUES[idx][0],
        "next_threshold": nxt[0] if nxt else None,
        "next_name": nxt[1] if nxt else None,
    }


def upgrade_cost(upgrade: dict, level: int) -> int:
    return int(math.floor(upgrade["base"] * (UPGRADE_COST_MULT ** level)))


def combo_for(day: str):
    return random.Random(f"combo-{day}").sample(
        [u["id"] for u in UPGRADES], COMBO_SIZE
    )


def cipher_for(day: str) -> str:
    return random.Random(f"cipher-{day}").choice(CIPHER_WORDS)


def morse_of(word: str) -> str:
    return " ".join(MORSE.get(ch, "") for ch in word)


def streak_bonus(days: int) -> int:
    return min(STREAK_STEP * days, STREAK_CAP)


def friend_thresholds() -> list:
    return [goal for goal, _ in FRIEND_TIERS]


def progression_user(user_id: int) -> dict:
    return {
        "mines_count": int(
            DB.execute(
                "SELECT COUNT(*) v FROM user_daily_claims WHERE user_id = ? AND kind = 'mine'",
                (user_id,),
            ).fetchone()["v"]
        ) if False else None
    }


def ensure_user(username: str, display_name: str, device_id: str, invite_code: str | None):
    """Cree le compte + credits de bienvenue (1000 GRAD, une seule fois)."""
    row = DB.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is not None:
        if not row["display_name"]:
            DB.execute(
                "UPDATE users SET display_name = ? WHERE id = ?",
                (display_name, row["id"]),
            )
            DB.commit()
            row = DB.execute(
                "SELECT * FROM users WHERE id = ?", (row["id"],)
            ).fetchone()
        return row, False
    referred_by = None
    if invite_code:
        inv = DB.execute(
            "SELECT * FROM invites WHERE code = ?", (invite_code,)
        ).fetchone()
        if inv is not None and inv["user_id"]:
            referred_by = inv["user_id"]
    ts = now()
    cur = DB.execute(
        "INSERT INTO users (username, display_name, device_id, balance, "
        "welcomed_at, mining_started_at, mining_credited, referred_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (username, display_name, device_id, WELCOME_BONUS, ts, ts, referred_by, ts),
    )
    uid = cur.lastrowid
    for u in UPGRADES:
        DB.execute(
            "INSERT OR IGNORE INTO user_upgrades (user_id, upgrade_id, level) "
            "VALUES (?, ?, 0)",
            (uid, u["id"]),
        )
    # Code d'invitation propre a l'utilisateur (pour ses futurs amis).
    DB.execute(
        "INSERT INTO invites (code, user_id, created_at) VALUES (?, ?, ?)",
        (username, uid, ts),
    )
    if referred_by:
        DB.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (REFERRED_WELCOME, uid),
        )
        DB.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (REFERRAL_BONUS, referred_by),
        )
    DB.commit()
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return row, True


def task_progress(task: dict, uid: int) -> int:
    kind = task["kind"]
    if kind == "mines":
        return int(
            DB.execute(
                "SELECT COUNT(*) v FROM user_daily_claims WHERE user_id = ? AND kind = 'mine'",
                (uid,),
            ).fetchone()["v"]
        )
    if kind == "invites":
        return int(
            DB.execute(
                "SELECT COUNT(*) v FROM users WHERE referred_by = ?", (uid,)
            ).fetchone()["v"]
        )
    if kind == "wallet":
        row = DB.execute("SELECT wallet FROM users WHERE id = ?", (uid,)).fetchone()
        return 1 if row and row["wallet"] else 0
    return 0


def build_state(uid: int) -> dict:
    DB.commit()
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        raise ValueError("utilisateur introuvable")
    settle_mining(uid)
    DB.commit()
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    credited = int(row["mining_credited"] or 0)
    accrued = cycle_accrued(row["mining_started_at"], credited, uid)
    rate = mining_rate_per_hour(uid)
    refs_count = int(
        DB.execute(
            "SELECT COUNT(*) v FROM users WHERE referred_by = ?", (uid,)
        ).fetchone()["v"]
    )
    inv = DB.execute(
        "SELECT code FROM invites WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)
    ).fetchone()

    ups = []
    levels = {
        r["upgrade_id"]: int(r["level"] or 0)
        for r in DB.execute(
            "SELECT upgrade_id, level FROM user_upgrades WHERE user_id = ?", (uid,)
        )
    }
    for u in UPGRADES:
        lvl = levels.get(u["id"], 0)
        ups.append(
            {
                "id": u["id"],
                "title": u["title"],
                "level": lvl,
                "cost": upgrade_cost(u, lvl),
            }
        )

    tasks = []
    claimed = {
        r["task_id"]
        for r in DB.execute(
            "SELECT task_id FROM user_tasks WHERE user_id = ?", (uid,)
        )
    }
    for t in TASKS:
        progress = task_progress(t, uid)
        goal = t.get("goal", 1)
        tasks.append(
            {
                "id": t["id"],
                "title": t["title"],
                "reward": t["reward"],
                "goal": goal,
                "progress": progress,
                "done": progress >= goal,
                "claimed": t["id"] in claimed,
            }
        )

    tiers = []
    done_tiers = {
        r["tier_goal"]
        for r in DB.execute(
            "SELECT tier_goal FROM friend_tiers WHERE user_id = ?", (uid,)
        )
    }
    for goal, reward in FRIEND_TIERS:
        tiers.append(
            {
                "goal": goal,
                "reward": reward,
                "claimed": goal in done_tiers,
                "done": refs_count >= goal,
            }
        )

    day = today_str()
    league = league_for(int(row["balance"]))

    return {
        "ok": True,
        "user": {
            "id": uid,
            "username": row["username"],
            "display_name": row["display_name"],
            "balance": int(row["balance"]),
            "role": row["role"],
            "wallet": row["wallet"],
        },
        "mining": {
            "accrued": accrued,
            "credited": credited,
            "remaining_to_max": MINING_MAX_PER_CYCLE - accrued,
            "rate_per_hour": rate,
            "max_per_cycle": MINING_MAX_PER_CYCLE,
            "cycle_remaining_seconds": cycle_remaining_seconds(row["mining_started_at"]),
            "active": bool(row["mining_started_at"]),
            "mines_count": int(row["mines_count"] or 0),
        },
        "welcome_bonus": WELCOME_BONUS,
        "league": league,
        "daily": daily_state(row, now()),
        "streak": {
            "days": int(row["streak"] or 0),
            "bonus_today": streak_bonus(int(row["streak"] or 0)),
            "cap": STREAK_CAP,
            "step": STREAK_STEP,
            "last_day": row["streak_last_day"],
        },
        "combo": {
            "items": combo_for(day),
            "bonus": COMBO_BONUS,
            "day": day,
            "claimed": row["combo_claimed_day"] == day,
        },
        "cipher": {
            "morse": morse_of(cipher_for(day)),
            "bonus": CIPHER_BONUS,
            "day": day,
            "claimed": row["cipher_claimed_day"] == day,
        },
        "upgrades": ups,
        "multiplier": mining_multiplier(uid),
        "tasks": tasks,
        "referral": {
            "count": refs_count,
            "bonus": REFERRAL_BONUS,
            "referred_welcome": REFERRED_WELCOME,
            "code": inv["code"] if inv else row["username"],
            "tiers": tiers,
        },
        "config": {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "min_app_version": MIN_APP_VERSION,
            "currency": CURRENCY,
        },
    }


# ---------------------------------------------------------------------------
# Actions de l'API
# ---------------------------------------------------------------------------
def check_version(body: dict):
    """Refuse les anciennes versions de l'application."""
    v = str(body.get("app_version") or "")
    if v and _version_tuple(v) < _version_tuple(MIN_APP_VERSION):
        return {
            "ok": False,
            "error": "version_obsolete",
            "message": "Met a jour l'application pour continuer.",
            "min_version": MIN_APP_VERSION,
        }
    return None


def _version_tuple(v: str):
    parts = []
    for p in v.replace("-", ".").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


async def act_register(uid, body, request):
    username = str(body.get("username") or "").strip().lower()
    display_name = str(body.get("display_name") or "").strip()[:60] or username
    device_id = str(body.get("device_id") or "").strip()
    invite_code = str(body.get("invite_code") or "").strip().lower() or None
    password = str(body.get("password") or "")
    if not USERNAME_RE.match(username):
        return {"ok": False, "error": "username_invalide", "message": "Choisis un nom d'utilisateur de 3 a 20 caracteres (lettres, chiffres, _)."}
    if len(device_id) < 8:
        return {"ok": False, "error": "device_invalide", "message": "Identifiant d'appareil manquant."}
    user, created = ensure_user(username, display_name, device_id, invite_code)
    if password:
        salt = secrets.token_urlsafe(16)
        DB.execute(
            "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt, hash_password(password, salt), user["id"]),
        )
        DB.commit()
    token = issue_token(user["id"])
    state = build_state(user["id"])
    state["token"] = token
    state["created"] = created
    return state


async def _require_user(request):
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-Auth-Token")
    user = user_from_token(token)
    if user is None:
        return None, {
            "ok": False,
            "error": "non_connecte",
            "message": "Connecte-toi d'abord.",
        }
    return user, None


async def act_me(uid, body, request):
    return build_state(uid)


async def act_mine(uid, body, request):
    """Lance un cycle de minage de 24 h (48 h de cooldown entre deux cycles)."""
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    ts = now()
    if row["mining_started_at"]:
        # Si le cycle precedent n'est pas termine, on met a jour l'avance.
        accrued = cycle_accrued(row["mining_started_at"], row["mining_credited"], uid)
        remaining = cycle_remaining_seconds(row["mining_started_at"])
        if remaining > 0:
            return {
                "ok": False,
                "error": "minage_en_cours",
                "message": "Le minage est deja en cours.",
                "remaining_seconds": remaining,
                "accrued": accrued,
                **build_state(uid),
            }
    DB.execute(
        "UPDATE users SET mining_started_at = ?, mining_credited = 0, "
        "mines_count = mines_count + 1 WHERE id = ?",
        (ts, uid),
    )
    DB.execute(
        "INSERT OR IGNORE INTO user_daily_claims (user_id, kind, day) VALUES (?, 'mine', ?)",
        (uid, today_str(ts)),
    )
    DB.commit()
    return {
        "ok": True,
        "message": "Minage lance : jusqu'a 240 GRAD sur 24 h.",
        **build_state(uid),
    }


async def act_daily_claim(uid, body, request):
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    ts = now()
    if (ts - float(row["daily_last_claim"] or 0)) < DAILY_COOLDOWN:
        return {
            "ok": False,
            "error": "recompense_deja_pris",
            "message": "Recompense deja recuperee aujourd'hui.",
            **build_state(uid),
        }
    day = daily_next_day(row, ts)
    bonus = daily_bonus(day)
    DB.execute(
        "UPDATE users SET daily_last_claim = ?, daily_day = ?, balance = balance + ?, "
        "daily_claims_count = daily_claims_count + 1 WHERE id = ?",
        (ts, day, bonus, uid),
    )
    DB.commit()
    return {
        "ok": True,
        "gained": bonus,
        "message": f"+{bonus} {CURRENCY} : recompense jour {day}.",
        **build_state(uid),
    }


async def act_combo_claim(uid, body, request):
    day = today_str()
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row["combo_claimed_day"] == day:
        return {
            "ok": False,
            "error": "combo_deja_pris",
            "message": "Combo deja pris aujourd'hui.",
            **build_state(uid),
        }
    DB.execute(
        "UPDATE users SET combo_claimed_day = ?, balance = balance + ? WHERE id = ?",
        (day, COMBO_BONUS, uid),
    )
    DB.commit()
    return {
        "ok": True,
        "gained": COMBO_BONUS,
        "message": f"Combo reussi : +{COMBO_BONUS} {CURRENCY} !",
        **build_state(uid),
    }


async def act_cipher_claim(uid, body, request):
    day = today_str()
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    expected = cipher_for(day)
    guess = str(body.get("word") or "").strip().upper()
    if row["cipher_claimed_day"] == day:
        return {
            "ok": False,
            "error": "cipher_deja_pris",
            "message": "Cipher deja decode aujourd'hui.",
            **build_state(uid),
        }
    if guess != expected:
        return {
            "ok": False,
            "error": "cipher_faux",
            "message": "Ce n'est pas le bon mot.",
            **build_state(uid),
        }
    DB.execute(
        "UPDATE users SET cipher_claimed_day = ?, balance = balance + ? WHERE id = ?",
        (day, CIPHER_BONUS, uid),
    )
    DB.commit()
    return {
        "ok": True,
        "gained": CIPHER_BONUS,
        "message": f"Cipher decode : +{CIPHER_BONUS} {CURRENCY} !",
        **build_state(uid),
    }


async def act_upgrade_buy(uid, body, request):
    up_id = str(body.get("upgrade_id") or "").strip()
    up = UPGRADE_BY_ID.get(up_id)
    if up is None:
        return {"ok": False, "error": "amelioration_inconnue", "message": "Amelioration inconnue."}
    row = DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    lvl_row = DB.execute(
        "SELECT level FROM user_upgrades WHERE user_id = ? AND upgrade_id = ?",
        (uid, up_id),
    ).fetchone()
    level = int(lvl_row["level"] or 0) if lvl_row else 0
    cost = upgrade_cost(up, level)
    if int(row["balance"]) < cost:
        return {
            "ok": False,
            "error": "solde_insuffisant",
            "message": "Solde insuffisant.",
            "cost": cost,
            **build_state(uid),
        }
    DB.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ?", (cost, uid)
    )
    DB.execute(
        "UPDATE user_upgrades SET level = ? WHERE user_id = ? AND upgrade_id = ?",
        (level + 1, uid, up_id),
    )
    DB.commit()
    return {
        "ok": True,
        "message": f"{up['title']} ameliore au niveau {level + 1}.",
        **build_state(uid),
    }


async def act_task_claim(uid, body, request):
    task_id = str(body.get("task_id") or "").strip()
    task = TASK_BY_ID.get(task_id)
    if task is None:
        return {"ok": False, "error": "tache_inconnue", "message": "Tache inconnue."}
    if DB.execute(
        "SELECT 1 FROM user_tasks WHERE user_id = ? AND task_id = ?", (uid, task_id)
    ).fetchone():
        return {
            "ok": False,
            "error": "tache_deja_faite",
            "message": "Tache deja validee.",
            **build_state(uid),
        }
    progress = task_progress(task, uid)
    if progress < task.get("goal", 1):
        return {
            "ok": False,
            "error": "tache_incomplete",
            "message": "Tache pas encore terminee.",
            "progress": progress,
            **build_state(uid),
        }
    DB.execute(
        "INSERT INTO user_tasks (user_id, task_id, claimed_at) VALUES (?, ?, ?)",
        (uid, task_id, now()),
    )
    DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (task["reward"], uid))
    DB.commit()
    return {
        "ok": True,
        "gained": task["reward"],
        "message": f"+{task['reward']} {CURRENCY} : {task['title']}",
        **build_state(uid),
    }


async def act_tier_claim(uid, body, request):
    try:
        goal = int(body.get("goal") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "palier_inconnu", "message": "Palier inconnu."}
    tier = next((t for t in FRIEND_TIERS if t[0] == goal), None)
    if tier is None:
        return {"ok": False, "error": "palier_inconnu", "message": "Palier inconnu."}
    if DB.execute(
        "SELECT 1 FROM friend_tiers WHERE user_id = ? AND tier_goal = ?", (uid, goal)
    ).fetchone():
        return {
            "ok": False,
            "error": "palier_deja_pris",
            "message": "Palier deja recupere.",
            **build_state(uid),
        }
    refs = int(
        DB.execute(
            "SELECT COUNT(*) v FROM users WHERE referred_by = ?", (uid,)
        ).fetchone()["v"]
    )
    if refs < goal:
        return {
            "ok": False,
            "error": "palier_non_atteint",
            "message": "Palier pas encore atteint.",
            **build_state(uid),
        }
    DB.execute(
        "INSERT INTO friend_tiers (user_id, tier_goal, claimed_at) VALUES (?, ?, ?)",
        (uid, goal, now()),
    )
    DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (tier[1], uid))
    DB.commit()
    return {
        "ok": True,
        "gained": tier[1],
        "message": f"Palier {goal} amis : +{tier[1]} {CURRENCY} !",
        **build_state(uid),
    }


async def act_wallet_set(uid, body, request):
    wallet = str(body.get("wallet") or "").strip()
    if wallet and not BNB_RE.match(wallet):
        return {
            "ok": False,
            "error": "wallet_invalide",
            "message": "Adresse BNB invalide (0x suivi de 40 caracteres hexadecimaux).",
        }
    DB.execute("UPDATE users SET wallet = ? WHERE id = ?", (wallet or None, uid))
    DB.commit()
    return {"ok": True, "message": "Portefeuille enregistre.", **build_state(uid)}


async def act_settings_name(uid, body, request):
    name = str(body.get("display_name") or "").strip()[:60]
    if not name:
        return {"ok": False, "error": "nom_vide", "message": "Nom vide."}
    DB.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, uid))
    DB.commit()
    return {"ok": True, "message": "Nom mis a jour.", **build_state(uid)}


async def act_leaderboard(uid, body, request):
    rows = DB.execute(
        "SELECT u.id, u.display_name, u.username, u.balance, u.role "
        "FROM users u ORDER BY u.balance DESC LIMIT 50"
    ).fetchall()
    me = None
    for i, r in enumerate(rows, start=1):
        if r["id"] == uid:
            me = {"rank": i, "balance": int(r["balance"])}
            break
    if me is None:
        rank = int(
            DB.execute(
                "SELECT COUNT(*) v FROM users WHERE balance > "
                "(SELECT balance FROM users WHERE id = ?)",
                (uid,),
            ).fetchone()["v"]
        ) + 1
        me = {
            "rank": rank,
            "balance": int(
                DB.execute("SELECT balance FROM users WHERE id = ?", (uid,)).fetchone()["balance"]
            ),
        }
    return {
        "ok": True,
        "me": me,
        "top": [
            {
                "rank": i,
                "display_name": r["display_name"],
                "username": r["username"],
                "balance": int(r["balance"]),
                "role": r["role"],
            }
            for i, r in enumerate(rows, start=1)
        ],
    }


# ---------------------------------------------------------------------------
# Admin (reserve aux comptes role=admin, cree hors API - jamais via l'app)
# ---------------------------------------------------------------------------
async def _require_admin(request):
    user, err = await _require_user(request)
    if err:
        return None, err
    if not is_admin(user):
        return None, {
            "ok": False,
            "error": "acces_refuse",
            "message": "Action reservee a l'administrateur.",
        }
    return user, None


async def act_admin_users(uid, body, request):
    admin, err = await _require_admin(request)
    if err:
        return err
    q = str(body.get("q") or "").strip()
    if q:
        rows = DB.execute(
            "SELECT id, username, display_name, balance, role, wallet, created_at "
            "FROM users WHERE username LIKE ? OR display_name LIKE ? "
            "ORDER BY balance DESC LIMIT 50",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = DB.execute(
            "SELECT id, username, display_name, balance, role, wallet, created_at "
            "FROM users ORDER BY balance DESC LIMIT 50"
        ).fetchall()
    return {
        "ok": True,
        "users": [
            {
                "id": r["id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "balance": int(r["balance"]),
                "role": r["role"],
                "wallet": r["wallet"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


async def act_admin_set_balance(uid, body, request):
    admin, err = await _require_admin(request)
    if err:
        return err
    try:
        target = int(body.get("user_id") or 0)
        delta = int(body.get("delta") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "parametres_invalides", "message": "Parametres invalides."}
    if target == admin["id"]:
        return {"ok": False, "error": "pas_sur_soi_meme", "message": "Impossible de modifier ton propre solde."}
    row = DB.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
    if row is None:
        return {"ok": False, "error": "utilisateur_introuvable", "message": "Utilisateur introuvable."}
    DB.execute(
        "UPDATE users SET balance = MAX(balance + ?, 0) WHERE id = ?", (delta, target)
    )
    DB.commit()
    return {"ok": True, "message": "Solde mis a jour.", **build_state(target)}


async def act_admin_set_role(uid, body, request):
    admin, err = await _require_admin(request)
    if err:
        return err
    try:
        target = int(body.get("user_id") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "parametres_invalides", "message": "Parametres invalides."}
    role = str(body.get("role") or "").strip()
    if role not in VALID_ROLES:
        return {"ok": False, "error": "role_invalide", "message": "Role invalide."}
    if target == admin["id"]:
        return {"ok": False, "error": "pas_sur_soi_meme", "message": "Impossible de modifier ton propre role."}
    row = DB.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
    if row is None:
        return {"ok": False, "error": "utilisateur_introuvable", "message": "Utilisateur introuvable."}
    DB.execute("UPDATE users SET role = ? WHERE id = ?", (role, target))
    DB.commit()
    return {
        "ok": True,
        "message": "Role mis a jour.",
        "user": {"id": target, "username": row["username"], "role": role},
    }


ACTIONS = {
    "register": act_register,
    "me": act_me,
    "mine": act_mine,
    "daily_claim": act_daily_claim,
    "combo_claim": act_combo_claim,
    "cipher_claim": act_cipher_claim,
    "upgrade_buy": act_upgrade_buy,
    "task_claim": act_task_claim,
    "tier_claim": act_tier_claim,
    "wallet_set": act_wallet_set,
    "settings_name": act_settings_name,
    "leaderboard": act_leaderboard,
    "admin_users": act_admin_users,
    "admin_set_balance": act_admin_set_balance,
    "admin_set_role": act_admin_set_role,
}


async def api_route(request: web.Request) -> web.Response:
    action = request.match_info["action"]
    if action not in ACTIONS:
        return web.json_response({"ok": False, "error": "action_inconnue"}, status=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    async with DB_LOCK:
        try:
            if action == "register":
                verdict = check_version(body)
                if verdict:
                    return web.json_response(verdict, status=400)
                result = await ACTIONS[action](None, body, request)
            else:
                user, err = await _require_user(request)
                if err:
                    return web.json_response(err, status=401)
                verdict = check_version(body)
                if verdict:
                    return web.json_response(verdict, status=400)
                result = await ACTIONS[action](user["id"], body, request)
            return web.json_response(result)
        except Exception:
            log.exception("erreur sur l'action %s", action)
            return web.json_response(
                {"ok": False, "error": "erreur_serveur", "message": "Erreur serveur, reessaie."},
                status=500,
            )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": APP_NAME, "version": APP_VERSION})


def health_front(request: web.Request) -> web.Response:
    html = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>GRAD-COIN</title></head>
<body style="background:#000;color:#f5c542;font-family:sans-serif;text-align:center;padding-top:20vh">
<h1>GRAD-COIN</h1><p>Application Android, minage gere par serveur.</p>
<p>Le solde et le minage sont calcules cote serveur. Aucun token n'est distribue ici.</p>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def on_start(app: web.Application) -> None:
    log.info("GRAD-COIN demarre sur le port %s (base %s)", PORT, DB_PATH)


def main() -> None:
    app = web.Application()
    app.router.add_post("/api/{action}", api_route)
    app.router.add_get("/health", health)
    app.router.add_get("/", health_front)
    app.on_startup.append(on_start)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
