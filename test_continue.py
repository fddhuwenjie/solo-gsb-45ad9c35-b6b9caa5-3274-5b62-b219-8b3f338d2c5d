"""行程续程（/api/continue 对应的 Planner.continue_trip）行为验证脚本。"""

import json
import sys

sys.path.insert(0, "/workspace")

from planner import PlanError, Planner, normalize


def m(hhmm):
    h, mm = map(int, hhmm.split(":"))
    return h * 60 + mm


# 原网络：1路 B→E 区段封锁，T1 在 B 截断；T2 从 B 接续到 E
def network(**over):
    base = {
        "stations": [
            {"id": "A", "name": "甲站"}, {"id": "B", "name": "乙站"},
            {"id": "C", "name": "丙站"}, {"id": "D", "name": "丁站"},
            {"id": "E", "name": "戊站"},
        ],
        "trips": [
            {"id": "T1", "route": "1路", "stops": [
                {"station": "A", "time": "08:00"},
                {"station": "B", "time": "08:10"},
                {"station": "C", "time": "08:20"},
                {"station": "D", "time": "08:30"},
                {"station": "E", "time": "08:40"}]},
            {"id": "T2", "route": "2路", "stops": [
                {"station": "B", "time": "08:25"},
                {"station": "E", "time": "08:50"}]},
        ],
        "transfers": [],
        "suspensions": [{"kind": "segment", "route": "1路", "from": "B",
                         "to": "E", "start": "00:00", "end": "23:59",
                         "label": "区间施工"}],
    }
    base.update(over)
    return base


def original_itinerary(data=None, earliest="07:00"):
    data = data or network()
    res = Planner(normalize(data)).plan("A", "E", m(earliest), "fastest")
    assert res["status"] == "ok", json.dumps(res, ensure_ascii=False)
    return res["itinerary"]


passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


def cont(itin, data=None, now="08:15", idx=None, **kw):
    data = data if data is not None else network()
    if idx is None:
        idx = next(i for i, l in enumerate(itin["legs"]) if l["type"] == "ride")
    return Planner(normalize(data)).continue_trip(
        itin, None, idx, m(now), kw.get("preference", "fastest"))


print("== 1. 正常续程：完成部分原样保留，余程自所在站当前时刻搜索 ==")
itin = original_itinerary()
r = cont(itin, now="08:15")
check("续程成功，08:50 乘 T2 到达", r["status"] == "ok"
      and r["itinerary"]["arrival"] == m("08:50"),
      json.dumps(r, ensure_ascii=False))
legs = r["itinerary"]["legs"]
check("时间线 = 原候车/原乘车/等到当前/余程候车/余程乘车",
      [(l["type"]) for l in legs] == ["wait", "ride", "wait", "wait", "ride"],
      json.dumps(legs, ensure_ascii=False))
done = [l for l in legs if l.get("completed")]
check("恰好 3 段已完成（原候车、原乘车、08:10→08:15 等待）", len(done) == 3)
check("完成段乘车保留截断说明", done[1].get("truncated") is True
      and "区段停运" in done[1].get("truncation", ""))
check("余程段不带 completed", all(not l.get("completed") for l in legs[3:]))
check("续程元信息", r["itinerary"]["continuation"] is True
      and r["itinerary"]["resume_station"] == "B"
      and r["itinerary"]["resume_time"] == m("08:15"))

print("== 2. 整程统计累计（换乘/步行/候车）==")
check("乘坐 2 个班次、换乘 1 次",
      r["itinerary"]["rides"] == 2 and r["itinerary"]["transfers"] == 1)
# 原候车 60 + 08:10→08:15 等 5 + 08:15→08:25 等 10
check("候车分钟整程累计 = 75", r["itinerary"]["waiting_minutes"] == 75)
check("步行分钟 = 0", r["itinerary"]["walking_minutes"] == 0)

# 当前时刻恰为最后到达时刻：不补“完成段→当前时刻”的等待段
r0 = cont(itin, now="08:10")
check("now=到达时刻时不产生额外等待段（完成段 2 个，候车仍只含余程候车）",
      sum(1 for l in r0["itinerary"]["legs"] if l.get("completed")) == 2
      and r0["itinerary"]["waiting_minutes"] == 60 + 15)

# 步行完成段：步行分钟累计
wdata = network(
    trips=[
        {"id": "T1", "route": "1路", "stops": [
            {"station": "A", "time": "08:00"}, {"station": "B", "time": "08:10"},
            {"station": "C", "time": "08:20"}, {"station": "D", "time": "08:30"},
            {"station": "E", "time": "08:40"}]}],
    transfers=[{"from": "B", "to": "D", "minutes": 15}],
    suspensions=[{"kind": "segment", "route": "1路", "from": "B", "to": "D",
                  "start": "00:00", "end": "23:59"}],
)
wit = original_itinerary(wdata)
widx = next(i for i, l in enumerate(wit["legs"]) if l["type"] == "walk")
wnew = json.loads(json.dumps(wdata))
wnew["trips"].append({"id": "T9", "route": "9路", "stops": [
    {"station": "D", "time": "08:33"}, {"station": "E", "time": "08:36"}]})
r = Planner(normalize(wnew)).continue_trip(wit, None, widx, m("08:28"), "fastest")
check("步行段作为完成段，余程乘 T9", r["status"] == "ok"
      and r["itinerary"]["arrival"] == m("08:36")
      and [l["trip_id"] for l in r["itinerary"]["legs"] if l["type"] == "ride"]
      == ["T1", "T9"], json.dumps(r, ensure_ascii=False))
check("步行分钟累计 = 15", r["itinerary"]["walking_minutes"] == 15)

print("== 3. 接续无解：展示完成部分 + 阻塞原因，未走段不标完成 ==")
blocked_data = network()
blocked_data["suspensions"].append(
    {"kind": "route", "ref": "2路", "start": "00:00", "end": "23:59"})
r = cont(itin, data=blocked_data, now="08:15")
check("状态 blocked 且阻塞指向 T2 整线停运", r["status"] == "blocked"
      and r["blocker"]["kind"] == "route_suspended"
      and r["blocker"]["trip_id"] == "T2",
      json.dumps(r, ensure_ascii=False))
blegs = r["itinerary"]["legs"]
check("只含完成部分（原候车/原乘车/等到当前），全部 completed",
      [l["type"] for l in blegs] == ["wait", "ride", "wait"]
      and all(l.get("completed") for l in blegs))
check("blocked 时 arrival 为 null", r["itinerary"]["arrival"] is None)
check("完成段截断说明仍保留", blegs[1].get("truncated") is True)

print("== 4. 输入校验 ==")
def expect_err(name, fn, frag):
    global passed, failed
    try:
        fn()
    except PlanError as exc:
        if frag in str(exc):
            passed += 1
            print(f"  ✓ {name}")
        else:
            failed += 1
            print(f"  ✗ {name}  {exc}")
        return
    failed += 1
    print(f"  ✗ {name}  未报错")


ride_idx = next(i for i, l in enumerate(itin["legs"]) if l["type"] == "ride")
expect_err("当前时刻早于最后到达时刻",
           lambda: cont(itin, now="08:05"), "时间不能倒流")
expect_err("候车段不可选为完成段",
           lambda: cont(itin, now="08:15", idx=0), "候车段不可选")
expect_err("完成段下标越界",
           lambda: cont(itin, now="08:15", idx=99), "超出时间线范围")
expect_err("非整数下标",
           lambda: Planner(normalize(network())).continue_trip(
               itin, None, 1.5, m("08:15"), "fastest"),
           "completed_index 必须为整数")
expect_err("终点不存在",
           lambda: Planner(normalize(network())).continue_trip(
               itin, "ZZ", ride_idx, m("08:15"), "fastest"),
           "终点站不存在")

bad = json.loads(json.dumps(itin))
bad["legs"][ride_idx]["arrive"] = m("07:55")
expect_err("完成部分时间不连续（到达早于出发）",
           lambda: cont(bad, now="08:15"), "到达时刻必须晚于出发时刻")
bad = json.loads(json.dumps(itin))
bad["legs"][ride_idx]["from"] = "C"
expect_err("完成部分站点不连续",
           lambda: cont(bad, now="08:15"), "站点不连续")

missing_a = network()
missing_a["stations"] = [s for s in missing_a["stations"] if s["id"] != "A"]
for t in missing_a["trips"]:
    t["stops"] = [s for s in t["stops"] if s["station"] != "A"]
expect_err("完成段站点在新网络中缺失",
           lambda: cont(itin, data=missing_a, now="08:15"), "不存在的站点")

print("== 5. 重乘同一班次：不得回到已乘站段，允许向前乘未乘段 ==")
# 调整后 T1 在 B 的发车为 08:15，B→E 08:18；另给走回 A 的 3 分钟步行。
# 若允许重乘 A→B，可 08:13 走回 A 赶 08:14 的车——违规更快，必须禁止；
# 在 B 直接向前乘未乘过的 B→E 是允许的。
reboard = {
    "stations": [{"id": s, "name": s} for s in ("A", "B", "E")],
    "trips": [{"id": "T1", "route": "1路", "stops": [
        {"station": "A", "time": "08:14"}, {"station": "B", "time": "08:15"},
        {"station": "E", "time": "08:18"}]}],
    "transfers": [{"from": "B", "to": "A", "minutes": 3},
                  {"from": "B", "to": "E", "minutes": 12}],
    "suspensions": [],
}
r = Planner(normalize(reboard)).continue_trip(
    itin, None, ride_idx, m("08:10"), "fastest")
check("在 B 直接乘同班未乘段 B→E，08:18 到达",
      r["status"] == "ok" and r["itinerary"]["arrival"] == m("08:18"),
      json.dumps(r, ensure_ascii=False))
t1_rides = [l for l in r["itinerary"]["legs"]
            if l["type"] == "ride" and l["trip_id"] == "T1"]
check("T1 出现两次：已完成 A→B + 余程 B→E（无 A→B 回头段）",
      [(l["from"], l["to"], bool(l.get("completed"))) for l in t1_rides]
      == [("A", "B", True), ("B", "E", False)],
      json.dumps(t1_rides, ensure_ascii=False))
check("不存在走回 A 的步行段",
      not any(l["type"] == "walk" and l["to"] == "A"
              for l in r["itinerary"]["legs"]))

print("== 6. 班次删除 / 时刻调整不抹掉已走事实 ==")
deleted_t1 = network()
deleted_t1["trips"] = [t for t in deleted_t1["trips"] if t["id"] != "T1"]
deleted_t1["suspensions"] = []  # 引用 1路 的区段记录随班次一并删除
r = cont(itin, data=deleted_t1, now="08:15")
check("T1 删除后余程仍可乘 T2", r["status"] == "ok"
      and r["itinerary"]["arrival"] == m("08:50"),
      json.dumps(r, ensure_ascii=False))
rides = [l for l in r["itinerary"]["legs"] if l["type"] == "ride"]
check("已完成 T1 段原样保留，余程为 T2",
      rides[0]["trip_id"] == "T1" and rides[0].get("completed")
      and rides[1]["trip_id"] == "T2" and not rides[1].get("completed"))

# 时刻调整使 T1 在 08:15 前已驶离 B：已完成段事实不变，余程不受影响
shifted = network()
shifted["trips"][0]["stops"] = [
    {"station": "A", "time": "07:55"}, {"station": "B", "time": "08:05"},
    {"station": "C", "time": "08:15"}, {"station": "D", "time": "08:25"},
    {"station": "E", "time": "08:35"}]
r = cont(itin, data=shifted, now="08:15")
check("T1 调整时刻后已走 A→B 沿用，余程乘 T2",
      r["status"] == "ok" and r["itinerary"]["arrival"] == m("08:50"))

print("== 7. 已在（新）终点：余程为空，不产生等待 ==")
r = Planner(normalize(network())).continue_trip(
    itin, "B", ride_idx, m("08:30"), "fastest")
check("到终点 B：到达时刻沿用 08:10，全部段完成",
      r["status"] == "ok" and r["itinerary"]["arrival"] == m("08:10")
      and all(l.get("completed") for l in r["itinerary"]["legs"])
      and r["itinerary"]["waiting_minutes"] == 60,
      json.dumps(r, ensure_ascii=False))

print(f"\n通过 {passed}，失败 {failed}")
sys.exit(1 if failed else 0)
