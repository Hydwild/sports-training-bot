"""телефон перестаёт быть идентификатором записи (web_customers)

Веб-запись использовала сам номер как user_id: он лежал открытым текстом
в signups, subscribers, master_reviews и manage_tokens — и уезжал в каждую
резервную копию. Миграция заводит суррогатного клиента на клуб + номер,
шифрует номер и переписывает ссылки.

Уже разосланные ссылки отмены (вида ?u=<номер>) после этого не совпадут по
подписи и попросят открыть страницу заново. Личные ссылки управления
переживают миграцию: они привязаны к user_id, который здесь переписан.

Revision ID: a17f6b9c4d20
Revises: f92c4a1d5e73
Create Date: 2026-07-21 21:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a17f6b9c4d20'
down_revision: Union[str, None] = 'f92c4a1d5e73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# таблицы, где веб-записи ссылаются на человека
_LINKED = [
    ('signups', True),
    ('subscribers', True),
    ('manage_tokens', True),
    ('master_reviews', False),   # колонки platform нет: там только веб
]

# телефон — это минимум 10 цифр. Всё, что меньше, уже суррогатный id:
# значит миграция данных здесь уже отработала.
_MIN_PHONE = 10 ** 9


def _create_table(conn) -> None:
    if 'web_customers' in sa.inspect(conn).get_table_names():
        return
    op.create_table(
        'web_customers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('phone_index', sa.String(length=64), nullable=False),
        sa.Column('phone_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('key_ver', sa.String(length=8), nullable=False,
                  server_default='jwt'),
        sa.Column('name', sa.String(length=200), nullable=False,
                  server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'],
                                ondelete='CASCADE'),
        sa.UniqueConstraint('tenant_id', 'phone_index',
                            name='uq_web_customer'),
    )
    op.create_index('ix_web_customers_tenant_id', 'web_customers',
                    ['tenant_id'])
    op.create_index('ix_web_customers_phone_index', 'web_customers',
                    ['phone_index'])


def _existing_web_users(conn) -> set[tuple[int, int]]:
    """{(tenant_id, номер)} — все веб-записи, ещё не переведённые на id."""
    tables = set(sa.inspect(conn).get_table_names())
    found: set[tuple[int, int]] = set()
    for table, has_platform in _LINKED:
        if table not in tables:
            continue
        where = "WHERE platform = 'web'" if has_platform else ""
        rows = conn.execute(sa.text(
            f"SELECT DISTINCT tenant_id, user_id FROM {table} {where}"))
        for tenant_id, user_id in rows:
            if user_id and int(user_id) >= _MIN_PHONE:
                found.add((int(tenant_id), int(user_id)))
    return found


def upgrade() -> None:
    conn = op.get_bind()
    _create_table(conn)

    pairs = _existing_web_users(conn)
    if not pairs:
        return

    # имена берём из профиля подписчика — чтобы клиент не превратился
    # в безымянную строку в панели тренера
    names = {}
    if 'subscribers' in sa.inspect(conn).get_table_names():
        for tenant_id, user_id, name in conn.execute(sa.text(
                "SELECT tenant_id, user_id, name FROM subscribers "
                "WHERE platform = 'web'")):
            names[(int(tenant_id), int(user_id))] = name or ""

    from app.core import phones

    for tenant_id, phone in sorted(pairs):
        digits = str(phone)
        enc, key_ver = phones.encrypt(digits)
        conn.execute(sa.text(
            "INSERT INTO web_customers "
            "(tenant_id, phone_index, phone_enc, key_ver, name, created_at) "
            "VALUES (:t, :idx, :enc, :ver, :name, CURRENT_TIMESTAMP)"),
            {"t": tenant_id, "idx": phones.phone_index(digits), "enc": enc,
             "ver": key_ver, "name": names.get((tenant_id, phone), "")})
        new_id = conn.execute(sa.text(
            "SELECT id FROM web_customers WHERE tenant_id = :t "
            "AND phone_index = :idx"),
            {"t": tenant_id, "idx": phones.phone_index(digits)}).scalar_one()

        tables = set(sa.inspect(conn).get_table_names())
        for table, has_platform in _LINKED:
            if table not in tables:
                continue
            extra = " AND platform = 'web'" if has_platform else ""
            conn.execute(sa.text(
                f"UPDATE {table} SET user_id = :new "
                f"WHERE tenant_id = :t AND user_id = :old{extra}"),
                {"new": new_id, "t": tenant_id, "old": phone})

    # подпись участника содержала номер открытым текстом («Имя 📱+7999…»).
    # Телефон тренеру по-прежнему виден, но берётся расшифровкой на лету.
    if 'subscribers' in sa.inspect(conn).get_table_names():
        conn.execute(sa.text(
            "UPDATE subscribers SET alias = NULL "
            "WHERE platform = 'web' AND alias LIKE '%📱%'"))


def downgrade() -> None:
    """Откат возможен только пока клиентов нет.

    С данными он означал бы возврат телефонов открытым текстом во все
    таблицы — этого не делаем. На пустой базе (CI проверяет обратимость
    миграций) просто убираем таблицу."""
    conn = op.get_bind()
    if 'web_customers' not in sa.inspect(conn).get_table_names():
        return
    count = conn.execute(sa.text("SELECT COUNT(*) FROM web_customers")).scalar()
    if count:
        raise RuntimeError(
            f"Откат отменён: {count} клиентов, их телефоны вернулись бы "
            "в базу открытым текстом")
    op.drop_table('web_customers')
