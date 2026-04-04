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
    print("[Warning] httpx 없음 → urllib fallback", flush=True)

from ai_manager import run_ai_loop, WEAPON_STATS as _AI_WEAPON_STATS

print("=== 서버 시작 ===", flush=True)

RTDB_URL    = "https://zgm-base-default-rtdb.asia-southeast1.firebasedatabase.app/"
RTDB_SECRET = os.environ.get("RTDB_SECRET", "")
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
WS_PORT     = int(os.environ.get("PORT", 7860))
WS_PING_INTERVAL = 20

TEAM_SIZE      = 4
SESSION_TTL    = 300
JOIN_TIMEOUT   = 30.0
SYNC_TICK_RATE = 10
AI_TICK_RATE   = 10
MAX_MOVE_SPEED = 6.5
KO_REVIVE_TIME = 20.0
RESCUE_WINDOW  = 10.0

# ── 매치메이킹 워커 설정 ──────────────────────────────────────────
WORKER_TICK     = 1.0   # 워커가 큐를 스캔하는 주기(초)
AI_FILL_AFTER   = 10.0  # 이 시간(초) 이상 기다린 티켓은 AI로 채움

MAP_MIN_X = -16.0; MAP_MAX_X = 16.0
MAP_MIN_Z = -9.0;  MAP_MAX_Z = 9.0
ZONE_POS    = {"x": 0.0, "z": 0.0}
ZONE_RADIUS = 4.0

WEAPON_STATS = _AI_WEAPON_STATS
WEAPONS = list(WEAPON_STATS.keys())
MAPS    = ["default"]

print(f"=== httpx 사용: {_USE_HTTPX} ===", flush=True)
print(f"=== RTDB_SECRET 길이: {len(RTDB_SECRET)} ===", flush=True)
print(f"=== REDIS_URL: {REDIS_URL[:30] if REDIS_URL else '없음'} ===", flush=True)

clients: dict  = {}
sessions: dict = {}

# ── 티켓 저장소 (메모리 내) ──────────────────────────────────────
# { pid: { "pid": str, "members": dict, "size": int, "joined_at": float } }
_tickets: dict = {}
_tickets_lock  = asyncio.Lock()

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
        print(f"[Redis GET 오류] {type(e).__name__}: {e}", flush=True)
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
# 서버 시작 시 RTDB 잔여 데이터 정리
# ================================================================
async def cleanup_stale_rtdb():
    print("[Startup] RTDB 잔여 데이터 정리...", flush=True)
    data = await rtdb_get("match_queue/parties")
    if data and isinstance(data, dict):
        for pid, pp in data.items():
            # AI 파티 제거
            if pid.startswith("ai_party_"):
                await rtdb_delete(f"match_queue/parties/{pid}")
                continue
            # matched/loading 상태 파티를 searching 으로 복원
            status = pp.get("status", "")
            if status in ("matched", "loading", "in_game"):
                await rtdb_patch(f"match_queue/parties/{pid}", {
                    "status": "searching",
                    "match_id": "",
                    "assigned_team": ""
                })
    await rtdb_delete("active_matches")
    print("[Startup] 정리 완료", flush=True)


# ================================================================
# ★★★ 티켓 기반 매치메이킹 워커 ★★★
#
# 실제 프로덕션 방식:
#   1. 클라이언트는 "큐 신청" 티켓만 제출하고 끝 (fire-and-forget)
#   2. 별도 워커가 독립적으로 WORKER_TICK 마다 큐를 스캔
#   3. 워커는 조합 가능한 티켓을 찾아 매치 생성
#   4. AI_FILL_AFTER 초 이상 기다린 티켓은 AI로 채움
#
# 이 방식의 장점:
#   - 큐 신청 시점과 매칭 로직이 완전히 분리됨 (race condition 없음)
#   - _trigger_match_running 같은 글로벌 플래그 불필요
#   - 새 파티가 들어와도 워커가 다음 틱에 자동으로 감지
# ================================================================
async def matchmaking_worker():
    """
    프로덕션 매치메이킹 서비스의 핵심 패턴:
    "독립 워커가 주기적으로 티켓 풀을 스캔해서 매치를 생성한다"
    (Valve, Riot, Blizzard 모두 이 방식의 변형을 사용)
    """
    print("[Worker] 매치메이킹 워커 시작", flush=True)
    while True:
        try:
            await asyncio.sleep(WORKER_TICK)
            await _worker_tick()
        except Exception as e:
            print(f"[Worker] 오류: {e}", flush=True)


async def _worker_tick():
    async with _tickets_lock:
        if not _tickets:
            return

        now = time.time()
        tickets = list(_tickets.values())

        print(f"[Worker] 틱 — 티켓 {len(tickets)}개: {[t['pid'] for t in tickets]}", flush=True)

        # ── 1단계: 실제 플레이어끼리 팀 조합 시도 ─────────────────
        ta, tb = _find_match(tickets)
        if ta and tb:
            print(f"[Worker] 실 플레이어 매칭 성공 "
                  f"RED={[t['pid'] for t in ta]} BLUE={[t['pid'] for t in tb]}", flush=True)
            for t in ta + tb:
                _tickets.pop(t["pid"], None)
            asyncio.create_task(create_match(ta, tb))
            return

        # ── 2단계: AI_FILL_AFTER 초 이상 기다린 티켓이 있으면 AI 채움 ──
        old_tickets = [t for t in tickets if now - t["joined_at"] >= AI_FILL_AFTER]
        if not old_tickets:
            return

        ticket = old_tickets[0]  # 가장 오래 기다린 파티 1개 처리
        _tickets.pop(ticket["pid"], None)

        real_count = ticket["size"]
        ta = [ticket.copy()]

        # 빈 자리 AI로 채움
        if real_count < TEAM_SIZE:
            extra = {
                f"ai_fill_{i+1}": {"nickname": f"BOT_A{i+1}", "tag": "AI", "is_bot": True}
                for i in range(TEAM_SIZE - real_count)
            }
            ta[0]["members"] = {**ticket["members"], **extra}
            ta[0]["size"] = TEAM_SIZE

        ai_pid = f"ai_party_{int(time.time())}"
        ai_members = {
            f"ai_{i+1}": {"nickname": f"BOT_{i+1}", "tag": "AI", "is_bot": True}
            for i in range(TEAM_SIZE)
        }
        tb = [{"pid": ai_pid, "members": ai_members, "size": TEAM_SIZE,
               "joined_at": now}]

        print(f"[Worker] AI 채움 — party={ticket['pid']} real={real_count}", flush=True)
        asyncio.create_task(create_match(ta, tb))


def _find_match(tickets: list):
    """
    현재 티켓 풀에서 두 팀(각 TEAM_SIZE)을 구성할 수 있는 조합을 찾는다.
    간단한 조합 탐색 — 대규모 서비스는 여기에 MMR/지역 필터를 추가한다.
    """
    def combo(pool, target, current):
        total = sum(t["size"] for t in current)
        if total == target:
            return current[:]
        if total > target:
            return []
        start = 0
        if current:
            for i, t in enumerate(pool):
                if t["pid"] == current[-1]["pid"]:
                    start = i + 1
                    break
        for i in range(start, len(pool)):
            if total + pool[i]["size"] <= target:
                r = combo(pool, target, current + [pool[i]])
                if r:
                    return r
        return []

    ta = combo(tickets, TEAM_SIZE, [])
    if not ta:
        return None, None
    used = {t["pid"] for t in ta}
    remaining = [t for t in tickets if t["pid"] not in used]
    tb = combo(remaining, TEAM_SIZE, [])
    return (ta, tb) if tb else (None, None)


# ================================================================
# 티켓 등록 / 취소
# ================================================================
async def enqueue_ticket(pid: str, members: dict):
    """파티가 큐에 들어올 때 티켓 등록"""
    async with _tickets_lock:
        _tickets[pid] = {
            "pid":       pid,
            "members":   members,
            "size":      len(members),
            "joined_at": time.time(),
        }
    print(f"[Ticket] 등록 — pid={pid} size={len(members)} "
          f"총티켓={len(_tickets)}", flush=True)


async def dequeue_ticket(pid: str):
    """파티가 큐를 떠날 때 티켓 제거"""
    async with _tickets_lock:
        removed = _tickets.pop(pid, None)
    if removed:
        print(f"[Ticket] 제거 — pid={pid}", flush=True)


# ================================================================
# @asynccontextmanager lifespan
# ================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    if _USE_HTTPX:
        _http_client = httpx.AsyncClient(timeout=5.0)
    await cleanup_stale_rtdb()
    # ★ 워커를 백그라운드 태스크로 시작
    worker_task = asyncio.create_task(matchmaking_worker())
    yield
    worker_task.cancel()
    if _http_client:
        await _http_client.aclose()

app = FastAPI(lifespan=lifespan)


# ================================================================
# 게임 로직 유틸
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
    print(f"[정리] {uid} 완료 (세션상태: {sess_status})", flush=True)


# ================================================================
# 스냅샷 / 싱크
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


# ================================================================
# KO / 전투
# ================================================================
async def ko_timer(mid: str, uid: str):
    await asyncio.sleep(KO_REVIVE_TIME)
    if mid not in sessions: return
    s = sessions[mid]
    p = s["players"].get(uid)
    if not p or not p.get("ko", False): return
    p["nav_wp"] = None; p["nav_wait"] = 0
    p["ko"] = False; p["hp"] = 30; p["penalty"] = True
    print(f"[KO] {uid} 자동 기상", flush=True)
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
# 매치 생성
# ================================================================
async def create_match(ta: list, tb: list):
    """
    ta, tb: 각각 팀을 구성하는 티켓 리스트
    티켓 구조: { "pid": str, "members": dict, "size": int, "joined_at": float }
    """
    mid    = f"m{int(time.time()*1000)}"
    a_pids = [t["pid"] for t in ta]
    b_pids = [t["pid"] for t in tb]

    def get_uids(team_tickets):
        return list(dict.fromkeys(
            uid for t in team_tickets for uid in t["members"]
        ))

    a_uids = get_uids(ta)
    b_uids = get_uids(tb)

    if set(a_uids) & set(b_uids): return
    if len(a_uids) != TEAM_SIZE or len(b_uids) != TEAM_SIZE: return

    all_uids  = a_uids + b_uids
    real_uids = [u for u in all_uids if not u.startswith("ai_")]
    ai_uids   = [u for u in all_uids if u.startswith("ai_")]
    sel_map   = random.choice(MAPS)

    ai_weapons = {uid: {"weapon": (w := random.choice(WEAPONS)), **WEAPON_STATS[w]}
                  for uid in ai_uids}

    print(f"[Match] {mid} RED:{a_uids} BLUE:{b_uids}", flush=True)

    # Redis에 세션 정보 저장
    redis_ok = await redis_set(f"s:{mid}", {
        "mid": mid, "status": "loading",
        "team_red": a_uids, "team_blue": b_uids,
        "map_id": sel_map, "created_at": int(time.time())
    })
    print(f"[Match] Redis: {'성공' if redis_ok else '실패 → 메모리로 계속'}", flush=True)

    # RTDB: 실제 플레이어 파티에 match_id 알림 (클라이언트가 폴링)
    for pid in a_pids:
        if not pid.startswith("ai_party_"):
            await rtdb_patch(f"match_queue/parties/{pid}",
                             {"match_id": mid, "assigned_team": "r", "status": "matched"})
    for pid in b_pids:
        if not pid.startswith("ai_party_"):
            await rtdb_patch(f"match_queue/parties/{pid}",
                             {"match_id": mid, "assigned_team": "b", "status": "matched"})

    # AI 플레이어 초기 상태
    red_ai  = [u for u in a_uids if u.startswith("ai_")]
    blue_ai = [u for u in b_uids if u.startswith("ai_")]
    ri = bi = 0
    ai_players = {}
    for uid in ai_uids:
        team    = "r" if uid in a_uids else "b"
        ai_list = red_ai if team == "r" else blue_ai
        idx     = ai_list.index(uid) if uid in ai_list else 0
        role    = "defender" if idx % 2 == 0 else "pressure"
        if team == "r":
            spawn = {"x": -10.0 + ri * 0.5, "y": 0.5, "z": float(ri * 2 - 3)}; ri += 1
        else:
            spawn = {"x": 10.0 + bi * 0.5,  "y": 0.5, "z": float(bi * 2 - 3)}; bi += 1
        ai_players[uid] = {
            "tm": team, "hp": 100, "ko": False, "penalty": False,
            "atk_cd": random.uniform(0, 1.2),
            "ai_role": role, "nav_wp": None, "nav_wait": 0,
            "strafe_dir": 1, "flank_side": 0, "tick": 0,
            "disconnected": False,
            **spawn, "ry": 0.0 if team == "r" else 3.14, "an": "idle"
        }
        print(f"  AI {uid}({team}) role={role}", flush=True)

    sessions[mid] = {
        "expected":        set(real_uids),
        "connected":       set(),
        "players":         ai_players,
        "weapons":         dict(ai_weapons),
        "weapon_selected": set(),
        "map_id":          sel_map,
        "team_red":        a_uids,
        "team_blue":       b_uids,
        "status":          "loading",
        "party_ids":       a_pids + b_pids,
    }
    print(f"[Match] {mid} 등록 완료 (실:{len(real_uids)} AI:{len(ai_uids)})", flush=True)
    asyncio.create_task(watch_timeout(mid))


async def watch_timeout(mid):
    await asyncio.sleep(JOIN_TIMEOUT)
    if mid not in sessions or sessions[mid]["status"] == "in_game":
        return
    print(f"[Timeout] {mid} JOIN_TIMEOUT({JOIN_TIMEOUT}s) 초과 → 정리", flush=True)
    await broadcast(mid, {"t": "s", "r": "timeout"})
    await cancel_session(mid)


async def cancel_session(mid):
    if mid not in sessions: return
    s = sessions.pop(mid)
    await redis_del(f"s:{mid}")
    await rtdb_delete(f"active_matches/{mid}")
    for pid in s.get("party_ids", []):
        if pid.startswith("ai_party_"):
            await rtdb_delete(f"match_queue/parties/{pid}")
        else:
            await rtdb_patch(f"match_queue/parties/{pid}", {
                "match_id": "", "assigned_team": "", "status": "searching"
            })
    print(f"[Session] {mid} 완전 정리", flush=True)


async def check_all_weapons_selected(mid):
    if mid not in sessions: return
    s = sessions[mid]
    if s["expected"] != s["weapon_selected"]: return
    s["status"] = "in_game"
    print(f"[Weapon] {mid} 전원 선택 → 게임 시작", flush=True)
    await broadcast(mid, {
        "t":         "w_ready",
        "mid":       mid,
        "weapons":   s["weapons"],
        "map_id":    s.get("map_id", "default"),
        "r":         s["team_red"],
        "b":         s["team_blue"],
        "team_red":  s["team_red"],
        "team_blue": s["team_blue"],
    })
    rd = await redis_get(f"s:{mid}")
    if rd:
        rd["status"] = "in_game"
        await redis_set(f"s:{mid}", rd)
    asyncio.create_task(sync_loop(mid))
    asyncio.create_task(run_ai_loop(mid, sessions, broadcast, ko_timer, AI_TICK_RATE))


# ================================================================
# WebSocket 엔드포인트
# ================================================================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    uid = None; mid = None
    print("[WS] 연결", flush=True)
    try:
        while True:
            raw = await ws.receive_text()
            try: msg = json.loads(raw)
            except: continue
            t = msg.get("t", "")

            # ── 큐 신청 ────────────────────────────────────────────
            # 기존: trigger_match() 직접 호출 (race condition 유발)
            # 변경: 티켓만 등록하고 워커가 알아서 처리
            if t == "q":
                uid = msg.get("u", "")
                pid = msg.get("pid", "")
                members = msg.get("members", {uid: {"nickname": uid, "tag": "PLAYER"}})

                print(f"[q] 큐 신청: uid={uid} pid={pid}", flush=True)

                # RTDB 파티 상태를 searching 으로 설정
                if pid:
                    await rtdb_patch(f"match_queue/parties/{pid}", {
                        "status": "searching",
                        "match_id": "",
                        "assigned_team": ""
                    })
                else:
                    # pid 없으면 uid로 파티 찾기
                    pid = uid  # fallback: uid를 pid로 사용
                    all_parties = await rtdb_get("match_queue/parties")
                    if all_parties and isinstance(all_parties, dict):
                        for fpid, fp in all_parties.items():
                            if uid in fp.get("members", {}) and not fpid.startswith("ai_party_"):
                                pid = fpid
                                members = fp.get("members", members)
                                await rtdb_patch(f"match_queue/parties/{fpid}", {
                                    "status": "searching",
                                    "match_id": "",
                                    "assigned_team": ""
                                })
                                break

                # ★ 티켓 등록 — 워커가 다음 틱에 자동으로 처리
                await enqueue_ticket(pid, members)
                await ws.send_text(json.dumps({"t": "w"}))

            # ── 큐 취소 ────────────────────────────────────────────
            elif t == "q_cancel":
                pid = msg.get("pid", "")
                if pid:
                    await dequeue_ticket(pid)
                    if pid:
                        await rtdb_patch(f"match_queue/parties/{pid}", {
                            "status": "idle",
                            "match_id": "",
                            "assigned_team": ""
                        })
                await ws.send_text(json.dumps({"t": "q_cancelled"}))

            # ── 게임 참가 ──────────────────────────────────────────
            elif t == "j":
                uid  = msg.get("u", "")
                mid  = msg.get("mid", "")
                team = msg.get("tm", "r")
                if mid not in sessions:
                    await ws.send_text(json.dumps({"t": "f", "r": "inv"})); continue
                s = sessions[mid]
                if uid not in s["expected"]:
                    await ws.send_text(json.dumps({"t": "f", "r": "na"})); continue
                if uid in clients:
                    old_ws = clients[uid].get("ws")
                    if old_ws and old_ws is not ws:
                        try: await old_ws.close()
                        except: pass
                    clients.pop(uid, None)
                clients[uid] = {
                    "ws": ws, "mid": mid, "team": team,
                    "last_pos": None, "last_pos_time": time.time(),
                    "last_attack_time": 0,
                }
                s["connected"].add(uid)
                if uid in s["players"] and s["players"][uid].get("disconnected", False):
                    p = s["players"][uid]; p["disconnected"] = False
                    spawn = {"x": p["x"], "y": p["y"], "z": p["z"],
                             "ry": p["ry"], "an": "idle"}
                else:
                    if team == "r":
                        idx   = len([u for u in s["team_red"] if u in s["connected"]]) - 1
                        spawn = {"x": -10.0, "y": 0.5, "z": float(idx*2), "ry": 0.0, "an": "idle"}
                    else:
                        idx   = len([u for u in s["team_blue"] if u in s["connected"]]) - 1
                        spawn = {"x": 10.0, "y": 0.5, "z": float(idx*2), "ry": 3.14, "an": "idle"}
                    s["players"][uid] = {
                        "tm": team, "hp": 100, "ko": False, "penalty": False,
                        "nav_wp": None, "nav_wait": 0, "strafe_dir": 1,
                        "flank_side": 0, "tick": 0, "ai_role": "none",
                        "disconnected": False, **spawn
                    }
                clients[uid]["last_pos"] = {"x": spawn["x"], "y": spawn["y"], "z": spawn["z"]}
                cn = len(s["connected"]); ex = len(s["expected"])
                snap = build_snapshot(mid, uid)
                await ws.send_text(json.dumps({
                    "t":         "js",
                    "u":         uid,
                    "tm":        team,
                    "sp":        spawn,
                    "sn":        snap,
                    "cn":        cn,
                    "ex":        ex,
                    "ai_weapons":  s["weapons"],
                    "map_id":    s.get("map_id", "default"),
                    "reconnect": s["status"] == "in_game",
                    "team_red":  s["team_red"],
                    "team_blue": s["team_blue"],
                }))
                await broadcast(mid, {"t": "sp", "u": uid, "tm": team,
                                       "sp": spawn, "cn": cn, "ex": ex}, exclude=uid)
                if s["expected"] == s["connected"]:
                    await broadcast(mid, {"t": "g", "mid": mid,
                                           "r": s["team_red"], "b": s["team_blue"]})

            # ── 무기 선택 ──────────────────────────────────────────
            elif t == "w_sel":
                uid    = msg.get("u", ""); mid = msg.get("mid", "")
                weapon = msg.get("weapon", "baguette")
                if mid not in sessions: continue
                s = sessions[mid]
                if weapon not in WEAPON_STATS: weapon = "baguette"
                s["weapons"][uid] = {"weapon": weapon, **WEAPON_STATS[weapon]}
                s["weapon_selected"].add(uid)
                await ws.send_text(json.dumps({
                    "t":        "w_ok",
                    "weapon":   weapon,
                    "stats":    WEAPON_STATS[weapon],
                    "cooldown": WEAPON_STATS[weapon]["cooldown"],
                }))
                asyncio.create_task(check_all_weapons_selected(mid))

            # ── 이동 ───────────────────────────────────────────────
            elif t == "mv":
                uid = msg.get("u", ""); mid = msg.get("mid", "")
                if mid not in sessions or uid not in clients: continue
                s = sessions[mid]
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

            # ── 공격 ───────────────────────────────────────────────
            elif t == "atk":
                uid = msg.get("u", ""); mid = msg.get("mid", "")
                asyncio.create_task(process_attack(mid, uid, msg))

            # ── 구조 ───────────────────────────────────────────────
            elif t == "res":
                uid = msg.get("u", ""); mid = msg.get("mid", "")
                asyncio.create_task(process_rescue(mid, uid, msg.get("target", "")))

            # ── 핑 ─────────────────────────────────────────────────
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
        "tickets":         len(_tickets),
        "ticket_pids":     list(_tickets.keys()),
        "httpx":           _USE_HTTPX,
        "rtdb_secret_len": len(RTDB_SECRET),
    }

@app.head("/")
async def health_head():
    return Response(status_code=200)


if __name__ == "__main__":
    for var, name in [(RTDB_SECRET,"RTDB_SECRET"),(REDIS_URL,"REDIS_URL"),(REDIS_TOKEN,"REDIS_TOKEN")]:
        if not var: print(f"[Server] 경고: {name} 없음!", flush=True)
    print(f"[Server] 포트 {WS_PORT}", flush=True)
    uvicorn.run(
        app, host="0.0.0.0", port=WS_PORT,
        ws_ping_interval=WS_PING_INTERVAL,
        ws_ping_timeout=30,
    )
