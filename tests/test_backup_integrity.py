"""
Проверка целостности резервной копии перед отправкой.

Без неё «успешный» бэкап мог оказаться архивом из нуля таблиц: файл
приходит владельцу, оператор спокоен, а восстанавливать нечего. Хуже
всего это выясняется в момент, когда копия действительно нужна.
"""
import gzip

from app.services import backup


def test_checksum_is_stable_and_content_dependent():
    a = backup.checksum(b"one")
    assert a == backup.checksum(b"one")
    assert a != backup.checksum(b"two")
    assert len(a) == 64


def test_verify_rejects_broken_archive():
    assert "не распаковывается" in backup.verify_dump(b"not a gzip archive")


def test_verify_rejects_tiny_dump():
    assert "мал" in backup.verify_dump(gzip.compress("пусто".encode()))


def test_verify_rejects_dump_without_tables(monkeypatch):
    monkeypatch.setattr(type(backup.settings), "is_sqlite",
                        property(lambda self: False))
    body = b"-- PostgreSQL database dump\n" + b"\n" * 2000
    assert "нет ни одной таблицы" in backup.verify_dump(gzip.compress(body))


def test_verify_accepts_real_postgres_dump(monkeypatch):
    monkeypatch.setattr(type(backup.settings), "is_sqlite",
                        property(lambda self: False))
    body = (b"-- PostgreSQL database dump\n"
            b"CREATE TABLE public.tenants (id integer);\n" + b"-" * 2000)
    assert backup.verify_dump(gzip.compress(body)) == ""


def test_verify_checks_sqlite_signature(monkeypatch):
    monkeypatch.setattr(type(backup.settings), "is_sqlite",
                        property(lambda self: True))
    assert "не файл базы SQLite" in backup.verify_dump(
        gzip.compress(b"x" * 2000))
    assert backup.verify_dump(
        gzip.compress(b"SQLite format 3\x00" + b"x" * 2000)) == ""


async def test_bad_dump_is_not_sent(monkeypatch):
    """Копия, не прошедшая проверку, не уходит и не закрывает день."""
    sent = []

    async def fake_dump():
        return gzip.compress("мусор".encode()), "backup.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_dump)
    monkeypatch.setattr(backup.settings, "platform_owner_tg_id", 12345)

    from app.bots import telegram as tg

    async def fake_send(*a, **kw):
        sent.append(a)
        return True

    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    res = await backup.send_backup_to_owner()
    assert res.ok is False
    assert "не прошёл проверку" in res.message
    assert not sent, "битая копия всё-таки отправлена"


async def test_good_dump_is_sent_with_checksum(monkeypatch):
    captions = []

    body = (b"-- PostgreSQL database dump\n"
            b"CREATE TABLE public.tenants (id integer);\n" + b"-" * 4000)
    data = gzip.compress(body)

    async def fake_dump():
        return data, "backup.sql.gz"

    monkeypatch.setattr(backup, "_make_dump", fake_dump)
    monkeypatch.setattr(type(backup.settings), "is_sqlite",
                        property(lambda self: False))
    monkeypatch.setattr(backup.settings, "platform_owner_tg_id", 12345)

    from app.bots import telegram as tg

    async def fake_send(owner, name, blob, caption=""):
        captions.append(caption)
        return True

    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    res = await backup.send_backup_to_owner()
    assert res.ok is True
    # контрольная сумма едет вместе с файлом — есть с чем сверить при
    # восстановлении
    assert backup.checksum(data) in captions[0]
    assert "SHA-256" in res.message
