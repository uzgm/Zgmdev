import asyncio, json, os, time, random, math
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
import uvicorn

try:
    import httpx
    _USE_HTTPX = True
except ImportError:
    _USE_HTTPX = False
    print("[Warning] httpx 없음 → urllib fallback", flush=True)

from ai_manager import run_ai_loop, WEAPON_STATS as _AI_WEAPON_STATS

print("=== 서버 시작 (룸 기반) ===", flush=True)

RTDB_URL    = "https://zgm-base-default-rtdb.asia-southeast1.firebasedatabase.app/"
RTDB_SECRET = os.environ.get("RTDB_SECRET", "")
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
WS_PORT     = int(os.environ.get("PORT", 7860))
WS_PING_INTERVAL = 20

# 게임 설정
SESSION_TTL    = 600
SYNC_TICK_RATE = 10
AI_TICK_RATE   = 10
MAX_MOVE_SPEED = 6.5
KO_REVIVE_TIME = 20.0
RESCUE_WINDOW  = 10.0
WEAPON_SELECT_TIMEOUT = 30.0

MAP_MIN_X = -16.0; MAP_MAX_X = 16.0
MAP_MIN_Z = -9.0;  MAP_MAX_Z = 9.0

WEAPON_STATS = _AI_WEAPON_STATS
WEAPONS = list(WEAPON_STATS.keys())
MAPS    = ["default"]

print(f"=== httpx: {_USE_HTTPX} | RTDB_SECRET 길이: {len(RTDB_SECRET)} ===", flush=True)

# ================================================================
# 전역 상태
# clients[uid]  = { ws, room_code, last_pos, last_pos_time, last_attack_time }
# sessions[room_code] = {
#   players, weapons, weapon_selected, connected, expected,
#   status, host_uid, max_players, room_name, created_at,
#   ai_uids
# }
# ================================================================
clients: dict  = {}
sessions: dict = {}

_http_client = None


# ================================================================
# HTTP 헬퍼
# ================================================================
async def _get(url: str):
    try:
        if _USE_HTTPX and _http_client:
            r = await _http_client.get(url)
            return r.json() if r.status_code == 200 else None
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            raw  = await loop.run_in_executor(None, lambda: ur.urlopen(url, timeout=5).read())
            return json.loads(raw)
    except Exception as e:
        print(f"[HTTP GET 오류] {type(e).__name__}: {e}", flush=True)
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
        print(f"[HTTP PUT 오류] {type(e).__name__}: {e}", flush=True)
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
        print(f"[HTTP PATCH 오류] {type(e).__name__}: {e}", flush=True)
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
        print(f"[HTTP DELETE 오류] {type(e).__name__}: {e}", flush=True)


# ================================================================
# Redis 헬퍼
# ================================================================
def _redis_headers():
    return {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}

async def redis_set(key, value, ex=SESSION_TTL) -> bool:
    if not REDIS_URL: return False
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
        print(f"[Redis SET 오류] {type(e).__name__}: {e}", flush=True)
        return False

async def redis_del(key):
    if not REDIS_URL: return
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
        print(f"[Redis DEL 오류] {type(e).__name__}: {e}", flush=True)


# ================================================================
# RTDB 헬퍼
# ================================================================
def _rtdb(path): return f"{RTDB_URL}{path}.json?auth={RTDB_SECRET}"

async def rtdb_get(path):      return await _get(_rtdb(path))
async def rtdb_put(path, d):   return await _put(_rtdb(path), d)
async def rtdb_patch(path, d): return await _patch(_rtdb(path), d)
async def rtdb_delete(path):   await _delete(_rtdb(path))


# ================================================================
# lifespan
# ================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    if _USE_HTTPX:
        _http_client = httpx.AsyncClient(timeout=5.0)
    print("[Startup] 룸 기반 서버 준비 완료", flush=True)
    yield
    if _http_client:
        await _http_client.aclose()

app = FastAPI(lifespan=lifespan)


# ================================================================
# 게임 로직 유틸
# ================================================================
def dist_2d(a: dict, b: dict) -> float:
    return math.sqrt((a.get("x", 0) - b.get("x", 0))**2 +
                     (a.get("z", 0) - b.get("z", 0))**2)

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
        print(f"[AntiCheat] {uid} 속도 위반 {speed:.1f}m/s", flush=True)
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
# 브로드캐스트
# ================================================================
async def broadcast(room_code: str, msg: dict, exclude=None):
    data    = json.dumps(msg, ensure_ascii=False)
    targets = [(uid, info) for uid, info in list(clients.items())
               if info.get("room_code") == room_code and uid != exclude]

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
    room_code = info.get("room_code", "")
    if room_code and room_code in sessions:
        s = sessions[room_code]
        s["connected"].discard(uid)
        p = s["players"].get(uid)
        if p: p["disconnected"] = True
        if s["status"] == "in_game":
            await broadcast(room_code, {"t": "l", "u": uid})
    print(f"[정리] {uid} 완료", flush=True)


# ================================================================
# 스냅샷 / 싱크
# ================================================================
def build_snapshot(room_code: str, exclude_uid: str) -> dict:
    s    = sessions.get(room_code, {})
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

def _state_sig(p: dict) -> str:
    return (f"{round(p.get('x',0),1)},{round(p.get('z',0),1)},"
            f"{p.get('hp',100)},{p.get('ko',False)},{p.get('an','idle')}")

async def sync_loop(room_code: str):
    interval   = 1.0 / SYNC_TICK_RATE
    last_sigs: dict = {}
    while room_code in sessions and sessions[room_code]["status"] == "in_game":
        s     = sessions[room_code]
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
            await broadcast(room_code,
                            {"t": "sv", "pos": delta, "ts": int(time.time() * 1000)})
        await asyncio.sleep(interval)


# ================================================================
# KO / 전투
# ================================================================
async def ko_timer(room_code: str, uid: str):
    await asyncio.sleep(KO_REVIVE_TIME)
    if room_code not in sessions: return
    s = sessions[room_code]
    p = s["players"].get(uid)
    if not p or not p.get("ko", False): return
    p["nav_wp"] = None; p["nav_wait"] = 0
    p["ko"] = False; p["hp"] = 30; p["penalty"] = True
    print(f"[KO] {uid} 자동 기상", flush=True)
    await broadcast(room_code,
                    {"t": "rev", "uid": uid, "hp": 30, "penalty": True, "auto": True})

async def process_attack(room_code: str, attacker_uid: str, msg: dict):
    if room_code not in sessions: return
    s        = sessions[room_code]
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
        await broadcast(room_code,
                        {"t": "hit", "attacker": attacker_uid, "target": tid,
                         "damage": damage, "hp": target["hp"], "weapon": weapon})
        if target["hp"] <= 0:
            target["hp"] = 0; target["ko"] = True; target["ko_time"] = time.time()
            target["nav_wp"] = None; target["nav_wait"] = 0
            await broadcast(room_code, {"t": "ko", "uid": tid})
            asyncio.create_task(ko_timer(room_code, tid))

async def process_rescue(room_code: str, rescuer_uid: str, target_uid: str):
    if room_code not in sessions: return
    s       = sessions[room_code]
    rescuer = s["players"].get(rescuer_uid)
    target  = s["players"].get(target_uid)
    if not rescuer or not target: return
    if not target.get("ko", False): return
    if rescuer.get("tm") != target.get("tm"): return
    if dist_2d(rescuer, target) > 1.5: return
    elapsed = time.time() - target.get("ko_time", time.time())
    if elapsed > RESCUE_WINDOW:
        await send_to(rescuer_uid,
                      {"t": "res_fail", "target": target_uid, "reason": "timeout"})
        return
    target["nav_wp"] = None; target["nav_wait"] = 0
    target["ko"] = False; target["hp"] = 100; target["penalty"] = False
    await broadcast(room_code,
                    {"t": "rev", "uid": target_uid, "hp": 100,
                     "penalty": False, "auto": False, "rescuer": rescuer_uid})


# ================================================================
# 룸 세션 생성 (RTDB 방 데이터 기반)
# ================================================================
async def create_game_session(room_code: str, room_data: dict):
    """
    RTDB의 방 데이터를 읽어 서버 인메모리 세션을 생성한다.
    호스트가 게임 시작 버튼을 누를 때 WS t=start_game 으로 호출됨.
    """
    if room_code in sessions:
        print(f"[Session] {room_code} 이미 존재", flush=True)
        return

    players_data = room_data.get("players", {})
    host_uid     = room_data.get("hostId", "")
    max_players  = room_data.get("maxPlayers", 4)

    # 플레이어 목록: 실제 유저 + AI
    real_uids = []
    ai_uids   = []
    for uid, pinfo in players_data.items():
        if pinfo.get("isAI", False):
            ai_uids.append(uid)
        else:
            real_uids.append(uid)

    # 팀 배정: 절반씩 나눔 (호스트는 항상 레드)
    all_uids = real_uids + ai_uids
    half     = max(1, len(all_uids) // 2)
    team_red  = all_uids[:half]
    team_blue = all_uids[half:]

    sel_map = random.choice(MAPS)

    # AI 플레이어 초기화
    ai_players  = {}
    ai_weapons  = {}
    red_idx = 0; blue_idx = 0

    for uid in ai_uids:
        team   = "r" if uid in team_red else "b"
        weapon = random.choice(WEAPONS)
        ai_weapons[uid] = {"weapon": weapon, **WEAPON_STATS[weapon]}

        if team == "r":
            spawn = {"x": -10.0 + red_idx * 0.5, "y": 0.5,
                     "z": float(red_idx * 2 - 3)}
            red_idx += 1
        else:
            spawn = {"x": 10.0 + blue_idx * 0.5, "y": 0.5,
                     "z": float(blue_idx * 2 - 3)}
            blue_idx += 1

        role = "defender" if (red_idx + blue_idx) % 2 == 0 else "pressure"
        ai_players[uid] = {
            "tm": team, "hp": 100, "ko": False, "penalty": False,
            "atk_cd": random.uniform(0, 1.2),
            "ai_role": role, "nav_wp": None, "nav_wait": 0,
            "strafe_dir": 1, "flank_side": 0, "tick": 0,
            "disconnected": False,
            **spawn, "ry": 0.0 if team == "r" else 3.14, "an": "idle"
        }

    sessions[room_code] = {
        "expected":        set(real_uids),
        "connected":       set(),
        "players":         ai_players,       # AI는 미리 채워둠, 실제 유저는 j 때 추가
        "weapons":         dict(ai_weapons),
        "weapon_selected": set(ai_uids),     # AI는 이미 선택된 것으로 처리
        "map_id":          sel_map,
        "team_red":        team_red,
        "team_blue":       team_blue,
        "status":          "loading",
        "host_uid":        host_uid,
        "max_players":     max_players,
        "room_name":       room_data.get("roomName", "방 " + room_code),
        "ai_uids":         ai_uids,
        "created_at":      time.time(),
    }

    print(f"[Session] {room_code} 생성 완료 "
          f"real={len(real_uids)} AI={len(ai_uids)} "
          f"RED={team_red} BLUE={team_blue}", flush=True)

    # RTDB에 gameStatus = starting 기록
    await rtdb_patch(f"rooms/{room_code}", {
        "gameStatus": "starting",
        "serverSessionId": room_code,
    })

    # 세션 타임아웃 감시
    asyncio.create_task(_session_timeout_watch(room_code))

    return sessions[room_code]


async def _session_timeout_watch(room_code: str):
    """실제 유저가 JOIN_TIMEOUT 안에 접속 안 하면 세션 정리"""
    JOIN_TIMEOUT = 60.0
    await asyncio.sleep(JOIN_TIMEOUT)
    if room_code not in sessions: return
    s = sessions[room_code]
    if s["status"] == "in_game": return
    print(f"[Timeout] {room_code} JOIN_TIMEOUT 초과 → 정리", flush=True)
    await broadcast(room_code, {"t": "s", "r": "timeout"})
    await _cancel_session(room_code)


async def _cancel_session(room_code: str):
    if room_code not in sessions: return
    sessions.pop(room_code, None)
    await redis_del(f"room:{room_code}")
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "waiting"})
    print(f"[Session] {room_code} 정리 완료", flush=True)


async def check_all_weapons_selected(room_code: str):
    if room_code not in sessions: return
    s = sessions[room_code]
    # 실제 유저 전원 + AI(이미 처리됨) 선택 완료 여부
    if s["expected"] != s["weapon_selected"]: return
    s["status"] = "in_game"
    print(f"[Weapon] {room_code} 전원 선택 → 게임 시작", flush=True)
    await broadcast(room_code, {
        "t":         "w_ready",
        "room_code": room_code,
        "weapons":   s["weapons"],
        "map_id":    s.get("map_id", "default"),
        "team_red":  s["team_red"],
        "team_blue": s["team_blue"],
    })
    # RTDB 상태 업데이트
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "playing"})
    asyncio.create_task(sync_loop(room_code))
    asyncio.create_task(run_ai_loop(room_code, sessions, broadcast, ko_timer, AI_TICK_RATE))


# ================================================================
# HTTP 엔드포인트
# ================================================================

# ── 룸 시작 (호스트 전용 HTTP 트리거, WS로도 가능)
@app.post("/room/start")
async def http_room_start(request: Request):
    """
    Godot 클라이언트가 직접 호출 가능한 게임 시작 트리거.
    body: { "room_code": "ABCDEF", "host_uid": "...", "room_data": {...} }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    room_code = body.get("room_code", "").upper().strip()
    host_uid  = body.get("host_uid", "")
    room_data = body.get("room_data", {})

    if not room_code:
        return JSONResponse({"ok": False, "error": "room_code required"}, status_code=400)

    # 이미 세션 있으면 중복 방지
    if room_code in sessions:
        s = sessions[room_code]
        return JSONResponse({
            "ok": True,
            "already_exists": True,
            "status": s["status"],
            "team_red": s["team_red"],
            "team_blue": s["team_blue"],
        })

    # RTDB에서 방 데이터 가져오기 (room_data가 없을 경우)
    if not room_data:
        room_data = await rtdb_get(f"rooms/{room_code}")
        if not room_data:
            return JSONResponse({"ok": False, "error": "room not found"}, status_code=404)

    # 호스트 검증
    if room_data.get("hostId", "") != host_uid:
        return JSONResponse({"ok": False, "error": "not host"}, status_code=403)

    sess = await create_game_session(room_code, room_data)
    if not sess:
        return JSONResponse({"ok": False, "error": "session create failed"}, status_code=500)

    print(f"[HTTP] 룸 시작 — room_code={room_code} host={host_uid}", flush=True)
    return JSONResponse({
        "ok":        True,
        "room_code": room_code,
        "team_red":  sess["team_red"],
        "team_blue": sess["team_blue"],
        "map_id":    sess["map_id"],
    })


@app.get("/room/{room_code}/status")
async def http_room_status(room_code: str):
    room_code = room_code.upper().strip()
    if room_code not in sessions:
        return JSONResponse({"ok": False, "exists": False})
    s = sessions[room_code]
    return JSONResponse({
        "ok":        True,
        "exists":    True,
        "status":    s["status"],
        "connected": len(s["connected"]),
        "expected":  len(s["expected"]),
        "team_red":  s["team_red"],
        "team_blue": s["team_blue"],
    })


# ================================================================
# WebSocket 엔드포인트
# ================================================================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    uid       = None
    room_code = None
    print("[WS] 연결", flush=True)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("t", "")

            # ── 게임 참가 (룸 코드 + 유저 ID)
            if t == "j":
                uid       = msg.get("u", "")
                room_code = msg.get("room_code", "").upper().strip()
                team_hint = msg.get("tm", "")   # 클라이언트가 알고 있는 팀 (옵션)

                if not room_code or room_code not in sessions:
                    await ws.send_text(json.dumps({"t": "f", "r": "no_session"}))
                    continue

                s = sessions[room_code]

                if uid not in s["expected"]:
                    await ws.send_text(json.dumps({"t": "f", "r": "na"}))
                    continue

                # 기존 연결 정리
                if uid in clients:
                    old_ws = clients[uid].get("ws")
                    if old_ws and old_ws is not ws:
                        try: await old_ws.close()
                        except: pass
                    clients.pop(uid, None)

                # 팀 결정 (서버 기준 우선)
                if uid in s["team_red"]:
                    team = "r"
                elif uid in s["team_blue"]:
                    team = "b"
                else:
                    team = team_hint if team_hint in ("r", "b") else "r"

                clients[uid] = {
                    "ws":               ws,
                    "room_code":        room_code,
                    "team":             team,
                    "last_pos":         None,
                    "last_pos_time":    time.time(),
                    "last_attack_time": 0,
                }
                s["connected"].add(uid)

                # 스폰 위치 결정
                is_reconnect = uid in s["players"] and not s["players"][uid].get("disconnected", True)
                if is_reconnect:
                    p = s["players"][uid]
                    p["disconnected"] = False
                    spawn = {"x": p["x"], "y": p["y"], "z": p["z"],
                             "ry": p["ry"], "an": "idle"}
                else:
                    idx = len([u for u in s["team_red"] if u in s["connected"]]) - 1 \
                          if team == "r" else \
                          len([u for u in s["team_blue"] if u in s["connected"]]) - 1
                    if team == "r":
                        spawn = {"x": -10.0, "y": 0.5,
                                 "z": float(max(idx, 0) * 2), "ry": 0.0, "an": "idle"}
                    else:
                        spawn = {"x": 10.0, "y": 0.5,
                                 "z": float(max(idx, 0) * 2), "ry": 3.14, "an": "idle"}
                    s["players"][uid] = {
                        "tm": team, "hp": 100, "ko": False, "penalty": False,
                        "nav_wp": None, "nav_wait": 0, "strafe_dir": 1,
                        "flank_side": 0, "tick": 0, "ai_role": "none",
                        "disconnected": False, **spawn
                    }

                clients[uid]["last_pos"] = {"x": spawn["x"], "y": spawn["y"], "z": spawn["z"]}

                snap = build_snapshot(room_code, uid)
                cn   = len(s["connected"])
                ex   = len(s["expected"])

                await ws.send_text(json.dumps({
                    "t":          "js",
                    "u":          uid,
                    "tm":         team,
                    "sp":         spawn,
                    "sn":         snap,
                    "cn":         cn,
                    "ex":         ex,
                    "ai_weapons": s["weapons"],
                    "map_id":     s.get("map_id", "default"),
                    "reconnect":  s["status"] == "in_game",
                    "team_red":   s["team_red"],
                    "team_blue":  s["team_blue"],
                    "room_code":  room_code,
                }))

                await broadcast(room_code,
                                {"t": "sp", "u": uid, "tm": team,
                                 "sp": spawn, "cn": cn, "ex": ex},
                                exclude=uid)

                # 전원 접속 완료 시 게임 준비 알림
                if s["expected"] == s["connected"]:
                    await broadcast(room_code, {
                        "t":         "g",
                        "room_code": room_code,
                        "r":         s["team_red"],
                        "b":         s["team_blue"],
                    })

            # ── 무기 선택
            elif t == "w_sel":
                uid       = msg.get("u", "")
                room_code = msg.get("room_code", "").upper().strip()
                weapon    = msg.get("weapon", "baguette")
                if room_code not in sessions: continue
                s = sessions[room_code]
                if weapon not in WEAPON_STATS: weapon = "baguette"
                s["weapons"][uid]         = {"weapon": weapon, **WEAPON_STATS[weapon]}
                s["weapon_selected"].add(uid)
                await ws.send_text(json.dumps({
                    "t":        "w_ok",
                    "weapon":   weapon,
                    "stats":    WEAPON_STATS[weapon],
                    "cooldown": WEAPON_STATS[weapon]["cooldown"],
                }))
                asyncio.create_task(check_all_weapons_selected(room_code))

            # ── 이동
            elif t == "mv":
                uid       = msg.get("u", "")
                room_code = msg.get("room_code", "")
                if not room_code and uid in clients:
                    room_code = clients[uid].get("room_code", "")
                if room_code not in sessions or uid not in clients: continue
                s = sessions[room_code]
                if uid not in s["players"]: continue
                new_x = float(msg.get("x", 0))
                new_y = float(msg.get("y", 0.5))
                new_z = float(msg.get("z", 0))
                valid, pos = validate_move(uid, new_x, new_y, new_z)
                p        = s["players"][uid]
                p["x"]   = pos["x"]; p["y"] = pos["y"]; p["z"] = pos["z"]
                p["ry"]  = msg.get("ry", p.get("ry", 0.0))
                p["an"]  = msg.get("an", p.get("an", "idle"))
                clients[uid]["last_pos"]      = pos
                clients[uid]["last_pos_time"] = time.time()
                if not valid:
                    await send_to(uid, {"t": "rb",
                                        "x": pos["x"], "y": pos["y"], "z": pos["z"],
                                        "force": True})

            # ── 공격
            elif t == "atk":
                uid       = msg.get("u", "")
                room_code = msg.get("room_code", "")
                if not room_code and uid in clients:
                    room_code = clients[uid].get("room_code", "")
                asyncio.create_task(process_attack(room_code, uid, msg))

            # ── 구조
            elif t == "res":
                uid       = msg.get("u", "")
                room_code = msg.get("room_code", "")
                if not room_code and uid in clients:
                    room_code = clients[uid].get("room_code", "")
                asyncio.create_task(process_rescue(room_code, uid, msg.get("target", "")))

            # ── 핑
            elif t == "p":
                await ws.send_text(json.dumps({"t": "po"}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS 오류] {uid}: {e}", flush=True)
    finally:
        if uid:
            await _cleanup_client(uid)
        print(f"[WS] 해제: {uid}", flush=True)


# ================================================================
# 헬스체크
# ================================================================
@app.get("/")
async def health():
    return {
        "status":          "ok",
        "clients":         len(clients),
        "sessions":        len(sessions),
        "session_codes":   list(sessions.keys()),
        "httpx":           _USE_HTTPX,
        "rtdb_secret_len": len(RTDB_SECRET),
    }

@app.head("/")
async def health_head():
    return Response(status_code=200)


if __name__ == "__main__":
    for var, name in [(RTDB_SECRET, "RTDB_SECRET"),
                      (REDIS_URL, "REDIS_URL"),
                      (REDIS_TOKEN, "REDIS_TOKEN")]:
        if not var:
            print(f"[Server] 경고: {name} 없음!", flush=True)
    print(f"[Server] 포트 {WS_PORT}", flush=True)
    uvicorn.run(
        app, host="0.0.0.0", port=WS_PORT,
        ws_ping_interval=WS_PING_INTERVAL,
        ws_ping_timeout=30,
    )
