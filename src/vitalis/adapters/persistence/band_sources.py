"""Per-device band facts for source comparison; never overwrite another route."""
from datetime import date,datetime,timezone
from uuid import uuid4
from sqlalchemy import Date,DateTime,ForeignKey,Index,JSON,String,cast,func,select,type_coerce
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base


class BandSourceRecord(Base):
    __tablename__='band_source_records'
    __table_args__=(Index('ix_band_source_owner_day','user_id','date','kind','fetched_at'),)
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id:Mapped[str]=mapped_column(ForeignKey('users.id'),nullable=False)
    date:Mapped[date]=mapped_column(Date,nullable=False)
    kind:Mapped[str]=mapped_column(String(16),nullable=False)
    source:Mapped[str]=mapped_column(String(32),nullable=False)
    source_scope:Mapped[str]=mapped_column(String(24),nullable=False)
    device_id:Mapped[str]=mapped_column(String(128),default='',nullable=False)
    data:Mapped[dict]=mapped_column(JSON,nullable=False)
    fetched_at:Mapped[datetime]=mapped_column(DateTime,nullable=False)
    source_record_id:Mapped[str|None]=mapped_column(String(64),nullable=True)


def save_band_source(repo,user_id,day,kind,payload,*,fetched_at=None):
    if kind not in {'sleep','activity','naps'}:raise ValueError('unsupported band record')
    source=payload.get('source') or 'zepp';scope=payload.get('source_scope') or 'unknown';device=payload.get('device_id') or ''
    previous=repo.db.execute(select(BandSourceRecord).where(BandSourceRecord.user_id==user_id,BandSourceRecord.date==day,
        BandSourceRecord.kind==kind,BandSourceRecord.source==source,BandSourceRecord.source_scope==scope,
        BandSourceRecord.device_id==device).order_by(BandSourceRecord.fetched_at.desc(),BandSourceRecord.id.desc()).limit(1)).scalar_one_or_none()
    if previous and previous.data==payload:return 0
    journal=repo._active_chart_journal()
    if journal and (journal.user_id != user_id or journal.source != source or journal.stream != 'sleep'):
        journal = None
    stamp=fetched_at or (journal.fetched_at if journal else None) or datetime.now(timezone.utc)
    if stamp.tzinfo:stamp=stamp.astimezone(timezone.utc).replace(tzinfo=None)
    record_id = journal.id if journal and journal.user_id == user_id and journal.source == source else None
    repo.db.add(BandSourceRecord(id=uuid4().hex,user_id=user_id,date=day,kind=kind,source=source,source_scope=scope,
        device_id=device,data=payload,fetched_at=stamp,source_record_id=record_id))
    repo.db.flush();repo._bump_existing_input_revisions({user_id})
    repo.enqueue_analysis_job(user_id,day,event_type='source_sync',source=source,affected_dates={day},
        affected_streams={kind,'band_source_records'},reason='source-qualified band facts changed')
    return 1


def read_band_sources(db,user_id,start,end,*,as_of,include_activity_details=True,kind=None,
                      include_minutes=True,include_hourly=True,limit=None,max_bytes=None):
    cutoff=as_of.astimezone(timezone.utc).replace(tzinfo=None) if as_of.tzinfo else as_of
    data=BandSourceRecord.data
    if not include_activity_details:
        # Drop the minute arrays in SQL: charts and profile construction only
        # need the compact daily facts, while explicit source API reads keep them.
        if db.get_bind().dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import JSONB
            data=cast(data,JSONB).op('-')('band_activity')
        else:
            data=type_coerce(func.json_remove(data,'$.band_activity'),JSON)
    elif not include_minutes or not include_hourly:
        fields=[]
        if not include_minutes:fields.extend(('minute_steps','raw_minutes'))
        if not include_hourly:fields.append('hourly_steps')
        if db.get_bind().dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import JSONB, array
            data=cast(data,JSONB)
            for field in fields:data=data.op('#-')(array(['band_activity',field]))
        else:
            data=type_coerce(func.json_remove(data,*(f'$.band_activity.{field}' for field in fields)),JSON)
    conditions=[BandSourceRecord.user_id==user_id,BandSourceRecord.date.between(start,end),
                BandSourceRecord.fetched_at<=cutoff]
    if kind is not None:conditions.append(BandSourceRecord.kind==kind)
    latest=select(BandSourceRecord.id,func.row_number().over(
        partition_by=(BandSourceRecord.date,BandSourceRecord.kind,BandSourceRecord.source,
                      BandSourceRecord.source_scope,BandSourceRecord.device_id),
        order_by=(BandSourceRecord.fetched_at.desc(),BandSourceRecord.id.desc())).label('position'))
    latest=latest.where(*conditions).subquery()
    query=select(BandSourceRecord.date,BandSourceRecord.kind,BandSourceRecord.source,
        BandSourceRecord.source_scope,BandSourceRecord.device_id,data.label('data'),
        BandSourceRecord.fetched_at,BandSourceRecord.source_record_id).where(BandSourceRecord.id.in_(
            select(latest.c.id).where(latest.c.position==1))).order_by(BandSourceRecord.date,
            BandSourceRecord.source,BandSourceRecord.source_scope,BandSourceRecord.device_id,BandSourceRecord.kind)
    if limit is not None:query=query.limit(limit)
    rows=db.execute(query.execution_options(yield_per=128))
    selected=[]
    size=0
    for row in rows:
        if max_bytes is not None:
            from vitalis.application.health_query_budget import json_size
            size+=json_size(row.data,max_bytes-size)
        selected.append({'date':row.date,'kind':row.kind,
            'data':{**row.data,'fetched_at':row.fetched_at.replace(tzinfo=timezone.utc).isoformat(),
                'source_record_id':row.source_record_id},'fetched_at':row.fetched_at.replace(tzinfo=timezone.utc)})
    return selected
