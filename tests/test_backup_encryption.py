"""
Резервная копия уходит в Telegram зашифрованной.

Без этого суточная копия — это отправка всей базы (телефоны, имена,
история записей) в чат: у Telegram она хранится столько, сколько живёт
аккаунт, и утечка одного сообщения раскрывает всех клиентов всех клубов.
"""
import gzip

import pytest

from app.services import backup

KEY = "тестовый-ключ-резервных-копий"


@pytest.fixture(autouse=True)
def _with_key(monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_enc_key", KEY)


def _dump() -> bytes:
    """mtime=0 обязателен: gzip пишет в заголовок текущее время, и два
    вызова по разные стороны секунды давали разные байты — сверка
    расшифрованного с эталоном падала примерно раз в несколько запусков."""
    body = b"SQLite format 3\x00" + b"\x00" * 4000
    return gzip.compress(body, mtime=0)


def test_roundtrip():
    blob = backup.encrypt_backup(_dump())
    assert blob.startswith(backup.ENC_MAGIC)
    assert b"SQLite format 3" not in blob        # содержимое не читается
    assert backup.decrypt_backup(blob) == _dump()


def test_wrong_key_is_rejected_not_garbage(monkeypatch):
    blob = backup.encrypt_backup(_dump())
    monkeypatch.setattr(backup.settings, "backup_enc_key", "другой-ключ")
    with pytest.raises(ValueError):
        backup.decrypt_backup(blob)


def test_tampered_file_is_rejected():
    """Аутентифицированное шифрование: подмена байтов не проходит молча."""
    blob = bytearray(backup.encrypt_backup(_dump()))
    blob[-5] ^= 0xFF
    with pytest.raises(ValueError):
        backup.decrypt_backup(bytes(blob))


def test_plain_archive_is_not_treated_as_encrypted():
    with pytest.raises(ValueError):
        backup.decrypt_backup(_dump())


async def test_sent_backup_is_encrypted(monkeypatch):
    sent = {}

    async def fake_dump():
        return _dump(), "backup.db.gz"

    async def fake_send(owner, name, blob, caption=""):
        sent["name"], sent["blob"], sent["caption"] = name, blob, caption
        return True

    from app.bots import telegram as tg
    monkeypatch.setattr(backup, "_make_dump", fake_dump)
    monkeypatch.setattr(backup.settings, "platform_owner_tg_id", 12345)
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    res = await backup.send_backup_to_owner()
    assert res.ok is True
    assert sent["name"].endswith(".enc")
    assert sent["blob"].startswith(backup.ENC_MAGIC)
    assert b"SQLite format 3" not in sent["blob"]
    # контрольная сумма считается по тому, что реально отправлено
    assert backup.checksum(sent["blob"]) in sent["caption"]
    # и файл действительно восстанавливается
    assert backup.decrypt_backup(sent["blob"]) == _dump()


async def test_pro_without_key_does_not_send_and_does_not_close_day(monkeypatch):
    """Ключ не задан — копия не уходит, день не помечается выполненным,
    планировщик повторит после настройки."""
    sent = []

    async def fake_dump():
        return _dump(), "backup.db.gz"

    async def fake_send(*a, **kw):
        sent.append(a)
        return True

    from app.bots import telegram as tg
    monkeypatch.setattr(backup, "_make_dump", fake_dump)
    monkeypatch.setattr(backup.settings, "backup_enc_key", "")
    monkeypatch.setattr(type(backup.settings), "is_pro",
                        property(lambda self: True))
    monkeypatch.setattr(backup.settings, "platform_owner_tg_id", 12345)
    monkeypatch.setattr(tg, "send_document_to_owner", fake_send)

    res = await backup.send_backup_to_owner()
    assert res.ok is False
    assert "BACKUP_ENC_KEY" in res.message
    assert not sent

    from app.services import tasks
    alerts = []

    async def fake_alert(where, err):
        alerts.append(where)

    monkeypatch.setattr(tasks, "_alert_admins", fake_alert)
    last_day = [None]
    await tasks._offsite_backup(last_day)
    assert last_day[0] is None, "день закрыт без копии"
    assert alerts


# ---------- инструмент восстановления ----------

def test_tool_verifies_and_decrypts(tmp_path, capsys):
    from scripts import backup_tool

    enc = tmp_path / "backup.db.gz.enc"
    enc.write_bytes(backup.encrypt_backup(_dump()))

    assert backup_tool.cmd_verify(str(enc)) == 0
    assert "Расшифрована и распакована" in capsys.readouterr().out

    out = tmp_path / "restored.db.gz"
    assert backup_tool.cmd_decrypt(str(enc), str(out), force=False) == 0
    assert out.read_bytes() == _dump()

    # существующий файл не перезаписывается без явного согласия
    assert backup_tool.cmd_decrypt(str(enc), str(out), force=False) == 2


def test_tool_reports_wrong_key(tmp_path, monkeypatch, capsys):
    from scripts import backup_tool

    enc = tmp_path / "backup.db.gz.enc"
    enc.write_bytes(backup.encrypt_backup(_dump()))
    monkeypatch.setattr(backup.settings, "backup_enc_key", "не-тот-ключ")

    assert backup_tool.cmd_verify(str(enc)) == 2
    assert "НЕ РАСШИФРОВАНА" in capsys.readouterr().out
