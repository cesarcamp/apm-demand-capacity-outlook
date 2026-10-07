"""
APM (Automated People Mover) PoC - synthetic data + forecast + capacity.

Output: apm/data/model/*.csv  (star schema ready for Power BI, Import mode)

ALL DATA IS SYNTHETIC. Assumptions (also documented in apm/README.md):
  * Loop line, 5 stations: RCC -> T1 -> T2 -> T3 -> ITB -> RCC. Passengers use the shortest direction.
  * Grain: 15-minute interval x station x direction (boardings).
  * Calendar: 49 days from 2026-01-05 (Mon).  Actuals: days 0-41.  Forecast: days 35-48.
      weeks 1-4 train -> week 5 calibration/validation -> week 6 hold-out (has actuals) -> week 7 future.
  * Demand mechanism (what the model must learn):
      arrivals  at terminal T : 28 % ride to RCC, 8 % to another terminal, 15-90 min after landing
      departures from terminal T: 22 % ride RCC -> T, 90-150 min before departure
      + small ambient staff demand
  * Hidden events (the forecast cannot know them):  2026-01-30 pre-holiday surge (+20 % pax),
      2026-02-12 storm (25 % flights cancelled, extra delays).
  * Capacity: trains per 15 min by day-part x boarding capacity per train, sized so the training
      P98 of demand-per-train uses ~95 % of capacity (i.e. ~2 % of normal intervals saturate). Planned maintenance on 2026-02-18 10:00-14:00 (-40 % trains).
"""
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "model"
OUT.mkdir(parents=True, exist_ok=True)

START = pd.Timestamp("2026-01-05")
N_DAYS, SLOTS = 49, 96
T = N_DAYS * SLOTS
LAST_ACTUAL_DAY = 41          # 2026-02-15
TRAIN_END_DAY = 34            # 2026-02-08 (end of week 5)
D_W4, D_W5, D_W6 = 28 * SLOTS, 35 * SLOTS, 42 * SLOTS

STATIONS = [("RCC", "Rental Car Center", "Hub"), ("T1", "Terminal 1", "Terminal"),
            ("T2", "Terminal 2", "Terminal"), ("T3", "Terminal 3", "Terminal"),
            ("ITB", "International Terminal", "Terminal")]
N_ST = len(STATIONS)
TERM_CFG = {1: dict(n=7, seats=(150, 190)), 2: dict(n=5, seats=(140, 180)),
            3: dict(n=8, seats=(150, 190)), 4: dict(n=4, seats=(260, 340))}   # key = station idx
DOW_MULT = {0: 1.00, 1: 0.93, 2: 0.95, 3: 1.00, 4: 1.08, 5: 0.85, 6: 1.05}
EVENTS = {25: dict(name="Pre-holiday surge", pax_mult=1.20, cancel=0.0, extra_delay=0),
          38: dict(name="Storm disruption", pax_mult=1.00, cancel=0.25, extra_delay=25)}
MAINT_DAY, MAINT_SLOTS = 44, range(40, 56)   # 2026-02-18, 10:00-14:00

ARR_BANKS = [("ARR1", 390, 510), ("ARR2", 630, 720), ("ARR3", 810, 900),
             ("ARR4", 1020, 1140), ("ARR5", 1230, 1320)]
DEP_BANKS = [("DEP1", 330, 450), ("DEP2", 570, 660), ("DEP3", 750, 840),
             ("DEP4", 960, 1080), ("DEP5", 1170, 1260)]
K_ARR = np.array([0.05, 0.25, 0.30, 0.20, 0.12, 0.08])      # lags 1..6 intervals after arrival
K_DEP = np.array([0.10, 0.25, 0.30, 0.25, 0.10])            # leads 6..10 intervals before departure


def direction(o, d):
    """1 = clockwise, 2 = counter-clockwise (shortest way round a 5-station loop)."""
    return 1 if (d - o) % N_ST <= (o - d) % N_ST else 2


# --------------------------------------------------------------------------- dimensions
dates = pd.date_range(START, periods=N_DAYS)
dim_date = pd.DataFrame({
    "DateKey": dates.strftime("%Y%m%d").astype(int), "Date": dates,
    "Year": dates.year, "Month": dates.month, "MonthName": dates.strftime("%b"),
    "Day": dates.day, "DayOfWeekNum": dates.dayofweek + 1, "DayName": dates.strftime("%a"),
    "DayType": np.where(dates.dayofweek < 5, "Weekday", "Weekend"),
    "PoC_Week": np.arange(N_DAYS) // 7 + 1,
    "IsActualPeriod": np.arange(N_DAYS) <= LAST_ACTUAL_DAY,
    "IsForecastPeriod": np.arange(N_DAYS) > TRAIN_END_DAY,
    "Event_Name": [EVENTS[i]["name"] if i in EVENTS else
                   ("Planned maintenance" if i == MAINT_DAY else "") for i in range(N_DAYS)]})

slots = np.arange(SLOTS)


def day_part(h):
    return ("Night" if h < 5 else "AM Peak" if h < 10 else "Midday" if h < 15
            else "PM Peak" if h < 20 else "Evening")


dim_time = pd.DataFrame({"Time15Key": slots, "Hour": slots // 4, "Minute": (slots % 4) * 15})
dim_time["Time_Label"] = dim_time["Hour"].astype(str).str.zfill(2) + ":" + dim_time["Minute"].astype(str).str.zfill(2)
dim_time["Day_Part"] = dim_time["Hour"].map(day_part)

dim_station = pd.DataFrame({"StationKey": range(1, N_ST + 1),
                            "Station_Code": [s[0] for s in STATIONS],
                            "Station_Name": [s[1] for s in STATIONS],
                            "Station_Type": [s[2] for s in STATIONS],
                            "Loop_Sequence": range(1, N_ST + 1)})
dim_dir = pd.DataFrame({"DirectionKey": [1, 2], "Direction_Name": ["Clockwise", "Counter-clockwise"]})

banks = [(0, "OFF_ARR", "Arrival off-bank", "Arrival", None, None)]
banks += [(i + 1, c, f"Arrival Bank {i + 1}", "Arrival", s, e) for i, (c, s, e) in enumerate(ARR_BANKS)]
banks += [(10, "OFF_DEP", "Departure off-bank", "Departure", None, None)]
banks += [(11 + i, c, f"Departure Bank {i + 1}", "Departure", s, e) for i, (c, s, e) in enumerate(DEP_BANKS)]
dim_bank = pd.DataFrame(banks, columns=["BankKey", "Bank_Code", "Bank_Name", "Movement", "Start_Min", "End_Min"])
dim_bank["Start_Time"] = dim_bank["Start_Min"].map(lambda m: "" if pd.isna(m) else f"{int(m)//60:02d}:{int(m)%60:02d}")
dim_bank["End_Time"] = dim_bank["End_Min"].map(lambda m: "" if pd.isna(m) else f"{int(m)//60:02d}:{int(m)%60:02d}")
dim_bank = dim_bank.drop(columns=["Start_Min", "End_Min"])

dim_scen = pd.DataFrame({"ScenarioKey": [1, 2, 3], "Scenario_Name": ["Low", "Most-Likely", "High"],
                         "Quantile": [0.10, 0.50, 0.90]})


def bank_of(movement, minute):
    lst, base = (ARR_BANKS, 1) if movement == "A" else (DEP_BANKS, 11)
    for i, (_, s, e) in enumerate(lst):
        if s <= minute < e:
            return base + i
    return 0 if movement == "A" else 10


# --------------------------------------------------------------------------- flights + demand
flights = []
lam = np.zeros((N_ST, 2, T))
ambient = np.where((slots >= 20), 3.0, 0.0)
for day in range(N_DAYS):
    dow = day % 7
    ev = EVENTS.get(day, dict(pax_mult=1.0, cancel=0.0, extra_delay=0))
    plan_lf = min(0.95, 0.85 * DOW_MULT[dow])
    share_noise = float(np.exp(rng.normal(0, 0.05)))
    for st, cfg in TERM_CFG.items():
        for mv, bank_list in (("A", ARR_BANKS), ("D", DEP_BANKS)):
            mins = []
            for _, s, e in bank_list:
                mins += list(rng.uniform(s, e, rng.poisson(cfg["n"] * (0.9 + 0.2 * rng.random()))))
            mins += list(rng.uniform(360, 1380, rng.poisson(6)))
            for m in mins:
                seats = int(rng.integers(*cfg["seats"]))
                planned = round(seats * plan_lf)
                cancelled = rng.random() < ev["cancel"]
                actual = 0 if cancelled else round(seats * float(np.clip(rng.normal(plan_lf, 0.05), 0.4, 1.0)) * ev["pax_mult"])
                sched_slot = int(m // 15)
                flights.append((day, sched_slot, st, mv, bank_of(mv, m), planned))
                if actual == 0:
                    continue
                if mv == "A":
                    delay = float(np.clip(rng.normal(6 + ev["extra_delay"], 14), -10, 90))
                    t0 = day * SLOTS + int((m + delay) // 15)
                    to_rcc = actual * 0.28 * share_noise
                    other = actual * 0.08 * share_noise / (N_ST - 2)
                    dests = [(0, to_rcc)] + [(d, other) for d in range(1, N_ST) if d != st]
                    for dest, pax in dests:
                        di = direction(st, dest) - 1
                        for k, w in enumerate(K_ARR, start=1):
                            if t0 + k < T:
                                lam[st, di, t0 + k] += pax * w
                else:
                    t0 = day * SLOTS + sched_slot
                    pax = actual * 0.22 * share_noise
                    di = direction(0, st) - 1
                    for k, w in zip(range(6, 11), K_DEP):
                        if t0 - k >= 0:
                            lam[0, di, t0 - k] += pax * w
lam += np.tile(ambient, N_DAYS)
ride = rng.poisson(lam).astype(float)                       # (station, dir, T) actual boardings

fl = pd.DataFrame(flights, columns=["day", "slot", "st", "mv", "BankKey", "pax"])
sched = (fl.groupby(["day", "slot", "st", "BankKey"])
         .agg(Flights=("pax", "size"), Scheduled_Pax=("pax", "sum")).reset_index())
arr_plan = np.zeros((4, T)); dep_plan = np.zeros((4, T))
for r in fl.itertuples():
    (arr_plan if r.mv == "A" else dep_plan)[r.st - 1, r.day * SLOTS + r.slot] += r.pax

# --------------------------------------------------------------------------- forecast models
PAD = 16
arr_p, dep_p = np.pad(arr_plan, ((0, 0), (PAD, PAD))), np.pad(dep_plan, ((0, 0), (PAD, PAD)))
daytype = np.tile(np.repeat((np.arange(N_DAYS) % 7 < 5).astype(int), SLOTS), 1)
slot_of = np.tile(slots, N_DAYS)


def feats(idx):
    cols = [np.ones(len(idx))]
    for term in range(4):
        cols += [arr_p[term, idx - k + PAD] for k in range(0, 8)]
        cols += [dep_p[term, idx + j + PAD] for j in range(2, 13)]
    return np.column_stack(cols)


def ridge_fit(X, y, lam_):
    pen = np.eye(X.shape[1]) * lam_; pen[0, 0] = 0
    return np.linalg.solve(X.T @ X + pen, X.T @ y)


def wape(a, f):
    return np.abs(a - f).sum() / a.sum()


def fit_predict(model, y, tr, te, lam_=100.0):
    """Fit on index array tr, return predictions on te."""
    if model == "Seasonal Profile":
        prof = np.zeros((2, SLOTS))
        for dt in (0, 1):
            for s in range(SLOTS):
                sel = tr[(daytype[tr] == dt) & (slot_of[tr] == s)]
                prof[dt, s] = y[sel].mean()
        return prof[daytype[te], slot_of[te]]
    beta = ridge_fit(feats(tr), y[tr], lam_)
    return np.clip(feats(te) @ beta, 0, None)


MODELS = ["Seasonal Profile", "Schedule-driven Ridge"]
idx_w14, idx_w5 = np.arange(0, D_W4), np.arange(D_W4, D_W5)
idx_train, idx_hold, idx_fc = np.arange(0, D_W5), np.arange(D_W5, D_W6), np.arange(D_W5, T)
series = [(s, d) for s in range(N_ST) for d in range(2)]

best_lam = min([1, 10, 100, 1e3, 1e4, 1e5], key=lambda L: sum(
    np.abs(ride[s, d, idx_w5] - fit_predict("Schedule-driven Ridge", ride[s, d], idx_w14, idx_w5, L)).sum()
    for s, d in series))
val = {m: wape(np.concatenate([ride[s, d, idx_w5] for s, d in series]),
               np.concatenate([fit_predict(m, ride[s, d], idx_w14, idx_w5, best_lam) for s, d in series]))
       for m in MODELS}
champion = min(val, key=val.get)
dim_model = pd.DataFrame({"ModelVersionKey": [1, 2], "Model_Name": MODELS, "Version": "v1",
                          "Trained_Until": "2026-02-08", "IsChampion": [m == champion for m in MODELS]})

fc_rows, hold = [], []
for mi, m in enumerate(MODELS, start=1):
    for s, d in series:
        y = ride[s, d]
        p_cal = fit_predict(m, y, idx_w14, idx_w5, best_lam)
        r = (y[idx_w5] - p_cal) / np.sqrt(p_cal + 1)
        qs = np.quantile(r, dim_scen["Quantile"].to_numpy())
        p = fit_predict(m, y, idx_train, idx_fc, best_lam)
        for sk, q in zip(dim_scen["ScenarioKey"], qs):
            v = np.clip(p + q * np.sqrt(p + 1), 0, None)
            fc_rows.append(pd.DataFrame({
                "DateKey": dim_date["DateKey"].to_numpy()[idx_fc // SLOTS], "Time15Key": slot_of[idx_fc],
                "StationKey": s + 1, "DirectionKey": d + 1, "ModelVersionKey": mi,
                "ScenarioKey": sk, "Forecast_Pax": np.round(v, 1)}))
fact_fc = pd.concat(fc_rows, ignore_index=True)

# hold-out metrics (week 6 has actuals)
ride_long = pd.DataFrame({
    "DateKey": np.concatenate([dim_date["DateKey"].to_numpy()[np.arange(T) // SLOTS]] * (N_ST * 2)),
    "Time15Key": np.tile(slot_of, N_ST * 2),
    "StationKey": np.repeat(np.arange(1, N_ST + 1), 2 * T),
    "DirectionKey": np.tile(np.repeat([1, 2], T), N_ST),
    "Boardings": ride.reshape(-1)})
last_key = int(dim_date["DateKey"].iloc[LAST_ACTUAL_DAY])
fact_ride = ride_long[ride_long["DateKey"] <= last_key].reset_index(drop=True)

keys = ["DateKey", "Time15Key", "StationKey", "DirectionKey"]
j = fact_fc.merge(fact_ride, on=keys)
j = j[j["DateKey"] >= int(dim_date["DateKey"].iloc[35])]
storm_key = int(dim_date["DateKey"].iloc[38])
mrows = []
for mk, g in j.groupby("ModelVersionKey"):
    piv = g.pivot_table(index=keys + ["Boardings"], columns="ScenarioKey", values="Forecast_Pax").reset_index()
    ex = piv[piv["DateKey"] != storm_key]
    piv["Hour"] = piv["Time15Key"] // 4
    hr = piv.groupby(["DateKey", "Hour", "StationKey", "DirectionKey"])[["Boardings", 2]].sum()
    mrows.append({"ModelVersionKey": mk, "Model_Name": MODELS[mk - 1], "IsChampion": MODELS[mk - 1] == champion,
                  "Validation_WAPE_W5": val[MODELS[mk - 1]],
                  "Holdout_WAPE_P50": wape(piv["Boardings"], piv[2]),
                  "Holdout_WAPE_P50_ex_storm": wape(ex["Boardings"], ex[2]),
                  "Holdout_WAPE_P50_hourly": wape(hr["Boardings"], hr[2]),
                  "Holdout_Bias": (piv["Boardings"].sum() - piv[2].sum()) / piv[2].sum(),
                  "P10_P90_Coverage": ((piv["Boardings"] >= piv[1]) & (piv["Boardings"] <= piv[3])).mean()})
metrics = pd.DataFrame(mrows)

# --------------------------------------------------------------------------- capacity
trains_dp = {"Night": 2, "AM Peak": 5, "Midday": 3, "PM Peak": 5, "Evening": 3}
dp_of_slot = dim_time["Day_Part"].to_numpy()
cap_rows = []
for s, d in series:
    trains_all = np.array([trains_dp[x] for x in dp_of_slot[slot_of]], dtype=float)
    need = ride[s, d, :D_W5] / trains_all[:D_W5]          # boardings per train offered
    per_train = max(20, int(np.ceil(np.quantile(need, 0.98) / 0.95 / 5) * 5))
    trains = np.array([trains_dp[x] for x in dp_of_slot[slot_of]], dtype=float)
    for slot in MAINT_SLOTS:
        trains[MAINT_DAY * SLOTS + slot] = max(1, round(trains[MAINT_DAY * SLOTS + slot] * 0.6))
    cap_rows.append(pd.DataFrame({
        "DateKey": dim_date["DateKey"].to_numpy()[np.arange(T) // SLOTS], "Time15Key": slot_of,
        "StationKey": s + 1, "DirectionKey": d + 1, "Trains_Planned": trains,
        "Boarding_Cap_Per_Train": per_train, "Capacity_Pax": trains * per_train}))
fact_cap = pd.concat(cap_rows, ignore_index=True)

# --------------------------------------------------------------------------- flight schedule fact
fact_sched = sched.rename(columns={"st": "StationKey", "slot": "Time15Key"})
fact_sched["StationKey"] = fact_sched["StationKey"] + 1   # st is a 0-based station index (RCC=0); Dim_Station keys start at 1
fact_sched["DateKey"] = dim_date["DateKey"].to_numpy()[fact_sched["day"]]
fact_sched = fact_sched[["DateKey", "Time15Key", "StationKey", "BankKey", "Flights", "Scheduled_Pax"]]

# --------------------------------------------------------------------------- write
out = {"Dim_Date": dim_date, "Dim_Time15": dim_time, "Dim_Station": dim_station, "Dim_Direction": dim_dir,
       "Dim_Flight_Bank": dim_bank, "Dim_Scenario": dim_scen, "Dim_ModelVersion": dim_model,
       "Fact_APM_Ridership": fact_ride, "Fact_APM_Forecast": fact_fc,
       "Fact_Station_Capacity": fact_cap, "Fact_Flight_Schedule": fact_sched, "model_metrics": metrics}
for name, df in out.items():
    df.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8")
    print(f"{name:22s} {len(df):>8,} rows")
print(f"\nridge lambda={best_lam:g}  champion={champion}")
print(metrics.round(4).to_string(index=False))

# sanity: utilisation of actuals vs capacity
u = fact_ride.merge(fact_cap, on=keys)
u["util"] = u["Boardings"] / u["Capacity_Pax"]
print("\nactual utilisation by station/direction (P50 / P97 / max / share>=100%)")
print(u.groupby(["StationKey", "DirectionKey"])["util"].agg(
    p50="median", p97=lambda x: x.quantile(.97), max="max", over=lambda x: (x >= 1).mean()).round(3))
