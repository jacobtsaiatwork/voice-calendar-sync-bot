import os
import datetime
from typing import List, Optional, Tuple
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from config import settings

db_url = settings.DATABASE_URL
if os.getenv("VERCEL") and "sqlite" in db_url:
    db_url = "sqlite:////tmp/voice_calendar_sync.db"
elif db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    db_url,
    connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
    pool_pre_ping=True
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class ScheduleItem(Base):
    """
    統一管理 Google Calendar 日曆事件 (CALENDAR_EVENT) 與 Google Tasks 待辦任務 (TASK) 的雙向同步實體。
    """
    __tablename__ = "schedule_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), index=True, nullable=False, default="default_user")
    item_type = Column(String(30), index=True, nullable=False, default="TASK")  # 'CALENDAR_EVENT' | 'TASK'
    title = Column(String(255), nullable=False)
    start_time = Column(DateTime, nullable=True)       # 事件開始時間 (CALENDAR_EVENT 專用)
    end_time = Column(DateTime, nullable=True)         # 事件結束時間 (CALENDAR_EVENT 專用)
    due_time = Column(DateTime, nullable=True)         # 任務截止時間 (TASK 專用，或相容舊版)
    is_all_day = Column(Boolean, default=False)
    location = Column(String(255), nullable=True)
    priority = Column(String(20), default="Medium")    # 'High' | 'Medium' | 'Low'
    category = Column(String(50), default="一般")      # '會議' | '工作' | '生活' | '購物' | '個人' 等
    notes = Column(Text, nullable=True)
    is_completed = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)

    # Google Calendar / Google Tasks 雙向同步欄位
    google_event_id = Column(String(128), index=True, nullable=True)
    google_task_id = Column(String(128), index=True, nullable=True)
    etag = Column(String(128), nullable=True)
    sync_status = Column(String(40), default="PENDING")  # 'SYNCED_CLOUD' | 'SYNCED_LOCAL_TWIN' | 'PENDING' | 'CONFLICT_RESOLVED'
    sync_source = Column(String(30), default="LINE")     # 'LINE' | 'GOOGLE'
    last_synced_at = Column(DateTime, nullable=True)

    # 提醒與時間戳記
    reminded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

    @property
    def effective_time(self) -> Optional[datetime.datetime]:
        """取得用於排序與提醒的基準時間"""
        return self.start_time or self.due_time


# 向下相容別名
TodoItem = ScheduleItem


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def detect_conflicts(
    db: Session,
    user_id: str,
    start_time: Optional[datetime.datetime],
    end_time: Optional[datetime.datetime],
    exclude_id: Optional[int] = None
) -> List[ScheduleItem]:
    """
    智慧撞期偵測 (Conflict Detection)：
    檢查指定使用者的 `CALENDAR_EVENT` 是否與 [start_time, end_time] 重疊。
    重疊條件：existing.start_time < new_end_time AND existing.end_time > new_start_time
    """
    if not start_time:
        return []
    effective_end = end_time or (start_time + datetime.timedelta(hours=1))

    query = db.query(ScheduleItem).filter(
        ScheduleItem.user_id == user_id,
        ScheduleItem.item_type == "CALENDAR_EVENT",
        ScheduleItem.is_deleted == False,
        ScheduleItem.is_completed == False,
        ScheduleItem.start_time != None,
        ScheduleItem.end_time != None,
        ScheduleItem.start_time < effective_end,
        ScheduleItem.end_time > start_time
    )
    if exclude_id is not None:
        query = query.filter(ScheduleItem.id != exclude_id)

    return query.order_by(ScheduleItem.start_time.asc()).all()


def find_next_available_slot(
    db: Session,
    user_id: str,
    desired_start: datetime.datetime,
    duration_minutes: int = 60,
    exclude_id: Optional[int] = None
) -> Tuple[datetime.datetime, datetime.datetime]:
    """
    自動尋找下一個無衝突的空檔時段（每次往後推進 30 分鐘或跳過衝突事件的結束時間）。
    """
    duration = datetime.timedelta(minutes=max(15, duration_minutes))
    candidate_start = desired_start
    candidate_end = candidate_start + duration

    for _ in range(48):  # 最多往後搜尋 24 小時內空檔
        conflicts = detect_conflicts(
            db, user_id, candidate_start, candidate_end, exclude_id=exclude_id
        )
        if not conflicts:
            return candidate_start, candidate_end
        # 跳到最後一個衝突事件的結束時間
        latest_end = max(c.end_time for c in conflicts if c.end_time)
        if latest_end and latest_end > candidate_start:
            candidate_start = latest_end
        else:
            candidate_start = candidate_start + datetime.timedelta(minutes=30)
        candidate_end = candidate_start + duration

    return candidate_start, candidate_end
