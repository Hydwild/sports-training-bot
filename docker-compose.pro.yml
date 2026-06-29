"""JWT и иерархия ролей, проверка подписи Telegram."""
import hashlib, hmac, time, pytest
from fastapi import HTTPException
from app.core import security
from app.core.config import settings


def test_jwt_roundtrip():
    token = security.create_token(42, 7, "coach", "Иван")
    claims = security.decode_token(token)
    assert claims["sub"] == "42" and claims["tenant_id"] == 7
    assert claims["role"] == "coach"


def test_role_hierarchy():
    assert security.ROLE_LEVEL["owner"] > security.ROLE_LEVEL["coach"]
    assert security.ROLE_LEVEL["coach"] > security.ROLE_LEVEL["assistant"]


async def test_require_role_blocks_lower():
    checker = security.require_role("coach")
    with pytest.raises(HTTPException) as e:
        await checker(claims={"role": "assistant"})
    assert e.value.status_code == 403
    # owner проходит проверку coach
    assert await checker(claims={"role": "owner"}) == {"role": "owner"}


def test_telegram_signature_valid_and_invalid():
    data = {"id": "111", "first_name": "A", "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(settings.tg_token.encode()).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    assert security.verify_telegram_auth(data) is True
    data["hash"] = "deadbeef"  # подделка
    assert security.verify_telegram_auth(data) is False
