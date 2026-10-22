"""将求解结果渲染为自包含的离线 HTML，包含地图、停靠明细和时间轴。"""
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path


def _timeline(routes: list[dict]) -> dict | None:
    """按无时区的业务当地时间构建时间轴，不受主机时区或夏令时影响。

    先行驶后等待只是展示约定，真实等待位置未知。
    零时长阶段保留边界，不为播放虚构持续时间。
    """
    parsed = []
    for route in routes:
        try:
            start = datetime.fromisoformat(route['departureTime'])
            end = datetime.fromisoformat(route['completionTime'])
            if start.tzinfo is not None or end.tzinfo is not None or end < start:
                raise ValueError('invalid local route times')
            previous = start
            segments = []
            for index, stop in enumerate(route['stops']):
                arrival = datetime.fromisoformat(stop['arrivalTime'])
                departure = datetime.fromisoformat(stop['departureTime'])
                wait = float(stop.get('waitingBeforeArrivalSeconds', 0))
                if not math.isfinite(wait) or wait < 0:
                    raise ValueError('invalid waiting duration')
                travel_end = arrival - timedelta(seconds=wait)
                if not previous <= travel_end <= arrival <= departure <= end:
                    raise ValueError('nonchronological stop')
                # 每次停靠拆成行驶、等待、卸货三段，避免等待被重复计算。
                for kind, a, b in (('travel', previous, travel_end),
                                   ('wait', travel_end, arrival), ('unload', arrival, departure)):
                    segments.append({'kind': kind, 'start': a, 'end': b, 'stopIndex': index})
                previous = departure
            if previous != end:
                raise ValueError('completion must match the last stop')
            parsed.append({'start': start, 'end': end, 'segments': segments})
        except (KeyError, TypeError, ValueError, OverflowError):
            # 单条路线时间异常只禁用该路线播放，不影响其余路线。
            parsed.append(None)
    valid = [r for r in parsed if r is not None]
    if not valid:
        return None
    # 全部有效路线共享最早出发时刻，支持不同发车时间和跨日播放。
    origin = min(r['start'] for r in valid)
    finish = max(r['end'] for r in valid)
    def offset(t):
        return (t - origin).total_seconds()
    return {'startTime': origin.isoformat(sep=' '), 'durationSeconds': offset(finish),
            'routes': [None if r is None else {
                'start': offset(r['start']), 'end': offset(r['end']),
                'segments': [{**seg, 'start': offset(seg['start']), 'end': offset(seg['end'])}
                             for seg in r['segments']]} for r in parsed]}



def _display_routes(payload: dict, routes: list[dict]) -> list[dict]:
    """仅在报告内部把逐 DN 输出合并为实际停靠，保持公开结果对象不变。

    eta 是开始卸货时间；等待由权威矩阵耗时反推，同站只计一次卸货。
    """
    if not any('siteDetails' in route for route in routes):
        return routes
    from .models import Request, group_key
    from .problem import prepare

    request = Request.model_validate(payload)
    prepared = prepare(request)
    orders = {dn.orderReleaseId: (index, dn) for index, dn in enumerate(prepared.dns, 1)}
    converted = []
    for index, route in enumerate(routes, 1):
        if 'siteDetails' not in route:
            converted.append(route)
            continue
        previous_time = datetime.fromisoformat(route['departure_date'])
        previous_node = 0
        stops = []
        for detail in route['siteDetails']:
            node, dn = orders[detail['siteAddress']]
            eta = datetime.fromisoformat(detail['eta'])
            # 相邻同站 DN 合并展示，并检查共用 eta；不重复增加卸货时长。
            if stops and stops[-1]['siteCode'] == dn.siteCode:
                if stops[-1]['arrivalTime'] != eta.isoformat(sep=' '):
                    raise ValueError('same-stop DNs must share eta')
                stops[-1]['orderReleaseIds'].append(dn.orderReleaseId)
            else:
                # 从两次卸货间隔扣除行驶时间，剩余部分是展示用等待时间。
                wait = (eta - previous_time).total_seconds() - prepared.duration[previous_node][node]
                if wait < 0:
                    raise ValueError('eta precedes unloading and travel completion')
                departure = eta + timedelta(seconds=prepared.service)
                stops.append({'siteCode': dn.siteCode, 'orderReleaseIds': [dn.orderReleaseId],
                              'arrivalTime': eta.isoformat(sep=' '),
                              'serviceStartTime': eta.isoformat(sep=' '),
                              'departureTime': departure.isoformat(sep=' '),
                              'waitingBeforeArrivalSeconds': wait, 'unloadingSeconds': prepared.service})
                previous_time = departure
            previous_node = node
        converted.append({'routeId': f'R{index}', 'vehicleTypeName': route['vehicle'],
                          'departureTime': route['departure_date'],
                          'completionTime': previous_time.isoformat(sep=' '),
                          'totalDistance': route['dist'], 'totalVolume': route['volume'],
                          'totalWeight': route['weight'], 'stops': stops,
                          'physicalStopCount': len(stops),
                          'siteCount': len({group_key(orders[dn][1], request.lonAndLatCombination)
                                            for dn in route['siteAddresses']})})
    return converted


def render_html(payload: dict, result: dict) -> str:
    """直接嵌入数据与前端资源，生成无需联网的报告。"""
    # 非法、非有限或越界坐标不绘制，坐标顺序统一为经度、纬度。
    def coordinate(lat, lon):
        try:
            lat, lon = float(lat), float(lon)
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                return [lon, lat]
        except (TypeError, ValueError, OverflowError):
            pass
        return None

    # 错误输入也要能展示状态，只提取结构合法的字典记录。
    def records(key):
        value = payload.get(key)
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    orders = {d.get('orderReleaseId'): d for d in records('dnSiteVoList')
              if isinstance(d.get('orderReleaseId'), str)}
    routes = []
    for route in _display_routes(payload, result.get('routes', [])):
        stops = []
        for stop in route.get('stops', []):
            points = [coordinate(orders.get(dn, {}).get('deliveryLatitude'),
                                 orders.get(dn, {}).get('deliveryLongitude'))
                      for dn in stop.get('orderReleaseIds', [])]
            valid = [p for p in points if p is not None]
            # 同站所有 DN 坐标必须齐全且一致，避免把冲突位置误画成一个点。
            point = valid[0] if valid and len(valid) == len(points) and all(p == valid[0] for p in valid) else None
            stops.append({**stop, 'coordinate': point})
        routes.append({**route, 'stops': stops})
    data = {'result': {**result, 'routes': routes},
            'depot': {'name': payload.get('sourceLocation', '仓库'),
                      'coordinate': coordinate(payload.get('sourceLatitude'), payload.get('sourceLongitude'))},
            'vehicles': records('vehicleTypeVoList'), 'timeline': _timeline(routes)}
    # 非有限数值转 null；嵌入前转义 HTML 特殊字符，防止数据提前闭合 script 标签。
    def finite(value):
        if isinstance(value, dict):
            return {k: finite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [finite(v) for v in value]
        if isinstance(value, (float, Decimal)) and not math.isfinite(value):
            return None
        return value

    encoded = json.dumps(finite(data), ensure_ascii=False, allow_nan=False,
                         default=lambda v: float(v) if isinstance(v, Decimal) else str(v))
    encoded = encoded.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return _HTML.replace('__REPORT_DATA__', encoded)


# 以 UTF-8 保存完整离线报告，页面无需额外资源文件。
def write_html(payload: dict, result: dict, path: str | Path) -> None:
    Path(path).write_text(render_html(payload, result), encoding='utf-8')


# 以下模板包含离线样式、页面结构和交互脚本；报告数据通过占位符嵌入。
_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DN Router · 配送结果</title>
<style>
/* 基础布局、路线配色及窄屏和打印适配。 */
:root{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#182c3b;background:#f2f5f7}*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:32px}h1{font-size:30px;margin:8px 0}h2{font-size:18px;margin:0 0 16px}p{line-height:1.6}.eyebrow{color:#16766f;letter-spacing:2px;font-size:12px;font-weight:700}.muted{color:#617584;font-size:13px}.top{display:flex;justify-content:space-between;align-items:center;gap:20px}.badge{padding:8px 14px;background:#e1eee9;color:#186758;border-radius:30px;font-size:13px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:24px 0}.metric,.panel{background:white;border:1px solid #dce4ea;border-radius:16px;padding:22px}.metric strong{display:block;font-size:29px;margin-top:10px}.layout{display:grid;grid-template-columns:300px 1fr;gap:20px}.routes{max-height:560px;overflow:auto}.route{display:block;width:100%;text-align:left;background:white;border:1px solid #dce4ea;border-left:5px solid var(--route,#16766f);padding:14px;margin:8px 0;border-radius:10px;cursor:pointer;color:inherit;font:inherit}.route[aria-pressed=true]{background:#eef5f7;outline:2px solid #244b60;outline-offset:-2px}.route strong,.route small{display:block;margin-bottom:6px}.maphead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}button{cursor:pointer}.reset{border:1px solid #cddae1;background:white;border-radius:8px;padding:8px 12px;color:#244b60}svg{display:block;width:100%;height:440px;background:#f7fafb;border-radius:10px;touch-action:none}svg text{font:12px sans-serif;fill:#263f50;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}.details{margin-top:20px}.route-detail{padding:18px 0;border-top:1px solid #e1e8ed}.route-detail h3{margin:0 0 10px}.loads{display:flex;gap:20px;flex-wrap:wrap;margin:12px 0}.load{min-width:180px;font-size:13px}progress{display:block;width:100%;accent-color:#16766f;margin-top:6px}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid #e5ebef}th{color:#617584;font-weight:500}.notice{margin-top:18px;padding:14px 18px;background:#fff5e4;border-radius:10px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}.empty{padding:40px;text-align:center;color:#617584}footer{margin-top:24px}.task{overflow-wrap:anywhere}.legend{display:flex;gap:18px;font-size:12px;color:#617584;margin-top:12px}@media(max-width:850px){main{padding:18px}.layout{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.routes{max-height:220px}svg{height:340px}.top{align-items:flex-start}h1{font-size:24px}}@media print{main{padding:0}.routes,.reset{display:none}.layout{display:block}.panel{break-inside:avoid}svg{height:400px}}

/* 时间播放控件、甘特阶段色块和当前时刻游标。 */
.time-panel{margin:0 0 20px}.clock{font:600 21px ui-monospace,monospace;color:#16766f}.playback{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:20px 0}.playback input{flex:1;min-width:160px;accent-color:#16766f}.playback label{font-size:13px;color:#617584}.playback select{padding:7px;border:1px solid #cddae1;border-radius:7px;background:white;color:#244b60}button:disabled,input:disabled,select:disabled{opacity:.45;cursor:default}.time-legend{display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:12px;color:#617584;margin:10px 0 24px}.time-legend span:before{content:'';display:inline-block;width:12px;height:9px;border-radius:2px;margin-right:6px}.time-legend .travel:before{background:#3485a4}.time-legend .wait:before{background:#dfab4a}.time-legend .unload:before{background:#448d6d}.gantt-scroll{overflow-x:auto;padding-bottom:8px}.gantt-axis,.gantt-row{display:grid;grid-template-columns:180px 1fr;gap:18px;min-width:820px}.gantt-axis{padding:8px 0 24px}.gantt-ticks{position:relative;height:18px}.gantt-ticks span{position:absolute;white-space:nowrap;transform:translateX(-50%);font-size:11px;color:#617584}.gantt-row{padding:12px 0;border-top:1px solid #e5ebef;align-items:center}.gantt-row.selected .gantt-name{font-weight:600}.gantt-choose{padding:0;background:none;border:0;text-align:left;font:inherit;font-size:14px;font-weight:600}.gantt-name .muted{font-size:12px;margin-top:5px}.gantt-track{position:relative;height:40px;border-radius:6px;background:repeating-linear-gradient(90deg,#f0f4f6 0,#f0f4f6 calc(25% - 1px),#dfe7ec calc(25% - 1px),#dfe7ec 25%);cursor:crosshair}.phase{position:absolute;top:7px;height:26px;border:1px solid #fff;border-radius:4px;overflow:hidden;text-align:left;color:white;padding:0;font-size:11px;min-width:1px}.phase span{padding:0 5px;white-space:nowrap;pointer-events:none}.phase.travel{background:#3485a4}.phase.wait{background:#dfab4a;color:#3c2a0c}.phase.unload{background:#448d6d}.phase:hover,.phase:focus-visible,.active-phase{outline:2px solid #172f40;outline-offset:1px;z-index:2}.time-cursor{position:absolute;top:-5px;bottom:-5px;width:2px;background:#cf4763;pointer-events:none;z-index:3}.time-cursor:before{content:'';position:absolute;top:-4px;left:-3px;border:4px solid transparent;border-top-color:#cf4763}.active-stop{background:#e8f5ef}.instant{position:absolute;color:#448d6d;transform:translateX(-50%);top:10px}.route-state{margin-top:8px;font-size:12px;color:#16766f}.vehicle-label{font-weight:600}@media(max-width:850px){.clock{font-size:18px}.playback input{order:4;flex-basis:100%}#map text{font-size:18px}}@media print{.playback{display:none}.gantt-scroll{overflow:visible}.gantt-axis,.gantt-row{min-width:0;grid-template-columns:140px 1fr}}
</style></head><body><main>
<header class="top"><div><div class="eyebrow">DN ROUTER / DELIVERY PLAN</div><h1>配送结果总览</h1><div class="muted task" id="task"></div></div><span class="badge" id="status"></span></header>
<section class="metrics" id="metrics" aria-label="求解汇总"></section>
<section class="panel time-panel" aria-label="时间播放与车辆甘特图">
<div class="maphead"><div><h2>配送时间轴</h2><div class="muted">拖动时间查看各车状态，点击甘特色块定位对应路线与时段</div></div><output id="clock" class="clock" aria-live="off"></output></div>
<div class="playback"><button id="play" class="reset" type="button">播放</button><button id="restart" class="reset" type="button">回到起点</button><input id="time-slider" type="range" min="0" max="0" step="1" value="0" aria-label="配送时间"><label>播放速度 <select id="speed" aria-label="播放速度"><option value="60">1 分钟 / 秒</option><option value="300" selected>5 分钟 / 秒</option><option value="900">15 分钟 / 秒</option></select></label></div>
<div class="time-legend"><span class="travel">行驶</span><span class="wait">等待</span><span class="unload">卸货</span><span>地图：实线已走 / 虚线未走 · 方形为车辆</span></div>
<div class="gantt-scroll"><div id="gantt"></div></div>
<p class="muted" id="time-note">行驶按矩阵耗时沿直线插值；为演示先行驶后等待，等待标记放在目的站附近，仅作位置示意。真实道路、等待地点与 GPS 轨迹未知。时间为业务当地时间。</p>
</section>

<div class="layout"><aside class="panel"><h2>车辆与路线</h2><div class="muted">选择路线，联动查看地图与停靠明细</div><div class="routes" id="routes"></div></aside>
<section class="panel"><div class="maphead"><h2>配送路线示意</h2><button class="reset" id="reset">重置视图</button></div><svg id="map" viewBox="0 0 900 440" role="img" aria-label="按经纬度绘制的配送路线"></svg><div class="legend"><span>◆ 仓库</span><span>数字为停靠顺序</span><span>箭头为配送方向</span></div><p class="muted">经纬度直线示意，不代表道路路径。车辆位置按时间插值，仅作示意。总距离采用业务矩阵原始单位；开放路线结束于末站，不绘制返仓线。重合坐标保留原位，可在表格查看全部停靠。</p><div class="muted" id="mapnote"></div></section></div>
<section class="panel details"><h2 id="detail-title">停靠与装载明细</h2><div id="details"></div></section><section id="notices"></section>
<footer class="muted">本地离线报告 · 时间为业务当地时间 · 可行方案不代表全局最优</footer></main>
<script type="application/json" id="report-data">__REPORT_DATA__</script>
<script>
'use strict';
// 读取嵌入数据；业务文本统一通过 textContent 写入，避免作为 HTML 执行。
const data=JSON.parse(document.getElementById('report-data').textContent),result=data.result,routes=result.routes||[];
const colors=['#187c80','#d36b39','#6d61af','#367cc0','#b44974','#728b27','#a37821','#596b82'];
const $=id=>document.getElementById(id),fmt=v=>v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3});
function el(tag,text,parent,cls){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;if(parent)parent.append(e);return e;}
// selected 为 -1 时展示全部路线；缩放和平移只改变地图视图。
let selected=-1,zoom=1,pan=[0,0],drag=null;
$('task').textContent=result.taskNo||'未指定任务编号';$('status').textContent=result.status||'未知状态';
for(const [label,value] of [['已服务 DN',result.servedOrderCount],['使用车辆',result.vehicleCount],['总距离 · 业务单位',result.totalDistance],['计算耗时 · ms',result.elapsedMs]]){const m=el('div',null,$('metrics'),'metric');el('span',label,m,'muted');el('strong',fmt(value),m);}
const allButton=el('button','全部路线',$('routes'),'route');allButton.type='button';allButton.onclick=()=>select(-1);
routes.forEach((r,i)=>{const b=el('button',null,$('routes'),'route');b.type='button';b.style.setProperty('--route',colors[i%colors.length]);el('strong',r.routeId+' · '+r.vehicleTypeName,b);el('small',r.stops.length+' 次停靠 · '+r.stops.reduce((n,s)=>n+s.orderReleaseIds.length,0)+' DN',b);el('span','距离 '+fmt(r.totalDistance),b,'muted');el('div',null,b,'route-state').id='route-state-'+i;b.onclick=()=>select(i);});
// 路线筛选同时驱动地图、甘特图和明细，不改变全局播放时刻。
function visible(){return routes.map((r,i)=>({r,i})).filter(x=>selected<0||x.i===selected);}
// 切换路线时重置地图视角并重绘关联区域，保留当前时间。
function select(i){selected=i;zoom=1;pan=[0,0];[...$('routes').children].forEach((b,j)=>b.setAttribute('aria-pressed',String(j===i+1)));drawGantt();details();updateTime();draw();}
const ns='http://www.w3.org/2000/svg';function sv(tag,attrs,parent){const e=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,v);parent.append(e);return e;}
// 按可见坐标拟合视图，用纬度余弦修正经度比例；仅绘制直线示意。
function draw(){const map=$('map');map.replaceChildren();const list=visible(),points=list.flatMap(({r})=>r.stops.map(s=>s.coordinate)).filter(Boolean);if(data.depot.coordinate)points.push(data.depot.coordinate);
// 坐标缺失时断开连线，仍保留停靠明细和缺失提示。
const missing=list.reduce((n,{r})=>n+r.stops.filter(s=>!s.coordinate).length,0);
$('mapnote').textContent=(missing?missing+' 个停靠坐标缺失或同站 DN 坐标不一致，图中断开，明细仍完整显示。 ':'')+(!data.depot.coordinate?'仓库坐标不可用。 ':'');
if(!points.length){const t=sv('text',{x:450,y:220,'text-anchor':'middle'},map);t.textContent='无可绘制的坐标';return;}
const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys),lat=(ymin+ymax)/2*Math.PI/180,cos=Math.max(.01,Math.cos(lat));
const scale=Math.min(760/Math.max((xmax-xmin)*cos,.001),320/Math.max(ymax-ymin,.001));const xy=p=>[450+(p[0]-(xmin+xmax)/2)*cos*scale,220-(p[1]-(ymin+ymax)/2)*scale];
// 每条路线使用独立箭头标记，随后在同一图层应用缩放和平移。
const defs=sv('defs',{},map);list.forEach(({i})=>{const m=sv('marker',{id:'arrow'+i,viewBox:'0 0 10 10',refX:15,refY:5,markerWidth:6,markerHeight:6,orient:'auto'},defs);sv('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:colors[i%colors.length]},m);});
const group=sv('g',{transform:`translate(${pan[0]} ${pan[1]}) translate(450 220) scale(${zoom}) translate(-450 -220)`},map);
// 重合坐标保留原位置，仅错开文字标签；已走路段叠加实线。
const labelSlots=new Map();
list.forEach(({r,i})=>{const color=colors[i%colors.length];let prev=data.depot.coordinate;
r.stops.forEach((s,j)=>{if(prev&&s.coordinate&&(prev[0]!==s.coordinate[0]||prev[1]!==s.coordinate[1])){const a=xy(prev),b=xy(s.coordinate),seg=timeline?.routes[i]?.segments.find(x=>x.kind==='travel'&&x.stopIndex===j);const fraction=seg?(seg.end===seg.start?(currentTime>=seg.end?1:0):Math.min(1,Math.max(0,(currentTime-seg.start)/(seg.end-seg.start)))):1;
sv('line',{x1:a[0],y1:a[1],x2:b[0],y2:b[1],stroke:color,'stroke-width':seg&&seg.start<=currentTime&&currentTime<seg.end?5:2.5,'stroke-dasharray':'7 6','marker-end':`url(#arrow${i})`,opacity:.4},group);
if(fraction>0)sv('line',{x1:a[0],y1:a[1],x2:a[0]+(b[0]-a[0])*fraction,y2:a[1]+(b[1]-a[1])*fraction,stroke:color,'stroke-width':3,'marker-end':fraction===1?`url(#arrow${i})`:'',opacity:.95},group);}prev=s.coordinate;});
r.stops.forEach((s,j)=>{if(!s.coordinate)return;const [x,y]=xy(s.coordinate),g=sv('g',{},group);sv('circle',{cx:x,cy:y,r:9,fill:color,stroke:'white','stroke-width':2},g);const title=sv('title',{},g);title.textContent=r.routeId+' / '+(j+1)+' / '+s.siteCode+' / '+s.orderReleaseIds.join(', ');const key=s.coordinate.join(','),slot=labelSlots.get(key)||0;labelSlots.set(key,slot+1);const t=sv('text',{x:x+13,y:y-12-slot*17},g);t.textContent=r.routeId+'·'+(j+1)+' '+s.siteCode;});});
if(data.depot.coordinate){const [x,y]=xy(data.depot.coordinate);sv('path',{d:`M ${x} ${y-10} l 10 10 l -10 10 l -10 -10 z`,fill:'#1e3445',stroke:'white','stroke-width':2},group);const t=sv('text',{x:x+15,y:y+5},group);t.textContent='仓库 '+data.depot.name;}
// 车辆位置由时间轴插值计算，等待时只显示目的站附近的示意位置。
const vehicleSlots=new Map();list.forEach(({r,i})=>{const state=stateAt(i);if(!state.point)return;const [x,y]=xy(state.point),color=colors[i%colors.length],g=sv('g',{'data-route':r.routeId,'data-state':state.kind||state.label},group);const slotKey=state.point.join(','),slot=vehicleSlots.get(slotKey)||0;vehicleSlots.set(slotKey,slot+1);
sv('circle',{cx:x,cy:y,r:17,fill:'white',opacity:.85,stroke:color,'stroke-width':2,'stroke-dasharray':state.kind==='wait'?'4 3':''},g);sv('rect',{x:x-7,y:y-7,width:14,height:14,rx:3,fill:color},g);const title=sv('title',{},g);title.textContent=r.routeId+' · '+state.label+' · '+wallTime(currentTime);const t=sv('text',{x:x>680?x-24:x+24,y:y+27+slot*22,'text-anchor':x>680?'end':'start',class:'vehicle-label'},g);t.textContent=r.routeId+' '+state.label;});}

// 装载率使用输入车型容量；停靠表按实际停靠展示合并后的 DN。
function details(){const parent=$('details');parent.replaceChildren();if(!routes.length){el('div','当前结果没有配送路线，请查看下方状态说明。',parent,'empty');return;}
visible().forEach(({r,i})=>{const box=el('article',null,parent,'route-detail');const h=el('h3',r.routeId+' · '+r.vehicleTypeName,box);h.style.color=colors[i%colors.length];el('div','出仓 '+r.departureTime+' → 完成 '+r.completionTime+' ｜ 距离 '+fmt(r.totalDistance)+' ｜ 站点计数 '+fmt(r.siteCount)+' ｜ 实际停靠 '+fmt(r.physicalStopCount),box,'muted');
const v=data.vehicles.find(v=>v.vehicleTypeName===r.vehicleTypeName)||{},loads=el('div',null,box,'loads');for(const [label,used,capacity]of [['体积',r.totalVolume,v.totalVolume],['重量',r.totalWeight,v.totalWeight]]){const cap=Number(capacity),ratio=cap>0?Number(used)/cap:null,d=el('div',label+' '+fmt(used)+' / '+fmt(capacity)+(ratio==null?'':' · '+fmt(ratio*100)+'%'),loads,'load');if(ratio!=null){const p=el('progress',null,d);p.max=1;p.value=ratio;p.setAttribute('aria-label',label+'装载率');}}
const scroll=el('div',null,box,'table-scroll'),table=el('table',null,scroll),head=el('tr',null,el('thead',null,table));['顺序','站点 / DN','开始卸货（eta）','离开','等待 / 卸货（分钟）'].forEach(s=>el('th',s,head));const body=el('tbody',null,table);r.stops.forEach((s,j)=>{const tr=el('tr',null,body);tr.dataset.route=i;tr.dataset.stop=j;el('td',j+1,tr);const td=el('td',s.siteCode,tr);el('div',s.orderReleaseIds.join(' / '),td,'muted');el('td',s.arrivalTime,tr);el('td',s.departureTime,tr);el('td',fmt(s.waitingBeforeArrivalSeconds/60)+' / '+fmt(s.unloadingSeconds/60),tr);});});}
// 错误、异常单和告警分别展示，即使没有路线也能查看失败原因。
for(const [label,items]of [['输入错误',result.errors],['异常 DN',result.rejectedOrders],['提示',result.warnings]])if(items?.length){const b=el('div',null,$('notices'),'notice');el('strong',label,b);for(const item of items)el('div',typeof item==='string'?item:JSON.stringify(item),b);}
if(result.solutionSource)el('p','方案来源：'+result.solutionSource+' ｜ 目标策略：'+(result.objectivePolicy||'—'),$('notices'),'muted');
// 指针位移换算为 SVG 视图坐标，保持不同屏幕尺寸下拖动比例一致。
$('reset').onclick=()=>{zoom=1;pan=[0,0];draw();};$('map').addEventListener('wheel',e=>{e.preventDefault();zoom=Math.min(8,Math.max(.5,zoom*(e.deltaY<0?1.15:1/1.15)));draw();},{passive:false});
$('map').addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY,...pan];$('map').setPointerCapture(e.pointerId);});$('map').addEventListener('pointermove',e=>{if(!drag)return;const rect=$('map').getBoundingClientRect();pan=[drag[2]+(e.clientX-drag[0])*900/rect.width,drag[3]+(e.clientY-drag[1])*440/rect.height];draw();});for(const event of ['pointerup','pointercancel'])$('map').addEventListener(event,()=>drag=null);
// Python 提供相对业务当地起点的秒数。
// 这里只借用 UTC 方法格式化墙上时钟，避免浏览器时区或夏令时改变显示。
const timeline=data.timeline,total=timeline?.durationSeconds||0;
const phaseNames={travel:'行驶',wait:'等待（位置示意）',unload:'卸货'};
const originParts=(timeline?.startTime||'2000-01-01 00:00:00').match(/\d+/g).map(Number);
const originDate=new Date(0);originDate.setUTCFullYear(originParts[0],originParts[1]-1,originParts[2]);originDate.setUTCHours(originParts[3],originParts[4],originParts[5]||0,0);
let currentTime=0,playing=false,playTimer=null,lastFrame=0;
// 通过统一起点加相对秒数显示日期，跨午夜时自然切换日期。
function wallTime(seconds,short=false){const d=new Date(originDate.getTime()+Math.floor(seconds)*1000),z=n=>String(n).padStart(2,'0');return (short?'':d.getUTCFullYear()+'-')+z(d.getUTCMonth()+1)+'-'+z(d.getUTCDate())+' '+z(d.getUTCHours())+':'+z(d.getUTCMinutes())+(short?'':':'+z(d.getUTCSeconds()));}
// 阶段使用左闭右开区间；终点直接标记完成，零时长阶段不参与插值。
function stateAt(i){const r=routes[i],track=timeline?.routes[i];if(!track)return {label:'无时间数据',point:null};
if(currentTime<track.start)return {label:'未出发',point:data.depot.coordinate};
if(currentTime>=track.end)return {label:'已完成',point:r.stops.at(-1)?.coordinate};
const seg=track.segments.find(s=>s.start<=currentTime&&currentTime<s.end);
if(!seg)return {label:'无时间数据',point:null};const stop=r.stops[seg.stopIndex],from=seg.stopIndex?r.stops[seg.stopIndex-1].coordinate:data.depot.coordinate,to=stop.coordinate;
let point=to;if(seg.kind==='travel'){const f=(currentTime-seg.start)/(seg.end-seg.start);point=from&&to?[from[0]+(to[0]-from[0])*f,from[1]+(to[1]-from[1])*f]:null;}
return {label:phaseNames[seg.kind]+' · '+stop.siteCode,point,kind:seg.kind,stopIndex:seg.stopIndex};}
// 暂停时清除定时器并同步按钮的可访问状态。
function pause(){playing=false;clearInterval(playTimer);playTimer=null;$('play').textContent='播放';$('play').setAttribute('aria-pressed','false');}
// 手动定位会暂停播放；定时推进传 manual=false 保持播放状态。
function setTime(value,manual=true){if(manual)pause();currentTime=Math.min(total,Math.max(0,Number(value)||0));updateTime();draw();}
// 统一刷新时钟、游标、车辆状态和停靠高亮，确保各视图使用同一时刻。
function updateTime(){const clock=$('clock');clock.textContent=timeline?wallTime(currentTime):'没有可播放的路线';$('time-slider').value=String(Math.round(currentTime));$('time-slider').setAttribute('aria-valuetext',clock.textContent);
const pct=total?currentTime/total*100:0;document.querySelectorAll('.time-cursor').forEach(e=>e.style.left=pct+'%');
routes.forEach((r,i)=>{const state=stateAt(i),node=$('route-state-'+i);if(node)node.textContent=state.label;const status=$('gantt-state-'+i);if(status)status.textContent=state.label;});
document.querySelectorAll('.phase').forEach(e=>{const active=Number(e.dataset.start)<=currentTime&&currentTime<Number(e.dataset.end);e.classList.toggle('active-phase',active);});
document.querySelectorAll('tr[data-route]').forEach(e=>{const state=stateAt(Number(e.dataset.route));e.classList.toggle('active-stop',state.kind!=='travel'&&state.stopIndex===Number(e.dataset.stop));});}
// 甘特图共用全局时间尺度；点击轨道定位时间，点击阶段定位该段起点。
function drawGantt(){const root=$('gantt');root.replaceChildren();if(!timeline){el('div','当前结果没有有效的配送时间表。',root,'empty');return;}
const axis=el('div',null,root,'gantt-axis');el('div','车辆 / 当前状态',axis,'muted');const ticks=el('div',null,axis,'gantt-ticks');
for(let n=0;n<=4;n++){const tick=el('span',wallTime(total*n/4,true),ticks);tick.style.left=n*25+'%';if(n===0)tick.style.transform='none';if(n===4)tick.style.transform='translateX(-100%)';}
visible().forEach(({r,i})=>{const track=timeline.routes[i],row=el('div',null,root,'gantt-row'+(selected===i?' selected':''));const name=el('div',null,row,'gantt-name');const choose=el('button',r.routeId+' · '+r.vehicleTypeName,name,'gantt-choose');choose.style.color=colors[i%colors.length];choose.onclick=()=>select(i);el('div',null,name,'muted').id='gantt-state-'+i;
const bar=el('div',null,row,'gantt-track');bar.setAttribute('aria-label',r.routeId+' 时间表');if(!track){el('span','无时间数据',bar,'muted');return;}
bar.onclick=e=>{const rect=bar.getBoundingClientRect();select(i);setTime((e.clientX-rect.left)/rect.width*total);};
for(const seg of track.segments){if(seg.end<=seg.start)continue;const stop=r.stops[seg.stopIndex],b=el('button',null,bar,'phase '+seg.kind);b.type='button';b.style.left=seg.start/Math.max(total,1)*100+'%';b.style.width=(seg.end-seg.start)/Math.max(total,1)*100+'%';b.dataset.start=seg.start;b.dataset.end=seg.end;
const label=r.routeId+' · '+phaseNames[seg.kind]+' · '+stop.siteCode+' ｜ '+wallTime(seg.start)+' → '+wallTime(seg.end)+' ｜ '+fmt((seg.end-seg.start)/60)+' 分钟';b.title=label;b.setAttribute('aria-label',label);el('span',phaseNames[seg.kind].replace('（位置示意）','')+' '+stop.siteCode,b);b.onclick=e=>{e.stopPropagation();select(i);setTime(seg.start);};}
if(track.start===track.end){const instant=el('span','◆',bar,'instant');instant.style.left=track.end/Math.max(total,1)*100+'%';instant.title='瞬时完成 '+wallTime(track.end);}
el('div',null,bar,'time-cursor');});}
// 无有效时间表或总时长为零时禁用播放，瞬时路线只保留完成标记。
$('time-slider').max=String(total);for(const id of ['play','restart','time-slider','speed'])$(id).disabled=!timeline||total<=0;
$('time-slider').oninput=e=>setTime(e.target.value);$('restart').onclick=()=>setTime(0);
$('speed').onchange=()=>lastFrame=performance.now();
// 用实际经过的时间乘播放倍率推进；到达终点暂停，再次播放从起点开始。
$('play').onclick=()=>{if(playing){pause();return;}if(currentTime>=total)setTime(0);playing=true;$('play').textContent='暂停';$('play').setAttribute('aria-pressed','true');lastFrame=performance.now();playTimer=setInterval(()=>{const now=performance.now(),delta=(now-lastFrame)/1000;lastFrame=now;setTime(currentTime+delta*Number($('speed').value),false);if(currentTime>=total)pause();},100);};
// 页面隐藏时暂停，避免恢复后因累计后台时间突然跳进。
document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});

// 所有状态声明完毕后初始化，默认展示全部路线。
select(-1);
</script></body></html>'''
