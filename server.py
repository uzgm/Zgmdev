import asyncio, json, os, time, random, math
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
import uvicorn

try:
    import httpx
    _USE_HTTPX = True
except ImportError:
    _USE_HTTPX = False
    print("[Warning] httpx \uc5c6\uc74c \u2192 urllib fallback", flush=True)

from ai_manager import run_ai_loop, WEAPON_STATS as _AI_WEAPON_STATS

print("=== \uc11c\ubc84 \uc2dc\uc791 ===", flush=True)

RTDB_URL    = "https://zgm-base-default-rtdb.asia-southeast1.firebasedatabase.app/"
RTDB_SECRET = os.environ.get("RTDB_SECRET", "")
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
WS_PORT     = int(os.environ.get("PORT", 7860))
WS_PING_INTERVAL = 20

TEAM_SIZE      = 4
SESSION_TTL    = 300
JOIN_TIMEOUT   = 30.0
AI_FILL_DELAY  = 10.0
SYNC_TICK_RATE = 10
AI_TICK_RATE   = 10
MAX_MOVE_SPEED = 6.5
KO_REVIVE_TIME = 20.0
RESCUE_WINDOW  = 10.0

MAP_MIN_X = -16.0; MAP_MAX_X = 16.0
MAP_MIN_Z = -9.0;  MAP_MAX_Z = 9.0
ZONE_POS    = {"x": 0.0, "z": 0.0}
ZONE_RADIUS = 4.0

WEAPON_STATS = _AI_WEAPON_STATS
WEAPONS = list(WEAPON_STATS.keys())
MAPS    = ["default"]

print(f"=== httpx \uc0ac\uc6a9: {_USE_HTTPX} ===", flush=True)
print(f"=== RTDB_SECRET \uae38\uc774: {len(RTDB_SECRET)} ===", flush=True)
print(f"=== REDIS_URL: {REDIS_URL[:30] if REDIS_URL else '\uc5c6\uc74c'} ===", flush=True)

clients: dict  = {}
sessions: dict = {}
match_lock = asyncio.Lock()
_http_client = None
_trigger_match_running: bool = False


# ================================================================
# HTTP \ud5ec\ud37c
# ================================================================
async def _get(url: str):
    try:
        if _USE_HTTPX and _http_client:
            r = await _http_client.get(url)
            print(f"[HTTP GET] status={r.status_code} url={url[:80]}", flush=True)
            return r.json() if r.status_code == 200 else None
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            raw  = await loop.run_in_executor(None, lambda: ur.urlopen(url, timeout=5).read())
            return json.loads(raw)
    except Exception as e:
        print(f"[HTTP GET \uc624\ub958] {type(e).__name__}: {e}", flush=True)
        return None

async def _put(url: str, data) -> bool:
    try:
        body = json.dumps(data)
        if _USE_HTTPX and _http_client:
            r = await _http_client.put(url, content=body,
                                       headers={"Content-Type": "application/json"})
            return r.status_code == 200
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req  = ur.Request(url, data=body.encode(),
                              headers={"Content-Type": "application/json"}, method="PUT")
            await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
            return True
    except Exception as e:
        print(f"[HTTP PUT \uc624\ub958] {type(e).__name__}: {e}", flush=True)
        return False

async def _patch(url: str, data) -> bool:
    try:
        body = json.dumps(data)
        if _USE_HTTPX and _http_client:
            r = await _http_client.patch(url, content=body,
                                         headers={"Content-Type": "application/json"})
            return r.status_code == 200
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req  = ur.Request(url, data=body.encode(),
                              headers={"Content-Type": "application/json"}, method="PATCH")
            await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
            return True
    except Exception as e:
        print(f"[HTTP PATCH \uc624\ub958] {type(e).__name__}: {e}", flush=True)
        return False

async def _delete(url: str):
    try:
        if _USE_HTTPX and _http_client:
            await _http_client.delete(url)
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req  = ur.Request(url, method="DELETE")
            await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
    except Exception as e:
        print(f"[HTTP DELETE \uc624\ub958] {type(e).__name__}: {e}", flush=True)


# ================================================================
# Redis \ud5ec\ud37c
# ================================================================
def _redis_headers():
    return {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}

async def redis_set(key, value, ex=SESSION_TTL) -> bool:
    try:
        body = json.dumps(["SET", key, json.dumps(value), "EX", ex])
        if _USE_HTTPX and _http_client:
            r = await _http_client.post(REDIS_URL, content=body, headers=_redis_headers())
            return r.json().get("result") == "OK"
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req  = ur.Request(REDIS_URL, data=body.encode(),
                              headers=_redis_headers(), method="POST")
            raw  = await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5).read())
            return json.loads(raw).get("result") == "OK"
    except Exception as e:
        print(f"[Redis SET \uc624\ub958] {type(e).__name__}: {e}", flush=True)
        return False

async def redis_get(key):
    try:
        body = json.dumps(["GET", key])
        if _USE_HTTPX and _http_client:
            r   = await _http_client.post(REDIS_URL, content=body, headers=_redis_headers())
            raw = r.json().get("result")
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req  = ur.Request(REDIS_URL, data=body.encode(),
                              headers=_redis_headers(), method="POST")
            data = await loop.run_in_executor(None,
                lambda: json.loads(ur.urlopen(req, timeout=5).read()))
            raw  = data.get("result")
        return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[Redis GET \uc624\ub958] {type(e).__name__}: {e}", flush=True)
        return None

async def redis_del(key):
    try:
        body = json.dumps(["DEL", key])
        if _USE_HTTPX and _http_client:
            await _http_client.post(REDIS_URL, content=body, headers=_redis_headers())
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req  = ur.Request(REDIS_URL, data=body.encode(),
                              headers=_redis_headers(), method="POST")
            await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
    except Exception as e:
        print(f"[Redis DEL \uc624\ub958] {type(e).__name__}: {e}", flush=True)


# ================================================================
# RTDB \ud5ec\ud37c
# ================================================================
def _rtdb(path): return f"{RTDB_URL}{path}.json?auth={RTDB_SECRET}"

async def rtdb_get(path):      return await _get(_rtdb(path))
async def rtdb_put(path, d):   return await _put(_rtdb(path), d)
async def rtdb_patch(path, d): return await _patch(_rtdb(path), d)
async def rtdb_delete(path):   await _delete(_rtdb(path))


# ================================================================
# \u2605 \uc880\ube44 \ud30c\ud2f0 \uc815\ub9ac (\uc11c\ubc84 \uc2dc\uc791 \uc2dc + \ub9e4\uce6d \uc9c1\uc804 \ubaa8\ub450 \uc0ac\uc6a9)
# ================================================================
async def cleanup_stale_parties(log_prefix: str = "[Cleanup]"):
    """
    RTDB match_queue/parties \uc5d0\uc11c \uc880\ube44 \ud30c\ud2f0\ub97c \uc81c\uac70\ud569\ub2c8\ub2e4.
    - ai_party_* : \ud56d\uc0c1 \uc81c\uac70
    - matched / loading \uc0c1\ud0dc & \ud604\uc7ac \uba54\ubaa8\ub9ac sessions \uc5d0 \uc5c6\ub294 \ud30c\ud2f0 : searching \uc73c\ub85c \ubcf5\uc6d0
    - in_game \uc0c1\ud0dc \ud30c\ud2f0 (\uc874\uc7ac\ud558\uba74 \uc548 \ub428) : \uc81c\uac70
    """
    data = await rtdb_get("match_queue/parties")
    if not data or not isinstance(data, dict):
        print(f"{log_prefix} \ud30c\ud2f0 \uc5c6\uc74c", flush=True)
        return

    active_mids = set(sessions.keys())
    removed = 0; restored = 0

    for pid, pp in data.items():
        status = pp.get("status", "")
        mid    = pp.get("match_id", "")

        # AI \ud30c\ud2f0\ub294 \ubb34\uc870\uac74 \uc81c\uac70
        if pid.startswith("ai_party_"):
            await rtdb_delete(f"match_queue/parties/{pid}")
            removed += 1
            print(f"{log_prefix} AI\ud30c\ud2f0 \uc81c\uac70: {pid}", flush=True)
            continue

        # matched/loading \uc0c1\ud0dc\uc778\ub370 \ud574\ub2f9 mid \uc138\uc158\uc774 \uba54\ubaa8\ub9ac\uc5d0 \uc5c6\uc73c\uba74 \u2192 searching \ubcf5\uc6d0
        if status in ("matched", "loading") and (not mid or mid not in active_mids):
            await rtdb_patch(f"match_queue/parties/{pid}", {
                "status": "searching",
                "match_id": "",
                "assigned_team": ""
            })
            restored += 1
            print(f"{log_prefix} \uc880\ube44\ud30c\ud2f0 \ubcf5\uc6d0: {pid} (status={status}, mid={mid})", flush=True)
            continue

        # \uc54c \uc218 \uc5c6\ub294 \uc0c1\ud0dc (searching/idle/'' \uc774 \uc544\ub2cc \uac83)
        if status not in ("searching", "idle", "", "matched", "loading"):
            await rtdb_delete(f"match_queue/parties/{pid}")
            removed += 1
            print(f"{log_prefix} \uc54c\uc218\uc5c6\ub294\uc0c1\ud0dc \uc81c\uac70: {pid} (status={status})", flush=True)

    print(f"{log_prefix} \uc644\ub8cc \u2014 \uc81c\uac70:{removed} \ubcf5\uc6d0:{restored}", flush=True)


async def cleanup_stale_rtdb():
    """\uc11c\ubc84 \uc2dc\uc791 \uc2dc \uc804\uccb4 \uc815\ub9ac"""
    print("[Startup] RTDB \uc794\uc5ec \ud30c\ud2f0 \uc815\ub9ac \uc2dc\uc791...", flush=True)
    await cleanup_stale_parties("[Startup]")
    await rtdb_delete("active_matches")
    print("[Startup] active_matches \ucd08\uae30\ud654 \uc644\ub8cc", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    if _USE_HTTPX:
        _http_client = httpx.AsyncClient(timeout=5.0)
    await cleanup_stale_rtdb()
    yield
    if _http_client:
        await _http_client.aclose()

app = FastAPI(lifespan=lifespan)


# ================================================================
# \uac8c\uc784 \ub85c\uc9c1 \uc720\ud2f8
# ================================================================
def dist_2d(a: dict, b: dict) -> float:
    return math.sqrt((a.get("x",0)-b.get("x",0))**2 + (a.get("z",0)-b.get("z",0))**2)

def clamp_pos(x, z):
    return max(MAP_MIN_X, min(MAP_MAX_X, x)), max(MAP_MIN_Z, min(MAP_MAX_Z, z))


def validate_move(uid: str, new_x: float, new_y: float, new_z: float):
    info = clients.get(uid)
    if not info:
        return True, {"x": new_x, "y": new_y, "z": new_z}
    last_pos  = info.get("last_pos")
    last_time = info.get("last_pos_time", time.time())
    dt        = time.time() - last_time
    if last_pos is None or dt <= 0:
        return True, {"x": new_x, "y": new_y, "z": new_z}
    dx = new_x - last_pos["x"]; dz = new_z - last_pos["z"]
    speed = math.sqrt(dx*dx + dz*dz) / dt
    if speed > MAX_MOVE_SPEED * 1.5:
        print(f"[AntiCheat] {uid} \uc18d\ub3c4 \uc704\ubc18 {speed:.1f}m/s", flush=True)
        return False, last_pos
    return True, {"x": new_x, "y": new_y, "z": new_z}

def check_attack_cooldown(uid: str, weapon: str) -> bool:
    info = clients.get(uid)
    if not info: return False
    cooldown = WEAPON_STATS.get(weapon, {}).get("cooldown", 1.0)
    if time.time() - info.get("last_attack_time", 0) < cooldown * 0.85:
        return False
    info["last_attack_time"] = time.time()
    return True


# ================================================================
# \ube0c\ub85c\ub4dc\uce90\uc2a4\ud2b8
# ================================================================
async def broadcast(mid: str, msg: dict, exclude=None):
    data    = json.dumps(msg, ensure_ascii=False)
    targets = [(uid, info) for uid, info in list(clients.items())
               if info["mid"] == mid and uid != exclude]

    async def _send(uid, info):
        try:
            await info["ws"].send_text(data)
        except Exception:
            await _cleanup_client(uid)

    if targets:
        await asyncio.gather(*[_send(uid, info) for uid, info in targets],
                             return_exceptions=True)

async def send_to(uid: str, msg: dict):
    info = clients.get(uid)
    if not info: return
    try:
        await info["ws"].send_text(json.dumps(msg, ensure_ascii=False))
    except Exception:
        await _cleanup_client(uid)


async def _cleanup_client(uid: str):
    info = clients.pop(uid, None)
    if not info: return
    mid = info.get("mid", "")
    sess_status = "N/A"
    if mid and mid in sessions:
        s = sessions[mid]
        sess_status = s["status"]
        s["connected"].discard(uid)
        s["weapon_selected"].discard(uid)
        p = s["players"].get(uid)
        if p: p["disconnected"] = True
        if s["status"] == "in_game":
            await broadcast(mid, {"t": "l", "u": uid})
    print(f"[\uc815\ub9ac] {uid} \uc644\ub8cc (\uc138\uc158\uc0c1\ud0dc: {sess_status})", flush=True)


# ================================================================
# \uc2a4\ub0c5\uc0f7 / \uc2f1\ud06c
# ================================================================
def build_snapshot(mid: str, exclude_uid: str) -> dict:
    s    = sessions.get(mid, {})
    snap = {}
    for uid, p in s.get("players", {}).items():
        if uid == exclude_uid: continue
        snap[uid] = {
            "x":  round(p.get("x",  0),   2),
            "y":  round(p.get("y",  0.5), 2),
            "z":  round(p.get("z",  0),   2),
            "ry": round(p.get("ry", 0),   3),
            "hp": p.get("hp", 100),
            "ko": p.get("ko", False),
            "tm": p.get("tm", "r"),
            "an": p.get("an", "idle"),
        }
    return snap


_last_broadcast: dict = {}

def _state_sig(p: dict) -> str:
    return (f"{round(p.get('x',0),1)},{round(p.get('z',0),1)},"
            f"{p.get('hp',100)},{p.get('ko',False)},{p.get('an','idle')}")

async def sync_loop(mid: str):
    interval  = 1.0 / SYNC_TICK_RATE
    last_sigs: dict = {}
    while mid in sessions and sessions[mid]["status"] == "in_game":
        s     = sessions[mid]
        delta = {}
        for uid, p in s["players"].items():
            sig = _state_sig(p)
            if last_sigs.get(uid) != sig:
                last_sigs[uid] = sig
                delta[uid] = {
                    "x":  round(p.get("x",  0.0), 2),
                    "y":  round(p.get("y",  0.5), 2),
                    "z":  round(p.get("z",  0.0), 2),
                    "ry": round(p.get("ry", 0.0), 3),
                    "an": p.get("an", "idle"),
                    "hp": p.get("hp", 100),
                    "ko": p.get("ko", False),
                    "tm": p.get("tm", "r"),
                }
        if delta:
            await broadcast(mid, {"t": "sv", "pos": delta, "ts": int(time.time()*1000)})
        await asyncio.sleep(interval)
    _last_broadcast.pop(mid, None)


# ================================================================
# KO / \uc804\ud22c
# ================================================================
async def ko_timer(mid: str, uid: str):
    await asyncio.sleep(KO_REVIVE_TIME)
    if mid not in sessions: return
    s = sessions[mid]
    p = s["players"].get(uid)
    if not p or not p.get("ko", False): return
    p["nav_wp"] = None; p["nav_wait"] = 0
    p["ko"] = False; p["hp"] = 30; p["penalty"] = True
    print(f"[KO] {uid} \uc790\ub3d9 \uae30\uc0c1", flush=True)
    await broadcast(mid, {"t": "rev", "uid": uid, "hp": 30, "penalty": True, "auto": True})


async def process_attack(mid: str, attacker_uid: str, msg: dict):
    if mid not in sessions: return
    s        = sessions[mid]
    attacker = s["players"].get(attacker_uid)
    if not attacker or attacker.get("ko", False): return
    weapon    = s["weapons"].get(attacker_uid, {}).get("weapon", "baguette")
    atk_range = WEAPON_STATS[weapon]["atk_range"]
    damage    = WEAPON_STATS[weapon]["damage"]
    if not check_attack_cooldown(attacker_uid, weapon): return
    atk_team       = attacker.get("tm", "r")
    attacker["x"]  = msg.get("x",  attacker.get("x",  0))
    attacker["z"]  = msg.get("z",  attacker.get("z",  0))
    attacker["ry"] = msg.get("ry", attacker.get("ry", 0))
    for tid, target in s["players"].items():
        if tid == attacker_uid: continue
        if target.get("tm") == atk_team: continue
        if target.get("ko", False): continue
        if dist_2d(attacker, target) > atk_range: continue
        target["hp"] = max(0, target["hp"] - damage)
        await broadcast(mid, {"t": "hit", "attacker": attacker_uid,
                               "target": tid, "damage": damage,
                               "hp": target["hp"], "weapon": weapon})
        if target["hp"] <= 0:
            target["hp"] = 0; target["ko"] = True; target["ko_time"] = time.time()
            target["nav_wp"] = None; target["nav_wait"] = 0
            await broadcast(mid, {"t": "ko", "uid": tid})
            asyncio.create_task(ko_timer(mid, tid))


async def process_rescue(mid: str, rescuer_uid: str, target_uid: str):
    if mid not in sessions: return
    s       = sessions[mid]
    rescuer = s["players"].get(rescuer_uid)
    target  = s["players"].get(target_uid)
    if not rescuer or not target: return
    if not target.get("ko", False): return
    if rescuer.get("tm") != target.get("tm"): return
    if dist_2d(rescuer, target) > 1.5: return
    elapsed = time.time() - target.get("ko_time", time.time())
    if elapsed > RESCUE_WINDOW:
        await send_to(rescuer_uid, {"t": "res_fail", "target": target_uid, "reason": "timeout"})
        return
    target["nav_wp"] = None; target["nav_wait"] = 0
    target["ko"] = False; target["hp"] = 100; target["penalty"] = False
    await broadcast(mid, {"t": "rev", "uid": target_uid, "hp": 100,
                           "penalty": False, "auto": False, "rescuer": rescuer_uid})


# ================================================================
# \ub9e4\uce6d
# ================================================================
_ai_fill_tasks: dict = {}

async def ai_fill_later(uid: str):
    _ai_fill_tasks[uid] = True
    await asyncio.sleep(AI_FILL_DELAY)

    # \uc774\ubbf8 in_game \uc138\uc158\uc5d0 \uc788\uc73c\uba74 \uc2a4\ud0b5 (loading \uc0c1\ud0dc\ub294 \uc2a4\ud0b5 \uc548 \ud568)
    already = any(
        uid in s["expected"] and s["status"] == "in_game"
        for s in sessions.values()
    )
    if already:
        print(f"[AI Fill] {uid} \uc774\ubbf8 in_game \uc138\uc158 \uc788\uc74c \u2192 \uc2a4\ud0b5", flush=True)
        _ai_fill_tasks.pop(uid, None)
        return

    data = await rtdb_get("match_queue/parties")
    if not data:
        _ai_fill_tasks.pop(uid, None)
        return

    user_party = user_pid = None
    for pid, pp in data.items():
        if pp.get("status") != "searching": continue
        if uid in pp.get("members", {}):
            user_party = pp; user_pid = pid; break

    if not user_party:
        print(f"[AI Fill] {uid} searching \ud30c\ud2f0 \ubabb \ucc3e\uc74c \u2192 \uc2a4\ud0b5", flush=True)
        _ai_fill_tasks.pop(uid, None)
        return

    members    = user_party.get("members", {}); real_count = len(members)
    ta         = [{"id": user_pid, "size": real_count, "members": dict(members)}]
    ai_members = {f"ai_{i+1}": {"nickname": f"BOT_{i+1}", "tag": "AI", "is_bot": True}
                  for i in range(TEAM_SIZE)}
    ai_pid = f"ai_party_{int(time.time())}"
    tb = [{"id": ai_pid, "size": TEAM_SIZE, "members": ai_members}]
    if real_count < TEAM_SIZE:
        for i in range(TEAM_SIZE - real_count):
            ta[0]["members"][f"ai_fill_{i+1}"] = {"nickname": f"BOT_A{i+1}", "tag": "AI", "is_bot": True}
        ta[0]["size"] = TEAM_SIZE
    print(f"[AI Fill] {uid} AI \ub9e4\uce6d \uc0dd\uc131", flush=True)
    await create_match(ta, tb)
    _ai_fill_tasks.pop(uid, None)

def find_combo(parties, target, current):
    total = sum(p["size"] for p in current)
    if total == target: return current[:]
    if total > target:  return []
    start = 0
    if current:
        for i, p in enumerate(parties):
            if p["id"] == current[-1]["id"]: start = i+1; break
    for i in range(start, len(parties)):
        if total + parties[i]["size"] <= target:
            r = find_combo(parties, target, current+[parties[i]])
            if r: return r
    return []

def try_match(parties):
    avail = []
    for pid, p in parties.items():
        m_id   = p.get("match_id", "")
        status = p.get("status", "")
        size   = len(p.get("members", {}))
        # \u2605 match_id\uac00 \ube48 \ubb38\uc790\uc5f4\uc774\uac70\ub098 None\uc774\uace0 status\uac00 searching\uc778 \uac83\ub9cc \ud6c4\ubcf4
        eligible = (not m_id) and (status == "searching") and (1 <= size <= TEAM_SIZE)
        if eligible:
            avail.append({"id": pid, "size": size, "members": p.get("members", {})})
        else:
            print(f"[try_match] \uc81c\uc678: {pid} match_id={repr(m_id)} status={repr(status)} size={size}", flush=True)

    print(f"[try_match] \ud6c4\ubcf4 \ud30c\ud2f0 {len(avail)}\uac1c: {[p['id'] for p in avail]}", flush=True)
    if len(avail) < 2: return None, None
    avail.sort(key=lambda x: x["size"], reverse=True)
    ta = find_combo(avail, TEAM_SIZE, [])
    if not ta: return None, None
    used = {p["id"] for p in ta}
    tb   = find_combo([p for p in avail if p["id"] not in used], TEAM_SIZE, [])
    return (ta, tb) if tb else (None, None)

def get_uids(parties): return [uid for p in parties for uid in p["members"]]

async def trigger_match():
    global _trigger_match_running
    if _trigger_match_running: return
    _trigger_match_running = True
    try:
        await asyncio.sleep(0.5)

        # \u2605 \ub9e4\uce6d \uc2dc\ub3c4 \uc804\uc5d0 \uc880\ube44 \ud30c\ud2f0 \uc815\ub9ac
        await cleanup_stale_parties("[PreMatch]")

        async with match_lock:
            data = await rtdb_get("match_queue/parties")
            print(f"[trigger_match] RTDB \uc870\ud68c: {type(data)} \ud30c\ud2f0\uc218
