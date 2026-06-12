# Aida

AI assistant for Python programming courses. Students can register, log in, and chat with an AI tutor that explains Python concepts, debugs code, and answers course questions.

## Prerequisites

- Python 3.10+
- A [Gemini API key](https://aistudio.google.com/)

## Setup

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Set your Gemini API key**

   ```bash
   # Windows (PowerShell)
   $env:GEMINI_API_KEY = "your-key-here"

   # Windows (Command Prompt)
   set GEMINI_API_KEY=your-key-here

   # macOS / Linux
   export GEMINI_API_KEY=your-key-here
   ```

3. **Run the server**

   ```bash
   python -m uvicorn main:app --reload
   ```

4. **Open the app**

   Navigate to [http://localhost:8000](http://localhost:8000) in your browser.

## Usage

- Register a new account (username ≥ 3 chars, password ≥ 6 chars)
- Start chatting — Aida will help with Python questions, code debugging, and course concepts
- You can attach images or files (e.g. screenshots of errors, `.py` files) to any message
- Chat history is saved automatically and accessible from the sidebar

## Optional configuration

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Your Gemini API key |
| `SECRET_KEY` | `aida-dev-secret-...` | JWT signing secret — change this in production |

The SQLite database (`aida.db`) is created automatically on first run.
