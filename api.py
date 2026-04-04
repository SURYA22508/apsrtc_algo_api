import pandas as pd
from datetime import datetime, timedelta
import requests
import pickle
import networkx as nx
from itertools import islice
from fastapi import FastAPI
import os

app = FastAPI()

# -------------------------------------------------
# LOAD GRAPH (SAFE LOAD)
# -------------------------------------------------
try:

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(BASE_DIR, "apsrtc_main_graph.pkl"), "rb") as f:
        G = pickle.load(f)
except:
    G = nx.Graph()
    print("⚠️ Graph file not found. Using empty graph.")

# -------------------------------------------------
# APSRTC SERVICES API
# -------------------------------------------------
URL = "https://utsappapicached01.apsrtconline.in/uts-vts-api/services/all"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer abhibus",
    "x-api-key": "53693855434468454E714A596D44457A586975414F573833334833596A584D3333735938444A69357131303D",
    "Origin": "https://www.apsrtclivetrack.com",
    "Referer": "https://www.apsrtclivetrack.com/",
    "User-Agent": "Mozilla/5.0"
}

# -------------------------------------------------
# LOAD BUS ROUTE
# -------------------------------------------------
def load_bus_route(doc_id):

    url = "https://utsappapicached01.apsrtconline.in/uts-vts-api/servicewaypointdetails/bydocid"
    payload = {"docId": doc_id}

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        return result.get("data", [])
    except Exception as e:
        print("Route load error:", e)
        return []

# -------------------------------------------------
# EXTRACT TRACKING DATA
# -------------------------------------------------
def extract_tracking_data(stops, dest):

    if not stops:
        return None

    stops.sort(key=lambda x: x.get("seqNo", 0))

    current_index = -1
    last_stop_name = None
    scheduled_arrival = None
    eta_time = None

    for i, stop in enumerate(stops):

        if stop.get("vtsArrivalTime"):
            current_index = i

        last_stop_name = stop.get("wayPointName", "")

        if stop.get("wayPointName") == dest:

            scheduled_arrival = stop.get("scheduleArrTime", "")

            if stop.get("ETA"):
                try:
                    eta_date = datetime.fromisoformat(stop["ETA"])
                    eta_time = eta_date.strftime("%H:%M")
                except:
                    eta_time = None

            break

    return {
        "scheduledArrival": scheduled_arrival,
        "eta": eta_time,
        "lastStop": last_stop_name,
        "currentStopIndex": current_index
    }

# -------------------------------------------------
# TRACK BUS
# -------------------------------------------------
def track_bus(doc_id, dest):

    stops = load_bus_route(doc_id)

    result = extract_tracking_data(stops, dest)

    if not result:
        return None

    result["serviceDocId"] = doc_id
    result["destination"] = dest

    return result

# -------------------------------------------------
# GET SERVICES BETWEEN STOPS
# -------------------------------------------------
def pair_out(source_id, destination_id):

    payload = {
        "apiVersion": 1,
        "sourceLinkId": source_id,
        "destinationLinkId": destination_id
    }

    try:

        response = requests.post(URL, headers=HEADERS, json=payload, timeout=10)

        if response.status_code != 200:
            return pd.DataFrame()

        data_json = response.json()

        services = data_json.get("services") or data_json.get("data")

        if not services:
            return pd.DataFrame()

        rows = []

        for s in services:

            rows.append({
                "serviceDocId": s.get("serviceDocId"),
                "sourceName": s.get("sourceName"),
                "destinationName": s.get("destinationName"),
                "serviceStartTime": s.get("serviceStartTime"),
                "serviceEndTime": s.get("serviceEndTime"),
                "oprsNo": s.get("oprsNo")
            })

        return pd.DataFrame(rows)

    except Exception as e:
        print("Pair API error:", e)
        return pd.DataFrame()

# -------------------------------------------------
# TIME OPTIMIZATION
# -------------------------------------------------
def finaltime(route_list):

    try:
        dd = pd.read_csv(os.path.join(BASE_DIR, "data.csv"))
    except:
        return None, pd.DataFrame()

    k = 0
    path = pd.DataFrame()
    start_time = datetime.now()

    for i in range(len(route_list) - 1):

        df = pair_out(route_list[i], route_list[i + 1])

        if df.empty:
            continue

        doc_ids = list(df["serviceDocId"])

        try:
            destination = dd[dd["placeId"] == int(route_list[i + 1])]["placeName"].iloc[0]
        except:
            continue

        tracking_results = []

        for d in doc_ids:

            result = track_bus(d, destination)

            if result:
                tracking_results.append(result)

        tracking_df = pd.DataFrame(tracking_results)

        if tracking_df.empty:
            continue

        df = df.merge(tracking_df, on="serviceDocId", how="left")

        df["scheduledArrival"] = pd.to_datetime(
            df["scheduledArrival"],
            format="%I:%M %p",
            errors="coerce"
        )

        df["scheduledArrival"] = df["scheduledArrival"].fillna(
            pd.to_datetime(df["serviceEndTime"], format="%H:%M", errors="coerce")
        )

        df["scheduledArrival"] = df["scheduledArrival"].dt.strftime("%H:%M")

        today = pd.Timestamp.today().normalize() + pd.Timedelta(days=k)

        df["serviceStartTime"] = today + pd.to_timedelta(df["serviceStartTime"] + ":00")
        df["scheduledArrival"] = today + pd.to_timedelta(df["scheduledArrival"] + ":00")

        # handle midnight buses
        df_day2 = df.copy()
        df_day2["serviceStartTime"] += pd.Timedelta(days=1)
        df_day2["scheduledArrival"] += pd.Timedelta(days=1)

        df = pd.concat([df, df_day2], ignore_index=True)

        df.loc[df["scheduledArrival"] < df["serviceStartTime"], "scheduledArrival"] += pd.Timedelta(days=1)

        df = df[df["serviceStartTime"] >= start_time + timedelta(minutes=30)]

        if df.empty:
            continue

        df = df.sort_values(by="scheduledArrival")

        endtime = df["scheduledArrival"].iloc[0]

        if start_time.date() != endtime.date():
            k += 1

        start_time = endtime

        path = pd.concat([path, df[:1]], ignore_index=True)

    return endtime if not path.empty else None, path

# -------------------------------------------------
# PATH FINDER
# -------------------------------------------------
def find(source, target):

    if source not in G or target not in G:
        return None, [], []

    best_time = datetime.now() + pd.Timedelta(days=100)
    best_path = pd.DataFrame()

    try:
        paths = list(islice(nx.shortest_simple_paths(G, source, target, weight="weight"), 3))
    except:
        return None, [], []

    all_paths = []

    for p in paths:
        print(p)
        e, path_df = finaltime(p)
        print(e,path_df)
        all_paths.append([e, path_df])

        if e and e < best_time:
            best_time = e
            best_path = path_df

    if isinstance(best_path, pd.DataFrame) and not best_path.empty:
        best_path = best_path.to_dict(orient="records")
    else:
        best_path = []

    return best_time, best_path, all_paths

# -------------------------------------------------
# API ENDPOINT
# -------------------------------------------------
@app.get("/route")
def route(source: int, target: int):
    print('strat')
    arrival, path, al = find(source, target)

    if not arrival:
        return {"error": "No route found"}

    # convert numpy types
    path = [
        {k: int(v) if hasattr(v, "item") else v for k, v in row.items()}
        for row in path
    ]

    all_times = []
    all_paths = []

    for t, p in al:

        all_times.append(str(t) if t else None)

        if isinstance(p, pd.DataFrame):
            all_paths.append(p.to_dict(orient="records"))
        else:
            all_paths.append([])

    return {
        "arrival_time": str(arrival),
        "best_path": path,
        "all_possible_times": all_times,
        "all_possible_paths": all_paths
    }