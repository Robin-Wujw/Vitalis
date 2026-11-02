"""Pure, explicit source navigation without synthesizing a fused measurement."""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256

from .chart_contracts import Chart, ChartBundle, ChartRoute

ACCOUNT_SCOPES = frozenset({'user_summary', 'user_fused'})


def _device_key(source: str, identity: str) -> str:
    return source + ':device:' + sha256(identity.upper().encode()).hexdigest()[:16]


def attach_source_routes(charts: list[Chart], inventory: list[dict]) -> list[Chart]:
    """Group aliases only when the same inventory record explicitly links them.

    Grouping is for navigation. Original series identities/points stay separate,
    even when two identifiers refer to the same physical device.
    """
    base = {}
    claims = defaultdict(set)
    for row in inventory:
        source = str(row.get('source') or 'zepp')
        identity = str(row.get('device_id') or '').strip()
        if not identity:
            continue
        key = _device_key(source, identity)
        ids = sorted({identity, *[str(x) for x in row.get('aliases') or [] if x]})
        base[key] = ChartRoute(id=key, kind='device', label=str(row.get('model') or '设备'),
            source=source, device_id=identity, device_ids=ids,
            reason='本窗口没有该设备的已保存记录；不能据此判断设备不支持。')
        for alias in ids:
            claims[(source, alias.upper())].add(key)
    output = []
    for chart in charts:
        routes = {key:value.model_copy(deep=True) for key,value in base.items()}
        routes['zepp:account'] = ChartRoute(id='zepp:account',kind='account',label='Zepp 账户汇总',source='zepp',
            reason='本窗口未取得账户级结果；不从设备数据自行补成厂商融合值。')

        def route_for(source, scope, device=None, *, analysis=False):
            source = str(source or 'unknown')
            if analysis:
                key, kind, label = 'vitalis:analysis','analysis','Vitalis 分析结果'
            elif scope in ACCOUNT_SCOPES:
                key, kind, label = source+':account','account',('Zepp 账户汇总' if source=='zepp' else source+' 账户汇总')
            elif device and scope == 'device':
                matches = claims.get((source,str(device).upper()),set())
                if len(matches)>1:
                    key,kind,label=source+':conflict:'+sha256(str(device).upper().encode()).hexdigest()[:16],'unknown','设备身份冲突'
                else:
                    key = next(iter(matches)) if matches else _device_key(source,str(device))
                    kind, label = 'device','未匹配设备 · '+str(device)[-4:]
            elif source in {'hevy','user'}:
                key, kind, label = source+':external','external',source+' 外部记录'
            else:
                key, kind, label = source+':unknown','unknown','设备归属未确认'
            if key not in routes:
                routes[key] = ChartRoute(id=key,kind=kind,label=label,source=source,
                    device_id=str(device) if kind=='device' else None,
                    device_ids=[str(device)] if kind=='device' else [])
            return routes[key]

        series = []
        for row in chart.series:
            route = route_for(row.source,row.source_scope,row.device_id)
            route.state='available' if any(p.value is not None for p in row.points) else route.state
            route.sample_count += row.sample_count
            if row.first_observed_at:
                route.first_observed_at=min(filter(None,(route.first_observed_at,row.first_observed_at)))
            if row.last_observed_at:
                route.last_observed_at=max(filter(None,(route.last_observed_at,row.last_observed_at)))
            series.append(row.model_copy(update={'route_id':route.id,'device_label':route.label,
                'label':row.label.split(' · ')[0]+' · '+route.label}))
        preview=[]
        for row in chart.preview_series:
            route=route_for(row.source,row.source_scope,row.device_id)
            preview.append(row.model_copy(update={'route_id':route.id,'device_label':route.label,
                'label':row.label.split(' · ')[0]+' · '+route.label}))
        intervals=[]
        for row in chart.intervals:
            if row.category=='annotation':
                intervals.append(row)
                continue
            route=route_for(row.source,row.source_scope,row.device_id)
            route.state='available'
            route.first_observed_at=min(filter(None,(route.first_observed_at,row.start)))
            route.last_observed_at=max(filter(None,(route.last_observed_at,row.end)))
            intervals.append(row.model_copy(update={'route_id':route.id}))
        bands=[]
        for row in chart.bands:
            route=route_for(row.source,row.source_scope,row.device_id)
            if any(p.lower is not None or p.upper is not None for p in row.points):route.state='available'
            bands.append(row.model_copy(update={'route_id':route.id}))
        regions=[]
        for row in chart.regions:
            route=route_for('vitalis','analysis',analysis=True)
            route.state='available'
            regions.append(row.model_copy(update={'route_id':route.id}))
        source_summaries=[]
        for row in chart.source_summaries:
            route=route_for(row.source,row.source_scope,row.device_id)
            route.state='available'
            source_summaries.append(row.model_copy(update={'route_id':route.id}))
        for route in routes.values():
            if route.state=='available':
                route.reason=('厂商账户级结果，未披露的融合算法不由 Vitalis 猜测或重算。' if route.kind=='account'
                    else '来源未能可靠归属设备，不作为第三条账户融合值。' if route.kind=='unknown'
                    else '保留原始来源；同设备别名仅分组显示，不合并或平均曲线。' if route.kind=='device'
                    else '由 Vitalis 对已保存事实计算，不是厂商融合结果。')
        ordered=sorted(routes.values(),key=lambda r:({'account':0,'device':1,'unknown':2,'analysis':3,'external':4}[r.kind],r.label,r.id))
        output.append(chart.model_copy(update={'series':series,'preview_series':preview,'intervals':intervals,
            'bands':bands,'regions':regions,'source_summaries':source_summaries,'routes':ordered}))
    return output


def filter_chart(chart: Chart, route_id: str) -> Chart:
    """Pure projection: a missing route cannot fall back to another device."""
    if route_id=='all':
        return chart.model_copy(deep=True,update={'selected_route':'all'})
    route=next((r for r in chart.routes if r.id==route_id),None)
    if route is None:
        raise ValueError('unknown chart source route')
    fields={name:[row for row in getattr(chart,name) if row.route_id==route_id
                  or (name=='intervals' and row.category=='annotation' and row.source=='user')]
            for name in ('series','preview_series','intervals','bands','regions')}
    state='partial' if route.state=='available' else 'unavailable'
    own_summaries=[item for item in chart.source_summaries if item.route_id==route_id]
    fields['source_summaries']=own_summaries
    summary=[route.label,route.reason or '本窗口无可用记录。',*[item.text for item in own_summaries]]
    if route.last_observed_at:summary.append('末次观测 '+route.last_observed_at.isoformat())
    return chart.model_copy(update={**fields,'selected_route':route_id,'state':state,'summary':summary,'reference':None})


def filter_bundle(bundle: ChartBundle, route_id: str) -> ChartBundle:
    if route_id=='all':return bundle
    if not any(r.id==route_id for chart in bundle.charts for r in chart.routes):
        raise ValueError('unknown chart source route')
    template=next(r for chart in bundle.charts for r in chart.routes if r.id==route_id)
    charts=[]
    for chart in bundle.charts:
        if any(r.id==route_id for r in chart.routes):charts.append(filter_chart(chart,route_id))
        else:charts.append(chart.model_copy(update={'series':[],'preview_series':[],'intervals':[],'bands':[],
            'regions':[],'source_summaries':[],'reference':None,'state':'unavailable','selected_route':route_id,
            'summary':['本指标没有该来源的记录，不使用其他来源补齐。'],
            'routes':[*chart.routes,template.model_copy(update={'state':'missing','sample_count':0,'first_observed_at':None,'last_observed_at':None})]}))
    return bundle.model_copy(update={'charts':charts})
