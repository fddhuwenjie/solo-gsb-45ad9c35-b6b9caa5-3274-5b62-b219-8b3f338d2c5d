"""续程（continue_trip）行为验证脚本。"""

import copy
import json
import sys

sys.path.insert(0, "/workspace")

from planner import SAMPLE_DATA, PlanError, Planner, normalize


passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


def line_net(**over):
    base = {
        "stations": [{"id": x} for x in "ABCDE"],
        "trips": [{
            "id": "T1", "route": "1路", "stops": [
                {"station": "A", "time": "08:00"},
                {"station": "B", "time": "08:10"},
                {"station": "C", "time": "08:20"},
                {"station": "D", "time": "08:30"},
                {"station": "E", "time": "08:40"},
            ],
        }],
        "transfers": [],
        "suspensions": [],
    }
    base.update(over)
    return base


def t(minutes):
    return minutes


def planner(data):
    return Planner(normalize(data))


print("== 1. 已完成段与候车原样沿用，余程重新搜索 ==")
# 原行程：停运发生前规划的 A->E（单一乘车段 T1 A->E）；途中 T1 在 B 被截断，
# 人实际于 08:10 到达 B。续程时新网络的停运已解除。
closed = [{"kind": "segment", "route": "1路", "from": "B", "to": "D",
           "start": "00:00", "end": "23:59"}]
p_ok = planner(line_net())
it = p_ok.plan("A", "E", 7 * 60, "fastest")["itinerary"]
ride_idx = max(i for i, l in enumerate(it["legs"]) if l["type"] == "ride")
check("原行程为 A->E 直达", it["legs"][ride_idx]["to"] == "E",
      json.dumps(it, ensure_ascii=False))
r = p_ok.continue_trip(it, ride_idx, t(8 * 60 + 10), "fastest",
                       completed_station="B")
check("续程成功", r["status"] == "ok", json.dumps(r, ensure_ascii=False))
ci = r["itinerary"]
check("整程到达 08:40", ci["arrival"] == 8 * 60 + 40,
      json.dumps(ci["arrival"]))
check("同一车辆继续向前不算新增乘坐/换乘",
      ci["rides"] == 1 and ci["transfers"] == 0,
      json.dumps({"rides": ci["rides"]}))
done_legs = [l for l in ci["legs"] if l.get("done")]
todo = [l for l in ci["legs"] if not l.get("done")]
check("完成段被截到实际到达站 B 且标完成",
      any(l["type"] == "ride" and l["to"] == "B" for l in done_legs))
check("未走余程不标完成", all(not l.get("done") for l in todo))
check("无额外等待时不插入 interim 段",
      not any(l.get("interim") for l in ci["legs"]))

print("== 1b. 完成段至当前时刻的等待计入整程候车 ==")
net1b = line_net(trips=line_net()["trips"] + [{
    "id": "T2", "route": "2路", "stops": [
        {"station": "B", "time": "08:25"},
        {"station": "E", "time": "08:45"}],
}])
r1b = planner(net1b).continue_trip(it, ride_idx, t(8 * 60 + 15), "fastest",
                                   completed_station="B")
check("等待后续乘 T2 成功", r1b["status"] == "ok"
      and r1b["itinerary"]["arrival"] == 8 * 60 + 45
      and r1b["itinerary"]["rides"] == 2,
      json.dumps(r1b, ensure_ascii=False))
check("interim 等待 5 分钟计入整程候车",
      any(l.get("interim") and l["minutes"] == 5
          for l in r1b["itinerary"]["legs"])
      and r1b["itinerary"]["waiting_minutes"] >= 5,
      str(r1b["itinerary"]["waiting_minutes"]))

print("== 1c. completed_station 必须是该班次乘车段内的经停站 ==")
for bad_cs in ("A", "E_NA", None):
    try:
        if bad_cs is None:
            # 不填中途站时，完成段整段 A->E 视为已完成，到达时间 08:40
            rr = p_ok.continue_trip(it, ride_idx, t(8 * 60 + 40), "fastest")
            check("缺省按整段完成、直接到达终点", rr["status"] == "ok"
                  and rr["itinerary"]["arrival"] == 8 * 60 + 40
                  and rr["explored"] == 0,
                  json.dumps(rr, ensure_ascii=False))
        else:
            p_ok.continue_trip(it, ride_idx, t(8 * 60 + 10), "fastest",
                               completed_station=bad_cs)
            check(f"非法实际到达站 {bad_cs} 报错", False)
    except PlanError as exc:
        check(f"非法实际到达站 {bad_cs} 报错", "经停站" in str(exc), str(exc))

print("== 2. 当前时刻不得早于最后到达时刻 ==")
try:
    p_ok.continue_trip(it, ride_idx, t(8 * 60 + 5), "fastest",
                       completed_station="B")
    check("早于到达时刻报错", False)
except PlanError as exc:
    check("早于到达时刻报错", "早于最后到达时刻" in str(exc), str(exc))
r = p_ok.continue_trip(it, ride_idx, t(8 * 60 + 10), "fastest",
                       completed_station="B")
check("当前时刻=到达时刻合法", r["status"] == "ok",
      json.dumps(r, ensure_ascii=False))

print("== 3. 只能选定乘车/步行段 ==")
wait_idx = next(i for i, l in enumerate(it["legs"]) if l["type"] == "wait")
for bad in (wait_idx, -1, len(it["legs"]), 0.5, "1", True, None):
    try:
        p_ok.continue_trip(it, bad, t(9 * 60), "fastest",
                           completed_station="B")
        check(f"非法完成段 {bad!r} 报错", False)
    except PlanError:
        check(f"非法完成段 {bad!r} 报错", True)

print("== 4. 站点/时间连续性核对 ==")
bad = copy.deepcopy(it)
bad["legs"][ride_idx]["from"] = "C"
try:
    p_ok.continue_trip(bad, ride_idx, t(9 * 60), "fastest",
                       completed_station="B")
    check("起点站不一致报错", False)
except PlanError as exc:
    check("起点站不一致报错", "不一致" in str(exc), str(exc))

bad = copy.deepcopy(it)
bad["legs"][ride_idx]["depart"] = 7 * 60
try:
    p_ok.continue_trip(bad, ride_idx, t(9 * 60), "fastest",
                       completed_station="B")
    check("时间倒挂报错", False)
except PlanError as exc:
    check("时间倒挂报错", "不连续" in str(exc), str(exc))

bad = copy.deepcopy(it)
bad["legs"][0]["station"] = "E"
try:
    p_ok.continue_trip(bad, ride_idx, t(9 * 60), "fastest",
                       completed_station="B")
    check("候车站点不一致报错", False)
except PlanError as exc:
    check("候车站点不一致报错", "候车站点" in str(exc), str(exc))

print("== 5. 绕行后重乘同一班次，不得回到已乘过的站段 ==")
# 新网络停运仍在：从 B 步行绕行到 D（15 分钟，08:25 到），08:30 重乘同班到 E
data5 = line_net(
    transfers=[{"from": "B", "to": "D", "minutes": 15}],
    suspensions=closed,
)
p5 = planner(data5)
r5 = p5.continue_trip(it, ride_idx, t(8 * 60 + 10), "fastest",
                      completed_station="B")
check("绕行重乘同班成功", r5["status"] == "ok",
      json.dumps(r5, ensure_ascii=False))
rides5 = [(l["trip_id"], l["from"], l["to"])
          for l in r5["itinerary"]["legs"] if l["type"] == "ride"]
check("只出现 A->B 与 D->E，未回到 B->C/C->D",
      rides5 == [("T1", "A", "B"), ("T1", "D", "E")], str(rides5))
check("步行 15 分钟，重乘为第二次乘坐（1 次换乘）",
      r5["itinerary"]["rides"] == 2
      and r5["itinerary"]["walking_minutes"] == 15,
      json.dumps(r5["itinerary"], ensure_ascii=False))

# 即便没有步行绕行，也不得在 B 重乘同班穿过 B->C（已乘规则）；
# 且无其他出路时应 blocked 而非回到已乘站段
data5b = line_net(suspensions=closed)
r5b = planner(data5b).continue_trip(it, ride_idx, t(8 * 60 + 10), "fastest",
                                    completed_station="B")
check("无绕行时余程 blocked", r5b["status"] == "blocked",
      json.dumps(r5b, ensure_ascii=False))
bad_rides = [(l["from"], l["to"]) for l in r5b["itinerary"]["legs"]
             if l["type"] == "ride" and not l.get("done")]
check("blocked 结果不含任何未完成的乘车段", bad_rides == [], str(bad_rides))

print("== 6. 余程无解：展示完成部分 + 阻塞原因，未走路段不标完成 ==")
data6 = line_net(suspensions=[
    {"kind": "segment", "route": "1路", "from": "B", "to": "E",
     "start": "00:00", "end": "23:59"},
])
p6 = planner(data6)
r6 = p6.continue_trip(it, ride_idx, t(8 * 60 + 10), "fastest",
                      completed_station="B")
check("余程 blocked", r6["status"] == "blocked",
      json.dumps(r6, ensure_ascii=False))
check("返回阻塞原因", bool(r6.get("blocker", {}).get("message")))
legs6 = r6["itinerary"]["legs"]
check("已完成 A->B 仍展示且标完成",
      any(l["type"] == "ride" and l["to"] == "B" and l.get("done")
          for l in legs6))
check("不存在任何未走却标完成的段",
      not any(l["type"] in ("ride", "walk") and not l.get("done")
              for l in legs6),
      str([(l["type"], l.get("done")) for l in legs6]))
check("blocked 时 arrival 为 null", r6["itinerary"]["arrival"] is None)
check("continuation 给出所在站/当前时刻",
      r6["continuation"]["current_station"] == "B"
      and r6["continuation"]["current_time"] == 8 * 60 + 10)


print("== 7. 新增停运不抹掉已走事实（示例数据）==")
p7 = planner(SAMPLE_DATA)
it7 = p7.plan("S1", "S6", 8 * 60 + 30, "fastest")["itinerary"]
k_idx = next(i for i, l in enumerate(it7["legs"])
             if l["type"] == "ride" and l["to"] == "S4")
new_net = copy.deepcopy(SAMPLE_DATA)
new_net["suspensions"].append(
    {"kind": "station", "ref": "S5", "start": "00:00", "end": "23:59",
     "label": "新增封站"})
r7 = planner(new_net).continue_trip(it7, k_idx, t(8 * 60 + 55), "fastest")
check("S5 新增封站后余程 blocked", r7["status"] == "blocked",
      json.dumps(r7, ensure_ascii=False))
pref = r7["itinerary"]["legs"]
check("K2b S1->S4 完成事实保留（含截断标注）",
      any(l["type"] == "ride" and l.get("trip_id") == "K2b"
          and l["to"] == "S4" and l.get("done")
          and l.get("truncated") for l in pref),
      json.dumps(pref, ensure_ascii=False))

print("== 8. 步行段作为完成段、累计指标整程化 ==")
# 完成步行 S4->S5（09:02 到达），随后在 S5 乘 K4
r8 = p7.continue_trip(it7, 2, t(9 * 60 + 2), "fastest")
check("步行段续程成功", r8["status"] == "ok"
      and r8["itinerary"]["arrival"] == 9 * 60 + 12,
      json.dumps(r8, ensure_ascii=False))
check("所在站为 S5，累计步行 7 分钟",
      r8["continuation"]["current_station"] == "S5"
      and r8["itinerary"]["walking_minutes"] == 7,
      json.dumps(r8["continuation"], ensure_ascii=False))

print("== 9. 所在站在新网络缺失 / 终点站缺失 ==")
net_no_s4 = copy.deepcopy(SAMPLE_DATA)
net_no_s4["trips"] = [t0 for t0 in net_no_s4["trips"]
                      if all(s["station"] != "S4" for s in t0["stops"])]
net_no_s4["stations"] = [s0 for s0 in net_no_s4["stations"]
                         if s0["id"] != "S4"]
net_no_s4["transfers"] = [x for x in net_no_s4["transfers"]
                          if "S4" not in (x["from"], x["to"])]
net_no_s4["suspensions"] = [
    x for x in net_no_s4["suspensions"]
    if x.get("ref") != "S4" and "S4" not in (x.get("from"), x.get("to"))]
try:
    planner(net_no_s4).continue_trip(it7, k_idx, t(9 * 60), "fastest")
    check("所在站缺失报错", False)
except PlanError as exc:
    check("所在站缺失报错", "所在站" in str(exc), str(exc))

print("== 10. 偏好缺省沿用原行程 ==")
r10 = p7.continue_trip(it7, k_idx, t(8 * 60 + 55))
check("缺省偏好成功", r10["status"] == "ok"
      and r10["itinerary"]["preference"] == "fastest",
      json.dumps(r10, ensure_ascii=False))

print(f"\n通过 {passed}，失败 {failed}")
sys.exit(1 if failed else 0)
