"""公交时刻换乘规划核心。

网络为“时间依赖图”：
- 站点为节点；
- 每个班次按发车时刻依次经停若干站点，只有在不早于到达时刻才能在该站上车；
- 站点之间可配置站内/站间步行换乘边（双向），换乘必须先走完步行时间；
- 临时停运分三类：站点停运（某站在时间窗内不能上/下车，班次只能跳站通过）、
  线路停运（时间窗内发车的班次整个班次被跳过）与线路区段停运（某线路在经停
  顺序中的起止站点之间的站段于时间窗内封锁：班次不整班取消，也不穿越封锁，
  乘车段最远生成到封锁段起点站，之后由步行或其他班次接续；封锁区段之外的
  上、下车不受影响）。

搜索采用多标签时间依赖 Dijkstra，标签按所选偏好对应的三元组做帕累托支配剪枝；
无解时汇总搜索过程中遇到的全部阻塞约束，返回时间最早的一个。

行程续程（continue_trip）：途中停运或班次调整后，以原时间线中最后完成的乘车/步行段
为界，已完成路段与候车记录原样沿用（只核对站点与时间连续性，不因新网络的变化而抹掉
已走事实）；自该段到达站与用户填写的当前时刻起，在更新后的网络上搜索余程，累计整程
换乘、步行与候车（含完成段至当前时刻的等待）。余程搜索中同一班次不得再经过已乘过的
站段；接续无解时返回完成部分（含等待至当前时刻）与余程的首个阻塞约束。
"""

from __future__ import annotations

import heapq
from itertools import count
from typing import Any

DAY_MINUTES = 24 * 60

# 三种偏好对应的字典序键序：
# 到达时刻、换乘次数(=乘坐班次数-1)、总步行分钟
PREFERENCE_FIELDS = {
    "fastest": ("arrival", "transfers", "walking"),
    "transfers": ("transfers", "arrival", "walking"),
    "walking": ("walking", "arrival", "transfers"),
}
PREFERENCE_LABELS = {
    "fastest": "最快到达",
    "transfers": "最少换乘",
    "walking": "最少步行",
}


class PlanError(ValueError):
    """输入数据不合法。"""


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def to_hhmm(value: int | None) -> str:
    if value is None:
        return "--:--"
    value = int(value)
    return f"{value // 60:02d}:{value % 60:02d}"


def parse_time(value: Any, field: str) -> int:
    """把 'HH:MM' 或分钟数解析为 0..1439 的整数分钟。"""
    if isinstance(value, bool):
        raise PlanError(f"{field} 时间格式不合法：{value!r}")
    if isinstance(value, (int, float)):
        minutes = int(value)
    elif isinstance(value, str):
        text = value.strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise PlanError(f"{field} 时间格式应为 HH:MM：{value!r}")
        try:
            hours, mins = int(parts[0]), int(parts[1])
        except ValueError:
            raise PlanError(f"{field} 时间格式应为 HH:MM：{value!r}")
        minutes = hours * 60 + mins
    else:
        raise PlanError(f"{field} 时间格式不合法：{value!r}")
    if not 0 <= minutes < DAY_MINUTES:
        raise PlanError(f"{field} 时间超出当日范围：{to_hhmm(minutes)}")
    return minutes


def _uid(prefix: str, value: Any, index: int) -> str:
    text = str(value).strip() if value is not None else ""
    return text or f"{prefix}{index + 1}"


def _add_segment_closure(segment_closures: dict, trips: list[dict],
                         item: dict, index: int, start: int, end: int,
                         label: str | None, station_ids: set[str]) -> None:
    """校验一条线路区段停运记录，并按班次经停顺序登记被封锁的站段区间。

    记录格式：{"kind": "segment", "route": 线路名, "from": 起点站, "to": 终点站,
               "start": ..., "end": ..., "label": ...}
    起止站点必须都出现在该线路至少一个班次的经停序列中且顺序一致；
    对不含起止站点的班次该记录不生效。
    """
    no = index + 1
    route = str(item.get("route") or item.get("ref") or "").strip()
    if not route:
        raise PlanError(f"停运记录 #{no} 缺少线路名")
    seg_from = str(item.get("from", "")).strip()
    seg_to = str(item.get("to", "")).strip()
    if not seg_from or not seg_to:
        raise PlanError(f"停运记录 #{no} 缺少经停顺序中的起止站点")
    for sid in (seg_from, seg_to):
        if sid not in station_ids:
            raise PlanError(f"停运记录 #{no} 引用了不存在的站点：{sid}")
    if seg_from == seg_to:
        raise PlanError(f"停运记录 #{no} 的起止站点相同：{seg_from}")

    route_trips = [t for t in trips if t["route"] == route]
    if not route_trips:
        raise PlanError(f"停运记录 #{no} 引用了不存在的线路：{route}")

    matched = False
    reversed_seen = False
    for trip in route_trips:
        seq = [s["station"] for s in trip["stops"]]
        if seg_from not in seq or seg_to not in seq:
            continue  # 该班次不经停此区段，记录对其不生效
        from_idx, to_idx = seq.index(seg_from), seq.index(seg_to)
        if from_idx >= to_idx:
            reversed_seen = True
            continue
        matched = True
        segment_closures.setdefault(trip["id"], []).append({
            "route": route, "from": seg_from, "to": seg_to,
            "from_idx": from_idx, "to_idx": to_idx,
            "start": start, "end": end, "label": label,
        })
    if not matched:
        if reversed_seen:
            raise PlanError(
                f"停运记录 #{no} 的起止站点与线路 {route} 的经停顺序相反："
                f"{seg_from}→{seg_to}")
        raise PlanError(
            f"停运记录 #{no}：线路 {route} 的经停站中不存在 "
            f"{seg_from}→{seg_to} 区段")


# --------------------------------------------------------------------------- #
# 数据规范化
# --------------------------------------------------------------------------- #
def normalize(data: dict) -> dict:
    """校验并规范化前端提交的数据。"""
    if not isinstance(data, dict):
        raise PlanError("请求体必须是 JSON 对象")

    # ---- 站点 ----
    raw_stations = data.get("stations", [])
    if not isinstance(raw_stations, list) or not raw_stations:
        raise PlanError("至少需要录入一个站点")
    stations: list[str] = []
    station_names: dict[str, str] = {}
    seen: set[str] = set()
    for i, item in enumerate(raw_stations):
        if isinstance(item, str):
            sid, name = item.strip(), item.strip()
        elif isinstance(item, dict):
            sid = str(item.get("id", "")).strip() or f"S{i + 1}"
            name = str(item.get("name", "")).strip() or sid
        else:
            raise PlanError(f"站点 #{i + 1} 格式不合法")
        if sid in seen:
            raise PlanError(f"站点编号重复：{sid}")
        seen.add(sid)
        stations.append(sid)
        station_names[sid] = name

    # ---- 班次 ----
    raw_trips = data.get("trips", [])
    if not isinstance(raw_trips, list):
        raise PlanError("trips 必须是数组")
    trips: list[dict] = []
    trip_ids: set[str] = set()
    for i, item in enumerate(raw_trips):
        if not isinstance(item, dict):
            raise PlanError(f"班次 #{i + 1} 格式不合法")
        tid = _uid("T", item.get("id"), i)
        if tid in trip_ids:
            raise PlanError(f"班次编号重复：{tid}")
        trip_ids.add(tid)
        route = str(item.get("route", tid)).strip() or tid
        raw_stops = item.get("stops", [])
        if not isinstance(raw_stops, list) or len(raw_stops) < 2:
            raise PlanError(f"班次 {tid} 至少需要两个经停站")
        stops: list[dict] = []
        prev_time = -1
        for j, stop in enumerate(raw_stops):
            if not isinstance(stop, dict):
                raise PlanError(f"班次 {tid} 的经停站 #{j + 1} 格式不合法")
            sid = str(stop.get("station", "")).strip()
            if sid not in seen:
                raise PlanError(f"班次 {tid} 引用了不存在的站点：{sid}")
            when = parse_time(stop.get("time"), f"班次 {tid} 站点 {sid} 时刻")
            if when <= prev_time:
                raise PlanError(
                    f"班次 {tid} 在 {sid} 的时刻 {to_hhmm(when)} 不晚于前一站，"
                    "各班次时刻必须严格递增"
                )
            prev_time = when
            stops.append({"station": sid, "time": when})
        trips.append({"id": tid, "route": route, "stops": stops})

    # ---- 步行换乘（双向）----
    raw_transfers = data.get("transfers", [])
    if not isinstance(raw_transfers, list):
        raise PlanError("transfers 必须是数组")
    walk_minutes: dict[tuple[str, str], int] = {}

    def add_walk(a: str, b: str, minutes: int) -> None:
        key = (a, b)
        old = walk_minutes.get(key)
        if old is None or minutes < old:
            walk_minutes[key] = minutes

    for i, item in enumerate(raw_transfers):
        if not isinstance(item, dict):
            raise PlanError(f"步行换乘 #{i + 1} 格式不合法")
        a = str(item.get("from", "")).strip()
        b = str(item.get("to", "")).strip()
        if a not in seen or b not in seen:
            raise PlanError(f"步行换乘 #{i + 1} 引用了不存在的站点：{a}->{b}")
        if a == b:
            raise PlanError(f"步行换乘 #{i + 1} 的起终点相同：{a}")
        try:
            minutes = int(item.get("minutes"))
        except (TypeError, ValueError):
            raise PlanError(f"步行换乘 {a}->{b} 的步行分钟数不合法")
        if minutes < 1:
            raise PlanError(f"步行换乘 {a}->{b} 的步行时间必须为正整数")
        add_walk(a, b, minutes)
        add_walk(b, a, minutes)

    walk_edges: dict[str, list[tuple[str, int]]] = {s: [] for s in stations}
    for (a, b), minutes in walk_minutes.items():
        walk_edges[a].append((b, minutes))
    for sid in walk_edges:
        walk_edges[sid].sort(key=lambda x: (x[1], x[0]))

    # ---- 临时停运 ----
    raw_susp = data.get("suspensions", [])
    if not isinstance(raw_susp, list):
        raise PlanError("suspensions 必须是数组")
    station_windows: dict[str, list[tuple[int, int, str | None]]] = {
        s: [] for s in stations
    }
    route_windows: dict[str, list[tuple[int, int, str | None]]] = {}
    # 线路区段停运：班次 ID -> 该班次经停序列中被封锁的站段区间列表
    segment_closures: dict[str, list[dict]] = {}
    for i, item in enumerate(raw_susp):
        if not isinstance(item, dict):
            raise PlanError(f"停运记录 #{i + 1} 格式不合法")
        kind = str(item.get("kind", "")).strip()
        ref = str(item.get("ref", "")).strip()
        label = item.get("label")
        if kind == "station":
            if ref not in seen:
                raise PlanError(f"停运记录 #{i + 1} 引用了不存在的站点：{ref}")
        elif kind == "route":
            if not ref:
                raise PlanError(f"停运记录 #{i + 1} 缺少线路名")
        elif kind == "segment":
            pass  # 线路名与起止站点在时间窗解析之后统一校验
        else:
            raise PlanError(
                f"停运记录 #{i + 1} 类型必须是 station、route 或 segment")
        start = parse_time(item.get("start"), f"停运记录 #{i + 1} 开始")
        end = parse_time(item.get("end"), f"停运记录 #{i + 1} 结束")
        if end <= start:
            raise PlanError(f"停运记录 #{i + 1} 的结束时刻必须晚于开始时刻")
        window = (start, end, str(label) if label else None)
        if kind == "station":
            station_windows[ref].append(window)
        elif kind == "route":
            route_windows.setdefault(ref, []).append(window)
        else:
            _add_segment_closure(
                segment_closures, trips, item, i, start, end,
                str(label) if label else None, seen)

    # 站点 -> 可乘坐的 (班次, 经停下标)，按到站时刻排序
    departures: dict[str, list[tuple[str, int]]] = {s: [] for s in stations}
    for trip in trips:
        for idx, stop in enumerate(trip["stops"]):
            departures[stop["station"]].append((trip["id"], idx))
    for sid in departures:
        departures[sid].sort(key=lambda x: (
            next(t["stops"][x[1]]["time"] for t in trips if t["id"] == x[0]),
            x[0],
        ))

    return {
        "stations": stations,
        "station_names": station_names,
        "trips": {t["id"]: t for t in trips},
        "trip_order": [t["id"] for t in trips],
        "walk_edges": walk_edges,
        "departures": departures,
        "station_windows": station_windows,
        "route_windows": route_windows,
        "segment_closures": segment_closures,
    }


SAMPLE_DATA: dict = {
    "stations": [
        {"id": "S1", "name": "东郊枢纽"},
        {"id": "S2", "name": "东郊路"},
        {"id": "S3", "name": "会展中心"},
        {"id": "S4", "name": "人民广场"},
        {"id": "S5", "name": "大学城"},
        {"id": "S6", "name": "火车北站"},
        {"id": "S7", "name": "临江道口"},
        {"id": "S8", "name": "西城枢纽"},
    ],
    "trips": [
        {"id": "K1", "route": "1路", "stops": [
            {"station": "S1", "time": "08:05"},
            {"station": "S3", "time": "08:23"},
            {"station": "S4", "time": "08:40"},
            {"station": "S5", "time": "09:02"},
        ]},
        {"id": "K2", "route": "2路", "stops": [
            {"station": "S1", "time": "08:08"},
            {"station": "S4", "time": "08:25"},
            {"station": "S6", "time": "08:35"},
        ]},
        {"id": "K2b", "route": "2路", "stops": [
            {"station": "S1", "time": "08:38"},
            {"station": "S4", "time": "08:55"},
            {"station": "S6", "time": "09:05"},
        ]},
        {"id": "K3", "route": "3路", "stops": [
            {"station": "S4", "time": "08:45"},
            {"station": "S6", "time": "08:52"},
            {"station": "S7", "time": "09:10"},
            {"station": "S8", "time": "09:22"},
        ]},
        {"id": "K4", "route": "4路", "stops": [
            {"station": "S5", "time": "09:05"},
            {"station": "S6", "time": "09:12"},
            {"station": "S7", "time": "09:20"},
            {"station": "S8", "time": "09:35"},
        ]},
    ],
    "transfers": [
        {"from": "S2", "to": "S1", "minutes": 4},
        {"from": "S4", "to": "S5", "minutes": 7},
        {"from": "S3", "to": "S6", "minutes": 12},
    ],
    "suspensions": [
        # 示例：临江道口当日全天封站，途经班次须跳站通过（仍可乘到西城枢纽）
        {"kind": "station", "ref": "S7", "start": "00:00", "end": "23:59",
         "label": "道口施工，全天封站"},
        # 示例：3 路午间区间停运
        {"kind": "route", "ref": "3路", "start": "12:00", "end": "13:00",
         "label": "3路午间停运"},
        # 示例：2 路 S4→S6 站段早高峰局部停运——跨段班次不取消、不穿越，
        # 只能在人民广场（S4）前下车，再步行或换乘其他班次前往火车北站
        {"kind": "segment", "route": "2路", "from": "S4", "to": "S6",
         "start": "08:50", "end": "09:10", "label": "2路 S4→S6 区间临时管制"},
    ],
}


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #
class Planner:
    def __init__(self, network: dict):
        self.n = network
        self.names = network["station_names"]

    def sname(self, sid: str) -> str:
        return self.names.get(sid, sid)

    def station_open(self, sid: str, when: int) -> tuple[bool, dict | None]:
        for start, end, label in self.n["station_windows"].get(sid, ()):  # type: ignore[arg-type]
            if start <= when <= end:
                return False, {"start": start, "end": end, "label": label}
        return True, None

    def route_open(self, route: str, when: int) -> tuple[bool, dict | None]:
        for start, end, label in self.n["route_windows"].get(route, ()):
            if start <= when <= end:
                return False, {"start": start, "end": end, "label": label}
        return True, None

    def segment_closure(self, trip_id: str, seg_idx: int) -> dict | None:
        """班次 trip_id 的第 seg_idx 个站段（stops[k]→stops[k+1]）是否被
        线路区段停运封锁：站段运行区间与停运时间窗（闭区间）相交即封锁。"""
        trip = self.n["trips"][trip_id]
        depart = trip["stops"][seg_idx]["time"]
        arrive = trip["stops"][seg_idx + 1]["time"]
        for clo in self.n["segment_closures"].get(trip_id, ()):
            if (clo["from_idx"] <= seg_idx < clo["to_idx"]
                    and depart <= clo["end"] and clo["start"] <= arrive):
                return clo
        return None

    @staticmethod
    def _window_text(window: dict | None) -> str:
        if not window:
            return "停运时段"
        base = f"{to_hhmm(window['start'])}–{to_hhmm(window['end'])}"
        if window.get("label"):
            base += f"（{window['label']}）"
        return base

    def plan(self, origin: str, destination: str, earliest: int,
             preference: str) -> dict:
        if origin not in self.names:
            raise PlanError(f"起点站不存在：{origin}")
        if destination not in self.names:
            raise PlanError(f"终点站不存在：{destination}")
        if preference not in PREFERENCE_FIELDS:
            raise PlanError(f"未知偏好：{preference}")
        result = self._search_core(
            origin, destination, earliest, preference, frozenset(),
            start_noun="起点站", start_action="start", start_window_action="出发")
        if result["status"] != "ok":
            return result
        return {"status": "ok", "explored": result["explored"],
                "itinerary": self._itinerary(origin, destination, earliest,
                                             preference, result["chain"],
                                             result["rides"], result["walk"],
                                             result["arrival"])}

    def _search_core(self, origin: str, destination: str, earliest: int,
                     preference: str, initial_ridden: frozenset,
                     start_noun: str, start_action: str,
                     start_window_action: str) -> dict:
        """从 (origin, earliest) 出发的多标签时间依赖搜索。

        initial_ridden 为已乘过的 (班次ID, 站段下标) 集合：余程中同一班次
        不得再经过这些站段（续程绕行后重乘同一班次时尤为关键）。
        成功返回 {"status": "ok", "chain", "rides", "walk", "arrival",
        "explored", "blockers"}；起点停运立即返回 blocked。
        """
        # 出发站在出发时刻即停运：任何方案都不可能成立
        open_at_start, window = self.station_open(origin, earliest)
        if not open_at_start:
            return self._blocked({
                "kind": "origin_closed",
                "action": start_action,
                "station": origin,
                "time": earliest,
                "window": window,
                "message": (
                    f"{start_noun} {self.sname(origin)} 在 {to_hhmm(earliest)} 处于临时停运时段"
                    f"{self._window_text(window)}，无法{start_window_action}。"
                ),
            })

        if origin == destination:
            return {"status": "ok", "chain": (), "rides": 0, "walk": 0,
                    "arrival": earliest, "explored": 0, "blockers": {}}

        blockers: dict[tuple, dict] = {}

        def record(blocker: dict) -> None:
            key = (blocker["kind"], blocker.get("action"),
                   blocker.get("station"), blocker.get("time"),
                   blocker.get("trip_id"))
            blockers.setdefault(key, blocker)

        order = PREFERENCE_FIELDS[preference]

        def vec(label: dict) -> tuple:
            values = {
                "arrival": label["arrival"],
                "transfers": max(0, label["rides"] - 1),
                "walking": label["walk"],
            }
            return tuple(values[f] for f in order)

        def dominates(a: dict, b: dict) -> bool:
            va, vb = list(vec(a)), list(vec(b))
            # 三元组不劣、且已乘站段集合为子集才可能支配；集合严格包含也视为
            # 支配（约束更少），完全相同的三元组+集合只保留最先产生的标签。
            return (all(x <= y for x, y in zip(va, vb))
                    and a["ridden"] <= b["ridden"]
                    and (va != vb or a["ridden"] != b["ridden"]))

        def key_tuple(label: dict) -> tuple:
            return tuple(vec(label)) + (label["ridden"],)

        # 标签：站点、到达时刻、乘坐班次数、累计步行分钟、已乘站段集合、链
        start_label = {
            "uid": 0, "station": origin, "arrival": earliest,
            "rides": 0, "walk": 0, "ridden": frozenset(initial_ridden),
            "chain": (),
        }
        buckets: dict[str, list[dict]] = {s: [] for s in self.n["stations"]}
        buckets[origin].append(start_label)

        heap: list[tuple] = []
        counter = count(1)
        heapq.heappush(heap, (earliest, start_label["uid"], start_label))
        uid_of = {0: start_label}
        explored = 0

        def try_extend(label: dict, station: str, arrival: int, rides: int,
                       walk: int, ridden: frozenset, step: dict) -> None:
            new_label = {
                "uid": next(counter), "station": station, "arrival": arrival,
                "rides": rides, "walk": walk, "ridden": ridden,
                "chain": label["chain"] + (step,),
            }
            bucket = buckets[station]
            if any(dominates(existing, new_label) or
                   key_tuple(existing) == key_tuple(new_label)
                   for existing in bucket):
                return
            for existing in list(bucket):
                if dominates(new_label, existing):
                    bucket.remove(existing)
            bucket.append(new_label)
            uid_of[new_label["uid"]] = new_label
            heapq.heappush(heap, (arrival, new_label["uid"], new_label))

        while heap:
            _, uid, label = heapq.heappop(heap)
            if not any(b["uid"] == uid for b in buckets[label["station"]]):
                continue  # 已被更优标签支配
            explored += 1
            station = label["station"]
            ready = label["arrival"]

            # ---- 步行换乘边（时间无关，走完后才允许在邻站乘车）----
            for neighbor, minutes in self.n["walk_edges"].get(station, ()):
                arrival = ready + minutes
                open_now, win = self.station_open(neighbor, arrival)
                if not open_now:
                    record({
                        "kind": "station_closed", "action": "walk",
                        "station": neighbor, "time": arrival,
                        "trip_id": None, "window": win,
                        "message": (
                            f"步行 {self.sname(station)}→{self.sname(neighbor)} 于 "
                            f"{to_hhmm(arrival)} 到达 {self.sname(neighbor)}，但该站处于"
                            f"临时停运时段{self._window_text(win)}，无法进入换乘。"
                        ),
                    })
                    continue
                try_extend(label, neighbor, arrival, label["rides"],
                           label["walk"] + minutes, label["ridden"],
                           {"type": "walk", "from": station, "to": neighbor,
                            "depart": ready, "arrive": arrival,
                            "minutes": minutes})

            # ---- 班次边（时间依赖：只能赶上 ready 之后的发车）----
            for trip_id, idx in self.n["departures"].get(station, ()):
                trip = self.n["trips"][trip_id]
                dep = trip["stops"][idx]["time"]
                if dep < ready:
                    continue  # 已发车
                # 注：已乘站段在下方延伸循环中拦截——在原下车站重新登车
                # 向前乘坐未乘过的站段是允许的（区段停运步行绕行后重乘）。

                # 本站上车时站点必须开放
                open_board, win_board = self.station_open(station, dep)
                if not open_board:
                    record({
                        "kind": "station_closed", "action": "board",
                        "station": station, "time": dep,
                        "trip_id": trip_id, "window": win_board,
                        "message": (
                            f"拟于 {to_hhmm(dep)} 在 {self.sname(station)} 乘坐班次 "
                            f"{trip_id}（{trip['route']}），但该站此时处于临时停运时段"
                            f"{self._window_text(win_board)}，不能上车。"
                        ),
                    })
                    continue
                # 线路在该发车时刻处于停运区间：跳过整个班次
                open_route, win_route = self.route_open(trip["route"], dep)
                if not open_route:
                    record({
                        "kind": "route_suspended", "action": "board",
                        "station": station, "time": dep,
                        "trip_id": trip_id, "window": win_route,
                        "route": trip["route"],
                        "message": (
                            f"班次 {trip_id}（{trip['route']}）原定于 {to_hhmm(dep)} 从 "
                            f"{self.sname(station)} 发车，但线路在临时停运区间"
                            f"{self._window_text(win_route)}内，该班次被跳过。"
                        ),
                    })
                    continue

                # 延伸到后续每个（到站时开放的）经停站；停运站跳站通过。
                # 线路区段停运的封锁站段不可穿越：乘车段最远生成到封锁段
                # 起点站（班次不整班取消），之后由步行或其他班次接续。
                # 已乘过的站段同样不可再次经过（续程重乘同一班次）。
                new_segments: set = set()
                for j in range(idx + 1, len(trip["stops"])):
                    if (trip_id, j - 1) in label["ridden"]:
                        break
                    closure = self.segment_closure(trip_id, j - 1)
                    if closure is not None:
                        seg_from = trip["stops"][j - 1]["station"]
                        seg_to = trip["stops"][j]["station"]
                        detail = (
                            f"{trip['route']} {self.sname(closure['from'])}→"
                            f"{self.sname(closure['to'])} "
                            f"{to_hhmm(closure['start'])}–"
                            f"{to_hhmm(closure['end'])}")
                        if closure["label"]:
                            detail += f"，{closure['label']}"
                        record({
                            "kind": "segment_suspended", "action": "ride",
                            "station": seg_from,
                            "time": trip["stops"][j - 1]["time"],
                            "trip_id": trip_id, "route": trip["route"],
                            "seg_from": seg_from, "seg_to": seg_to,
                            "window": {"start": closure["start"],
                                       "end": closure["end"],
                                       "label": closure["label"]},
                            "message": (
                                f"班次 {trip_id}（{trip['route']}）于 "
                                f"{to_hhmm(trip['stops'][j - 1]['time'])} 到达 "
                                f"{self.sname(seg_from)} 后，前方 "
                                f"{self.sname(seg_from)}→{self.sname(seg_to)} 站段"
                                f"被线路区段停运封锁（{detail}），车辆无法穿越；"
                                f"本次乘车最远可在 {self.sname(seg_from)} 下车，"
                                "之后需步行或换乘其他班次。"
                            ),
                        })
                        break
                    target = trip["stops"][j]["station"]
                    arrive_at = trip["stops"][j]["time"]
                    open_alight, win_alight = self.station_open(target, arrive_at)
                    if not open_alight:
                        record({
                            "kind": "station_closed", "action": "alight",
                            "station": target, "time": arrive_at,
                            "trip_id": trip_id, "window": win_alight,
                            "message": (
                                f"班次 {trip_id}（{trip['route']}）于 {to_hhmm(arrive_at)} "
                                f"到达 {self.sname(target)}，该站正处于临时停运时段"
                                f"{self._window_text(win_alight)}，班次跳站通过，不能下车。"
                            ),
                        })
                        new_segments.add(j - 1)
                        continue
                    new_segments.add(j - 1)
                    try_extend(
                        label, target, arrive_at, label["rides"] + 1,
                        label["walk"],
                        label["ridden"] | frozenset(
                            (trip_id, s) for s in new_segments),
                        {"type": "ride", "trip_id": trip_id,
                         "route": trip["route"], "from": station,
                         "to": target, "depart": dep, "arrive": arrive_at,
                         "minutes": arrive_at - dep})

        candidates = buckets[destination]
        if not candidates:
            return {"status": "blocked",
                    "blocker": self._first_blocker(blockers, origin,
                                                   destination, earliest,
                                                   start_noun=start_noun,
                                                   start_action=start_action),
                    "explored": explored}

        best = min(candidates, key=lambda lb: (
            tuple([{"arrival": lb["arrival"],
                    "transfers": max(0, lb["rides"] - 1),
                    "walking": lb["walk"]}[f] for f in order]),
            lb["uid"]))
        return {"status": "ok", "chain": best["chain"], "rides": best["rides"],
                "walk": best["walk"], "arrival": best["arrival"],
                "explored": explored, "blockers": blockers}

    # ------------------------------------------------------------------ #
    def _first_blocker(self, blockers: dict, origin: str,
                       destination: str, earliest: int,
                       start_noun: str = "起点站",
                       start_action: str = "start") -> dict:
        items = list(blockers.values())

        # 结构性阻塞：出发站根本没有班次服务
        if not self.n["departures"].get(origin):
            return {
                "kind": "no_service", "action": start_action, "station": origin,
                "time": earliest, "trip_id": None, "window": None,
                "message": f"{start_noun} {self.sname(origin)} 没有任何经停班次，无法出发。",
            }
        later_exists = any(
            self.n["trips"][tid]["stops"][idx]["time"] >= earliest
            for tid, idx in self.n["departures"].get(origin, ())
        )
        if not later_exists and not any(
                b.get("station") == origin for b in items):
            return {
                "kind": "no_service_after", "action": start_action,
                "station": origin, "time": earliest, "trip_id": None,
                "window": None,
                "message": (
                    f"在 {to_hhmm(earliest)} 之后已无从 {self.sname(origin)} "
                    "发车的班次，当日运营结束。"
                ),
            }
        if not self.n["departures"].get(destination):
            return {
                "kind": "no_service", "action": "end", "station": destination,
                "time": earliest, "trip_id": None, "window": None,
                "message": f"终点站 {self.sname(destination)} 没有任何经停班次，无法到达。",
            }
        if items:
            kind_rank = {"origin_closed": 0, "station_closed": 1,
                         "segment_suspended": 2, "route_suspended": 3,
                         "no_service_after": 4}
            first = min(items, key=lambda b: (
                b.get("time", earliest), str(b.get("station", "")),
                str(b.get("trip_id", "")), kind_rank.get(b["kind"], 9)))
            return first
        return {
            "kind": "unreachable", "action": "end", "station": destination,
            "time": earliest, "trip_id": None, "window": None,
            "message": (
                f"在给定的班次、步行换乘与停运条件下，{self.sname(origin)} 无法到达 "
                f"{self.sname(destination)}（两区间不存在连通路径）。"
            ),
        }

    def _blocked(self, blocker: dict) -> dict:
        return {"status": "blocked", "blocker": blocker}

    def _truncation_note(self, leg: dict) -> str | None:
        """若乘车段恰因线路区段停运而在封锁段起点站终止，返回说明文字。"""
        trip = self.n["trips"].get(leg["trip_id"])
        if not trip:
            return None
        stops = trip["stops"]
        alight_idx = next(
            (k for k, st in enumerate(stops)
             if st["station"] == leg["to"] and st["time"] == leg["arrive"]),
            None)
        if alight_idx is None or alight_idx >= len(stops) - 1:
            return None
        clo = self.segment_closure(leg["trip_id"], alight_idx)
        if clo is None:
            return None
        return (
            f"受 {trip['route']} 区段停运（{self.sname(clo['from'])}→"
            f"{self.sname(clo['to'])} {to_hhmm(clo['start'])}–"
            f"{to_hhmm(clo['end'])}）影响，本次乘车在 "
            f"{self.sname(leg['to'])} 终止，需步行或换乘其他班次继续。")

    # ------------------------------------------------------------------ #
    def _itinerary(self, origin: str, destination: str, earliest: int,
                   preference: str, chain: tuple, rides: int,
                   walk: int = 0, arrival: int | None = None) -> dict:
        legs: list[dict] = []
        cursor = earliest
        total_wait = 0
        total_walk = 0
        for step in chain:
            if step["depart"] > cursor:
                wait = step["depart"] - cursor
                total_wait += wait
                legs.append({"type": "wait", "station": step["from"],
                             "start": cursor, "end": step["depart"],
                             "minutes": wait})
            leg = dict(step)
            if leg["type"] == "ride":
                note = self._truncation_note(leg)
                if note:
                    leg["truncated"] = True
                    leg["truncation"] = note
            legs.append(leg)
            if step["type"] == "walk":
                total_walk += step["minutes"]
            cursor = step["arrive"]
        if arrival is None:
            arrival = cursor
        return {
            "origin": origin,
            "destination": destination,
            "earliest": earliest,
            "preference": preference,
            "preference_label": PREFERENCE_LABELS[preference],
            "arrival": arrival,
            "rides": rides,
            "transfers": max(0, rides - 1),
            "walking_minutes": walk or total_walk,
            "waiting_minutes": total_wait,
            "legs": legs,
        }

    # ------------------------------------------------------------------ #
    # 行程续程：停运 / 班次调整后从已走完的路段接续
    # ------------------------------------------------------------------ #
    def continue_trip(self, original: dict, destination: str | None,
                      completed_index: int, current_time: int,
                      preference: str) -> dict:
        """以原时间线中最后完成的乘车/步行段为界发起续程。

        - 已完成路段与候车记录原样沿用，只核对站点与时间连续性，不因更新后
          网络中的新停运 / 班次调整而抹掉已走事实；
        - 当前时刻不得早于最后完成段的到达时刻，差值计为在所在站的候车；
        - 从所在站与当前时刻在更新后的网络上搜索余程，整程的换乘、步行、
          候车统一累计；同一班次绕行后重乘时不得回到已乘过的站段；
        - 接续无解时返回完成部分（含等待至当前时刻）与余程首个阻塞约束。
        """
        if not isinstance(original, dict):
            raise PlanError("原行程必须为规划结果中的 itinerary 对象")
        legs = original.get("legs")
        if not isinstance(legs, list) or not legs:
            raise PlanError("原行程缺少时间线 legs，无法续程")
        if isinstance(completed_index, bool) or not isinstance(completed_index, int):
            raise PlanError("完成段下标 completed_index 必须为整数")
        if not 0 <= completed_index < len(legs):
            raise PlanError(
                f"完成段下标超出时间线范围（0–{len(legs) - 1}）：{completed_index}")
        chosen = legs[completed_index]
        if not isinstance(chosen, dict) or chosen.get("type") not in ("ride", "walk"):
            raise PlanError("最后完成的一段必须是乘车段或步行段（候车段不可选）")
        if preference not in PREFERENCE_FIELDS:
            raise PlanError(f"未知偏好：{preference}")

        if destination is None:
            destination = original.get("destination", "")
        destination = str(destination).strip()
        if not destination:
            raise PlanError("缺少终点站")
        if destination not in self.names:
            raise PlanError(f"终点站不存在：{destination}")

        # ---- 核对并固化已完成前缀（站点 / 时间连续性）----
        prefix, origin, last_arrival = self._validate_completed_prefix(
            legs, completed_index)

        if current_time < last_arrival:
            raise PlanError(
                f"当前时刻 {to_hhmm(current_time)} 早于最后完成段的到达时刻 "
                f"{to_hhmm(last_arrival)}，时间不能倒流")

        at_station = prefix[-1]["to"]

        earliest = original.get("earliest")
        if isinstance(earliest, str):
            earliest = parse_time(earliest, "原行程最早出发时刻")
        elif not isinstance(earliest, int):
            first = prefix[0]
            earliest = first.get("start", first.get("depart", 0))

        # 已到达（新）终点：余程为空，完成段之后不再产生候车
        if at_station == destination:
            itin = self._continue_itinerary(
                origin, destination, earliest, preference, prefix, None,
                last_arrival, (), last_arrival)
            return {"status": "ok", "explored": 0, "itinerary": itin}

        # 完成段至当前时刻的等待：在所在站等待，属于已发生事实
        gap: dict | None = None
        if current_time > last_arrival:
            gap = {"type": "wait", "station": at_station,
                   "start": last_arrival, "end": current_time,
                   "minutes": current_time - last_arrival,
                   "completed": True}

        # 已乘过的站段映射到更新后的同班次经停序列，余程不得重乘
        ridden = self._ridden_from_prefix(prefix)

        search = self._search_core(
            at_station, destination, current_time, preference, ridden,
            start_noun="当前所在站", start_action="continue",
            start_window_action="继续出行")

        if search["status"] == "blocked":
            itin = self._continue_itinerary(
                origin, destination, earliest, preference, prefix, gap,
                current_time, (), None)
            return {"status": "blocked", "blocker": search["blocker"],
                    "itinerary": itin}

        itin = self._continue_itinerary(
            origin, destination, earliest, preference, prefix, gap,
            current_time, search["chain"], search["arrival"])
        return {"status": "ok", "explored": search["explored"],
                "itinerary": itin}

    def _validate_completed_prefix(self, legs: list, completed_index: int
                                   ) -> tuple[list[dict], str, int]:
        """核对 legs[:completed_index+1] 的站点与时间连续性。

        返回 (规范化后的完成段列表, 起点站, 最后到达时刻)。
        候车段沿用原记录；完成段涉及的站点必须仍存在于更新后的网络。
        """
        prefix: list[dict] = []
        cursor: int | None = None
        prev_station: str | None = None
        origin: str | None = None

        def tm(leg: dict, field: str, no: int) -> int:
            value = leg.get(field)
            if value is None:
                raise PlanError(f"原时间线第 {no} 段缺少时刻字段 {field}")
            return parse_time(value, f"原时间线第 {no} 段 {field}")

        def require_station(sid: str, no: int) -> str:
            sid = str(sid or "").strip()
            if not sid:
                raise PlanError(f"原时间线第 {no} 段缺少站点")
            if sid not in self.names:
                raise PlanError(
                    f"已完成部分引用了更新后网络中不存在的站点：{sid}，"
                    "已走事实保留，但请先在站点表中补回该站")
            return sid

        for k in range(completed_index + 1):
            no = k + 1
            leg = legs[k]
            if not isinstance(leg, dict):
                raise PlanError(f"原时间线第 {no} 段格式不合法")
            ltype = leg.get("type")
            if ltype == "wait":
                station = require_station(leg.get("station"), no)
                start = tm(leg, "start", no)
                end = tm(leg, "end", no)
                if end <= start:
                    raise PlanError(f"原时间线第 {no} 段候车时长必须为正")
                if cursor is not None and start != cursor:
                    raise PlanError(
                        f"已完成部分时间不连续：第 {no} 段候车始于 "
                        f"{to_hhmm(start)}，而上一段于 {to_hhmm(cursor)} 结束")
                if prev_station is not None and station != prev_station:
                    raise PlanError(
                        f"已完成部分站点不连续：第 {no} 段在 {station} 候车，"
                        f"而上一段终于 {prev_station}")
                prefix.append({"type": "wait", "station": station,
                               "start": start, "end": end,
                               "minutes": end - start, "completed": True})
                cursor, prev_station = end, station
                if origin is None:
                    origin = station
                continue

            if ltype not in ("ride", "walk"):
                raise PlanError(
                    f"原时间线第 {no} 段类型必须是 wait / ride / walk：{ltype!r}")
            frm = require_station(leg.get("from"), no)
            to = require_station(leg.get("to"), no)
            if frm == to:
                raise PlanError(f"原时间线第 {no} 段的起终点相同：{frm}")
            depart = tm(leg, "depart", no)
            arrive = tm(leg, "arrive", no)
            if arrive <= depart:
                raise PlanError(f"原时间线第 {no} 段到达时刻必须晚于出发时刻")
            if cursor is not None and depart != cursor:
                raise PlanError(
                    f"已完成部分时间不连续：第 {no} 段于 {to_hhmm(depart)} 出发，"
                    f"而上一段于 {to_hhmm(cursor)} 结束")
            if prev_station is not None and frm != prev_station:
                raise PlanError(
                    f"已完成部分站点不连续：第 {no} 段从 {frm} 出发，"
                    f"而上一段终于 {prev_station}")

            out: dict = {"type": ltype, "from": frm, "to": to,
                         "depart": depart, "arrive": arrive,
                         "minutes": arrive - depart, "completed": True}
            if ltype == "ride":
                trip_id = str(leg.get("trip_id", "")).strip()
                if not trip_id:
                    raise PlanError(f"原时间线第 {no} 段缺少班次 ID")
                out["trip_id"] = trip_id
                out["route"] = str(leg.get("route") or trip_id).strip() or trip_id
                # 原时间线中的区段截断标注是已走事实的一部分，原样保留
                if leg.get("truncated"):
                    out["truncated"] = True
                    if leg.get("truncation"):
                        out["truncation"] = str(leg["truncation"])
            prefix.append(out)
            cursor, prev_station = arrive, to
            if origin is None:
                origin = frm

        return prefix, origin or "", cursor or 0

    def _ridden_from_prefix(self, prefix: list[dict]) -> frozenset:
        """把完成前缀中的乘车段映射为更新后网络同班次经停下标区间。

        班次已取消或经停顺序调整到无法对应时，该段不施加重乘限制
        （已走事实本身仍原样保留在时间线中）。
        """
        ridden: set[tuple[str, int]] = set()
        for leg in prefix:
            if leg["type"] != "ride":
                continue
            trip = self.n["trips"].get(leg["trip_id"])
            if trip is None:
                continue
            seq = [s["station"] for s in trip["stops"]]
            if leg["from"] not in seq or leg["to"] not in seq:
                continue
            i, j = seq.index(leg["from"]), seq.index(leg["to"])
            if i >= j:
                continue
            for seg in range(i, j):
                ridden.add((leg["trip_id"], seg))
        return frozenset(ridden)

    def _continue_itinerary(self, origin: str, destination: str,
                            earliest: int, preference: str,
                            prefix: list[dict], gap: dict | None,
                            current_time: int, rest_chain: tuple,
                            arrival: int | None) -> dict:
        """拼装续程时间线：完成前缀 + 等待至当前时刻 + 余程。

        完成部分各段带 completed=true；余程段（含余程候车）不带该标记。
        """
        out_legs: list[dict] = list(prefix)
        if gap is not None:
            out_legs.append(gap)
        completed_count = len(out_legs)

        cursor = current_time
        for step in rest_chain:
            if step["depart"] > cursor:
                wait = step["depart"] - cursor
                out_legs.append({"type": "wait", "station": step["from"],
                                 "start": cursor, "end": step["depart"],
                                 "minutes": wait})
            leg = dict(step)
            if leg["type"] == "ride":
                note = self._truncation_note(leg)
                if note:
                    leg["truncated"] = True
                    leg["truncation"] = note
            out_legs.append(leg)
            cursor = step["arrive"]

        rides = sum(1 for l in out_legs if l["type"] == "ride")
        walking = sum(l["minutes"] for l in out_legs if l["type"] == "walk")
        waiting = sum(l["minutes"] for l in out_legs if l["type"] == "wait")

        at_station = prefix[-1]["to"]
        return {
            "continuation": True,
            "origin": origin,
            "destination": destination,
            "earliest": earliest,
            "preference": preference,
            "preference_label": PREFERENCE_LABELS[preference],
            "resume_station": at_station,
            "resume_time": current_time,
            "completed_count": completed_count,
            "arrival": arrival,
            "rides": rides,
            "transfers": max(0, rides - 1),
            "walking_minutes": walking,
            "waiting_minutes": waiting,
            "legs": out_legs,
        }
