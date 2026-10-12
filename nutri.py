#!/usr/bin/env python3
"""Deterministic nutrition checker for the nutrition-guardian skill.

The LLM never does nutrition math. It calls this script and turns the JSON
result into a short, friendly reply.

Usage:
  nutri.py lookup "<text>"                     find food ids matching text
  nutri.py check  <id>[:servings] ...          verdict vs today's remaining budget
  nutri.py log    <id>[:servings] ... [--meal breakfast|lunch|dinner|snack]
  nutri.py log-custom --name N --carbs C --protein P --potassium K \
                      --phosphorus PH --sodium NA [--kcal KC] [--meal M]
  nutri.py check-custom --name N --carbs C ... (same fields as log-custom)
  nutri.py today                               totals vs targets + remaining
  nutri.py weight <kg> [--date YYYY-MM-DD] [--confirm]   one per day; latest wins
  nutri.py activity [--steps N] [--minutes M] [--resting-hr H] [--date D] [--source S]
  nutri.py trend                               weight trend + 7-day activity + alerts
  nutri.py undo                                remove the last logged entry

Env:
  NUTRI_PROFILE   path to profile.json (default: ../references/profile.json)
  NUTRI_DATA_DIR  where the daily log lives (default: ~/.hermes/nutrition)
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REF = HERE.parent / "references"
def _find(name, env):
    """Env override, then ../references/<name>, then next to this script (flat upload)."""
    if os.environ.get(env):
        return Path(os.environ[env])
    for cand in (REF / name, HERE / name):
        if cand.exists():
            return cand
    return REF / name


PROFILE = _find("profile.json", "NUTRI_PROFILE")
FOODS = _find("foods.json", "NUTRI_FOODS")
DATA_DIR = Path(os.environ.get("NUTRI_DATA_DIR", Path.home() / ".hermes" / "nutrition"))

NUTRIENTS = ["kcal", "carbs_g", "protein_g", "potassium_mg", "phosphorus_mg", "sodium_mg"]
DAILY_LIMITS = ["potassium_mg", "phosphorus_mg", "sodium_mg"]
LABEL = {"carbs_g": "carbs", "protein_g": "protein", "potassium_mg": "potassium",
         "phosphorus_mg": "phosphorus", "sodium_mg": "sodium", "kcal": "calories"}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def foods_by_id():
    return {f["id"]: f for f in load(FOODS)["foods"]}


def today_str():
    return dt.datetime.now().astimezone().date().isoformat()


def log_path(day=None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{day or today_str()}.json"


def read_log():
    p = log_path()
    return load(p) if p.exists() else []


def write_log(entries):
    with open(log_path(), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)


def totals(entries):
    t = {n: 0.0 for n in NUTRIENTS}
    for e in entries:
        for n in NUTRIENTS:
            t[n] += e["nutrients"].get(n, 0)
    return {n: round(v, 1) for n, v in t.items()}


def parse_items(specs, db):
    items = []
    for spec in specs:
        fid, _, s = spec.partition(":")
        if fid not in db:
            sys.exit(json.dumps({"error": f"unknown food id '{fid}'. Run lookup first, or use log-custom with estimated values."}))
        servings = float(s) if s else 1.0
        f = db[fid]
        items.append({
            "id": fid, "name": f["name"], "servings": servings, "serving": f["serving"],
            "nutrients": {n: round(f.get(n, 0) * servings, 1) for n in NUTRIENTS},
            "avoid": f.get("avoid"), "swap": f.get("swap"), "note": f.get("note"),
        })
    return items


def evaluate(items, prof, so_far):
    tg = prof["targets"]
    add = totals(items)
    after = {n: so_far[n] + add[n] for n in NUTRIENTS}
    level, reasons = 0, []  # 0 green, 1 yellow, 2 red

    def bump(lv, msg):
        nonlocal level
        level = max(level, lv)
        reasons.append(msg)

    for it in items:
        if it["avoid"]:
            bump(2, f"AVOID {it['name']}: {it['avoid']}")

    cap = tg["carbs_g_per_meal"]["max"]
    if add["carbs_g"] > cap:
        bump(2, f"carbs {add['carbs_g']:.0f} g is over the per-meal limit of {cap} g")
    elif add["carbs_g"] > 0.75 * cap:
        bump(1, f"carbs {add['carbs_g']:.0f} g is close to the per-meal limit of {cap} g")

    for n in DAILY_LIMITS:
        lim = tg[n]["max"]
        left = lim - so_far[n]
        if after[n] > lim:
            bump(2, f"{LABEL[n]} would reach {after[n]:.0f} of {lim} mg today (only {max(left,0):.0f} mg left)")
        elif after[n] > 0.85 * lim or add[n] > 0.3 * lim:
            bump(1, f"{LABEL[n]} {add[n]:.0f} mg uses a big share of today's {lim} mg (would be at {after[n]:.0f})")

    pmax = tg["protein_g"]["max"]
    if after["protein_g"] > pmax * 1.1:
        bump(2, f"protein would reach {after['protein_g']:.0f} g, over today's max of {pmax} g (kidneys)")
    elif after["protein_g"] > pmax:
        bump(1, f"protein would reach {after['protein_g']:.0f} g, just over today's max of {pmax} g")

    return level, reasons, add, after


def best_portion(items, prof, so_far):
    """Largest servings multiplier (0.25 steps, up to 1.0) that stays green."""
    if any(it["avoid"] for it in items):
        return 0.0
    for k in range(4, 0, -1):
        scale = k / 4
        scaled = [dict(it, nutrients={n: v * scale for n, v in it["nutrients"].items()}) for it in items]
        lv, _, _, _ = evaluate(scaled, prof, so_far)
        if lv == 0:
            return scale
    return 0.0


def cmd_lookup(q):
    q = q.lower().strip()
    hits = []
    for f in load(FOODS)["foods"]:
        names = [f["id"], f["name"].lower()] + [a.lower() for a in f.get("aliases", [])]
        if any(q in n or n in q for n in names):
            hits.append({"id": f["id"], "name": f["name"], "serving": f["serving"]})
    print(json.dumps({"query": q, "matches": hits, "hint": None if hits else
                      "Not in the database. Estimate values and use log-custom, and say the numbers are estimates."},
                     ensure_ascii=False, indent=1))


def custom_item(a):
    return {"id": "custom", "name": a.name + " (estimated)", "servings": 1, "serving": "as described",
            "nutrients": {"kcal": a.kcal, "carbs_g": a.carbs, "protein_g": a.protein,
                          "potassium_mg": a.potassium, "phosphorus_mg": a.phosphorus, "sodium_mg": a.sodium},
            "avoid": None, "swap": None, "note": "Values are the agent's estimate, not database values."}


def cmd_check(specs, extra=None):
    prof, db = load(PROFILE), foods_by_id()
    items = parse_items(specs, db) + ([extra] if extra else [])
    so_far = totals(read_log())
    level, reasons, add, after = evaluate(items, prof, so_far)
    portion = best_portion(items, prof, so_far) if level > 0 else 1.0
    out = {
        "verdict": ["green", "yellow", "red"][level],
        "reasons": reasons or ["fits today's targets"],
        "suggested_portion": None if level == 0 else
            ("do not eat" if portion == 0 else f"{portion:g} of what was asked"),
        "swaps": [it["swap"] for it in items if it["swap"]],
        "notes": [it["note"] for it in items if it["note"]],
        "this_food": add, "today_before": so_far, "today_after": after,
        "profile_status": prof.get("_status", ""),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


def _append(entries, new, meal):
    stamp = dt.datetime.now().astimezone().isoformat(timespec="minutes")
    for e in new:
        entries.append({"time": stamp, "meal": meal, **e})
    write_log(entries)


def cmd_log(specs, meal):
    db = foods_by_id()
    items = parse_items(specs, db)
    entries = read_log()
    _append(entries, [{"id": i["id"], "name": i["name"], "servings": i["servings"],
                       "nutrients": i["nutrients"], "estimated": False} for i in items], meal)
    cmd_today(logged=[i["name"] for i in items])


def cmd_log_custom(a):
    n = {"kcal": a.kcal, "carbs_g": a.carbs, "protein_g": a.protein, "potassium_mg": a.potassium,
         "phosphorus_mg": a.phosphorus, "sodium_mg": a.sodium}
    entries = read_log()
    _append(entries, [{"id": "custom", "name": a.name, "servings": 1, "nutrients": n, "estimated": True}], a.meal)
    cmd_today(logged=[a.name + " (estimated)"])


def cmd_today(logged=None):
    prof = load(PROFILE)
    tg = prof["targets"]
    entries = read_log()
    t = totals(entries)
    flags = []
    for n in DAILY_LIMITS:
        if t[n] > tg[n]["max"]:
            flags.append(f"{LABEL[n]} over limit ({t[n]:.0f}/{tg[n]['max']} mg)")
    if t["protein_g"] > tg["protein_g"]["max"]:
        flags.append(f"protein over max ({t['protein_g']:.0f}/{tg['protein_g']['max']} g)")
    hour = dt.datetime.now().astimezone().hour
    if hour >= 18 and t["protein_g"] < tg["protein_g"]["min"]:
        flags.append(f"protein low ({t['protein_g']:.0f}/{tg['protein_g']['min']} g min) — muscle needs it")
    if hour >= 18 and t["kcal"] < tg["kcal"]["min"] * 0.7:
        flags.append(f"ate little today ({t['kcal']:.0f} kcal) — check appetite / side effects")
    meals = sorted({e["meal"] for e in entries if e.get("meal")})
    out = {
        "date": today_str(), "logged_now": logged, "totals": t,
        "remaining": {
            "protein_g": f"{max(tg['protein_g']['min'] - t['protein_g'], 0):.0f}-{max(tg['protein_g']['max'] - t['protein_g'], 0):.0f}",
            **{n: max(round(tg[n]["max"] - t[n]), 0) for n in DAILY_LIMITS},
        },
        "carbs_per_meal_max": tg["carbs_g_per_meal"]["max"],
        "meals_logged": meals, "entries": len(entries), "flags": flags,
        "body": trend_data(),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


def body_path():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "body.json"


def read_body():
    p = body_path()
    return load(p) if p.exists() else {"weight": {}, "activity": {}}


def write_body(b):
    with open(body_path(), "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=1)


def weight_on_or_before(weights, day):
    keys = sorted(k for k in weights if k <= day)
    return (keys[-1], weights[keys[-1]]) if keys else (None, None)


def trend_data():
    prof, b = load(PROFILE), read_body()
    cfg = prof.get("body", {})
    w = b["weight"]
    today = dt.date.fromisoformat(today_str())
    alerts, out = [], {}
    if w:
        last_day = max(w)
        last = w[last_day]
        out["latest_weight"] = {"date": last_day, "kg": last}
        ld = dt.date.fromisoformat(last_day)
        for days, key in [(1, "1d"), (7, "7d"), (30, "30d")]:
            ref_day, ref = weight_on_or_before(w, (ld - dt.timedelta(days=days)).isoformat())
            if ref is None:
                continue
            gap = (ld - dt.date.fromisoformat(ref_day)).days
            if gap > 2 * days:  # too old to describe this window honestly
                continue
            delta = round(last - ref, 1)
            pct = round(100 * delta / ref, 1)
            out[f"change_{key}"] = {"since": ref_day, "days": gap, "kg": delta, "pct": pct}
            if key in ("7d", "30d") and -pct >= cfg.get(f"weight_loss_alert_pct_{key}", 1e9):
                alerts.append(f"weight down {abs(delta)} kg ({abs(pct)}%) in {gap} days — possible under-eating or muscle loss")
            if key in ("1d", "7d") and delta >= cfg.get(f"weight_gain_alert_kg_{key}", 1e9):
                alerts.append(f"weight up {delta} kg in {gap} day(s) — possible fluid buildup; contact the doctor")
        alerts = list(dict.fromkeys(alerts))
        if (today - ld).days >= 3:
            out["reminder"] = f"no weight logged since {last_day}"
    else:
        out["reminder"] = "no weight logged yet"
    acts = b["activity"]
    week = [(today - dt.timedelta(days=i)).isoformat() for i in range(7)]
    steps = [acts[d]["steps"] for d in week if d in acts and acts[d].get("steps") is not None]
    hrs = [acts[d]["resting_hr"] for d in week if d in acts and acts[d].get("resting_hr") is not None]
    out["activity_7d"] = {
        "days_logged": len([d for d in week if d in acts]),
        "avg_steps": round(sum(steps) / len(steps)) if steps else None,
        "steps_goal": cfg.get("steps_goal"),
        "avg_resting_hr": round(sum(hrs) / len(hrs)) if hrs else None,
        "today": acts.get(today_str()),
    }
    out["alerts"] = alerts
    return out


def cmd_weight(kg, date, confirm=False):
    if not 25 <= kg <= 200:
        sys.exit(json.dumps({"error": f"{kg} kg looks wrong; please re-check (kg, not lb?)"}))
    b = read_body()
    if b["weight"] and not confirm:
        prev_day = max(k for k in b["weight"] if k <= (date or today_str())) if any(
            k <= (date or today_str()) for k in b["weight"]) else max(b["weight"])
        prev = b["weight"][prev_day]
        if abs(kg - prev) / prev > 0.08:
            sys.exit(json.dumps({"needs_confirmation": True,
                "message": f"{kg} kg is far from the last reading ({prev} kg on {prev_day}). "
                           "Please re-check the scale and unit (kg vs lb / 斤). "
                           "If confirmed, rerun with --confirm."}, ensure_ascii=False))
    b["weight"][date or today_str()] = round(kg, 1)
    write_body(b)
    print(json.dumps({"recorded": {"date": date or today_str(), "kg": round(kg, 1)}, **trend_data()},
                     ensure_ascii=False, indent=1))


def cmd_activity(a):
    b = read_body()
    day = a.date or today_str()
    rec = b["activity"].get(day, {})
    for k, v in [("steps", a.steps), ("minutes", a.minutes), ("resting_hr", a.resting_hr)]:
        if v is not None:
            rec[k] = v
    rec["source"] = a.source
    b["activity"][day] = rec
    write_body(b)
    print(json.dumps({"recorded": {"date": day, **rec}, **trend_data()}, ensure_ascii=False, indent=1))


def cmd_undo():
    entries = read_log()
    if not entries:
        print(json.dumps({"undone": None, "message": "nothing logged today"}))
        return
    last = entries.pop()
    write_log(entries)
    print(json.dumps({"undone": last["name"]}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("lookup").add_argument("query")
    sp.add_parser("check").add_argument("items", nargs="+")  # food ids
    lg = sp.add_parser("log")
    lg.add_argument("items", nargs="+")
    lg.add_argument("--meal", default=None)
    lc = sp.add_parser("log-custom")
    lc.add_argument("--name", required=True)
    for k in ["carbs", "protein", "potassium", "phosphorus", "sodium"]:
        lc.add_argument(f"--{k}", type=float, required=True)
    lc.add_argument("--kcal", type=float, default=0)
    lc.add_argument("--meal", default=None)
    cc = sp.add_parser("check-custom")
    cc.add_argument("--name", required=True)
    for k in ["carbs", "protein", "potassium", "phosphorus", "sodium"]:
        cc.add_argument(f"--{k}", type=float, required=True)
    cc.add_argument("--kcal", type=float, default=0)
    sp.add_parser("today")
    sp.add_parser("undo")
    wt = sp.add_parser("weight")
    wt.add_argument("kg", type=float)
    wt.add_argument("--date", default=None)
    wt.add_argument("--confirm", action="store_true")
    ac = sp.add_parser("activity")
    ac.add_argument("--steps", type=int)
    ac.add_argument("--minutes", type=int)
    ac.add_argument("--resting-hr", dest="resting_hr", type=int)
    ac.add_argument("--date", default=None)
    ac.add_argument("--source", default="manual")
    sp.add_parser("trend")
    a = ap.parse_args()
    if a.cmd == "lookup":
        cmd_lookup(a.query)
    elif a.cmd == "check":
        cmd_check(a.items)
    elif a.cmd == "log":
        cmd_log(a.items, a.meal)
    elif a.cmd == "check-custom":
        cmd_check([], custom_item(a))
    elif a.cmd == "log-custom":
        cmd_log_custom(a)
    elif a.cmd == "today":
        cmd_today()
    elif a.cmd == "undo":
        cmd_undo()
    elif a.cmd == "weight":
        cmd_weight(a.kg, a.date, a.confirm)
    elif a.cmd == "activity":
        cmd_activity(a)
    elif a.cmd == "trend":
        print(json.dumps(trend_data(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
