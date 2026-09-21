from sqlalchemy import delete, insert, select, update

from matrix_app.models import UserRecord


class UserRepository:
    def fetch_user(self, session, user: UserRecord):
        session.execute(select(UserRecord))
        return user

    def save_user(self, session, user: UserRecord):
        session.add(user)
        session.execute(insert(UserRecord))
        return user

    async def fetch_user_async(self, session, user_id: int):
        result = await session.execute(select(UserRecord))
        return result.scalars().all()

    async def get_user_async(self, session, user_id: int):
        return await session.get(UserRecord, user_id)

    async def save_user_async(self, session, user: UserRecord):
        await session.merge(user)
        await session.execute(update(UserRecord))
        await session.execute(delete(UserRecord))
        return user

    async def construct_and_delete_user_async(self, session):
        user = UserRecord()
        await session.delete(user)
        return user
