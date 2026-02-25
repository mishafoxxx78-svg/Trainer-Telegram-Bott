import asyncio
import random
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from sqlalchemy import (
    Integer,
    String,
    Boolean,
    ForeignKey,
    DateTime,
    func,
    select,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    DeclarativeBase,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


# ================= CONFIG =================

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

DATABASE_URL = "sqlite+aiosqlite:///bot.db"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ================= MODELS =================

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    attempts = relationship("Attempt", back_populates="user")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(String)
    correct_answer: Mapped[str] = mapped_column(String)
    difficulty: Mapped[str] = mapped_column(String)
    points: Mapped[int] = mapped_column(Integer)

    attempts = relationship("Attempt", back_populates="task")


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    user_answer: Mapped[str] = mapped_column(String)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    user = relationship("User", back_populates="attempts")
    task = relationship("Task", back_populates="attempts")


# ================= FSM =================

class TaskStates(StatesGroup):
    choosing_difficulty = State()
    waiting_for_answer = State()


# ================= KEYBOARDS =================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📚 Получить задачу")],
        [KeyboardButton(text="📊 Моя статистика")],
        [KeyboardButton(text="🏆 Рейтинг")],
    ],
    resize_keyboard=True,
)

difficulty_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="easy")],
        [KeyboardButton(text="medium")],
        [KeyboardButton(text="hard")],
    ],
    resize_keyboard=True,
)


# ================= DATABASE INIT =================

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed_tasks():
    async with async_session() as session:
        result = await session.execute(select(Task))
        if result.scalars().first():
            return

        session.add_all([
            Task(question="2 + 2 ?", correct_answer="4", difficulty="easy", points=1),
            Task(question="Столица Франции?", correct_answer="Париж", difficulty="medium", points=2),
            Task(question="10 * 10 ?", correct_answer="100", difficulty="hard", points=3),
        ])
        await session.commit()


# ================= BOT LOGIC =================

dp = Dispatcher()


async def get_or_create_user(telegram_id, username):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                telegram_id=telegram_id,
                username=username
            )
            session.add(user)
            await session.commit()

        return user


@dp.message(F.text == "/start")
async def start_handler(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Добро пожаловать в тренажёр программирования!",
        reply_markup=main_keyboard,
    )


@dp.message(F.text == "📚 Получить задачу")
async def choose_difficulty(message: Message, state: FSMContext):
    await state.set_state(TaskStates.choosing_difficulty)
    await message.answer(
        "Выберите уровень сложности:",
        reply_markup=difficulty_keyboard,
    )


@dp.message(TaskStates.choosing_difficulty)
async def send_task(message: Message, state: FSMContext):
    difficulty = message.text.lower()

    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.difficulty == difficulty)
        )
        tasks = result.scalars().all()

    if not tasks:
        await message.answer("Нет задач этого уровня.")
        return

    task = random.choice(tasks)

    await state.update_data(task_id=task.id)
    await state.set_state(TaskStates.waiting_for_answer)

    await message.answer(
        f"Задача ({difficulty}, {task.points} балл/балла):\n\n{task.question}",
        reply_markup=main_keyboard,
    )


@dp.message(TaskStates.waiting_for_answer)
async def check_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]

    async with async_session() as session:
        task = await session.get(Task, task_id)

        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one()

        is_correct = (
            message.text.strip().lower()
            == task.correct_answer.strip().lower()
        )

        attempt = Attempt(
            user_id=user.id,
            task_id=task.id,
            user_answer=message.text,
            is_correct=is_correct,
        )

        session.add(attempt)

        if is_correct:
            user.score += task.points
            await message.answer(f"✅ Верно! +{task.points} баллов")
        else:
            await message.answer(
                f"❌ Неверно.\nПравильный ответ: {task.correct_answer}"
            )

        await session.commit()

    await state.clear()


@dp.message(F.text == "📊 Моя статистика")
async def stats_handler(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one()

        result = await session.execute(
            select(Attempt).where(Attempt.user_id == user.id)
        )
        attempts = result.scalars().all()

    total = len(attempts)
    correct = len([a for a in attempts if a.is_correct])

    await message.answer(
        f"📊 Ваша статистика:\n\n"
        f"Баллы: {user.score}\n"
        f"Всего попыток: {total}\n"
        f"Правильных: {correct}"
    )


@dp.message(F.text == "🏆 Рейтинг")
async def rating_handler(message: Message):
    async with async_session() as session:
        result = await session.execute(
            select(User).order_by(User.score.desc()).limit(10)
        )
        users = result.scalars().all()

    text = "🏆 Топ пользователей:\n\n"

    for i, user in enumerate(users, 1):
        name = user.username if user.username else "Без имени"
        text += f"{i}. {name} — {user.score} баллов\n"

    await message.answer(text)


# ================= MAIN =================

async def main():
    await create_tables()
    await seed_tasks()

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())