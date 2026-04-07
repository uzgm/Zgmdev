"""
mafia_server.py — 마피아 권위형 서버
- Redis (Upstash): 게임 상태 저장 (역할/투표/생존 등)
- RTDB: 방 목록 표시용 (gameStatus 반영만)
- WebSocket /ws/mafia: 실시간 페이즈/투표/채팅 전체 관리
"""
import asyncio, json, os, random, time
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

RTDB_URL    = "https://zgm-base-default-rtdb.asia-southeast1.firebasedatabase.app/"
RTDB_SECRET = os.environ.get("RTDB_SECRET", "")
REDIS_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
WS_PORT     = int(os.environ.get("PORT", 7860))

PHASE_TIMES = {
    "meet":    10,
    "night":   25,
    "morning": 15,
    "day":     35,
    "vote":    25,
}
MAX_ROUNDS  = 10
SESSION_TTL = 3600  # Redis TTL (초)

print(f"=== 마피아 서버 | httpx={_USE_HTTPX} | redis={bool(REDIS_URL)} ===", flush=True)

# ================================================================
# 전역
# sessions[room_code] = {
#   "players": { uid: { ws, nickname, tag, isHost, isAI } },
#   "phase_task": asyncio.Task | None,
#   "status":  "waiting" | "playing" | "ended"
# }
# 게임 상태(역할/투표/생존)는 Redis 에 저장
# ================================================================
sessions: dict = {}
_http_client   = None


# ================================================================
# HTTP 헬퍼
# ================================================================
async def _http_get(url: str):
    try:
        if _USE_HTTPX and _http_client:
            r = await _http_client.get(url)
            return r.json() if r.status_code == 200 else None
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: ur.urlopen(url, timeout=5).read())
        return json.loads(raw)
    except Exception as e:
        print(f"[GET 오류] {e}", flush=True); return None

async def _http_put(url: str, data) -> bool:
    try:
        body = json.dumps(data)
        if _USE_HTTPX and _http_client:
            r = await _http_client.put(url, content=body,
                                        headers={"Content-Type": "application/json"})
            return r.status_code == 200
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        req = ur.Request(url, data=body.encode(),
                         headers={"Content-Type": "application/json"}, method="PUT")
        await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
        return True
    except Exception as e:
        print(f"[PUT 오류] {e}", flush=True); return False

async def _http_patch(url: str, data) -> bool:
    try:
        body = json.dumps(data)
        if _USE_HTTPX and _http_client:
            r = await _http_client.patch(url, content=body,
                                          headers={"Content-Type": "application/json"})
            return r.status_code == 200
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        req = ur.Request(url, data=body.encode(),
                         headers={"Content-Type": "application/json"}, method="PATCH")
        await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
        return True
    except Exception as e:
        print(f"[PATCH 오류] {e}", flush=True); return False

async def _http_delete(url: str):
    try:
        if _USE_HTTPX and _http_client:
            await _http_client.delete(url)
        else:
            import urllib.request as ur
            loop = asyncio.get_event_loop()
            req = ur.Request(url, method="DELETE")
            await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5))
    except Exception as e:
        print(f"[DELETE 오류] {e}", flush=True)

def _rtdb(path): return f"{RTDB_URL}{path}.json?auth={RTDB_SECRET}"
async def rtdb_get(path):      return await _http_get(_rtdb(path))
async def rtdb_patch(path, d): return await _http_patch(_rtdb(path), d)


# ================================================================
# Redis 헬퍼 (Upstash REST)
# ================================================================
def _redis_headers():
    return {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}

async def _redis_cmd(*args):
    if not REDIS_URL: return None
    try:
        body = json.dumps(list(args))
        if _USE_HTTPX and _http_client:
            r = await _http_client.post(REDIS_URL, content=body, headers=_redis_headers())
            return r.json().get("result")
        import urllib.request as ur
        loop = asyncio.get_event_loop()
        req = ur.Request(REDIS_URL, data=body.encode(),
                         headers=_redis_headers(), method="POST")
        raw = await loop.run_in_executor(None, lambda: ur.urlopen(req, timeout=5).read())
        return json.loads(raw).get("result")
    except Exception as e:
        print(f"[Redis 오류] {e}", flush=True); return None

async def redis_set(key: str, value: dict, ex: int = SESSION_TTL):
    return await _redis_cmd("SET", key, json.dumps(value), "EX", ex)

async def redis_get(key: str) -> dict | None:
    raw = await _redis_cmd("GET", key)
    if raw is None: return None
    try: return json.loads(raw)
    except: return None

async def redis_del(key: str):
    await _redis_cmd("DEL", key)

def _gs_key(room_code: str) -> str:
    return f"mafia:gs:{room_code}"


# ================================================================
# 게임 상태 로드/저장
# ================================================================
async def load_gs(room_code: str) -> dict:
    gs = await redis_get(_gs_key(room_code))
    return gs or {}

async def save_gs(room_code: str, gs: dict):
    await redis_set(_gs_key(room_code), gs)


# ================================================================
# 역할 계산
# ================================================================
def calc_roles(total: int) -> dict:
    c = {k: 0 for k in ["mafia","doctor","police","citizen","mayor",
                         "doktor_gil","seer","lawyer","anchor","gamedev"]}
    if total <= 4:
        c["mafia"] = 1; c["police"] = 1; c["citizen"] = total - 2
    elif total <= 6:
        c["mafia"] = 1; c["doctor"] = 1; c["police"] = 1
        c["seer"] = 1; c["citizen"] = total - 4
    elif total <= 8:
        c["mafia"] = 2; c["doctor"] = 1; c["police"] = 1
        c["seer"] = 1; c["mayor"] = 1; c["citizen"] = total - 6
    elif total == 9:
        c["mafia"] = 2; c["doctor"] = 1; c["police"] = 1
        c["seer"] = 1; c["mayor"] = 1; c["anchor"] = 1; c["citizen"] = total - 7
    elif total == 10:
        c["mafia"] = 2; c["doctor"] = 1; c["police"] = 1; c["seer"] = 1
        c["mayor"] = 1; c["lawyer"] = 1; c["doktor_gil"] = 1
        c["anchor"] = 1; c["citizen"] = total - 9
    elif total == 11:
        c["mafia"] = 2; c["doctor"] = 1; c["police"] = 1; c["seer"] = 1
        c["mayor"] = 1; c["lawyer"] = 1; c["doktor_gil"] = 1
        c["anchor"] = 1; c["gamedev"] = 1; c["citizen"] = total - 10
    else:
        c["mafia"] = 3; c["doctor"] = 1; c["police"] = 1; c["seer"] = 1
        c["mayor"] = 1; c["lawyer"] = 1; c["doktor_gil"] = 1
        c["anchor"] = 1; c["gamedev"] = 1; c["citizen"] = total - 11
    return c


# ================================================================
# 브로드캐스트
# ================================================================
async def broadcast(room_code: str, msg: dict, exclude: str = None):
    if room_code not in sessions: return
    data = json.dumps(msg, ensure_ascii=False)
    async def _send(uid, p):
        if p.get("isAI"): return
        try: await p["ws"].send_text(data)
        except: pass
    await asyncio.gather(*[
        _send(uid, p)
        for uid, p in list(sessions[room_code]["players"].items())
        if uid != exclude
    ], return_exceptions=True)

async def send_to(room_code: str, uid: str, msg: dict):
    p = sessions.get(room_code, {}).get("players", {}).get(uid)
    if not p or p.get("isAI"): return
    try: await p["ws"].send_text(json.dumps(msg, ensure_ascii=False))
    except: pass


# ================================================================
# 세션 초기화
# ================================================================
async def init_session(room_code: str, room_data: dict) -> dict:
    pdata    = room_data.get("players", {})
    host_uid = room_data.get("hostId", "")
    uids     = list(pdata.keys())
    total    = len(uids)

    # 역할 배정
    role_counts = calc_roles(total)
    role_list   = []
    for role, cnt in role_counts.items():
        role_list.extend([role] * cnt)
    shuffled_uids = uids[:]
    random.shuffle(shuffled_uids)
    random.shuffle(role_list)
    roles = {uid: role_list[i] for i, uid in enumerate(shuffled_uids)}

    gs = {
        "phase": "meet",
        "day":   1,
        "roles": roles,
        "alive": {uid: True for uid in uids},
        "host_uid": host_uid,
        "players": {
            uid: {
                "nickname": pdata[uid].get("nickname", "?"),
                "tag":      pdata[uid].get("tag", "0000"),
                "isAI":     pdata[uid].get("isAI", False),
                "isHost":   pdata[uid].get("isHost", False),
            } for uid in uids
        },
        # 밤 행동 (매 밤 초기화)
        "night_votes":        {},
        "doctor_protect":     {},
        "police_investigate": {},
        "doktor_kill":        {},
        "seer_divine":        {},
        "lawyer_block":       {},
        "anchor_target":      {},
        "gamedev_teach":      {},
        # 낮 투표
        "day_votes": {},
        # 상태
        "tie_pool":                [],
        "consecutive_ties":        0,
        "anchor_translate_target": "",
        "morning_result":          {},
    }

    await save_gs(room_code, gs)

    sessions[room_code] = {
        "players":    {},
        "phase_task": None,
        "status":     "waiting",
    }

    print(f"[Init] {room_code} | {total}명 | {role_counts}", flush=True)
    return gs


# ================================================================
# 밤 행동 처리
# ================================================================
async def process_night(room_code: str, gs: dict) -> dict:
    roles      = gs["roles"]
    alive      = gs["alive"]
    alive_list = [u for u, a in alive.items() if a]

    # 변호사 봉쇄
    lawyer_blocked = list(gs["lawyer_block"].values())

    # 게임개발자 처리
    gd_blocked   = []
    gd_killed    = ""
    gd_csharp_t  = ""
    gd_results   = {}
    for dev_id, data in gs["gamedev_teach"].items():
        if dev_id in lawyer_blocked: continue
        if roles.get(dev_id) != "gamedev": continue
        tgt  = data.get("target", "")
        lang = data.get("lang", "python")
        if not tgt: continue
        gd_results[dev_id] = {"target": tgt, "lang": lang}
        if lang == "python":
            if tgt not in gd_blocked: gd_blocked.append(tgt)
        elif lang == "cpp":
            gd_killed = tgt
            if tgt not in gd_blocked: gd_blocked.append(tgt)
        elif lang == "csharp":
            gd_csharp_t = tgt
            if tgt in alive_list:
                if tgt not in gd_blocked: gd_blocked.append(tgt)

    # 마피아 킬
    mafia_votes = {
        v: t for v, t in gs["night_votes"].items()
        if v not in lawyer_blocked and v not in gd_blocked
        and roles.get(v) == "mafia" and alive.get(v) and alive.get(t)
    }
    mafia_target = ""
    if mafia_votes:
        count = {}
        for t in mafia_votes.values():
            count[t] = count.get(t, 0) + 1
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        mafia_target = random.choice(cands)

    # 의사 보호
    protected = []
    for pid, tgt in gs["doctor_protect"].items():
        if pid in lawyer_blocked or pid in gd_blocked: continue
        if tgt not in protected: protected.append(tgt)

    # 닥터길유
    doktor_target = ""
    for pid, tgt in gs["doktor_kill"].items():
        if pid in lawyer_blocked or pid in gd_blocked: continue
        if roles.get(pid) == "doktor_gil":
            doktor_target = tgt; break

    # 점쟁이
    seer_results = {}
    for seer_id, tgt in gs["seer_divine"].items():
        if seer_id in lawyer_blocked or seer_id in gd_blocked: continue
        if roles.get(seer_id) != "seer": continue
        real = roles.get(tgt, "citizen")
        shown = real if random.random() < 0.7 else (
            random.choice(["citizen","police","doctor","mayor"]) if real == "mafia" else "mafia"
        )
        seer_results[seer_id] = {"target": tgt, "shown_role": shown}

    # 스토커
    police_results = {}
    for cop_id, tgt in gs["police_investigate"].items():
        if cop_id in lawyer_blocked or cop_id in gd_blocked: continue
        if roles.get(cop_id) != "police": continue
        police_results[cop_id] = {
            "target": tgt,
            "result": "mafia" if roles.get(tgt) == "mafia" else "citizen"
        }

    # 앵커
    anchor_tgt = ""
    for pid, tgt in gs["anchor_target"].items():
        if pid in lawyer_blocked or pid in gd_blocked: continue
        if roles.get(pid) == "anchor" and alive.get(tgt):
            anchor_tgt = tgt; break

    # 사망 처리
    actual_elim = ""
    if mafia_target and mafia_target not in protected:
        alive[mafia_target] = False; actual_elim = mafia_target

    actual_doktor = ""
    if doktor_target and doktor_target != actual_elim:
        alive[doktor_target] = False; actual_doktor = doktor_target

    actual_gd_dead = ""
    if gd_killed and gd_killed not in (actual_elim, actual_doktor):
        alive[gd_killed] = False; actual_gd_dead = gd_killed

    # C# 은폐
    show_elim = actual_elim
    if gd_csharp_t:
        if gd_csharp_t not in alive_list or gd_csharp_t == actual_elim:
            show_elim = ""

    mr = {
        "round":                 gs["day"],
        "eliminated":            show_elim,
        "eliminated_real":       actual_elim,
        "protected":             mafia_target != "" and actual_elim == "",
        "doktor_killed":         actual_doktor,
        "gamedev_killed":        actual_gd_dead,
        "lawyer_blocked":        lawyer_blocked,
        "gamedev_blocked":       gd_blocked,
        "gamedev_teach_results": gd_results,
        "gamedev_csharp_target": gd_csharp_t,
        "seer_results":          seer_results,
        "police_results":        police_results,
        "anchor_target":         anchor_tgt,
    }

    gs["alive"]                    = alive
    gs["morning_result"]           = mr
    gs["anchor_translate_target"]  = anchor_tgt
    # 밤 행동 초기화
    for k in ["night_votes","doctor_protect","police_investigate",
              "doktor_kill","seer_divine","lawyer_block","anchor_target","gamedev_teach"]:
        gs[k] = {}

    return mr


# ================================================================
# 낮 투표 처리
# ================================================================
async def process_votes(room_code: str, gs: dict) -> str:
    roles      = gs["roles"]
    alive_list = [u for u, a in gs["alive"].items() if a]
    votes      = gs["day_votes"]

    count: dict = {}
    for voter, tgt in votes.items():
        if not gs["alive"].get(tgt): continue
        w = 2 if roles.get(voter) == "mayor" else 1
        count[tgt] = count.get(tgt, 0) + w

    eliminated = ""
    if count:
        mx    = max(count.values())
        cands = [t for t, c in count.items() if c == mx]
        if len(cands) == 1:
            eliminated = cands[0]
            gs["consecutive_ties"] = 0; gs["tie_pool"] = []
        else:
            for c in cands:
                if c not in gs["tie_pool"]: gs["tie_pool"].append(c)
            gs["consecutive_ties"] += 1
            valid = [c for c in gs["tie_pool"] if c in alive_list]
            if valid:
                eliminated = random.choice(valid)
                gs["consecutive_ties"] = 0; gs["tie_pool"] = []
    else:
        if alive_list: eliminated = random.choice(alive_list)

    if eliminated:
        gs["alive"][eliminated] = False
    gs["day_votes"] = {}
    return eliminated


# ================================================================
# 승리 체크
# ================================================================
def check_win(gs: dict) -> str | None:
    roles = gs["roles"]
    alive = gs["alive"]
    m = sum(1 for u, a in alive.items() if a and roles.get(u) == "mafia")
    c = sum(1 for u, a in alive.items() if a and roles.get(u) != "mafia")
    if m == 0:   return "citizen"
    if m >= c:   return "mafia"
    return None


# ================================================================
# 페이즈 루프 (권위형 타이머)
# ================================================================
async def phase_loop(room_code: str):
    print(f"[Phase] {room_code} 루프 시작", flush=True)
    try:
        while room_code in sessions and sessions[room_code]["status"] == "playing":
            gs    = await load_gs(room_code)
            phase = gs.get("phase", "meet")
            wait  = PHASE_TIMES.get(phase, 10)

            # 페이즈 시작 알림
            await broadcast(room_code, {
                "t":     "phase",
                "phase": phase,
                "day":   gs.get("day", 1),
                "time":  wait,
            })

            # 1초 틱
            for rem in range(wait, 0, -1):
                if room_code not in sessions: return
                await asyncio.sleep(1)
                if rem % 5 == 0 or rem <= 5:
                    await broadcast(room_code, {"t": "tick", "time": rem - 1})

            if room_code not in sessions: return
            gs = await load_gs(room_code)

            # ── 페이즈 전환 ──────────────────────────────────
            if phase == "meet":
                gs["phase"] = "night"
                await save_gs(room_code, gs)

            elif phase == "night":
                mr = await process_night(room_code, gs)
                gs["phase"] = "morning"
                await save_gs(room_code, gs)

                # morning 브로드캐스트 + 슬립 (별도 루프 반복 없이 인라인)
                await broadcast(room_code, {
                    "t":      "phase",
                    "phase":  "morning",
                    "day":    gs["day"],
                    "time":   PHASE_TIMES["morning"],
                    "result": mr,
                })
                for rem in range(PHASE_TIMES["morning"], 0, -1):
                    if room_code not in sessions: return
                    await asyncio.sleep(1)
                    if rem % 5 == 0 or rem <= 5:
                        await broadcast(room_code, {"t": "tick", "time": rem - 1})

                gs = await load_gs(room_code)
                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "elimination")
                    return
                gs["phase"] = "day"
                await save_gs(room_code, gs)

            elif phase == "day":
                gs["phase"] = "vote"
                await save_gs(room_code, gs)

            elif phase == "vote":
                elim = await process_votes(room_code, gs)
                await broadcast(room_code, {
                    "t":          "vote_result",
                    "eliminated": elim,
                    "alive":      gs["alive"],
                    "tie_pool":   gs["tie_pool"],
                })
                winner = check_win(gs)
                if winner or gs["day"] >= MAX_ROUNDS:
                    await end_game(room_code, gs, winner or "citizen", "vote")
                    return
                gs["day"]  += 1
                gs["phase"] = "night"
                await save_gs(room_code, gs)

    except asyncio.CancelledError:
        print(f"[Phase] {room_code} 취소", flush=True)
    except Exception as e:
        print(f"[Phase] {room_code} 오류: {e}", flush=True)


async def end_game(room_code: str, gs: dict, winner: str, reason: str):
    gs["phase"]  = "result"
    gs["winner"] = winner
    await save_gs(room_code, gs)
    await broadcast(room_code, {
        "t":      "game_result",
        "winner": winner,
        "reason": reason,
        "roles":  gs["roles"],
        "alive":  gs["alive"],
        "day":    gs["day"],
    })
    if room_code in sessions:
        sessions[room_code]["status"] = "ended"
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "ended"})
    print(f"[End] {room_code} winner={winner}", flush=True)


# ================================================================
# 유틸
# ================================================================
def _calc_gd_blocked(gs: dict) -> list:
    blocked = []
    for _, data in gs.get("gamedev_teach", {}).items():
        tgt = data.get("target", "")
        if tgt and tgt not in blocked:
            blocked.append(tgt)
    return blocked


# ================================================================
# lifespan + app 선언  ← 핵심: 라우터보다 반드시 먼저!
# ================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    if _USE_HTTPX:
        _http_client = httpx.AsyncClient(timeout=5.0)
    print("[Startup] 마피아 서버 준비 완료", flush=True)
    yield
    if _http_client:
        await _http_client.aclose()

app = FastAPI(lifespan=lifespan)  # ← 여기서 app 생성, 이후 데코레이터 사용 가능


# ================================================================
# HTTP: 게임 시작
# ================================================================
@app.post("/room/start")
async def http_room_start(request: Request):
    """
    RoomWaiting.gd 호스트가 게임 시작 버튼 누를 때 호출.
    body: { room_code, host_uid, room_data? }
    """
    try: body = await request.json()
    except: return JSONResponse({"ok": False, "error": "invalid json"}, 400)

    room_code = body.get("room_code", "").upper().strip()
    host_uid  = body.get("host_uid", "")
    room_data = body.get("room_data", {})

    if not room_code:
        return JSONResponse({"ok": False, "error": "room_code required"}, 400)

    if room_code in sessions and sessions[room_code]["status"] == "playing":
        return JSONResponse({"ok": True, "already": True})

    if not room_data:
        room_data = await rtdb_get(f"rooms/{room_code}")
        if not room_data:
            return JSONResponse({"ok": False, "error": "room not found"}, 404)

    if room_data.get("hostId", "") != host_uid:
        return JSONResponse({"ok": False, "error": "not host"}, 403)

    await init_session(room_code, room_data)
    await rtdb_patch(f"rooms/{room_code}", {"gameStatus": "playing"})

    print(f"[HTTP] 시작 room={room_code} host={host_uid}", flush=True)
    return JSONResponse({"ok": True, "room_code": room_code})


@app.get("/room/{room_code}/state")
async def http_room_state(room_code: str):
    gs = await load_gs(room_code.upper())
    if not gs:
        return JSONResponse({"ok": False, "exists": False})
    return JSONResponse({
        "ok":    True,
        "phase": gs.get("phase"),
        "day":   gs.get("day", 1),
        "alive": gs.get("alive", {}),
    })


# ================================================================
# WebSocket /ws/mafia
# ================================================================
@app.websocket("/ws/mafia")
async def ws_mafia(ws: WebSocket):
    await ws.accept()
    uid       = None
    room_code = None

    try:
        while True:
            raw = await ws.receive_text()
            try: msg = json.loads(raw)
            except: continue
            t = msg.get("t", "")

            # ── 참가 ─────────────────────────────────────────
            if t == "join":
                uid       = msg.get("uid", "")
                room_code = msg.get("room_code", "").upper().strip()

                if room_code not in sessions:
                    await ws.send_text(json.dumps({"t": "error", "r": "no_session"}))
                    continue

                gs = await load_gs(room_code)
                if uid not in gs.get("players", {}):
                    await ws.send_text(json.dumps({"t": "error", "r": "not_in_room"}))
                    continue

                # WS 등록
                pinfo = gs["players"][uid]
                sessions[room_code]["players"][uid] = {
                    "ws":       ws,
                    "nickname": pinfo["nickname"],
                    "tag":      pinfo["tag"],
                    "isHost":   pinfo.get("isHost", False),
                    "isAI":     pinfo.get("isAI", False),
                }

                my_role = gs["roles"].get(uid, "citizen")

                # 현재 상태 전송 (재접속 포함)
                await ws.send_text(json.dumps({
                    "t":         "joined",
                    "uid":       uid,
                    "role":      my_role,
                    "phase":     gs["phase"],
                    "day":       gs["day"],
                    "alive":     gs["alive"],
                    "players":   gs["players"],
                    "tie_pool":  gs["tie_pool"],
                    "mafia_team": {
                        pid: r for pid, r in gs["roles"].items() if r == "mafia"
                    } if my_role in ("mafia", "anchor") else {},
                }))

                # 전원 접속 확인 → 페이즈 루프 시작
                s           = sessions[room_code]
                real_uids   = [u for u, p in gs["players"].items() if not p.get("isAI")]
                connected   = [u for u, p in s["players"].items() if not p.get("isAI")]
                if set(real_uids) == set(connected) and s["status"] == "waiting":
                    s["status"] = "playing"
                    if s.get("phase_task"): s["phase_task"].cancel()
                    s["phase_task"] = asyncio.create_task(phase_loop(room_code))
                    print(f"[WS] {room_code} 전원 접속 → 루프 시작", flush=True)

                await broadcast(room_code, {
                    "t":  "player_joined",
                    "uid": uid,
                    "cn": len(connected),
                    "ex": len(real_uids),
                }, exclude=uid)

            # ── 밤 행동 ──────────────────────────────────────
            elif t == "night_action":
                if not (uid and room_code and room_code in sessions): continue
                gs     = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                if gs["phase"] != "night": continue

                my_role = gs["roles"].get(uid, "")

                # 봉쇄 체크
                if uid in gs["lawyer_block"].values(): continue
                gd_blocked = _calc_gd_blocked(gs)
                if uid in gd_blocked: continue

                action = msg.get("action", "")
                target = msg.get("target", "")

                field_map = {
                    "mafia_kill":         ("night_votes",        "mafia"),
                    "doctor_protect":     ("doctor_protect",     "doctor"),
                    "police_investigate": ("police_investigate", "police"),
                    "doktor_kill":        ("doktor_kill",        "doktor_gil"),
                    "seer_divine":        ("seer_divine",        "seer"),
                    "lawyer_block":       ("lawyer_block",       "lawyer"),
                    "anchor_target":      ("anchor_target",      "anchor"),
                }
                if action in field_map:
                    field, req_role = field_map[action]
                    if my_role == req_role:
                        gs[field][uid] = target
                        await save_gs(room_code, gs)
                        await ws.send_text(json.dumps({"t": "action_ok", "action": action}))

                elif action == "gamedev_teach":
                    if my_role == "gamedev":
                        gs["gamedev_teach"][uid] = {
                            "target": target,
                            "lang":   msg.get("lang", "python")
                        }
                        await save_gs(room_code, gs)
                        await ws.send_text(json.dumps({
                            "t": "action_ok", "action": "gamedev_teach",
                            "lang": msg.get("lang", "python")
                        }))

            # ── 낮 투표 ──────────────────────────────────────
            elif t == "day_vote":
                if not (uid and room_code and room_code in sessions): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                if gs["phase"] != "vote": continue
                gs["day_votes"][uid] = msg.get("target", "")
                await save_gs(room_code, gs)
                await ws.send_text(json.dumps({"t": "vote_ok"}))

            # ── 채팅 ─────────────────────────────────────────
            elif t == "chat":
                if not (uid and room_code and room_code in sessions): continue
                gs      = await load_gs(room_code)
                channel = msg.get("channel", "general")
                text    = msg.get("text", "").strip()
                if not text: continue

                # 밤 일반채팅 차단
                if gs["phase"] == "night" and channel == "general": continue

                my_role = gs["roles"].get(uid, "")
                if channel == "mafia" and my_role not in ("mafia", "anchor"): continue

                # 앵커 강제 번역기
                if channel == "general" and uid == gs.get("anchor_translate_target", ""):
                    suffixes = [
                        " (사실 난 마피아야 쿳소!)",
                        " (오마에와 모우 신데이루)",
                        " (I am the Mafia, actually)",
                        " (브레드 킬러가 나야...사실...)",
                        " [강제번역: 나는 마피아입니다]",
                    ]
                    text += random.choice(suffixes)

                pinfo    = gs["players"].get(uid, {})
                chat_msg = {
                    "t":        "chat",
                    "channel":  channel,
                    "uid":      uid,
                    "nickname": pinfo.get("nickname", "?"),
                    "tag":      pinfo.get("tag", "0000"),
                    "text":     text,
                    "ts":       int(time.time() * 1000),
                }
                if channel == "general":
                    await broadcast(room_code, chat_msg)
                else:
                    for pid, r in gs["roles"].items():
                        if r in ("mafia", "anchor"):
                            await send_to(room_code, pid, chat_msg)

            # ── 감정 ─────────────────────────────────────────
            elif t == "emotion":
                if not (uid and room_code): continue
                gs = await load_gs(room_code)
                if not gs["alive"].get(uid): continue
                await broadcast(room_code, {
                    "t":       "emotion",
                    "uid":     uid,
                    "emotion": msg.get("emotion", ""),
                }, exclude=uid)

            # ── 핑 ───────────────────────────────────────────
            elif t == "ping":
                await ws.send_text(json.dumps({"t": "pong"}))

            # ── 나가기 ───────────────────────────────────────
            elif t == "leave":
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS 오류] uid={uid}: {e}", flush=True)
    finally:
        if uid and room_code and room_code in sessions:
            sessions[room_code]["players"].pop(uid, None)
            await broadcast(room_code, {"t": "player_left", "uid": uid})
        print(f"[WS] 해제: {uid}", flush=True)


# ================================================================
# 헬스체크
# ================================================================
@app.get("/")
async def health():
    return {
        "status":   "ok",
        "sessions": len(sessions),
        "rooms":    list(sessions.keys()),
        "httpx":    _USE_HTTPX,
        "redis":    bool(REDIS_URL),
    }

@app.head("/")
async def health_head():
    return Response(status_code=200)


if __name__ == "__main__":
    for var, name in [
        (RTDB_SECRET, "RTDB_SECRET"),
        (REDIS_URL,   "REDIS_URL"),
        (REDIS_TOKEN, "REDIS_TOKEN"),
    ]:
        if not var: print(f"[경고] {name} 없음!", flush=True)
    print(f"[Server] 포트 {WS_PORT}", flush=True)
    uvicorn.run(
        app, host="0.0.0.0", port=WS_PORT,
        ws_ping_interval=20, ws_ping_timeout=30,
    )
