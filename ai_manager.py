"""
ai_manager.py — AI FSM 전담 모듈
server.py에서 import해서 사용
"""

import asyncio, math, random, time

# ── 공유 상수 (server.py의 constants와 동일해야 함)
AI_MOVE_SPEED    = 3.5
AI_ATK_COOLDOWN  = 1.2
RESCUE_WINDOW    = 10.0
KO_REVIVE_TIME   = 20.0

MAP_MIN_X = -16.0; MAP_MAX_X = 16.0
MAP_MIN_Z = -9.0;  MAP_MAX_Z = 9.0

ZONE_POS    = {"x": 0.0, "z": 0.0}
ZONE_RADIUS = 4.0

ZONE_THREAT_RADIUS  = 9.0
ZONE_DEFEND_RADIUS  = 3.2
ENGAGE_RANGE_BONUS  = 1.3
STRAFE_DIST         = 2.5
STRAFE_SWITCH_TICKS = 12
PRESSURE_ADV_DST    = 6.0

WEAPON_STATS = {
    "baguette":  {"damage": 15, "atk_speed": 1.0, "atk_range": 2.2, "cooldown": 1.0},
    "sandwich":  {"damage": 25, "atk_speed": 0.6, "atk_range": 1.5, "cooldown": 1.667},
    "saltbread": {"damage": 10, "atk_speed": 1.8, "atk_range": 1.0, "cooldown": 0.556},
}

# ================================================================
# 수학 유틸
# ================================================================
def dist_2d(a: dict, b: dict) -> float:
    return math.sqrt((a.get("x",0)-b.get("x",0))**2 + (a.get("z",0)-b.get("z",0))**2)

def dir_to(src: dict, dst: dict) -> tuple[float, float]:
    dx = dst.get("x",0) - src.get("x",0)
    dz = dst.get("z",0) - src.get("z",0)
    d  = math.sqrt(dx*dx + dz*dz)
    return (dx/d, dz/d) if d > 0.001 else (0.0, 0.0)

def clamp_pos(x: float, z: float) -> tuple[float, float]:
    return max(MAP_MIN_X, min(MAP_MAX_X, x)), max(MAP_MIN_Z, min(MAP_MAX_Z, z))

def in_zone(pos: dict) -> bool:
    return dist_2d(pos, ZONE_POS) <= ZONE_RADIUS

def count_team_in_zone(players: dict, team: str) -> int:
    return sum(1 for p in players.values()
               if p.get("tm") == team and not p.get("ko") and in_zone(p))

# ================================================================
# 이동 헬퍼
# ================================================================
def _move_straight(p: dict, dst: dict, spd: float):
    dx, dz = dir_to(p, dst)
    if abs(dx) > 0.001 or abs(dz) > 0.001:
        nx, nz = clamp_pos(p["x"] + dx*spd, p["z"] + dz*spd)
        p["x"] = nx; p["z"] = nz
        p["ry"] = math.atan2(dx, dz); p["an"] = "run"
    else:
        p["an"] = "idle"

def _clear_nav(p: dict):
    p["nav_wp"] = None; p["nav_wait"] = 0

def _follow_nav(p: dict, spd: float):
    wp = p.get("nav_wp")
    if wp is None: p["an"] = "idle"; return
    if dist_2d(p, wp) <= 0.8: p["nav_wp"] = None; p["an"] = "idle"; return
    _move_straight(p, wp, spd)

# ================================================================
# 전술 위치 계산
# ================================================================
def zone_cover_pos(ai_team: str, enemy_pos: dict | None = None) -> dict:
    if enemy_pos:
        dx, dz = dir_to(ZONE_POS, enemy_pos)
        side = 1 if random.random() > 0.5 else -1
        px = ZONE_POS["x"] - dx*ZONE_DEFEND_RADIUS*0.7 + (-dz)*side*ZONE_DEFEND_RADIUS*0.6
        pz = ZONE_POS["z"] - dz*ZONE_DEFEND_RADIUS*0.7 + ( dx)*side*ZONE_DEFEND_RADIUS*0.6
    else:
        base = 0.0 if ai_team == "r" else math.pi
        a = base + random.uniform(-0.8, 0.8)
        r = ZONE_DEFEND_RADIUS * random.uniform(0.4, 0.85)
        px = ZONE_POS["x"] + math.cos(a)*r
        pz = ZONE_POS["z"] + math.sin(a)*r
    px, pz = clamp_pos(px, pz)
    return {"x": px, "z": pz}

def strafe_pos(src: dict, enemy_pos: dict, strafe_dir: int) -> dict:
    dx, dz = dir_to(src, enemy_pos)
    wx, wz = clamp_pos(src["x"]+(-dz)*STRAFE_DIST*strafe_dir,
                       src["z"]+( dx)*STRAFE_DIST*strafe_dir)
    return {"x": wx, "z": wz}

def pressure_approach_pos(src: dict, enemy: dict) -> dict:
    dx, dz = dir_to(src, enemy)
    d = dist_2d(src, enemy)
    mx = src["x"] + dx*d*0.5; mz = src["z"] + dz*d*0.5
    side = 1 if src.get("flank_side", 0) >= 0 else -1
    wx, wz = clamp_pos(mx+(-dz)*STRAFE_DIST*side, mz+(dx)*STRAFE_DIST*side)
    return {"x": wx, "z": wz}

# ================================================================
# 교전 FSM
# ================================================================
def _engage(p: dict, enemy_pos: dict, spd: float):
    tick = p.get("tick", 0)
    if tick % STRAFE_SWITCH_TICKS == 0:
        p["strafe_dir"] = 1 if p.get("strafe_dir", 1) <= 0 else -1
    wp = strafe_pos(p, enemy_pos, p.get("strafe_dir", 1))
    dx, dz = dir_to(p, wp)
    if abs(dx) > 0.001 or abs(dz) > 0.001:
        nx, nz = clamp_pos(p["x"]+dx*spd, p["z"]+dz*spd)
        p["x"] = nx; p["z"] = nz; p["an"] = "run"
    fdx, fdz = dir_to(p, enemy_pos)
    if abs(fdx) > 0.001 or abs(fdz) > 0.001:
        p["ry"] = math.atan2(fdx, fdz)

def _engage_in_zone(p: dict, enemy_pos: dict, spd: float):
    _engage(p, enemy_pos, spd)
    if not in_zone(p):
        dx, dz = dir_to(p, ZONE_POS)
        nx, nz = clamp_pos(p["x"]+dx*spd, p["z"]+dz*spd)
        p["x"] = nx; p["z"] = nz

def _idle_patrol(p: dict, ai_team: str, spd: float):
    wp = p.get("nav_wp")
    if wp is None or dist_2d(p, wp) < 0.8:
        p["nav_wp"] = zone_cover_pos(ai_team, None)
    if p.get("nav_wp"):
        _move_straight(p, p["nav_wp"], spd * 0.6)

# ================================================================
# 역할별 이동 FSM
# ================================================================
def _defender_move(p: dict, nearest_enemy, zone_threat_enemy,
                   zone_threat_dist: float, engage_range: float,
                   spd: float, ai_team: str):
    currently_in_zone = in_zone(p)
    enemy_threatening = zone_threat_enemy and zone_threat_dist <= ZONE_THREAT_RADIUS

    if enemy_threatening and nearest_enemy:
        if currently_in_zone:
            _engage_in_zone(p, nearest_enemy[1], spd)
        else:
            wp = p.get("nav_wp")
            if wp is None or not in_zone(wp):
                p["nav_wp"] = zone_cover_pos(ai_team, nearest_enemy[1])
                p["nav_wait"] = 0
            _follow_nav(p, spd)
    else:
        if currently_in_zone:
            wp = p.get("nav_wp")
            if wp is None or dist_2d(p, wp) < 0.8:
                p["nav_wp"] = zone_cover_pos(ai_team, None)
            _follow_nav(p, spd * 0.7)
        else:
            if p.get("nav_wp") is None or not in_zone(p.get("nav_wp", ZONE_POS)):
                p["nav_wp"] = zone_cover_pos(ai_team, None)
            _follow_nav(p, spd)

def _pressure_move(p: dict, nearest_enemy, nearest_dist: float,
                   zone_threat_enemy, engage_range: float,
                   spd: float, ai_team: str, players: dict):
    target_enemy = zone_threat_enemy or nearest_enemy
    if target_enemy is None:
        _idle_patrol(p, ai_team, spd); return
    _, te = target_enemy
    d = dist_2d(p, te)
    if d <= engage_range * 1.5:
        _engage(p, te, spd)
    elif d <= PRESSURE_ADV_DST * 2:
        if p.get("nav_wp") is None or p.get("tick", 0) % 25 == 0:
            p["nav_wp"] = pressure_approach_pos(p, te)
        _follow_nav(p, spd)
    else:
        _clear_nav(p); _move_straight(p, te, spd)

# ================================================================
# AI 메인 이동 결정 (매 틱 호출)
# ================================================================
def ai_move_tick(p: dict, role: str,
                 nearest_enemy, nearest_dist: float,
                 zone_threat_enemy, zone_threat_dist: float,
                 rescuable, engage_range: float,
                 dt: float, ai_team: str, players: dict):
    spd = AI_MOVE_SPEED * dt
    p["tick"] = p.get("tick", 0) + 1

    # 1순위: 구조
    if rescuable:
        _clear_nav(p); _move_straight(p, rescuable[1], spd); return

    # 2순위: 근접 교전
    if nearest_enemy and nearest_dist <= engage_range * 2:
        _engage(p, nearest_enemy[1], spd); return

    # 3순위: 적 없으면 순찰
    if nearest_enemy is None:
        _idle_patrol(p, ai_team, spd); return

    # 역할별 전술
    if role == "defender":
        _defender_move(p, nearest_enemy, zone_threat_enemy,
                       zone_threat_dist, engage_range, spd, ai_team)
    else:
        _pressure_move(p, nearest_enemy, nearest_dist,
                       zone_threat_enemy, engage_range, spd, ai_team, players)

# ================================================================
# AI 틱 루프
# ★ tick_rate 파라미터 추가 — server.py의 AI_TICK_RATE를 받아 sync_loop와 주기 일치
# ================================================================
async def run_ai_loop(mid: str, sessions: dict, broadcast_fn, ko_timer_fn,
                      tick_rate: int = 10):
    """
    tick_rate: server.py에서 주입 (기본 10Hz, sync_loop와 맞춤)
    """
    interval = 1.0 / tick_rate
    print(f"[AI] {mid} 루프 시작 (tick_rate={tick_rate}Hz)", flush=True)

    while mid in sessions and sessions[mid]["status"] == "in_game":
        s       = sessions[mid]
        dt      = interval
        players = s["players"]

        for uid, p in list(players.items()):
            if not uid.startswith("ai_") or p.get("ko", False): continue

            ai_team    = p.get("tm", "r")
            enemy_team = "b" if ai_team == "r" else "r"
            weapon     = s["weapons"].get(uid, {}).get("weapon", "baguette")
            atk_range  = WEAPON_STATS[weapon]["atk_range"]
            damage     = WEAPON_STATS[weapon]["damage"]
            engage_range = atk_range * ENGAGE_RANGE_BONUS

            # ── 유효 타겟 필터 (KO·disconnected 제외)
            enemies = [(tid, tp) for tid, tp in players.items()
                       if tp.get("tm") == enemy_team
                       and not tp.get("ko", False)
                       and not tp.get("disconnected", False)]
            ko_allies = [(tid, tp) for tid, tp in players.items()
                         if tp.get("tm") == ai_team
                         and tp.get("ko", False)
                         and not tp.get("disconnected", False)
                         and tid != uid]

            nearest_enemy     = min(enemies, key=lambda e: dist_2d(p, e[1]), default=None)
            nearest_dist      = dist_2d(p, nearest_enemy[1]) if nearest_enemy else 999.0
            zone_threat_enemy = min(enemies, key=lambda e: dist_2d(e[1], ZONE_POS), default=None)
            zone_threat_dist  = dist_2d(zone_threat_enemy[1], ZONE_POS) if zone_threat_enemy else 999.0

            rescuable = next(
                ((tid, tp) for tid, tp in ko_allies
                 if time.time() - tp.get("ko_time", time.time()) <= RESCUE_WINDOW
                 and dist_2d(p, tp) <= 8.0),
                None
            )

            # Zone 아군 없으면 강제 defender
            if count_team_in_zone(players, ai_team) == 0:
                p["ai_role"] = "defender"

            # ── 쿨다운 감소
            p["atk_cd"] = max(0.0, p.get("atk_cd", 0.0) - dt)

            # ── 범위 내 적 공격
            for tid, tp in enemies:
                if dist_2d(p, tp) <= atk_range and p["atk_cd"] <= 0:
                    p["atk_cd"] = AI_ATK_COOLDOWN
                    tp["hp"] = max(0, tp["hp"] - damage)
                    await broadcast_fn(mid, {
                        "t": "hit", "attacker": uid, "target": tid,
                        "damage": damage, "hp": tp["hp"], "weapon": weapon,
                    })
                    if tp["hp"] <= 0:
                        tp["hp"] = 0; tp["ko"] = True; tp["ko_time"] = time.time()
                        _clear_nav(tp)
                        print(f"[AI-KO] {uid} → {tid}", flush=True)
                        await broadcast_fn(mid, {"t": "ko", "uid": tid})
                        asyncio.create_task(ko_timer_fn(mid, tid))
                    break

            # ── 구조 실행
            if rescuable:
                tid, tp = rescuable
                if dist_2d(p, tp) <= 1.5:
                    _clear_nav(tp)
                    tp["ko"] = False; tp["hp"] = 100; tp["penalty"] = False
                    await broadcast_fn(mid, {"t": "rev", "uid": tid, "hp": 100,
                                             "penalty": False, "auto": False, "rescuer": uid})
                    rescuable = None

            # ── 이동 결정
            ai_move_tick(p, p.get("ai_role", "defender"),
                         nearest_enemy, nearest_dist,
                         zone_threat_enemy, zone_threat_dist,
                         rescuable, engage_range, dt, ai_team, players)

        await asyncio.sleep(interval)

    print(f"[AI] {mid} 루프 종료", flush=True)
