"""Экспорт Excel и PDF: валидные непустые файлы нужного формата."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService
from app.services import exporters


async def test_xlsx_and_pdf(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб")
    await session.commit()
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    svc = BookingService(session, t.id)
    tr = await svc.create_training(title="Турнир", start_at=now, location="Зал",
                                   max_participants=5, platform="tg", user_id=1)
    await svc.sign_up(tr.id, "tg", 1, "Аня")
    await svc.sign_up(tr.id, "vk", 2, "Боря")
    data = await svc.export_rows(tr.id)
    assert data is not None
    training, rows = data
    assert len(rows) == 2

    xlsx = exporters.build_xlsx(training.title, "01.01", "Зал", 5, rows)
    assert xlsx[:2] == b"PK"  # zip-сигнатура xlsx
    pdf = exporters.build_pdf(training.title, "01.01", "Зал", 5, rows)
    assert pdf[:5] == b"%PDF-"  # сигнатура PDF
