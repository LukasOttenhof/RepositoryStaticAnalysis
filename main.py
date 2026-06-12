"""Aida — Python programming course AI assistant."""
import os
import uuid
import json
import base64
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sqlalchemy import create_engine, Column, String, Text, DateTime
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from passlib.context import CryptContext
from jose import JWTError, jwt

from google import genai
from google.genai import types

# ── Config ─────────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "aida-dev-secret-please-change-in-production")
ALGORITHM = "HS256"
TOKEN_HOURS = 24
DB_URL = "sqlite:///./aida.db"
SYSTEM_PROMPT = (
    "You are Aida, a helpful AI assistant for students in a Python programming course. "
    "Help students understand Python concepts, debug their code, explain programming principles, "
    "and answer questions related to their coursework. Be clear, educational, and encouraging. "
    "Always use fenced code blocks with language tags (e.g. ```python) when showing code."
)

# ── Database ───────────────────────────────────────────────────────────────────
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Chat(Base):
    __tablename__ = "chats"
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class DBMessage(Base):
    __tablename__ = "messages"
    id = Column(String, primary_key=True)
    chat_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)  # JSON-encoded
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)

# ── Auth helpers ───────────────────────────────────────────────────────────────
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_pw(pw: str) -> str:
    return _pwd.hash(pw)


def verify_pw(pw: str, hashed: str) -> bool:
    return _pwd.verify(pw, hashed)


def make_token(user_id: str) -> str:
    exp = datetime.utcnow() + timedelta(hours=TOKEN_HOURS)
    return jwt.encode({"sub": user_id, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = payload.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="Invalid token")
        return uid
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_user_id(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    return decode_token(authorization[7:])


# ── Pydantic models ────────────────────────────────────────────────────────────
class AuthReq(BaseModel):
    username: str
    password: str


class FileAttach(BaseModel):
    name: str
    media_type: str
    data: str  # base64


class StreamReq(BaseModel):
    chat_id: Optional[str] = None
    message: str
    files: List[FileAttach] = []


# ── Gemini client ──────────────────────────────────────────────────────────────
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ── App setup ──────────────────────────────────────────────────────────────────
os.makedirs("static", exist_ok=True)
app = FastAPI(title="Aida")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── HTML routes ────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/ask")
async def ask_page():
    return FileResponse("static/app.html")


@app.get("/chats/{chat_id}")
async def chat_page(chat_id: str):
    return FileResponse("static/app.html")


# ── Auth endpoints ─────────────────────────────────────────────────────────────
@app.post("/api/register")
async def register(req: AuthReq):
    username = req.username.strip()
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    db = SessionFactory()
    try:
        if db.query(User).filter_by(username=username).first():
            raise HTTPException(400, "Username already taken")
        user = User(id=str(uuid.uuid4()), username=username, password_hash=hash_pw(req.password))
        db.add(user)
        db.commit()
        return {"token": make_token(user.id), "username": user.username}
    finally:
        db.close()


@app.post("/api/login")
async def login(req: AuthReq):
    db = SessionFactory()
    try:
        user = db.query(User).filter_by(username=req.username.strip()).first()
        if not user or not verify_pw(req.password, user.password_hash):
            raise HTTPException(401, "Invalid username or password")
        return {"token": make_token(user.id), "username": user.username}
    finally:
        db.close()


# ── Chat list ──────────────────────────────────────────────────────────────────
@app.get("/api/chats")
async def list_chats(user_id: str = Depends(get_user_id)):
    db = SessionFactory()
    try:
        chats = (
            db.query(Chat)
            .filter_by(user_id=user_id)
            .order_by(Chat.updated_at.desc())
            .all()
        )
        return [
            {
                "id": c.id,
                "title": c.title or "New Chat",
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in chats
        ]
    finally:
        db.close()


@app.get("/api/chats/{chat_id}/messages")
async def get_chat_messages(chat_id: str, user_id: str = Depends(get_user_id)):
    db = SessionFactory()
    try:
        chat = db.query(Chat).filter_by(id=chat_id, user_id=user_id).first()
        if not chat:
            raise HTTPException(404, "Chat not found")
        msgs = (
            db.query(DBMessage)
            .filter_by(chat_id=chat_id)
            .order_by(DBMessage.created_at)
            .all()
        )
        return {
            "chat": {"id": chat.id, "title": chat.title or "New Chat"},
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": json.loads(m.content),
                    "created_at": m.created_at.isoformat(),
                }
                for m in msgs
            ],
        }
    finally:
        db.close()


# ── Streaming endpoint ─────────────────────────────────────────────────────────
@app.post("/api/stream")
async def stream_message(req: StreamReq, user_id: str = Depends(get_user_id)):
    # Gather DB data synchronously before returning the streaming response
    db = SessionFactory()
    try:
        chat = None
        if req.chat_id:
            chat = db.query(Chat).filter_by(id=req.chat_id, user_id=user_id).first()
            if not chat:
                raise HTTPException(404, "Chat not found")

        history: List[dict] = []
        if chat:
            for msg in (
                db.query(DBMessage)
                .filter_by(chat_id=chat.id)
                .order_by(DBMessage.created_at)
                .all()
            ):
                history.append({"role": msg.role, "content": json.loads(msg.content)})

        is_new = chat is None
        chat_id = chat.id if chat else None
    finally:
        db.close()

    return StreamingResponse(
        _generate(req, user_id, chat_id, is_new, history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Stream generator ───────────────────────────────────────────────────────────
async def _generate(
    req: StreamReq,
    user_id: str,
    chat_id: Optional[str],
    is_new: bool,
    history: List[dict],
):
    client = gemini_client
    full_response = ""
    final_chat_id = chat_id

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    try:
        # ── 1. Process file attachments ────────────────────────────────────────
        stored_items: List[dict] = []   # persisted to DB
        llm_content: List[dict] = [] # sent to the LLM

        for f in req.files:
            if f.media_type.startswith("image/"):
                desc = await _describe_image(client, f.data, f.media_type)
                stored_items.append({"type": "image", "filename": f.name, "description": desc})
                llm_content.append({
                    "type": "text",
                    "text": f"[Image: {f.name}]\n{desc}",
                })
            else:
                try:
                    text = base64.b64decode(f.data).decode("utf-8", errors="replace")
                    if len(text) > 50_000:
                        text = text[:50_000] + "\n... [truncated]"
                    stored_items.append({"type": "file", "filename": f.name, "content": text})
                    llm_content.append({
                        "type": "text",
                        "text": f"[File: {f.name}]\n```\n{text}\n```",
                    })
                except Exception as e:
                    stored_items.append({"type": "file", "filename": f.name, "content": f"[read error: {e}]"})

        stored_items.append({"type": "text", "text": req.message})
        llm_content.append({"type": "text", "text": req.message})

        # ── 2. Create chat row if new ──────────────────────────────────────────
        if is_new:
            final_chat_id = str(uuid.uuid4())
            db = SessionFactory()
            try:
                db.add(Chat(id=final_chat_id, user_id=user_id))
                db.commit()
            finally:
                db.close()
            yield sse({"type": "chat_created", "chat_id": final_chat_id})

        # ── 3. Build full message list for Gemini ─────────────────────────────
        gemini_contents: List[types.Content] = []
        for h in history:
            role = h["role"]
            content = h["content"]
            gemini_role = "model" if role == "assistant" else "user"
            if isinstance(content, str):
                parts = [types.Part(text=content)]
            else:
                parts = []
                for item in content:
                    if item["type"] == "text":
                        parts.append(types.Part(text=item["text"]))
                    elif item["type"] == "file":
                        parts.append(types.Part(text=f"[File: {item['filename']}]\n```\n{item['content']}\n```"))
                    elif item["type"] == "image":
                        parts.append(types.Part(text=f"[Image: {item['filename']}]\n{item['description']}"))
            gemini_contents.append(types.Content(role=gemini_role, parts=parts))

        user_parts = [types.Part(text=b["text"]) for b in llm_content if b["type"] == "text"]
        gemini_contents.append(types.Content(role="user", parts=user_parts))

        # ── 4. Stream the response ─────────────────────────────────────────────
        async for chunk in await client.aio.models.generate_content_stream(
            model="gemini-3.5-flash",
            contents=gemini_contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=4096,
            ),
        ):
            if chunk.text:
                full_response += chunk.text
                yield sse({"type": "token", "content": chunk.text})

        # ── 5. Persist messages ────────────────────────────────────────────────
        db = SessionFactory()
        try:
            chat_row = db.query(Chat).filter_by(id=final_chat_id).first()
            if chat_row:
                chat_row.updated_at = datetime.utcnow()

            db.add(DBMessage(
                id=str(uuid.uuid4()),
                chat_id=final_chat_id,
                role="user",
                content=json.dumps(stored_items),
            ))
            db.add(DBMessage(
                id=str(uuid.uuid4()),
                chat_id=final_chat_id,
                role="assistant",
                content=json.dumps(full_response),
            ))
            db.commit()

            # ── 6. Generate title for brand-new chats ──────────────────────────
            if is_new and chat_row:
                title = await _generate_title(client, req.message)
                chat_row.title = title
                db.commit()
                yield sse({"type": "title", "title": title})
        finally:
            db.close()

        yield sse({"type": "done"})

    except HTTPException as e:
        yield sse({"type": "error", "message": e.detail})
    except Exception as e:
        yield sse({"type": "error", "message": repr(e) or type(e).__name__})


async def _describe_image(client: genai.Client, data: str, media_type: str) -> str:
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(inline_data=types.Blob(data=base64.b64decode(data), mime_type=media_type)),
                        types.Part(text="Briefly describe this image and extract any visible text. Format: Description: [desc] | Text: [text or none]"),
                    ],
                )
            ],
            config=types.GenerateContentConfig(max_output_tokens=512),
        )
        return response.text
    except Exception as e:
        return f"[Image processing failed: {e}]"


async def _generate_title(client: genai.Client, first_message: str) -> str:
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=
                        f'Create a 3-5 word title for a chat starting with: "{first_message[:200]}"\n'
                        "Reply with only the title, no quotes."
                    )],
                )
            ],
            config=types.GenerateContentConfig(max_output_tokens=20),
        )
        return response.text.strip().strip('"').strip("'")
    except Exception:
        return "New Chat"
