"""公交时刻换乘规划台 —— Flask 后端。"""

from flask import Flask, jsonify, request, send_from_directory

from planner import (
    PREFERENCE_LABELS,
    PlanError,
    SAMPLE_DATA,
    Planner,
    normalize,
    parse_time,
)

app = Flask(__name__, static_folder="static", static_url_path="")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/meta")
def meta():
    return jsonify({"preferences": [
        {"id": pid, "label": label}
        for pid, label in PREFERENCE_LABELS.items()
    ]})


@app.get("/api/sample")
def sample():
    return jsonify(SAMPLE_DATA)


@app.post("/api/plan")
def plan():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error",
                        "message": "请求体必须是 JSON 对象"}), 400

    query = payload.get("query") or {}
    data = payload.get("data") or {}

    try:
        network = normalize(data)
        origin = str(query.get("origin", "")).strip()
        destination = str(query.get("destination", "")).strip()
        earliest = parse_time(query.get("earliest", "00:00"), "最早出发时刻")
        preference = str(query.get("preference", "fastest")).strip()
        result = Planner(network).plan(origin, destination, earliest,
                                       preference)
    except PlanError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    return jsonify(result)


@app.post("/api/continue")
def continue_trip():
    """行程途中停运/班次调整后的续程。

    请求体：{"itinerary": 原规划结果中的 itinerary,
             "completed_index": 时间线中最后完成段（ride/walk）的下标,
             "completed_station": 可选，完成段实际到达的中途经停站
                （人在车上、班次在该站终止时填写）,
             "current_time": 当前时刻 "HH:MM",
             "preference": 可选，缺省沿用原行程偏好,
             "data": 更新后的网络（站点/班次/步行/停运）}
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error",
                        "message": "请求体必须是 JSON 对象"}), 400

    itinerary = payload.get("itinerary")
    data = payload.get("data") or {}

    try:
        network = normalize(data)
        current_time = parse_time(payload.get("current_time"), "当前时刻")
        raw_index = payload.get("completed_index")
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise PlanError("completed_index 必须是已完成段的整数下标")
        preference = payload.get("preference")
        if preference is not None:
            preference = str(preference).strip() or None
        completed_station = payload.get("completed_station")
        if completed_station is not None:
            completed_station = str(completed_station).strip() or None
        result = Planner(network).continue_trip(
            itinerary, raw_index, current_time, preference,
            completed_station)
    except PlanError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
