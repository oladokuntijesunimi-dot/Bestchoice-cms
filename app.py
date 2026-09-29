import os
import functools
from datetime import datetime

from flask import (
    Flask, request, jsonify, session, redirect, url_for,
    render_template, flash, send_from_directory
)
from dotenv import load_dotenv
from groq import Groq

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

load_dotenv()

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_FILE = os.path.join(APP_ROOT, "information.txt")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Initialize Groq client safely. Some combinations of the `groq` SDK and
# underlying HTTP libraries can raise unexpected exceptions during import or
# construction. Wrap initialization so the Flask app can still start and
# surface a clear error to the API instead of crashing the whole process.
groq_client = None
GROQ_CLIENT_ERROR = None
if GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as exc:  # catch initialization-time errors
        groq_client = None
        GROQ_CLIENT_ERROR = str(exc)
        # Print to stdout/stderr so deploy logs show the underlying problem.
        print("Warning: failed to initialize Groq client:", repr(exc))

SOCIETY_NAME = "MOLETE (IBADAN) BEST CHOICE MULTIPURPOSE CO-OPERATIVE SOCIETY LTD"

# In-memory admin credentials, seeded from .env, mutable at runtime.
# Per spec: password changes apply to session/memory immediately and do not
# require rewriting the .env file (they will reset to the .env value on
# server restart -- this is intentional and should be documented for staff).
ADMIN_CREDENTIALS = {
    "username": os.environ.get("ADMIN_USERNAME", "admin"),
    "password": os.environ.get("ADMIN_PASSWORD", "changeme123"),
}

# Simple in-memory "system cache" placeholder so the Danger Zone action has
# something real to reset (e.g. a rolling log of recent Q&A / errors).
SYSTEM_CACHE = {
    "recent_queries": [],
    "last_wiped": None,
}

MAX_RECENT_QUERIES = 25
MAX_CHAT_MEMORY_TURNS = 10
MAX_CHAT_MESSAGE_CHARS = 1200


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def read_knowledge_file():
    if not os.path.exists(KNOWLEDGE_FILE):
        return ""
    with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
        return f.read()


def write_knowledge_file(content):
    with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def append_knowledge_file(content):
    existing = read_knowledge_file()
    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    with open(KNOWLEDGE_FILE, "a", encoding="utf-8") as f:
        f.write(separator + content.strip() + "\n")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def knowledge_stats():
    exists = os.path.exists(KNOWLEDGE_FILE)
    content = read_knowledge_file() if exists else ""
    char_count = len(content)
    # Non-empty lines double as our "knowledge paragraphs" counter now that
    # there is no vector store to report a chunk count from.
    line_count = len([ln for ln in content.split("\n") if ln.strip()])
    return {
        "char_count": char_count,
        "paragraph_count": line_count,
        "file_exists": exists,
        "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def sanitize_chat_history(history):
    """Keep only recent user/assistant messages in the shape Groq expects."""
    if not isinstance(history, list):
        return []

    clean_history = []
    allowed_roles = {"user", "assistant"}
    max_messages = MAX_CHAT_MEMORY_TURNS * 2

    for item in history[-max_messages:]:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = (item.get("content") or "").strip()

        if role not in allowed_roles or not content:
            continue

        clean_history.append({
            "role": role,
            "content": content[:MAX_CHAT_MESSAGE_CHARS],
        })

    return clean_history


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("chat.html", society_name=SOCIETY_NAME)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    question = (data.get("message") or "").strip()
    chat_history = sanitize_chat_history(data.get("history"))

    if not question:
        return jsonify({"error": "Please enter a question."}), 400

    if not groq_client:
        # If there was an initialization error, show that to the admin in a
        # concise form; otherwise, indicate the chatbot is not configured.
        if GROQ_CLIENT_ERROR:
            return jsonify({
                "error": "The chatbot client failed to initialize. Check server logs for details.",
                "detail": GROQ_CLIENT_ERROR,
            }), 503

        return jsonify({
            "error": "The chatbot is not configured. Please set GROQ_API_KEY "
                     "in the .env file and restart the server."
        }), 503

    knowledge_text = read_knowledge_file()

    if not knowledge_text.strip():
        return jsonify({
            "answer": "I don't have any information loaded yet. Please "
                       "contact the cooperative office directly, or ask an "
                       "administrator to update the knowledge base."
        })

    system_prompt = (
        f"You are the official AI assistant for {SOCIETY_NAME}. "
        "You will be given the COMPLETE contents of the cooperative's "
        "knowledge file below, followed by a member's question. "
        "Read the entire file carefully, find the relevant facts, and "
        "answer in your own natural, newly composed, complete sentence(s). "
        "Do not copy sentences verbatim from the source text -- synthesize "
        "a fresh, friendly, clear answer as if you were a knowledgeable "
        "staff member speaking to the member. "
        "Use the recent conversation history to understand follow-up "
        "questions, pronouns, and previous amounts or loan choices. If the "
        "member asks for a repayment, eligibility, or loan amount and the "
        "history already contains enough facts to calculate it, do the "
        "calculation instead of asking them to repeat the details. "
        "If the answer is genuinely not contained in the knowledge file, "
        "say politely that you don't have that information and suggest "
        "the member contact the cooperative office directly. Never "
        "invent facts, figures, or policies that are not in the text.\n\n"
        "----- BEGIN KNOWLEDGE FILE -----\n"
        f"{knowledge_text}\n"
        "----- END KNOWLEDGE FILE -----"
    )

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            *chat_history,
            {"role": "user", "content": question},
        ]

        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.4,
            max_tokens=600,
        )
        answer = completion.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        app.logger.exception("Groq chat completion failed (model=%s)", GROQ_MODEL)
        SYSTEM_CACHE["recent_queries"].append({
            "question": question,
            "error": str(exc),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return jsonify({
            "error": "Sorry, something went wrong while contacting the "
                     "assistant. Please try again in a moment."
        }), 502

    SYSTEM_CACHE["recent_queries"].append({
        "question": question,
        "answer": answer,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    SYSTEM_CACHE["recent_queries"] = SYSTEM_CACHE["recent_queries"][-MAX_RECENT_QUERIES:]

    return jsonify({"answer": answer})


# --------------------------------------------------------------------------
# Admin auth
# --------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (username == ADMIN_CREDENTIALS["username"]
                and password == ADMIN_CREDENTIALS["password"]):
            session.clear()
            session["admin_logged_in"] = True
            session["admin_username"] = username
            flash("Welcome back! You are now signed in.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid username or password.", "error")

    return render_template("admin_login.html", society_name=SOCIETY_NAME)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("admin_login"))


# --------------------------------------------------------------------------
# Admin dashboard
# --------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin_dashboard():
    content = read_knowledge_file()
    stats = knowledge_stats()
    return render_template(
        "admin_dashboard.html",
        society_name=SOCIETY_NAME,
        content=content,
        stats=stats,
        admin_username=session.get("admin_username", "admin"),
    )


@app.route("/admin/api/stats")
@login_required
def admin_api_stats():
    return jsonify(knowledge_stats())


@app.route("/admin/save-knowledge", methods=["POST"])
@login_required
def admin_save_knowledge():
    new_content = request.form.get("content", "")
    try:
        write_knowledge_file(new_content)
        flash("Knowledge file saved successfully.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save file: {exc}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/append-knowledge", methods=["POST"])
@login_required
def admin_append_knowledge():
    addition = request.form.get("addition", "").strip()
    if not addition:
        flash("Nothing to append -- the section was empty.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        append_knowledge_file(addition)
        flash("New section appended to the knowledge file.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not append section: {exc}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/change-password", methods=["POST"])
@login_required
def admin_change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if current != ADMIN_CREDENTIALS["password"]:
        flash("Current password is incorrect.", "error")
    elif len(new) < 8:
        flash("New password must be at least 8 characters long.", "error")
    elif new == current:
        flash("New password must be different from the current password.", "error")
    elif new != confirm:
        flash("New password and confirmation do not match.", "error")
    else:
        ADMIN_CREDENTIALS["password"] = new
        flash("Password changed successfully.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/clear-knowledge", methods=["POST"])
@login_required
def admin_clear_knowledge():
    try:
        write_knowledge_file("")
        flash("Knowledge file has been cleared.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not clear file: {exc}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/wipe-cache", methods=["POST"])
@login_required
def admin_wipe_cache():
    SYSTEM_CACHE["recent_queries"] = []
    SYSTEM_CACHE["last_wiped"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    flash("System cache has been wiped.", "success")
    return redirect(url_for("admin_dashboard"))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.path.exists(KNOWLEDGE_FILE):
        write_knowledge_file(
            f"{SOCIETY_NAME}\n\n"
            "This is a placeholder knowledge file. Use the admin portal "
            "at /admin to edit or replace this content.\n"
        )

    port = int(os.environ.get("PORT", 5004))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(debug=debug, host="0.0.0.0", port=port)
