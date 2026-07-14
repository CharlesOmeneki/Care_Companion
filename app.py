# app.py - complete Flask application (updated preview/normalization for profile + care_needed_other support)
import os
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()
print("Current working directory:", os.getcwd())
print("App file directory:", os.path.dirname(os.path.abspath(__file__)))
print("DATABASE_URL:", os.getenv("DATABASE_URL"))
print("MAIL_USERNAME:", os.getenv("MAIL_USERNAME"))


import time
import json
import re
import uuid
import base64


try:
    from pywebpush import webpush, WebPushException
except Exception:
    webpush = None
    WebPushException = Exception

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session,
    send_from_directory, jsonify, g, get_flashed_messages, abort, current_app
)

# Add near top of app.py with other imports
from PIL import Image, UnidentifiedImageError

from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message as MailMessage
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Try to import current_user from flask_login; if not available provide a safe fallback
try:
    from flask_login import current_user
except Exception:
    # Minimal fallback object so code that references current_user still works
    class _FallbackCurrentUser:
        is_authenticated = False
        def get_id(self):
            return None
    current_user = _FallbackCurrentUser()

# migration support (optional; ensure flask-migrate installed)
from flask_migrate import Migrate
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError


# SocketIO import (used later)
from flask_socketio import SocketIO, join_room, leave_room, emit

# App configuration
app = Flask(__name__)


# URL generation for emails (verification, password reset, etc.)
app.config["SERVER_NAME"] = os.environ.get(
    "SERVER_NAME",
    "findcarecompanion.com"
)
app.config["PREFERRED_URL_SCHEME"] = "https"


@app.before_request
def _debug_log_enable_actions():
    # TEMPORARY: logs requests that contain enable_actions or suppress_overlay
    if request.args.get("enable_actions") or request.args.get("suppress_overlay"):
        try:
            app.logger.warning("DBG_QUERY: path=%s args=%s referrer=%s method=%s session_keys=%s user_id=%s",
                               request.path, dict(request.args), request.referrer, request.method, list(session.keys()), session.get("user_id"))
        except Exception:
            app.logger.exception("DBG_QUERY logging failed")



def jinja_split(value, sep=','):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return [p.strip() for p in str(value).split(sep) if p.strip() != ""]
    except Exception:
        return []

def jinja_basename(value):
    """Return os.path.basename for use in templates."""
    try:
        return os.path.basename(str(value)) if value is not None else ""
    except Exception:
        return ""

# Register filters (safe to call even if you already registered some earlier)
app.jinja_env.filters.setdefault('split', jinja_split)
app.jinja_env.filters.setdefault('basename', jinja_basename)


# enable jinja2 'do' tag so templates can use {% do ... %} if needed
app.jinja_env.add_extension('jinja2.ext.do')

# Replace these in production with environment values
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "replace-with-a-secure-random-string")
app.config["SECURITY_PASSWORD_SALT"] = os.environ.get("SECURITY_PASSWORD_SALT", "replace-with-a-salt")

basedir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(basedir, "data")
if not os.path.exists(data_dir):
    os.makedirs(data_dir)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(data_dir, "users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

UPLOAD_DIR = os.path.join(data_dir, "uploads")
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "jfif", "gif", "webp"}
MAX_OTHER_PHOTOS = 20

# Flask-Mail configuration
# Using Zoho Mail SMTP settings (smtp.zoho.com). Use port 465 for SSL or 587 for TLS.
# If you have 2FA enabled on your Zoho account you may need an application-specific password.
# For production it's strongly recommended to set credentials via environment variables,
# not hard-coded in source.
# Flask-Mail configuration - SSL (alternate)
# --------------------------
# Flask-Mail configuration
# --------------------------

# Flask-Mail configuration
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.zoho.com")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 465))
app.config["MAIL_USE_SSL"] = os.environ.get("MAIL_USE_SSL", "True").lower() == "true"
app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "False").lower() == "true"
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")

app.config["MAIL_DEFAULT_SENDER"] = (
    "Care Companion",
    app.config["MAIL_USERNAME"],
)

print("\n===== MAIL CONFIG =====")
print("MAIL_USERNAME:", repr(app.config["MAIL_USERNAME"]))
print("MAIL_PASSWORD:", "Loaded" if app.config["MAIL_PASSWORD"] else "Missing")
print("MAIL_DEFAULT_SENDER:", repr(app.config["MAIL_DEFAULT_SENDER"]))
print("=======================\n")

# Initialize extensions
db = SQLAlchemy(app)
migrate = Migrate(app, db)
mail = Mail(app)
ts = URLSafeTimedSerializer(app.config["SECRET_KEY"])


# --- Socket.IO initialization (add immediately after mail = Mail(app)) ---
# Use cors_allowed_origins set appropriately for your deployment (use '*' for development)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', manage_session=False)
# -----------------------------------------------------------------------





@app.context_processor
def inject_user():
    """
    Make `user` available in every template as the SQLAlchemy User instance
    (or None). This ensures template code like `user.profile.published` works.
    This will issue a single DB lookup per request if the session contains user_id.
    """
    try:
        uid = session.get("user_id")
        if not uid:
            return dict(user=None)
        user = db.session.get(User, uid)
        return dict(user=user)
    except Exception:
        app.logger.exception("inject_user failed")
        return dict(user=None)


# Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(120), nullable=False)
    last_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), nullable=False, default="caregiver")
    verified = db.Column(db.Boolean, default=False)
    push_prompt_shown_at = db.Column(db.DateTime, nullable=True)
    push_notifications_enabled = db.Column(db.Boolean, nullable=False, default=False)
    push_notifications_rejected_at = db.Column(db.DateTime, nullable=True) 

    # NEW: soft-delete flag and active flag
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
 
    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Profile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)

    profile_photo = db.Column(db.String(500), nullable=True)
    other_photos = db.Column(db.Text, nullable=True)  # JSON list

    bio = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(200), nullable=True)  # prefer "City/Country"
    languages = db.Column(db.String(250), nullable=True)

    experience_years = db.Column(db.Integer, nullable=True)
    specialties = db.Column(db.String(500), nullable=True)
    # support a typed 'other' text stored separately
    specialties_other = db.Column(db.String(100), nullable=True)

    hourly_rate = db.Column(db.String(200), nullable=True)   # legacy field: stores salary/hourly formatted text

    # Newly persisted fields
    marital_status = db.Column(db.String(120), nullable=True)
    age = db.Column(db.Integer, nullable=True)
    disability = db.Column(db.String(120), nullable=True)
    disability_details = db.Column(db.String(100), nullable=True)

    certifications = db.Column(db.String(500), nullable=True)
    travel_radius_km = db.Column(db.Integer, nullable=True)
    # store willingness to travel (yes/no)
    willing_to_travel = db.Column(db.String(10), nullable=True)
    background_check = db.Column(db.String(10), nullable=True)
    availability = db.Column(db.String(200), nullable=True)
    services = db.Column(db.String(500), nullable=True)
    services_other = db.Column(db.String(100), nullable=True)
    preferred_age = db.Column(db.String(200), nullable=True)

    care_needed = db.Column(db.String(500), nullable=True)
    care_needed_other = db.Column(db.String(100), nullable=True)  # typed "other" stored separately
    preferred_schedule = db.Column(db.String(200), nullable=True)
    number_of_dependents = db.Column(db.Integer, nullable=True)
    care_hours_per_week = db.Column(db.Integer, nullable=True)
    household_info = db.Column(db.String(500), nullable=True)
    hourly_budget = db.Column(db.String(80), nullable=True)
    preferred_gender = db.Column(db.String(40), nullable=True)
    preferred_caregiver_age = db.Column(db.String(40), nullable=True)  # <-- added
    medical_needs = db.Column(db.Text, nullable=True)

    # NEW: whether profile has been published at least once (one-time publish)
    published = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User", backref=db.backref("profile", uselist=False))








# Updated Message model: includes reply_to_id and helper to return reply snapshot + reactions
class Message(db.Model):
    __tablename__ = "message"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.String(120), nullable=False, index=True)   # e.g. conv_2_5
    sender_id = db.Column(db.Integer, nullable=False, index=True)
    text = db.Column(db.Text, nullable=True)
    attachments = db.Column(db.Text, nullable=True)  # JSON-encoded list of attachments
    reply_to_id = db.Column(db.Integer, nullable=True)  # added column (FK not enforced here)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(40), nullable=True)

    def to_dict(self):
        """
        Return a JSON-serializable dict for this message.
        Includes a lightweight `reply_to` snippet (id, text, sender_id) and aggregated reactions.
        """
        base = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "sender_id": self.sender_id,
            "text": self.text,
            "attachments": json.loads(self.attachments) if self.attachments else [],
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None,
            "status": self.status
        }

        # attach reply_to snapshot if present
        try:
            if getattr(self, "reply_to_id", None):
                ref = db.session.get(Message, self.reply_to_id)
                if ref:
                    base["reply_to"] = {
                        "id": ref.id,
                        "sender_id": ref.sender_id,
                        "text": (ref.text or "")[:300]
                    }
                else:
                    base["reply_to"] = None
            else:
                base["reply_to"] = None
        except Exception:
            base["reply_to"] = None

        # attach aggregated reactions
        try:
            rows = MessageReaction.query.filter_by(message_id=self.id).all()
            agg = {}
            for r in rows:
                agg.setdefault(r.emoji, 0)
                agg[r.emoji] += 1
            base["reactions"] = [{"emoji": k, "count": v} for k, v in agg.items()]
        except Exception:
            base["reactions"] = []

        return base


class MessageReaction(db.Model):
    __tablename__ = "message_reaction"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    emoji = db.Column(db.String(8), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def aggregate_reactions_for_message(message_id):
    rows = MessageReaction.query.filter_by(message_id=message_id).all()
    agg = {}
    for r in rows:
        agg.setdefault(r.emoji, 0)
        agg[r.emoji] += 1
    return [{"emoji": k, "count": v} for k, v in agg.items()]


# -------------------- Per-user hidden messages --------------------
class MessageHidden(db.Model):
    __tablename__ = "message_hidden"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)






@app.context_processor
def inject_user():
    try:
        uid = session.get("user_id")
        if not uid:
            return dict(user=None, user_for_js=None)

        user = db.session.get(User, uid)
        if not user:
            return dict(user=None, user_for_js=None)

        profile = getattr(user, "profile", None)
        user_for_js = {
            "id": getattr(user, "id", None),
            "role": (getattr(user, "role", "") or "").lower(),
            "profile": {"published": bool(getattr(profile, "published", False))}
        }
        return dict(user=user, user_for_js=user_for_js)
    except Exception:
        app.logger.exception("inject_user() failed")
        return dict(user=None, user_for_js=None)


# ------------------- Newsletter subscriber model + safe blueprint -------------------
import re
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, url_for, current_app

EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

class Subscriber(db.Model):
    __tablename__ = "subscriber"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<Subscriber {self.email}>"

# Blueprint so route registration happens when we register (not at import-time)
subscribe_bp = Blueprint('subscribe_bp', __name__)

# ---- /api/subscribe (only registered users) ----
@app.route('/api/subscribe', methods=['POST'])
def api_subscribe():
    try:
        data = request.get_json(force=True, silent=True) or {}
        email = (data.get('email') or '').strip().lower()

        if not email or not EMAIL_RE.match(email):
            return jsonify({'ok': False, 'error': 'Invalid email address.'}), 400

        # Look up registered user (case-insensitive)
        try:
            user = db.session.query(User).filter(func.lower(User.email) == email.lower()).one_or_none()
        except Exception:
            user = None
            current_app.logger.exception("subscribe: user lookup failed")

        if not user:
            register_url = url_for('register', _external=True) if 'register' in current_app.view_functions else url_for('register', _external=False)
            return jsonify({
                'ok': False,
                'message': "We couldn't find an account for that email. Please register to subscribe.",
                'redirect': register_url
            }), 200

        # Check existing subscription
        try:
            existing = db.session.query(Subscriber).filter(func.lower(Subscriber.email) == email.lower()).one_or_none()
        except Exception:
            existing = None
            current_app.logger.exception("subscribe: subscriber existence check failed")

        if existing:
            return jsonify({
                'ok': True,
                'message': "You're already subscribed to Care Companion updates — we'll keep sending helpful tips and updates to that address."
            }), 200

        # Save subscriber first
        try:
            sub = Subscriber(email=email, created_at=datetime.utcnow())
            db.session.add(sub)
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            current_app.logger.exception("subscribe: DB save failed")
            return jsonify({'ok': False, 'error': 'Could not save subscription. Please try again later.'}), 500

        # Return success to the browser right away
        response = jsonify({'ok': True, 'message': 'Subscription confirmed — check your email.'})
        response.status_code = 200

        # Best-effort email/notification work after the response is prepared
        try:
            title = "Welcome — Care Companion newsletter"
            message = (
                "Thanks for subscribing to Care Companion updates. "
                "You’ll receive monthly updates with caregiver tips, checklists and early access invites."
            )

            try:
                html_body = render_template(
                    'email_subscribed.html',
                    first_name=(getattr(user, 'first_name', '') or ''),
                    home_url=(url_for('home', _external=True) if 'home' in current_app.view_functions else None),
                    profile_url=(url_for('profile_setup', _external=True) if 'profile_setup' in current_app.view_functions else None),
                    title=title,
                    message=message,
                    current_year=datetime.utcnow().year
                )
            except Exception:
                current_app.logger.exception("subscribe: render_template failed for email_subscribed.html; using fallback")
                html_body = f"<h3>{title}</h3><p>{message}</p>"

            try:
                if '_html_to_text' in globals() and callable(_html_to_text):
                    text_body = _html_to_text(html_body)
                else:
                    text_body = re.sub(r'<[^>]+>', '', html_body)
            except Exception:
                text_body = title + "\n\n" + message

            sent_ok = False
            try:
                if '_safe_send_email' in globals() and callable(_safe_send_email):
                    sent_ok = _safe_send_email(subject=title, recipients=[email], text_body=text_body, html_body=html_body)
                else:
                    msg = MailMessage(subject=title, recipients=[email])
                    msg.body = text_body
                    msg.html = html_body
                    mail.send(msg)
                    sent_ok = True
            except Exception:
                current_app.logger.exception("subscribe: email send failed; attempting async fallback")
                try:
                    if '_send_email_async' in globals() and callable(_send_email_async):
                        _send_email_async(title, [email], html_body, text_body=text_body)
                        sent_ok = True
                except Exception:
                    current_app.logger.exception("subscribe: async fallback failed")

            try:
                if 'create_notification_and_emit' in globals() and callable(create_notification_and_emit):
                    create_notification_and_emit(
                        user_id=int(user.id),
                        title="Newsletter subscription confirmed",
                        message="You have subscribed to Care Companion updates — check your email for details.",
                        delivery_email=0,
                        delivery_inapp=1,
                        user_email=user.email
                    )
                elif 'Notification' in globals() and 'NotificationRecipient' in globals():
                    n = Notification(
                        title="Newsletter subscription confirmed",
                        message="You have subscribed to Care Companion updates — check your email for details.",
                        sender="system",
                        delivery_email=False,
                        delivery_inapp=True
                    )
                    db.session.add(n)
                    db.session.flush()
                    nr = NotificationRecipient(notification_id=n.id, user_id=int(user.id), is_read=False)
                    db.session.add(nr)
                    db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                current_app.logger.exception("subscribe: creating in-app notification failed")

        except Exception:
            current_app.logger.exception("subscribe: post-save email/notification block failed")

        return response

    except Exception:
        current_app.logger.exception("subscribe: uncaught server error")
        return jsonify({'ok': False, 'error': 'Server error. Please try again later.'}), 500

# --------------------------------------------------------------------------------


# -------------------- Real-time chat (Socket.IO) integration --------------------




def get_current_user():
    """
    Return a lightweight dict of the currently logged-in user (or None).
    Uses session['user_id'] as in your app.
    """
    try:
        uid = session.get("user_id")
        if not uid:
            return None
        user = db.session.get(User, uid)
        if not user:
            return None
        prof = getattr(user, "profile", None)
        avatar = None
        if prof and getattr(prof, "profile_photo", None):
            try:
                avatar = url_for("uploaded_file", filename=prof.profile_photo)
            except Exception:
                avatar = None
        return {
            "id": user.id,
            "name": f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip() or getattr(user, "email", f"user{user.id}"),
            "avatar_url": avatar,
            "role": getattr(user, "role", None)
        }
    except Exception:
        app.logger.exception("get_current_user failed")
        return None

def get_user_brief(user_id):
    """Return minimal JSON-able summary for a user id (or None)."""
    try:
        u = db.session.get(User, user_id)
        if not u:
            return None
        prof = getattr(u, "profile", None)
        avatar = None
        if prof and getattr(prof, "profile_photo", None):
            try:
                avatar = url_for("uploaded_file", filename=prof.profile_photo)
            except Exception:
                avatar = None
        return {
            "id": u.id,
            "name": f"{u.first_name} {u.last_name}".strip() or u.email,
            "avatar_url": avatar
        }
    except Exception:
        app.logger.exception("get_user_brief failed for %s", user_id)
        return None

# -------------------- Routes used by chat.html --------------------

@app.route("/admin/users/<int:user_id>/chat")
def admin_view_user_chat(user_id):
    if not session.get("is_admin"):
        abort(403)

    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    return render_template(
        "chat.html",
        admin_view=True,
        view_user_id=user.id,
        viewed_user=user,
        other_id=None
    )



@app.route("/chat/<int:other_id>")
def chat_page(other_id):
    user = get_current_user()
    if not user:
        # send to login if not authenticated
        return redirect(url_for("login", next=url_for("chat_page", other_id=other_id)))
    return render_template("chat.html", other_id=other_id, user=user)




def get_chat_context_user():
    """
    Admin inspection mode:
    - If ?as_user_id=<id> is present and the session is admin, use that user
      even if there is no normal logged-in user session.
    - Otherwise fall back to the normal logged-in user.
    """
    try:
        as_user_id = request.args.get("as_user_id", type=int)

        # Admin view: inspect another user's chats without needing a normal user session
        if as_user_id and session.get("is_admin"):
            u = db.session.get(User, as_user_id)
            if u:
                return u, get_user_brief(u.id)

        # Normal logged-in user
        uid = session.get("user_id")
        if uid:
            u = db.session.get(User, uid)
            if u:
                return u, get_user_brief(u.id)

        return None, None
    except Exception:
        app.logger.exception("get_chat_context_user failed")
        return None, None

@app.route("/api/me")
def api_me():
    u, brief = get_chat_context_user()
    if not brief:
        return jsonify({"error": "unauthenticated"}), 401
    return jsonify(brief)


def _normalize_attachments(raw_attachments):
    """
    Return attachments as a list, regardless of whether the DB stores them
    as JSON text, a list, or None.
    """
    if not raw_attachments:
        return []
    if isinstance(raw_attachments, list):
        return raw_attachments
    if isinstance(raw_attachments, str):
        try:
            parsed = json.loads(raw_attachments)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _attachment_preview_kind(attachments):
    """
    Returns: 'image' or 'attachment'
    """
    try:
        if not attachments:
            return "attachment"
        first = attachments[0] or {}
        url = str(first.get("url") or first.get("file") or first.get("path") or "")
        atype = str(first.get("type") or "").lower()
        if atype.startswith("image") or atype == "image":
            return "image"
        if any(url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
            return "image"
    except Exception:
        pass
    return "attachment"


def _build_last_message_preview(msg):
    """
    Build a compact preview for the conversation list that supports:
    - text messages
    - image attachments
    - non-image attachments
    """
    attachments = _normalize_attachments(getattr(msg, "attachments", None))
    text = (getattr(msg, "text", "") or "").strip()

    kind = "text"
    if attachments:
        kind = _attachment_preview_kind(attachments)
        if not text:
            text = "Photo" if kind == "image" else "Attachment"

    created_at = None
    try:
        if getattr(msg, "created_at", None):
            created_at = msg.created_at.isoformat() + "Z"
    except Exception:
        created_at = None

    return {
        "id": msg.id,
        "text": text[:120],
        "created_at": created_at,
        "sender_id": msg.sender_id,
        "attachments": attachments,
        "kind": kind,
    }


@app.route("/api/contacts")
def api_contacts():
    me_obj, me = get_chat_context_user()
    if not me_obj:
        return jsonify([])

    try:
        convo_rows = db.session.query(Message.conversation_id).distinct().all()
        convo_ids = [r[0] for r in convo_rows if r and r[0]]

        contacts = []

        hidden_subq = None
        try:
            if "MessageHidden" in globals():
                hidden_subq = (
                    db.session.query(MessageHidden.message_id)
                    .filter(MessageHidden.user_id == me["id"])
                    .subquery()
                )
        except Exception:
            hidden_subq = None

        for convo_id in convo_ids:
            if not convo_id.startswith("conv_"):
                continue

            parts = convo_id.replace("conv_", "").split("_")
            if len(parts) != 2:
                continue

            try:
                p1 = int(parts[0])
                p2 = int(parts[1])
            except Exception:
                continue

            if me["id"] not in (p1, p2):
                continue

            other_id = p2 if p1 == me["id"] else p1
            other = db.session.get(User, other_id)
            if not other:
                continue

            other_profile = getattr(other, "profile", None)

            # Normal users keep your role filtering.
            # Admin sees everything.
            if not session.get("is_admin"):
                if me.get("role") == "caregiver" and getattr(other, "role", None) not in (None, "family"):
                    continue

            last = None
            try:
                last_q = Message.query.filter_by(conversation_id=convo_id)
                if hidden_subq is not None:
                    last_q = last_q.filter(~Message.id.in_(hidden_subq))

                last_msg_row = (
                    last_q
                    .order_by(Message.created_at.desc())
                    .limit(1)
                    .first()
                )
                if last_msg_row:
                    last = _build_last_message_preview(last_msg_row)
            except Exception:
                app.logger.exception("Failed to fetch last visible message for convo %s", convo_id)

            unread = 0
            try:
                unread_q = Message.query.filter(
                    Message.conversation_id == convo_id,
                    Message.sender_id != me["id"],
                    Message.status != "seen"
                )
                if hidden_subq is not None:
                    unread_q = unread_q.filter(~Message.id.in_(hidden_subq))
                unread = unread_q.count()
            except Exception:
                unread = 0

            contacts.append({
                "id": other.id,
                "name": f"{other.first_name} {other.last_name}".strip() or other.email,
                "avatar_url": (other_profile and getattr(other_profile, "profile_photo", None) and url_for("uploaded_file", filename=other_profile.profile_photo)) or None,
                "last_message": last,
                "unread": int(unread),
                "_last_dt": last and last.get("created_at")
            })

        contacts.sort(key=lambda c: c.get("_last_dt") or "", reverse=True)
        for c in contacts:
            c.pop("_last_dt", None)

        return jsonify(contacts)

    except Exception:
        app.logger.exception("Failed to assemble contacts from messages table")
        return jsonify([])

@app.route("/api/conversations/<int:other_id>")
def api_conversations(other_id):
    me_obj, me = get_chat_context_user()
    if not me_obj:
        return jsonify({"error": "unauthenticated"}), 401

    other = db.session.get(User, other_id)
    if not other:
        return jsonify({"error": "other_not_found"}), 404

    convo_id = f"conv_{min(me['id'], other_id)}_{max(me['id'], other_id)}"

    messages = []
    try:
        q = Message.query.filter_by(conversation_id=convo_id).order_by(Message.created_at.asc())

        try:
            hidden_subq = db.session.query(MessageHidden.message_id).filter(MessageHidden.user_id == me["id"]).subquery()
            q = q.filter(~Message.id.in_(hidden_subq))
        except Exception:
            pass

        q = q.all()

        for m in q:
            m_dict = m.to_dict() if hasattr(m, "to_dict") else {
                "id": m.id,
                "conversation_id": m.conversation_id,
                "sender_id": m.sender_id,
                "text": m.text,
                "attachments": _normalize_attachments(getattr(m, "attachments", None)),
                "created_at": (m.created_at.isoformat() + "Z") if m.created_at else None,
                "status": m.status
            }

            m_dict["attachments"] = _normalize_attachments(m_dict.get("attachments"))
            sender_brief = get_user_brief(m.sender_id) or {"id": m.sender_id, "name": None, "avatar_url": None}
            m_dict["sender"] = sender_brief
            messages.append(m_dict)

    except Exception:
        app.logger.exception("Failed to fetch messages for convo %s", convo_id)
        messages = []

    other_profile = getattr(other, "profile", None)
    other_obj = {
        "id": other.id,
        "name": f"{other.first_name} {other.last_name}".strip() or other.email,
        "avatar_url": (other_profile and getattr(other_profile, "profile_photo", None) and url_for("uploaded_file", filename=other_profile.profile_photo)) or None,
        "last_seen": None
    }

    return jsonify({
        "conversation_id": convo_id,
        "other": other_obj,
        "messages": messages
    })

@app.route('/api/send_message', methods=['POST'])
def api_send_message():
    """
    HTTP fallback to create a message (used by the frontend when socket is not available).
    Body: { conversation_id, text, attachments, reply_to }
    """
    me = get_current_user()
    if not me:
        return jsonify({"success": False, "error": "unauthenticated"}), 401

    payload = request.get_json(silent=True) or {}
    conversation_id = payload.get("conversation_id")
    text = (payload.get("text") or "").strip()
    attachments = payload.get("attachments") or []
    reply_to = payload.get("reply_to")  # optional message id

    if not conversation_id or (not text and not attachments):
        return jsonify({"success": False, "error": "empty"}), 400

    # Basic server-side validation: ensure the current user is part of the deterministic convo id
    try:
        parts = conversation_id.replace("conv_", "").split("_")
        if len(parts) != 2:
            return jsonify({"success": False, "error": "invalid_conversation_id"}), 400
        p1 = int(parts[0])
        p2 = int(parts[1])
        if me["id"] not in (p1, p2):
            return jsonify({"success": False, "error": "not_a_member"}), 403

        # determine the other participant id
        other_id = p2 if p1 == me["id"] else p1

        # SERVER-SIDE BLOCK ENFORCEMENT:
        def _is_blocked_between(sender_id, recipient_id):
            try:
                return Block.query.filter_by(
                    blocker_id=int(recipient_id),
                    blocked_id=int(sender_id)
                ).first() is not None
            except Exception:
                app.logger.exception("block check failed")
                return False

        if _is_blocked_between(me["id"], other_id):
            return jsonify({"success": False, "error": "blocked", "message": "You are blocked by this user."}), 403
        if _is_blocked_between(other_id, me["id"]):
            return jsonify({"success": False, "error": "blocked", "message": "You have blocked this user."}), 403

    except Exception:
        return jsonify({"success": False, "error": "invalid_conversation_id_format"}), 400

    saved_msg = None
    try:
        saved = Message(
            conversation_id=conversation_id,
            sender_id=me["id"],
            text=text or None,
            attachments=json.dumps(attachments) if attachments else None,
            reply_to_id=int(reply_to) if reply_to else None,
            status="delivered"
        )
        db.session.add(saved)
        db.session.commit()
        maybe_prompt_push_for_user(me["id"]) 
        saved_msg = saved.to_dict()
        saved_msg["sender"] = get_user_brief(me["id"]) or me
        notify_chat_recipient(me["id"], other_id, conversation_id, saved_msg) 
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to persist message (HTTP fallback)")
        saved_msg = {
            "id": int(time.time() * 1000),
            "conversation_id": conversation_id,
            "sender_id": me["id"],
            "text": text,
            "attachments": attachments,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "delivered",
            "sender": me,
            "reply_to": None,
            "reactions": []
        }

    try:
        socketio.emit("message", saved_msg, room=conversation_id)
        socketio.emit("message", saved_msg, room=f"user_{me['id']}")
        socketio.emit("message", saved_msg, room=f"user_{other_id}")
    except Exception:
        app.logger.exception("Failed to emit message from HTTP endpoint")

    return jsonify({"success": True, "message": saved_msg}), 200

# -------------------- Socket.IO event handlers --------------------

@socketio.on("identify")
def handle_identify(payload):
    me = get_current_user()
    if not me:
        return
    try:
        # always join the personal room
        join_room(f"user_{me['id']}")

        # optionally join a conversation room if the client sends it
        if isinstance(payload, dict):
            convo = payload.get("conversation_id")
            if convo:
                join_room(str(convo))

        emit("welcome", {"me": me, "user_room": f"user_{me['id']}"})
    except Exception:
        app.logger.exception("handle_identify failed")


@socketio.on("join_room")
def on_join(data):
    convo = data.get("conversation_id")
    if not convo:
        return
    try:
        join_room(convo)
        emit("joined", {"conversation_id": convo})
    except Exception:
        app.logger.exception("join_room failed")

@socketio.on("leave_room")
def on_leave(data):
    convo = data.get("conversation_id")
    if not convo:
        return
    try:
        leave_room(convo)
    except Exception:
        app.logger.exception("leave_room failed")

@socketio.on("send_message")
def on_send_message(data):
    """
    Expected data:
      { conversation_id, text, attachments, reply_to }
    Persist message into Message model then broadcast the saved message.
    """
    me = get_current_user()
    if not me:
        return emit("error", {"error": "unauthenticated"})

    conversation_id = data.get("conversation_id")
    text = (data.get("text") or "").strip()
    attachments = data.get("attachments") or []
    reply_to = data.get("reply_to")

    if not conversation_id or (not text and not attachments):
        return {"success": False, "error": "empty"}

    # Basic server-side validation: ensure the current user is part of the deterministic convo id
    try:
        parts = conversation_id.replace("conv_", "").split("_")
        if len(parts) != 2:
            return {"success": False, "error": "invalid_conversation_id"}
        p1 = int(parts[0])
        p2 = int(parts[1])
        if me["id"] not in (p1, p2):
            return {"success": False, "error": "not_a_member"}

        # determine other party id
        other_id = p2 if p1 == me["id"] else p1

        # SERVER-SIDE BLOCK ENFORCEMENT (for socket sends)
        def _is_blocked_between(sender_id, recipient_id):
            try:
                return Block.query.filter_by(
                    blocker_id=int(recipient_id),
                    blocked_id=int(sender_id)
                ).first() is not None
            except Exception:
                app.logger.exception("block check failed (socket)")
                return False

        if _is_blocked_between(me["id"], other_id):
            return {"success": False, "error": "blocked", "message": "You are blocked by this user."}
        if _is_blocked_between(other_id, me["id"]):
            return {"success": False, "error": "blocked", "message": "You have blocked this user."}

    except Exception:
        return {"success": False, "error": "invalid_conversation_id_format"}

    # Persist
    saved_msg = None
    try:
        saved = Message(
            conversation_id=conversation_id,
            sender_id=me["id"],
            text=text or None,
            attachments=json.dumps(attachments) if attachments else None,
            reply_to_id=int(reply_to) if reply_to else None,
            status="delivered"
        )
        db.session.add(saved)
        db.session.commit()
        maybe_prompt_push_for_user(me["id"]) 
        saved_msg = saved.to_dict()
        saved_msg["sender"] = get_user_brief(me["id"]) or me
        notify_chat_recipient(me["id"], other_id, conversation_id, saved_msg)
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to persist message")
        saved_msg = {
            "id": int(time.time() * 1000),
            "conversation_id": conversation_id,
            "sender_id": me["id"],
            "text": text,
            "attachments": attachments,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "delivered",
            "sender": me,
            "reply_to": None,
            "reactions": []
        }

    try:
        # Broadcast to everyone likely listening
        socketio.emit("message", saved_msg, room=conversation_id)
        socketio.emit("message", saved_msg, room=f"user_{me['id']}")
        socketio.emit("message", saved_msg, room=f"user_{other_id}")

        # Ack to sender
        return {"success": True, "message": saved_msg}
    except Exception:
        app.logger.exception("on_send_message failed broadcasting")
        return {"success": False, "error": "server_error"}

@socketio.on("typing")
def on_typing(data):
    me = get_current_user()
    if not me:
        return
    conversation_id = data.get("conversation_id")
    typing = bool(data.get("typing"))
    try:
        emit("typing", {"conversation_id": conversation_id, "name": me["name"], "typing": typing}, room=conversation_id, include_self=False)
    except Exception:
        app.logger.exception("on_typing failed")

@socketio.on("message_read")
def on_read(data):
    """
    Mark messages in this conversation as 'seen' when a user opens the convo.
    Emit message_status events for updated messages.
    """
    me = get_current_user()
    if not me:
        return
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        return

    try:
        # Update unread messages sent by the other party to 'seen'
        msgs = Message.query.filter(
            Message.conversation_id == conversation_id,
            Message.sender_id != me["id"],
            Message.status != "seen"
        ).all()

        updated_ids = []
        if msgs:
            for m in msgs:
                m.status = "seen"
                updated_ids.append(m.id)
            db.session.commit()

        # Broadcast a message_status event listing updated message ids and a timestamp
        timestamp = datetime.utcnow().isoformat() + "Z"
        # Emit an array of IDs so clients can update multiple messages fast
        emit("message_status", {"conversation_id": conversation_id, "status": "seen", "message_ids": updated_ids, "timestamp": timestamp}, room=conversation_id)
    except Exception:
        app.logger.exception("message_read failed")
        try:
            db.session.rollback()
        except Exception:
            pass

@socketio.on("disconnect")
def on_disconnect():
    app.logger.debug("socket disconnected")





# -------------------- Helper: broadcast profile updates --------------------
def broadcast_profile_update(user_id, name=None, avatar_url=None):
    """
    Call this function after a user updates their profile so open chat pages receive a realtime update.
    """
    try:
        payload = {"user_id": user_id}
        if name is not None:
            payload["name"] = name
        if avatar_url is not None:
            payload["avatar_url"] = avatar_url
        socketio.emit("profile_updated", payload)
    except Exception:
        app.logger.exception("broadcast_profile_update failed")













# -------------------- Reactions endpoint --------------------
@app.route("/api/messages/<int:msg_id>/react", methods=["POST"])
def api_react_message(msg_id):
    me = get_current_user()
    if not me:
        return jsonify({"success": False, "error": "unauthenticated"}), 401
    payload = request.get_json(silent=True) or {}
    emoji = payload.get("emoji")
    if not emoji:
        return jsonify({"success": False, "error": "missing_emoji"}), 400
    try:
        # toggle reaction for this user + emoji
        existing = MessageReaction.query.filter_by(message_id=msg_id, user_id=me["id"], emoji=emoji).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
        else:
            r = MessageReaction(message_id=msg_id, user_id=me["id"], emoji=emoji)
            db.session.add(r)
            db.session.commit()

        reactions = aggregate_reactions_for_message(msg_id)

        # try to emit socket event for this conversation so clients update in realtime
        try:
            msg = db.session.get(Message, msg_id)
            if msg:
                socketio.emit("reaction", {"message_id": msg_id, "conversation_id": msg.conversation_id, "reactions": reactions})
        except Exception:
            app.logger.exception("Failed to emit reaction event")

        return jsonify({"success": True, "reactions": reactions}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("api_react_message failed")
        return jsonify({"success": False, "error": "server_error"}), 500



# -------------------- Chat helper models & moderation endpoints --------------------

from sqlalchemy.exc import SQLAlchemyError

class Block(db.Model):
    __tablename__ = "block"
    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, nullable=False, index=True)
    blocked_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class Report(db.Model):
    __tablename__ = "report"
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, nullable=False, index=True)
    reported_id = db.Column(db.Integer, nullable=False, index=True)
    reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# Helper: check block relationship
def is_blocked_between(sender_id, recipient_id):
    """
    Return True if recipient_id has blocked sender_id.
    i.e. recipient does not want to receive messages from sender.
    """
    try:
        if sender_id is None or recipient_id is None:
            return False
        return Block.query.filter_by(blocker_id=int(recipient_id), blocked_id=int(sender_id)).first() is not None
    except Exception:
        # on DB error, default to False (or consider logging more)
        app.logger.exception("is_blocked_between check failed")
        return False


def convo_id_for_pair(a, b):
    a_i = int(a); b_i = int(b)
    return f"conv_{min(a_i,b_i)}_{max(a_i,b_i)}"

@app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
def api_delete_message(msg_id):
    """
    Delete a message globally (sender/admin) or hide it locally for the current user.
    Accepts JSON body {"local_only": true} or query param ?local_only=1 to hide locally.

    Robustness: if the ORM insert into MessageHidden fails due to an older schema
    that requires `hidden_at`, fall back to a raw INSERT that sets both hidden_at
    and created_at. This keeps runtime behavior stable without requiring an immediate
    DB migration.
    """
    me = get_current_user()
    if not me:
        return jsonify({"success": False, "error": "unauthenticated"}), 401

    # read JSON body (if any) but tolerant
    payload = {}
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    # also accept query param for convenience
    qs_local = request.args.get("local_only")
    local_only = bool(payload.get("local_only")) or (str(qs_local).lower() in ("1", "true", "yes"))

    try:
        msg = db.session.get(Message, msg_id)
        if not msg:
            return jsonify({"success": False, "error": "not_found"}), 404

        # LOCAL HIDE: create MessageHidden row for this user only
        if local_only:
            try:
                exists = MessageHidden.query.filter_by(message_id=msg_id, user_id=me["id"]).first()
                if not exists:
                    # Attempt ORM insert first (preferred)
                    try:
                        mh = MessageHidden(message_id=msg_id, user_id=me["id"])
                        db.session.add(mh)
                        db.session.commit()
                    except Exception as orm_exc:
                        # Fallback: if ORM insert fails because DB expects hidden_at (older schema),
                        # try a raw INSERT that writes hidden_at and created_at explicitly.
                        try:
                            app.logger.warning("ORM MessageHidden insert failed (will try raw fallback): %s", orm_exc)
                            from sqlalchemy import text
                            now = datetime.utcnow().isoformat(sep=' ')
                            # Some DBs may have only hidden_at column; include created_at if present.
                            # We attempt to insert into both columns; if created_at doesn't exist the execute will raise,
                            # so we try a simpler insert if needed.
                            try:
                                db.session.execute(text(
                                    "INSERT INTO message_hidden (message_id, user_id, hidden_at, created_at) VALUES (:mid, :uid, :now, :now)"
                                ), {"mid": msg_id, "uid": me["id"], "now": now})
                                db.session.commit()
                            except Exception:
                                db.session.rollback()
                                # Try without created_at (some older schemas may not have created_at)
                                try:
                                    db.session.execute(text(
                                        "INSERT INTO message_hidden (message_id, user_id, hidden_at) VALUES (:mid, :uid, :now)"
                                    ), {"mid": msg_id, "uid": me["id"], "now": now})
                                    db.session.commit()
                                except Exception as raw_exc2:
                                    db.session.rollback()
                                    app.logger.exception("Raw fallback insert into message_hidden failed: %s", raw_exc2)
                                    raise raw_exc2
                        except Exception:
                            app.logger.exception("Fallback path for MessageHidden failed")
                            return jsonify({"success": False, "error": "server_error", "message": "Could not hide message (db insert failed)"}), 500

                # Emit so client can update view (server may include user_id)
                try:
                    socketio.emit("message_hidden", {
                        "message_id": msg_id,
                        "conversation_id": msg.conversation_id,
                        "user_id": me["id"],
                        "timestamp": datetime.utcnow().isoformat() + "Z"
                    })
                except Exception:
                    app.logger.exception("Failed to emit message_hidden")

                return jsonify({"success": True, "message_id": msg_id, "local_only": True}), 200

            except Exception as e:
                db.session.rollback()
                app.logger.exception("Failed to create MessageHidden (outer): %s", e)
                return jsonify({"success": False, "error": "server_error", "message": str(e)}), 500

        # GLOBAL DELETE: only sender or admin may do this
        allowed = (msg.sender_id == me["id"]) or session.get("is_admin") or (
            hasattr(current_user, "is_authenticated") and getattr(current_user, "is_authenticated", False)
            and getattr(current_user, "id", None) == me["id"]
        )
        if not allowed:
            return jsonify({"success": False, "error": "forbidden"}), 403

        # capture attachments so we can attempt to remove files after DB delete
        attachments = []
        try:
            if msg.attachments:
                attachments = json.loads(msg.attachments)
        except Exception:
            attachments = []

        convo = msg.conversation_id
        db.session.delete(msg)
        db.session.commit()

        # try to remove attachment files (best-effort; do not fail the request if file ops fail)
        try:
            UPLOAD_FOLDER = app.config.get("UPLOAD_FOLDER", "uploads")
            for a in attachments:
                fn = None
                if isinstance(a, dict):
                    fn = a.get("filename") or a.get("file") or None
                    if not fn:
                        url = a.get("url") or ""
                        try:
                            fn = os.path.basename(url) if url else None
                        except Exception:
                            fn = None
                elif isinstance(a, str):
                    fn = os.path.basename(a)
                if not fn:
                    continue
                try:
                    path = os.path.join(UPLOAD_FOLDER, fn)
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    app.logger.exception("Failed removing attachment file %s for message %s", fn, msg_id)
        except Exception:
            app.logger.exception("Attachment cleanup encountered an unexpected error")

        # notify participants so frontends can remove message globally
        try:
            socketio.emit("message_deleted", {"message_id": msg_id, "conversation_id": convo}, room=convo)
        except Exception:
            app.logger.exception("Failed to emit message_deleted")

        return jsonify({"success": True, "message_id": msg_id}), 200

    except Exception as exc:
        db.session.rollback()
        app.logger.exception("api_delete_message failed: %s", exc)
        return jsonify({"success": False, "error": "server_error", "message": str(exc)}), 500



# Edit a message (sender only)
@app.route("/api/messages/<int:msg_id>/edit", methods=["POST"])
def api_edit_message(msg_id):
    me = get_current_user()
    if not me:
        return jsonify({"success": False, "error": "unauthenticated"}), 401
    payload = request.get_json(silent=True) or {}
    new_text = (payload.get("text") or "").strip()
    if new_text == "":
        return jsonify({"success": False, "error": "empty_text"}), 400
    try:
        msg = db.session.get(Message, msg_id)
        if not msg:
            return jsonify({"success": False, "error": "not_found"}), 404
        if msg.sender_id != me["id"] and not session.get("is_admin"):
            return jsonify({"success": False, "error": "forbidden"}), 403

        msg.text = new_text
        # optional: mark as edited in status
        msg.status = (msg.status or "") + " | edited" if msg.status else "edited"
        db.session.add(msg)
        db.session.commit()

        out = {
            "id": msg.id, "conversation_id": msg.conversation_id, "sender_id": msg.sender_id,
            "text": msg.text, "created_at": (msg.created_at.isoformat()+"Z") if msg.created_at else None, "status": msg.status
        }

        # broadcast edit to conversation
        try:
            socketio.emit("message_edited", out, room=msg.conversation_id)
        except Exception:
            app.logger.exception("Failed to emit message_edited")

        return jsonify({"success": True, "message": out}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("api_edit_message failed")
        return jsonify({"success": False, "error": "server_error"}), 500

# --- Conversation clear endpoints (local-only, production-safe) ---
from flask import request, jsonify

def _hide_conversation_locally_for_user(conversation_id, user_id):
    """
    Hide all messages in a conversation for one user only.
    This does NOT delete the messages from the Message table.
    """
    if not conversation_id or not user_id:
        return 0

    try:
        message_rows = Message.query.filter_by(conversation_id=conversation_id).all()
    except Exception:
        app.logger.exception("Failed to load messages for local clear: %s", conversation_id)
        return 0

    if not message_rows:
        return 0

    message_ids = [m.id for m in message_rows]
    inserted = 0

    # Try ORM path first if MessageHidden exists
    if 'MessageHidden' in globals():
        try:
            existing_hidden_ids = {
                row[0] for row in db.session.query(MessageHidden.message_id)
                .filter(
                    MessageHidden.user_id == int(user_id),
                    MessageHidden.message_id.in_(message_ids)
                )
                .all()
            }

            for mid in message_ids:
                if mid in existing_hidden_ids:
                    continue
                db.session.add(MessageHidden(message_id=mid, user_id=int(user_id)))

            db.session.commit()
            inserted = len([mid for mid in message_ids if mid not in existing_hidden_ids])
            return inserted
        except Exception:
            db.session.rollback()
            app.logger.exception("ORM local-clear insert failed; trying raw fallback")

    # Raw SQL fallback for older schemas
    try:
        from sqlalchemy import text
        now = datetime.utcnow().isoformat(sep=' ')
        for mid in message_ids:
            try:
                db.session.execute(text("""
                    INSERT INTO message_hidden (message_id, user_id, hidden_at, created_at)
                    VALUES (:mid, :uid, :now, :now)
                """), {"mid": mid, "uid": int(user_id), "now": now})
                inserted += 1
                continue
            except Exception:
                try:
                    db.session.execute(text("""
                        INSERT INTO message_hidden (message_id, user_id, hidden_at)
                        VALUES (:mid, :uid, :now)
                    """), {"mid": mid, "uid": int(user_id), "now": now})
                    inserted += 1
                except Exception:
                    pass

        db.session.commit()
        return inserted
    except Exception:
        db.session.rollback()
        app.logger.exception("Raw local-clear fallback failed for %s", conversation_id)
        return 0


@app.route('/api/conversations/clear', methods=['POST'])
def api_conversations_clear():
    me = get_current_user()
    if not me:
        return jsonify({'error': 'unauthenticated'}), 401

    payload = request.get_json(silent=True) or {}
    conversation_id = payload.get('conversation_id')
    other_id = payload.get('other_id')

    if not conversation_id and other_id:
        try:
            a = min(int(me['id']), int(other_id))
            b = max(int(me['id']), int(other_id))
            conversation_id = f"conv_{a}_{b}"
        except Exception:
            return jsonify({'error': 'bad_params'}), 400

    if not conversation_id:
        return jsonify({'error': 'missing_conversation_id'}), 400

    try:
        hidden_count = _hide_conversation_locally_for_user(conversation_id, me['id'])
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Failed to locally clear convo %s", conversation_id)
        return jsonify({'error': 'server_error', 'details': str(e)}), 500

    try:
        if 'socketio' in globals() and socketio:
            socketio.emit(
                'conversation_cleared',
                {
                    'conversation_id': conversation_id,
                    'other_id': other_id,
                    'user_id': me['id'],
                    'local_only': True,
                    'hidden_count': hidden_count
                },
                room=f"user_{me['id']}"
            )
    except Exception:
        app.logger.exception("socket emit failed for conversation_cleared")

    return jsonify({'success': True, 'conversation_id': conversation_id, 'local_only': True}), 200


@app.route('/api/conversations/clear_by_user/<int:other_id>', methods=['POST'])
def api_conversations_clear_by_user(other_id):
    me = get_current_user()
    if not me:
        return jsonify({'error': 'unauthenticated'}), 401

    a = min(int(me['id']), int(other_id))
    b = max(int(me['id']), int(other_id))
    conversation_id = f"conv_{a}_{b}"

    try:
        hidden_count = _hide_conversation_locally_for_user(conversation_id, me['id'])
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Failed to locally clear convo (by_user) %s", conversation_id)
        return jsonify({'error': 'server_error', 'details': str(e)}), 500

    try:
        if 'socketio' in globals() and socketio:
            socketio.emit(
                'conversation_cleared',
                {
                    'conversation_id': conversation_id,
                    'other_id': other_id,
                    'user_id': me['id'],
                    'local_only': True,
                    'hidden_count': hidden_count
                },
                room=f"user_{me['id']}"
            )
    except Exception:
        app.logger.exception("socket emit failed for conversation_cleared (by_user)")

    return jsonify({'success': True, 'conversation_id': conversation_id, 'local_only': True}), 200

# Block / unblock user
@app.route("/api/block/<int:other_id>", methods=["POST"])
def api_block_user(other_id):
    me = get_current_user()
    if not me:
        return jsonify({"success": False, "error": "unauthenticated"}), 401
    payload = request.get_json(silent=True) or {}
    should_block = payload.get("block", True)
    try:
        if should_block:
            # idempotent create
            exists = Block.query.filter_by(blocker_id=me["id"], blocked_id=other_id).first()
            if not exists:
                b = Block(blocker_id=me["id"], blocked_id=other_id)
                db.session.add(b)
                db.session.commit()
            # inform client(s)
            try:
                socketio.emit("user_blocked", {"blocker_id": me["id"], "blocked_id": other_id})
            except Exception:
                app.logger.exception("emit user_blocked")
            return jsonify({"success": True, "blocked": True}), 200
        else:
            # unblock
            removed = Block.query.filter_by(blocker_id=me["id"], blocked_id=other_id).delete()
            db.session.commit()
            # inform clients about unblock (so UIs can refresh)
            try:
                socketio.emit("user_unblocked", {"blocker_id": me["id"], "blocked_id": other_id})
            except Exception:
                app.logger.exception("emit user_unblocked")
            return jsonify({"success": True, "blocked": False, "removed": int(removed)}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("api_block_user failed")
        return jsonify({"success": False, "error": "server_error"}), 500

# List blocked by current user
@app.route("/api/blocked", methods=["GET"])
def api_list_blocked():
    me = get_current_user()
    if not me:
        return jsonify([]), 200
    try:
        rows = Block.query.filter_by(blocker_id=me["id"]).all()
        out = [{"blocked_id": r.blocked_id, "created_at": (r.created_at.isoformat()+"Z") if r.created_at else None} for r in rows]
        return jsonify(out), 200
    except Exception:
        app.logger.exception("api_list_blocked failed")
        return jsonify([]), 200


# Report a user (creates an audit row + sends email to admin)

@app.route("/api/report/<int:other_id>", methods=["POST"])
def api_report_user(other_id):
    me = get_current_user()
    if not me:
        return jsonify({"success": False, "error": "unauthenticated"}), 401

    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "").strip() or "user"
    reason = (payload.get("reason") or "").strip() or None
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Discover report table columns so we can insert correctly into whichever schema is present.
        cols_info = db.session.execute(text("PRAGMA table_info('report')")).fetchall()
        existing_cols = [r[1] for r in cols_info]  # r[1] is column name

        insert_cols = []
        insert_vals = []
        params = {}

        # reporter_id always present in our code
        if "reporter_id" in existing_cols:
            insert_cols.append("reporter_id")
            insert_vals.append(":reporter_id")
            params["reporter_id"] = int(me["id"])

        # include 'kind' if present, default to 'user'
        if "kind" in existing_cols:
            insert_cols.append("kind")
            insert_vals.append(":kind")
            params["kind"] = kind

        # Some DBs have target_id (NOT NULL) and/or reported_id; include whichever exist.
        # We'll populate both (if both exist) with the same value (other_id) so NOT NULL constraints are satisfied.
        if "target_id" in existing_cols:
            insert_cols.append("target_id")
            insert_vals.append(":target_id")
            params["target_id"] = int(other_id)

        if "reported_id" in existing_cols:
            insert_cols.append("reported_id")
            insert_vals.append(":reported_id")
            params["reported_id"] = int(other_id)

        # reason is optional (may or may not exist); include if present
        if "reason" in existing_cols:
            insert_cols.append("reason")
            insert_vals.append(":reason")
            params["reason"] = reason

        # created_at / created may exist under multiple names; try common ones
        if "created_at" in existing_cols:
            insert_cols.append("created_at")
            insert_vals.append(":created_at")
            params["created_at"] = now
        elif "created" in existing_cols:
            insert_cols.append("created")
            insert_vals.append(":created")
            params["created"] = now

        if not insert_cols:
            # Defensive: nothing to insert (very odd)
            return jsonify({"success": False, "error": "no_report_columns"}), 500

        insert_sql = "INSERT INTO report (%s) VALUES (%s)" % (", ".join(insert_cols), ", ".join(insert_vals))

        # Execute and commit
        db.session.execute(text(insert_sql), params)
        db.session.commit()

        # Fetch last inserted id (best-effort)
        row = db.session.execute(text("SELECT max(id) as id FROM report")).fetchone()
        report_id = int(row[0]) if row and row[0] is not None else None

        # Send notification email (non-fatal)
        try:
            subject = f"[Care Companion] New report #{report_id or ''}"
            body = (
                f"Reporter: {me.get('name') or me.get('id')} (id={me.get('id')})\n"
                f"Reported ID: {other_id}\n"
                f"Kind: {kind}\n\n"
                f"Reason:\n{reason or '(none)'}\n\n"
                f"Time: {now}\n"
            )
            msg = MailMessage(subject, recipients=["carecompanion@zohomail.com"], body=body)
            mail.send(msg)
        except Exception:
            app.logger.exception("Failed to send report email (non-fatal)")

        return jsonify({"success": True, "report_id": report_id}), 200

    except Exception as e:
        db.session.rollback()
        app.logger.exception("api_report_user failed")
        # return error detail to help debugging (remove `detail` in production)
        return jsonify({"success": False, "error": "server_error", "detail": str(e)}), 500


@app.route("/api/upload_attachment", methods=["POST"])
def upload_attachment():
    """
    Upload an image attachment for messages.
    Returns JSON: { success: True, url: "<public url>", filename: "<file>" }
    """
    try:
        if "user_id" not in session:
            return jsonify({"success": False, "error": "Not authenticated"}), 401

        user = db.session.get(User, session["user_id"])
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 404

        f = request.files.get("attachment")
        if not f or not getattr(f, "filename", ""):
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        if not allowed_file(f.filename):
            return jsonify({"success": False, "error": "File type not allowed"}), 400

        # Try to normalize and save image (similar to profile_update_photo)
        try:
            try:
                if hasattr(f.stream, "seek"):
                    f.stream.seek(0)
            except Exception:
                pass

            img = Image.open(f.stream)
            img.load()
            img = img.convert("RGB")
            # optional: limit dimensions
            MAX_DIM = (3000, 3000)
            img.thumbnail(MAX_DIM, Image.LANCZOS)

            base = secure_filename(f"{user.id}_attach_{int(time.time())}")
            fname = f"{base}.jpg"
            save_path = os.path.join(app.config.get("UPLOAD_FOLDER", "uploads"), fname)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            img.save(save_path, format="JPEG", quality=85, optimize=True)
        except UnidentifiedImageError:
            return jsonify({"success": False, "error": "Uploaded file is not a valid image"}), 400
        except Exception as exc:
            app.logger.exception("Failed to process attachment upload: %s", exc)
            return jsonify({"success": False, "error": "Failed to process image"}), 500

        # Public URL for the uploaded file (reuses your /uploads/ handler)
        url = url_for("uploaded_file", filename=fname, _external=False)
        return jsonify({"success": True, "url": url, "filename": fname})
    except Exception:
        app.logger.exception("Unhandled error in upload_attachment")
        return jsonify({"success": False, "error": "Internal server error"}), 500

# -------------------- End moderation endpoints --------------------

# ---------- Invite model + routes (paste into your app.py) ----------
# imports used by this block (most are already present in your file)
import uuid
from datetime import datetime, timedelta

# Invite model (place near other SQLAlchemy models)
class Invite(db.Model):
    __tablename__ = "invite"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uses = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.String(500), nullable=True)

    creator = db.relationship("User", backref=db.backref("invites", lazy="dynamic"))



# Route: create invite and redirect to the share UI
@app.route("/invites/create_and_share")
def create_invite_and_share():
    """
    Create an invite token and redirect the inviter to the share UI where they can pick an app.
    If not logged-in, send user to login (your existing login route). After login you can return them here.
    """
    me = get_current_user()
    if not me:
        # redirect to login and then back here
        return redirect(url_for("login", next=url_for("create_invite_and_share")))

    try:
        token = uuid.uuid4().hex[:16]  # shortened but still collision-resistant
        inv = Invite(token=token, created_by=int(me["id"]), message=None)
        db.session.add(inv)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed creating invite token")
        # graceful fallback: redirect back with flash
        flash("Could not create invite. Please try again.", "error")
        return redirect(url_for("profile_setup"))

    # Redirect inviter to the share page where they choose the app to send to
    return redirect(url_for("invite_share", token=token))


# Route: invitor share UI (where the clicking user selects which social app to share on)
# --- invite share route (robust avatar resolution) ---
import os  # ensure this is imported at the top of app.py (it already is in your file)

@app.route("/invite/<token>/share")
def invite_share(token):
    """
    Render the modern invite share page.
    Builds a deterministic avatar_url for the inviter so the template never receives an empty src.
    """
    me = get_current_user()
    if not me:
        return redirect(url_for("login", next=url_for("invite_share", token=token)))

    inv = Invite.query.filter_by(token=token).first()
    if not inv:
        abort(404)

    # build the public invite URL (landing page)
    invite_url = url_for("invite_landing", token=token, _external=True)

    inviter = None
    try:
        if inv and inv.created_by:
            u = db.session.get(User, inv.created_by)
            if u:
                prof = getattr(u, "profile", None)
                avatar_url = None
                has_profile_photo = False

                # prefer profile.profile_photo if present
                if prof and getattr(prof, "profile_photo", None):
                    try:
                        fn = os.path.basename(prof.profile_photo)
                        if fn:
                            avatar_url = url_for("uploaded_file", filename=fn)
                            has_profile_photo = True
                    except Exception:
                        avatar_url = None

                # fallback to simple top-level fields if profile missing (compat)
                if not avatar_url and getattr(u, "profile_photo", None):
                    try:
                        fn = os.path.basename(u.profile_photo)
                        if fn:
                            avatar_url = url_for("uploaded_file", filename=fn)
                            has_profile_photo = True
                    except Exception:
                        avatar_url = None

                # guaranteed fallback: static default image
                if not avatar_url:
                    try:
                        avatar_url = url_for("static", filename="images/default_user.png")
                    except Exception:
                        avatar_url = "/static/images/default_user.png"

                inviter = {
                    "id": u.id,
                    "first_name": getattr(u, "first_name", ""),
                    "last_name": getattr(u, "last_name", ""),
                    "name": f"{getattr(u,'first_name','').strip()} {getattr(u,'last_name','').strip()}".strip() or getattr(u, "email", None) or f"user{u.id}",
                    "avatar_url": avatar_url,
                    "has_profile_photo": bool(has_profile_photo)
                }
    except Exception:
        app.logger.exception("invite_share: failed to resolve inviter info")
        inviter = None

    return render_template("invite_share.html", invite=inv, invite_url=invite_url, inviter=inviter)


# Route: public invite landing (what recipients see when they click the shared link)
@app.route("/i/<token>")
def invite_landing(token):
    # find invite
    inv = Invite.query.filter_by(token=token).first()
    if not inv:
        # you can render a friendly 404 template here instead
        abort(404)

    # increment use (best-effort)
    try:
        inv.uses = (inv.uses or 0) + 1
        db.session.add(inv)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to increment invite uses")

    # redirect immediately to home
    return redirect(url_for('home'))



# --- Photo share (independent from invite) ---
import os  # ensure os is imported near top of your app.py (it already is in your file)

@app.route("/share/photo")
def share_photo():
    """
    Dedicated page for sharing a single asset (photo) URL.
    Query param:
      ?asset=<url>   (optional) - public URL of the photo to share
    """
    asset = request.args.get('asset')
    public_asset = asset if (asset and str(asset).strip()) else url_for('home', _external=True)

    inviter = None
    try:
        uid = session.get('user_id')
        if uid:
            u = db.session.get(User, uid)
            if u:
                # attempt to construct a reliable uploaded_file URL if profile_photo exists
                avatar_url = None
                prof = getattr(u, "profile", None)
                if prof and getattr(prof, "profile_photo", None):
                    try:
                        # use basename to be robust against full paths or accidental prefixes
                        fn = os.path.basename(prof.profile_photo)
                        if fn:
                            avatar_url = url_for('uploaded_file', filename=fn)
                    except Exception:
                        avatar_url = None

                # fallback to a guaranteed static default if avatar_url not resolved
                if not avatar_url:
                    try:
                        avatar_url = url_for('static', filename='images/avatar cc.jfif')
                    except Exception:
                        avatar_url = '/static/images/avatar cc.jfif'

                inviter = {
                    "id": u.id,
                    "name": f"{getattr(u,'first_name','').strip()} {getattr(u,'last_name','').strip()}".strip() or getattr(u, 'email', None) or f"user{u.id}",
                    "avatar_url": avatar_url,
                    "has_profile_photo": bool(prof and getattr(prof, "profile_photo", None))
                }
    except Exception:
        app.logger.exception("share_photo: failed to build inviter info")
        inviter = None

    return render_template("share_photo.html", asset_url=public_asset, inviter=inviter)






# ------------------- IMAGE HELPERS (NEW) -------------------
def allowed_file(filename):
    """Return True if filename has an allowed image extension."""
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def normalize_image_and_save(file_storage, dest_dir, convert_to="JPEG", max_size=(2000, 2000)):
    """
    Normalize incoming image (works for jfif/jpeg/png/webp/gif) and save as a unique JPEG.
    Returns the saved basename filename (not full path).
    Raises an exception if the file is not a valid image or saving fails.
    """
    os.makedirs(dest_dir, exist_ok=True)

    # Pillow will raise if the stream isn't a valid image
    img = Image.open(file_storage.stream)
    img = img.convert("RGB")  # convert to RGB so we can save as JPEG reliably

    # Resize/thumbnail to avoid saving huge images (keeps aspect ratio)
    img.thumbnail(max_size, Image.ANTIALIAS)

    ext = "jpg" if convert_to.upper() in ("JPEG", "JPG") else "png"
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    out_path = os.path.join(dest_dir, unique_name)

    # Save with reasonable quality
    img.save(out_path, format=convert_to, quality=85, optimize=True)

    return unique_name

def parse_other_photos_field(profile):
    """
    Return a Python list of photo filenames (basenames) for the given Profile instance.

    Accepts legacy formats:
      - None -> []
      - JSON list string -> parsed list
      - comma-separated string -> split into list
      - already a Python list -> returned as list

    ALWAYS returns a list of non-empty basenames (no leading paths or URLs).
    """
    if not profile:
        return []
    raw = getattr(profile, "other_photos", None)
    if not raw:
        return []

    items = []
    # Already a native list/tuple
    if isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw if p and str(p).strip()]
    else:
        # string-ish: try JSON first
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return []
            try:
                parsed = json.loads(s)
                if isinstance(parsed, (list, tuple)):
                    items = [str(p).strip() for p in parsed if p and str(p).strip()]
                else:
                    # not a list -> fallback to CSV
                    items = [p.strip() for p in s.split(",") if p.strip()]
            except Exception:
                # not JSON -> fallback to CSV
                items = [p.strip() for p in s.split(",") if p.strip()]
        else:
            # fallback: coerce to string and split by comma
            try:
                s = str(raw)
                items = [p.strip() for p in s.split(",") if p.strip()]
            except Exception:
                items = []

    # Normalize every item to a safe basename (strip URLs/paths)
    cleaned = []
    for it in items:
        bn = _basename_safe(it)
        if bn:
            cleaned.append(bn)
    return cleaned



def _basename_safe(val):
    """
    Return only the filename (basename) for any input that may be a URL/path/filename.
    Returns None for falsy input.
    """
    try:
        if not val:
            return None
        s = str(val).strip()
        if s == "":
            return None
        # If it's a URL, parse path portion
        try:
            from urllib.parse import urlparse, unquote
            u = urlparse(s)
            # if it looks like a URL (has scheme or netloc) use the path part
            if u.scheme or u.netloc:
                path = unquote(u.path or "")
            else:
                path = s
        except Exception:
            path = s
        # If path contains segment like '/uploads/...', take basename of whole path
        return os.path.basename(path)
    except Exception:
        try:
            return os.path.basename(str(val))
        except Exception:
            return None


# ----------------- END IMAGE HELPERS -----------------------





# Helpers
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# currency symbols for the 10 suggested currencies (includes NGN)
CURRENCY_SYMBOLS = {
    "NGN": "₦",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "CAD": "C$",
    "AUD": "A$",
    "ZAR": "R",
    "GHS": "GH₵",
    "KES": "KSh",
    "INR": "₹",
}


def format_money(amount_raw):
    """
    Convert numeric amount (string/number) to whole-number string with thousands separators.
    Returns None if invalid.
    """
    try:
        if amount_raw is None or str(amount_raw).strip() == "":
            return None
        n = float(str(amount_raw).replace(",", "").strip())
        if n < 0:
            return None
        return f"{int(round(n)):,}"
    except Exception:
        return None


def format_time_human(hhmm):
    """
    Convert 'HH:MM' (24h) to '8am' or '6:30pm' style. If invalid return original string.
    """
    try:
        t = datetime.strptime(hhmm, "%H:%M")
        hour = t.hour
        minute = t.minute
        suffix = "am" if hour < 12 else "pm"
        hour12 = hour % 12
        if hour12 == 0:
            hour12 = 12
        if minute == 0:
            return f"{hour12}{suffix}"
        return f"{hour12}:{minute:02d}{suffix}"
    except Exception:
        return hhmm or ""


def _first_nonempty(*vals):
    """
    Return the first value that is not None and not an empty string (after str().strip()).
    Returns None if none found.

    NOTE: returns the original non-string value (e.g. int) if provided so numeric fields
    like age or experience_years stay numeric where possible.
    """
    for v in vals:
        if v is None:
            continue
        # If it's not a string, consider it present (e.g. int)
        if not isinstance(v, str):
            # but still guard against empty containers
            try:
                if hasattr(v, "__len__") and len(v) == 0:
                    continue
            except Exception:
                pass
            return v
        try:
            s = v.strip()
        except Exception:
            s = ""
        if s != "":
            return s
    return None


def none_if_blank(s):
    if s is None:
        return None
    s2 = (s if not isinstance(s, str) else s.strip())
    return s2 if (s2 is not None and s2 != "") else None


def safe_int(val):
    try:
        return int(val) if val not in (None, "", False) else None
    except (ValueError, TypeError):
        return None


# ---------------------------
# New helper functions: humanize tokens / CSV for preview & persistence
# ---------------------------
def humanize_token(token):
    """Turn a token like 'light_housekeeping' or 'wound_care' into 'Light housekeeping' / 'Wound care'.
    Also accept 'Other: text' style tokens and comma-separated lists."""
    if token is None:
        return None
    s = str(token).strip()
    if s == "":
        return None
    low = s.lower()
    # typed other forms: "Other: Speech therapy" or "other=Speech"
    if low.startswith("other:") or low.startswith("other="):
        # return the typed part, capitalized-ish
        try:
            part = s.split(":", 1)[1] if ":" in s else s.split("=", 1)[1]
            return part.strip().capitalize() if part and part.strip() else "Other"
        except Exception:
            return s.capitalize()
    # if CSV, humanize each piece
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        return ", ".join([humanize_token(p) or p for p in parts])
    # prettify underscores/hyphens
    pretty = s.replace("_", " ").replace("-", " ").strip()
    return pretty.capitalize()


def humanize_csv(csv_value):
    """Given CSV string or list, return a humanized CSV string (comma + space).
       Returns None if nothing meaningful found."""
    if csv_value is None:
        return None
    if isinstance(csv_value, (list, tuple)):
        parts = [str(x).strip() for x in csv_value if str(x).strip()]
    else:
        parts = [p.strip() for p in str(csv_value).split(",") if p.strip()]
    cleaned = [p for p in parts if p.lower() not in ("none", "null", '"none"', '"null"')]
    if not cleaned:
        return None
    humanized = [humanize_token(p) for p in cleaned]
    humanized = [h for h in humanized if h]
    return ", ".join(humanized) if humanized else None
# ---------------------------


def normalize_for_template(d):
    """
    Normalize a dict (possibly session-stored values) into a shape that's friendly for templates.
    Ensures multi-value CSV strings are preserved as strings and lists aren't passed accidentally.
    """
    out = {}
    if not d:
        return out
    for k, v in d.items():
        if v is None:
            out[k] = None
        elif isinstance(v, list):
            # prefer comma-joined string for templates expecting CSV
            out[k] = ",".join([str(x).strip() for x in v if str(x).strip() != ""])
        else:
            out[k] = v
    return out


def build_preview(user, profile, draft1, draft2, other_photos):
    """
    Build a single preview dict used by profile_setup.html so the template doesn't need
    to do complicated fallbacks logic. Priority: draft2 -> draft1 -> profile -> fallback.
    This version defensively normalizes None and non-string values so .strip() is never
    called on None.
    """
    p = {}

    # profile photo: prefer draft1.profile_photo, else persisted profile
    profile_photo = None
    if draft1:
        profile_photo = draft1.get("profile_photo") if draft1.get("profile_photo") else None
    if profile_photo is None and profile:
        profile_photo = getattr(profile, "profile_photo", None)
    p["profile_photo"] = profile_photo

    # name
    p["display_name"] = f"{user.first_name} {user.last_name}" if user else ""

    # bio: prefer draft1.bio -> profile.bio -> empty string
    bio_raw = _first_nonempty(draft1.get("bio") if draft1 else None, getattr(profile, "bio", None))
    p["bio"] = bio_raw.strip() if isinstance(bio_raw, str) else (bio_raw or None)

    # marital & age (from step1)
    # tolerate alternate key names
    ms_raw = _first_nonempty(
        draft1.get("marital_status") if draft1 else None,
        draft1.get("marital") if draft1 else None,
        getattr(profile, "marital_status", None)
    )
    p["marital_status"] = ms_raw or None

    # tolerate age, age_years, years_old
    age_src = None
    if draft1:
        for key in ("age", "age_years", "years_old"):
            v = draft1.get(key)
            if v not in (None, "", False):
                age_src = v
                break
    if age_src is None and profile and getattr(profile, "age", None) not in (None, "", False):
        age_src = profile.age
    try:
        p["age"] = int(age_src) if age_src not in (None, "") else None
    except Exception:
        p["age"] = None

    # location (prefer structured draft2 location, else profile.location, else draft1)
    loc_raw = _first_nonempty(draft2.get("location") if draft2 else None, getattr(profile, "location", None), draft1.get("location") if draft1 else None)
    p["location"] = loc_raw or None

    # languages
    languages_raw = _first_nonempty(draft2.get("languages") if draft2 else None, getattr(profile, "languages", None), draft1.get("languages") if draft1 else None)
    p["languages"] = languages_raw or None

    # Experience
    exp_raw = None
    if draft2 and draft2.get("experience_years") not in (None, ""):
        exp_raw = draft2.get("experience_years")
    elif profile and profile.experience_years is not None:
        exp_raw = profile.experience_years
    try:
        p["experience_years"] = int(exp_raw) if exp_raw not in (None, "") else None
    except Exception:
        p["experience_years"] = None

    # Salary: draft2 may have formatted 'hourly_rate' or structured fields, else profile.hourly_rate
    salary = None
    if draft2:
        # structured draft2 fields preferred if available
        if draft2.get("salary_amount") and draft2.get("salary_currency") and draft2.get("salary_frequency"):
            fm = format_money(draft2.get("salary_amount"))
            sym = CURRENCY_SYMBOLS.get(draft2.get("salary_currency"), draft2.get("salary_currency"))
            if fm:
                salary = f"{sym}{fm}/{draft2.get('salary_frequency')}"
        # fallback to already formatted hourly_rate in draft2
        if not salary and draft2.get("hourly_rate"):
            salary = draft2.get("hourly_rate")
    # fallback to profile.hourly_rate (legacy)
    if not salary and profile and getattr(profile, "hourly_rate", None):
        salary = profile.hourly_rate
    p["salary"] = salary or None

   # --- Specialties: database is the source of truth after save ---
    specialties_raw = _first_nonempty(
        getattr(profile, "specialties", None),
        draft2.get("specialties") if draft2 else None
    )

    specialties_other_raw = _first_nonempty(
        getattr(profile, "specialties_other", None),
        draft2.get("specialties_other") if draft2 else None
    ) 

    # Normalize specialties so templates that check for the literal token 'other' work.
    def _normalize_token_contains_other(val):
        if val is None:
            return False
        if isinstance(val, list):
            for x in val:
                try:
                    if str(x).strip().lower() == "other":
                        return True
                except Exception:
                    continue
            return False
        if isinstance(val, str):
            s = val.strip().lower()
            if s == "other" or s == "others":
                return True
            for part in re.split(r"[,|;]", s):
                if part.strip() == "other":
                    return True
                if part.strip().startswith("other:") or part.strip().startswith("other="):
                    return True
            return False
        return False

    if _normalize_token_contains_other(specialties_raw):
        if specialties_other_raw:
            p["specialties"] = specialties_other_raw
        else:
            p["specialties"] = "Other"
    else:
        p["specialties"] = specialties_raw or None

    p["specialties_other"] = specialties_other_raw or None

    # preferred ages (caregiver)
    preferred_age_raw = _first_nonempty(draft2.get("preferred_age") if draft2 else None, getattr(profile, "preferred_age", None))
    p["preferred_age"] = preferred_age_raw or None

    # disability and details (robust)
    disability_raw = _first_nonempty(draft2.get("disability") if draft2 else None, getattr(profile, "disability", None))
    p["disability"] = (disability_raw or None)

    disability_details_raw = _first_nonempty(draft2.get("disability_details") if draft2 else None, getattr(profile, "disability_details", None))
    if isinstance(p["disability"], str) and p["disability"].strip().lower() in ("yes", "y", "true", "1", "on"):
        p["disability_details"] = disability_details_raw or None
    else:
        p["disability_details"] = disability_details_raw or None

    # availability: attempt to produce a single canonical string in p["availability"]
    # prefer structured draft2 (days + start/end), otherwise parse persisted profile.availability
    def _build_availability_from_structured(src):
        if not src:
            return None
        days_raw = src.get("availability_days") if (src.get("availability_days") is not None) else None
        start = src.get("availability_start") if (src.get("availability_start") is not None) else None
        end = src.get("availability_end") if (src.get("availability_end") is not None) else None

        days_list = []
        if days_raw:
            if isinstance(days_raw, (list, tuple)):
                days_list = [d.strip() for d in days_raw if d and str(d).strip()]
            else:
                # CSV-like
                days_list = [d.strip() for d in re.split(r'[,|;]', str(days_raw)) if d.strip()]

        # normalize full-week to "Mon-Sun"
        days_label = None
        if days_list:
            norm_days = [d for d in days_list if d]
            # simple heuristic: if 7 unique day tokens -> Mon-Sun
            if len(norm_days) >= 7:
                days_label = "Mon-Sun"
            else:
                days_label = ", ".join(norm_days)

        if start and end:
            return (f"{days_label}, {format_time_human(start)}–{format_time_human(end)}") if days_label else f"{format_time_human(start)}–{format_time_human(end)}"
        else:
            return days_label or None

    # try draft2 structured first
    avail_from_draft = None
    try:
        if draft2:
            avail_from_draft = _build_availability_from_structured(draft2)
    except Exception:
        avail_from_draft = None

    avail = avail_from_draft
    if not avail and profile and getattr(profile, "availability", None):
        # parse persisted availability string for time-range pattern
        try:
            avail_str = str(profile.availability).strip()
            # find time range pattern like "HH:MM--HH:MM" or "HH:MM–HH:MM"
            m = re.search(r'(\d{1,2}:\d{2})\s*[-–—]{1,2}\s*(\d{1,2}:\d{2})', avail_str)
            if m:
                s = m.group(1); e = m.group(2)
                # remove the matched time-range from text to get a days part
                days_part = re.sub(re.escape(m.group(0)), '', avail_str).strip().strip(',').strip()
                # split days_part into tokens
                parts = [x.strip() for x in re.split(r'[,|;]', days_part) if x.strip()]
                # if days_part itself contains "Mon-Sun" or 7 days, collapse
                days_label = None
                lowered = [p.lower() for p in parts]
                if any("mon-sun" in p for p in lowered) or len(parts) >= 7:
                    days_label = "Mon-Sun"
                elif parts:
                    days_label = ", ".join(parts)
                if days_label:
                    avail = f"{days_label}, {format_time_human(s)}–{format_time_human(e)}"
                else:
                    avail = f"{format_time_human(s)}–{format_time_human(e)}"
            else:
                # no explicit time-range; use the stored availability string (it may already be human-friendly)
                avail = avail_str
        except Exception:
            avail = None

    # final fallback: if draft2 contains a single string 'availability', use it
    if not avail and draft2 and draft2.get("availability"):
        try:
            raw = draft2.get("availability")
            if isinstance(raw, str) and raw.strip():
                avail = raw.strip()
        except Exception:
            pass

    p["availability"] = avail or None

    # --- Services: database is the source of truth after save ---
    s_raw = _first_nonempty(
        getattr(profile, "services", None),
        draft2.get("services") if draft2 else None
    )

    p["services"] = s_raw or None

    p["services_other"] = _first_nonempty(
        getattr(profile, "services_other", None),
        draft2.get("services_other") if draft2 else None
    ) or None 

    # other family fields
    care_needed_raw = _first_nonempty(draft2.get("care_needed") if draft2 else None, getattr(profile, "care_needed", None))
    p["care_needed"] = care_needed_raw or None

    # expose typed-other so template can prefer it when rendering
    p["care_needed_other"] = _first_nonempty(draft2.get("care_needed_other") if draft2 else None, getattr(profile, "care_needed_other", None)) or None

    # number_of_dependents - may be integer or string
    nd_raw = None
    if draft2 and draft2.get("number_of_dependents") is not None:
        nd_raw = draft2.get("number_of_dependents")
    elif profile and getattr(profile, "number_of_dependents", None) is not None:
        nd_raw = profile.number_of_dependents
    try:
        p["number_of_dependents"] = int(nd_raw) if nd_raw not in (None, "", False) else None
    except Exception:
        p["number_of_dependents"] = None

    # NEW: preferred_caregiver_age preview
    p["preferred_caregiver_age"] = _first_nonempty(draft2.get("preferred_caregiver_age") if draft2 else None, getattr(profile, "preferred_caregiver_age", None)) or None


    # Preferred schedule: try to present it in the same human-friendly form as availability
    try:
        raw_pref = _first_nonempty(draft2.get("preferred_schedule") if draft2 else None,
                                  getattr(profile, "preferred_schedule", None))
        def _format_pref_schedule(val):
            if not val:
                return None
            # If structured like availability (dict with availability_days/start/end)
            try:
                if isinstance(val, dict):
                    return _build_availability_from_structured(val)
            except Exception:
                pass

            # If it's a string, try to detect a HH:MM - HH:MM range and format like availability
            try:
                s = str(val).strip()
                m = re.search(r'(\d{1,2}:\d{2})\s*[-–—]{1,2}\s*(\d{1,2}:\d{2})', s)
                if m:
                    s_time = m.group(1); e_time = m.group(2)
                    days_part = re.sub(re.escape(m.group(0)), '', s).strip().strip(',').strip()
                    parts = [x.strip() for x in re.split(r'[,|;]', days_part) if x.strip()]
                    if any("mon-sun" in p.lower() for p in parts) or len(parts) >= 7:
                        days_label = "Mon-Sun"
                    elif parts:
                        days_label = ", ".join(parts)
                    else:
                        days_label = None
                    if days_label:
                        return f"{days_label}, {format_time_human(s_time)}–{format_time_human(e_time)}"
                    return f"{format_time_human(s_time)}–{format_time_human(e_time)}"
                # no time-range found — return the stored text as-is
                return s if s else None
            except Exception:
                return str(val) if val else None

        p["preferred_schedule"] = _format_pref_schedule(raw_pref)
    except Exception:
        p["preferred_schedule"] = None
 
    
    p["hourly_budget"] = _first_nonempty(draft2.get("hourly_budget") if draft2 else None, getattr(profile, "hourly_budget", None)) or None

    # photos list -> normalize to public URLs (leave full URLs as-is, convert filenames to uploaded_file URLs)
    def _make_public_url(fn):
        try:
            if not fn:
                return None
            s = str(fn).strip()
            # If it's already an absolute URL or a site-root path, return as-is
            if s.startswith("http://") or s.startswith("https://") or s.startswith("/"):
                return s
            # Otherwise assume it's a stored filename and build a url_for path
            try:
                return url_for('uploaded_file', filename=s)
            except Exception:
                # If url_for not available for whatever reason, fallback to /uploads/<filename>
                return "/uploads/" + s
        except Exception:
            return None

    try:
        if other_photos and isinstance(other_photos, (list, tuple)):
            public_photos = [_make_public_url(x) for x in other_photos if _make_public_url(x)]
        elif other_photos and isinstance(other_photos, str):
            # tolerate a single CSV string (legacy)
            parts = [p.strip() for p in other_photos.split(",") if p.strip()]
            public_photos = [_make_public_url(x) for x in parts if _make_public_url(x)]
        else:
            public_photos = []
    except Exception:
        public_photos = []

    p["other_photos"] = public_photos
 
    # --- Provide human-friendly preview labels for templates (fast path) ---
    def humanize_or_fallback(val):
        if not val:
            return None
        try:
            h = humanize_csv(val)
            if h:
                return h
        except Exception:
            pass
        # fallback: if list, map tokens; if string, split CSV and prettify tokens
        try:
            if isinstance(val, (list, tuple)):
                parts = [humanize_token(x) or str(x).strip() for x in val if str(x).strip()]
                return ", ".join(parts) if parts else None
            if isinstance(val, str):
                parts = [p.strip() for p in re.split(r'[,|;]', val) if p.strip()]
                parts = [humanize_token(p) or p for p in parts]
                return ", ".join(parts) if parts else None
        except Exception:
            pass
        return None

    p["services_display"] = humanize_or_fallback(p.get("services"))
    p["specialties_display"] = humanize_or_fallback(p.get("specialties"))
    p["care_needed_display"] = humanize_or_fallback(p.get("care_needed"))

    return p


# -------------------
# Template helpers
# -------------------
import os
from datetime import datetime

def _format_time_human_safe(hhmm):
    # reuse your format_time_human logic safely inside Jinja helper
    try:
        return format_time_human(hhmm)
    except Exception:
        return hhmm or ""

def fmt_availability_from_inputs(src, pref=False):
    """
    Jinja-callable helper used by templates.
    Accepts a dict-like `src` (draft2 or profile-like) and returns a human-friendly
    availability string. If pref=True the template asked for a 'preferred' compact label.
    """
    try:
        if not src:
            return None

        # Structured fields preferred: availability_days, availability_start, availability_end
        days_raw = None
        if isinstance(src, dict):
            days_raw = src.get("availability_days")
            start = src.get("availability_start")
            end = src.get("availability_end")
        else:
            # allow attribute-style objects (SQLAlchemy row)
            days_raw = getattr(src, "availability_days", None)
            start = getattr(src, "availability_start", None)
            end = getattr(src, "availability_end", None)

        # Normalize days into list
        days_list = []
        if days_raw:
            if isinstance(days_raw, (list, tuple)):
                days_list = [d.strip() for d in days_raw if d and str(d).strip()]
            else:
                days_list = [d.strip() for d in re.split(r'[,|;]', str(days_raw)) if d.strip()]

        days_label = None
        if days_list:
            if len(set([d.lower() for d in days_list])) >= 7:
                days_label = "Mon-Sun"
            else:
                days_label = ", ".join(days_list)

        if start and end:
            time_range = f"{_format_time_human_safe(start)}–{_format_time_human_safe(end)}"
            return (f"{days_label}, {time_range}" if days_label else time_range)

        # Fallback: if src has an 'availability' text (persisted), try to parse times
        avail_text = None
        if isinstance(src, dict):
            avail_text = src.get("availability") or None
        else:
            avail_text = getattr(src, "availability", None)

        if avail_text:
            try:
                avail_str = str(avail_text).strip()
                # if contains time-range like "HH:MM - HH:MM" convert times to human
                m = re.search(r'(\d{1,2}:\d{2})\s*[-–—]{1,2}\s*(\d{1,2}:\d{2})', avail_str)
                if m:
                    s = m.group(1); e = m.group(2)
                    # remove the matched time-range from text to get a days_part
                    days_part = re.sub(re.escape(m.group(0)), '', avail_str).strip().strip(',').strip()
                    parts = [x.strip() for x in re.split(r'[,|;]', days_part) if x.strip()]
                    if any("mon-sun" in p.lower() for p in parts) or len(parts) >= 7:
                        days_label = "Mon-Sun"
                    elif parts:
                        days_label = ", ".join(parts)
                    if days_label:
                        return f"{days_label}, {_format_time_human_safe(s)}–{_format_time_human_safe(e)}"
                    else:
                        return f"{_format_time_human_safe(s)}–{_format_time_human_safe(e)}"
                # otherwise return the stored text as-is
                return avail_str
            except Exception:
                return str(avail_text)

        # final fallback: if days_label exists return it, else None
        return days_label or None
    except Exception:
        app.logger.exception("fmt_availability_from_inputs failed")
        return None

# Register the helper for templates (make available as function)
app.jinja_env.globals["fmt_availability_from_inputs"] = fmt_availability_from_inputs

# Add small convenience filters used by your templates
app.jinja_env.filters["split"] = lambda s, sep=",": (s.split(sep) if (s is not None and hasattr(s, "split")) else [])
app.jinja_env.filters["basename"] = lambda s: (os.path.basename(s) if s else s)
# -------------------



def send_verification_email(to_email, token, first_name):
    verify_url = url_for("verify_email", token=token, _external=True)
    html = render_template("email_verification.html", verify_url=verify_url, first_name=first_name)
    try:
        msg = MailMessage(subject="Verify your Care Companion account", recipients=[to_email], html=html)
        mail.send(msg)
    except Exception as e:
       import traceback

       print("\n===== EMAIL ERROR =====")
       print("Recipient:", to_email)
       print("Verify URL:", verify_url)
       print("Error:", str(e))
       traceback.print_exc()
       print("=======================\n") 


def generate_reset_token(email):
    return ts.dumps(email, salt=app.config["SECURITY_PASSWORD_SALT"])


def verify_reset_token(token, expiration=3600):
    try:
        email = ts.loads(token, salt=app.config["SECURITY_PASSWORD_SALT"], max_age=expiration)
    except (SignatureExpired, BadSignature):
        return None
    return email


def send_email(to_email, subject, html):
    try:
        msg = MailMessage(subject=subject, recipients=[to_email], html=html)
        mail.send(msg)
    except Exception as e:
        print("Email sending failed:", e)
        print("=== EMAIL FALLBACK ===")
        print("To:", to_email)
        print("Subject:", subject)
        print(html)
        print("======================")


def send_suspension_email(user, reason):
    """
    Notify a user that their account has been suspended and explain how to appeal.
    """
    admin_email = app.config.get("MAIL_DEFAULT_SENDER")[1] if isinstance(app.config.get("MAIL_DEFAULT_SENDER"), (list, tuple)) else app.config.get("MAIL_DEFAULT_SENDER")

    subject = "Your Care Companion account has been suspended"

    appeal_message = (
        f"Hello {getattr(user, 'first_name', 'there')},\n\n"
        f"Your Care Companion account has been suspended for the following reason:\n\n"
        f"{reason or 'Violation of the website terms.'}\n\n"
        "If you believe this was a mistake, please reply to this email with your appeal.\n"
        "Our team will review it and decide whether your account can be restored.\n\n"
        "Thank you,\n"
        "Care Companion Support"
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#111">
      <h2>Your Care Companion account has been suspended</h2>
      <p>Hello <strong>{getattr(user, 'first_name', 'there')}</strong>,</p>
      <p>Your account has been suspended for the following reason:</p>
      <div style="padding:12px 14px;border-radius:10px;background:#f3f4f6;border:1px solid #e5e7eb;margin:12px 0">
        {reason or 'Violation of the website terms.'}
      </div>
      <p>If you believe this was a mistake, please reply to this email with your appeal.</p>
      <p>Our team will review it and decide whether your account can be restored.</p>
      <p>Regards,<br>Care Companion Support</p>
    </div>
    """

    try:
        msg = MailMessage(subject=subject, recipients=[user.email], body=appeal_message, html=html)
        msg.reply_to = admin_email
        mail.send(msg)
    except Exception:
        app.logger.exception("Failed to send suspension email")



def send_unsuspension_email(user):
    subject = "Your Care Companion account has been restored"

    body = (
        f"Hello {getattr(user, 'first_name', 'there')},\n\n"
        "Your Care Companion account has been restored and you can now log in again.\n\n"
        "Thank you,\n"
        "Care Companion Support"
    )

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#111">
      <h2>Your Care Companion account has been restored</h2>
      <p>Hello <strong>{getattr(user, 'first_name', 'there')}</strong>,</p>
      <p>Your account has been restored and you can now log in again.</p>
      <p>Regards,<br>Care Companion Support</p>
    </div>
    """

    try:
        msg = MailMessage(subject=subject, recipients=[user.email], body=body, html=html)
        mail.send(msg)
    except Exception:
        app.logger.exception("Failed to send unsuspension email")


# send_reset_email helper
def send_reset_email(user):
    token = generate_reset_token(user.email)
    reset_url = url_for("reset_password", token=token, _external=True)
    html_body = render_template(
        "email_reset.html",
        reset_url=reset_url,
        first_name=(user.first_name if hasattr(user, "first_name") else None),
        support_url="https://your-domain.example.com/support",
        year=datetime.utcnow().year,
    )
    text_body = (
        "Care Companion - Reset your password\n\n"
        f"Hi {getattr(user, 'first_name', 'there')},\n\n"
        "We received a request to reset the password for your Care Companion account.\n"
        "Click the link below to choose a new password. The link expires in 1 hour:\n\n"
        f"{reset_url}\n\n"
        "If you didn't request a reset, you can ignore this message.\n\nThanks,\nCare Companion\n"
    )
    subject = "Care Companion - Reset your password"
    try:
        msg = MailMessage(subject=subject, recipients=[user.email])
        msg.body = text_body
        msg.html = html_body
        mail.send(msg)
    except Exception as exc:
        print("Failed to send reset email:", exc)
        print("Reset link:", reset_url)


@app.route("/profile/publish", methods=["POST"])
def publish_profile():
    """
    Persist the user's drafts into the canonical Profile row and mark the profile
    as published (one-time flag). This route is intended to be called when the
    user clicks the "Save & Publish" button during registration/initial setup.
    """
    if "user_id" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    user = db.session.get(User, session["user_id"])
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    # drafts from session (may be empty dicts)
    step1 = session.get("profile_step1", {}) or {}
    step2 = session.get("profile_step2", {}) or {}

    try:
        # get or create profile row
        profile = Profile.query.filter_by(user_id=user.id).first()
        if not profile:
            profile = Profile(user_id=user.id)
            db.session.add(profile)
        # remember whether profile was already published so we only send welcome once
        prev_published = bool(getattr(profile, "published", False))

        # --- copy values (prefer raw draft values; keep them simple) ---
        profile.bio = step1.get("bio")
        profile.marital_status = step1.get("marital_status")
        profile.age = safe_int(step1.get("age"))
        profile.location = step1.get("location")
        profile.languages = step1.get("languages")

        profile.experience_years = safe_int(step2.get("experience_years"))
        profile.specialties = step2.get("specialties")
        profile.specialties_other = step2.get("specialties_other")
        profile.hourly_rate = step2.get("hourly_rate")
        profile.availability = step2.get("availability")
        profile.services = step2.get("services")
        profile.services_other = step2.get("services_other")
        profile.preferred_age = step2.get("preferred_age")
        profile.care_needed = step2.get("care_needed")
        profile.care_needed_other = step2.get("care_needed_other")
        profile.number_of_dependents = safe_int(step2.get("number_of_dependents"))
        profile.care_hours_per_week = safe_int(step2.get("care_hours_per_week"))
        profile.household_info = step2.get("household_info")
        profile.hourly_budget = step2.get("hourly_budget")
        profile.preferred_gender = step2.get("preferred_gender")
        profile.preferred_caregiver_age = step2.get("preferred_caregiver_age")
        profile.medical_needs = step2.get("medical_needs")

        # --- Humanize values before final commit so DB stores readable labels ---
        try:
            if user.role == "caregiver":
                if getattr(profile, "specialties", None):
                    profile.specialties = humanize_csv(profile.specialties)
                if getattr(profile, "services", None):
                    profile.services = humanize_csv(profile.services)
            else:
                if getattr(profile, "care_needed", None):
                    profile.care_needed = humanize_csv(profile.care_needed)
        except Exception:
            app.logger.exception("Humanize in publish_profile failed")

        # Mark the profile as published (this is the authoritative server-side one-time flag)
        profile.published = True

        # persist
        db.session.commit()

        # --- send one-time welcome notification (only when first published) ---
        try:
            if not prev_published:
                try:
                    send_welcome_notification(user)
                except Exception:
                    app.logger.exception("send_welcome_notification failed in publish_profile")
        except Exception:
            app.logger.exception("publish_profile: error checking prev_published")
        
        
         
        # clear session drafts (profile now canonical)
        session.pop("profile_step1", None)
        session.pop("profile_step2", None)
        session.modified = True

        flash("Profile saved!", "success")
        return redirect(url_for("profile_setup"))

    except Exception as exc:
        app.logger.exception("Failed saving profile in publish_profile: %s", exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        flash("Failed to save profile. Please try again.", "error")
        return redirect(url_for("profile_setup"))



# --------------------------
# Routes
# --------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/find-caregiver")
def find_caregiver():
    """
    - Unauthenticated: redirect to login (preserve next -> profile_setup).
    - Logged in:
       * If caregiver with published profile -> redirect to find_family (server-side block).
       * Otherwise -> show caregivers connections page.
    """
    if "user_id" in session:
        user = db.session.get(User, session["user_id"])
        if user:
            try:
                # Block published caregivers from viewing caregivers listing
                if getattr(user, "role", "").lower() == "caregiver":
                    profile = Profile.query.filter_by(user_id=user.id).first()
                    if profile and getattr(profile, "published", False):
                        flash("Caregivers cannot view the caregivers listing. You were redirected to families.", "info")
                        return redirect(url_for("find_family"))
            except Exception:
                app.logger.exception("find_caregiver: protective check failed")

        # All other logged-in users: original behavior (show caregivers)
        return redirect(url_for("connections", role="caregiver"))

    # Not logged in -> ask them to login / onboard first
    return redirect(url_for("login", next=url_for("profile_setup")))


@app.route("/find-family")
def find_family():
    """
    - Unauthenticated: redirect to login (preserve next -> profile_setup).
    - Logged in:
       * If family with published profile -> redirect to find_caregiver (server-side block).
       * Otherwise -> show families connections page.
    """
    if "user_id" in session:
        user = db.session.get(User, session["user_id"])
        if user:
            try:
                # Block published families from viewing families listing
                if getattr(user, "role", "").lower() == "family":
                    profile = Profile.query.filter_by(user_id=user.id).first()
                    if profile and getattr(profile, "published", False):
                        flash("Families cannot view the families listing. You were redirected to caregivers.", "info")
                        return redirect(url_for("find_caregiver"))
            except Exception:
                app.logger.exception("find_family: protective check failed")

        # All other logged-in users: original behavior (show families)
        return redirect(url_for("connections", role="family"))

    # Not logged in -> ask them to login / onboard first
    return redirect(url_for("login", next=url_for("profile_setup")))






@app.route("/how-it-works")
def how_it_works():
    # The `user` variable will be injected into templates by `inject_user()`.
    # Avoid passing the lightweight dict from get_current_user() which lacks
    # a .profile attribute expected by templates.
    return render_template("how_it_works.html")


@app.route("/about")
def about():
    # template: templates/about.html (the About page you asked for)
    return render_template("about.html")


# Contact page route
@app.route("/contact")
def contact():
    """Render contact page (templates/contact_us.html)."""
    try:
        user = get_current_user()
    except Exception:
        user = None
    return render_template("contact_us.html")


# Contact form receiver
@app.route("/contact/submit", methods=["POST"])
def contact_submit():
    """
    Accepts JSON or form POSTs from the contact page.
    Returns JSON { success: bool, sent: bool, message: str }.
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            data = request.form.to_dict()

        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()
        subject = (data.get("subject") or "General").strip()
        message = (data.get("message") or "").strip()
        referrer = (data.get("referrer") or request.headers.get("Referer") or "").strip()

        if not name or not email or not message:
            return jsonify({"success": False, "error": "missing_name_or_email_or_message"}), 400

        # persist to JSONL (simple fallback)
        try:
            entry = {
                "name": name,
                "email": email,
                "phone": phone,
                "subject": subject,
                "message": message,
                "referrer": referrer,
                "created_at": datetime.utcnow().isoformat()
            }
            logfile = os.path.join(data_dir, "contact_messages.jsonl")
            with open(logfile, "a", encoding="utf8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            app.logger.exception("Could not persist contact message (non-fatal)")

        # send email notification (best-effort)
        try:
            default_sender = app.config.get("MAIL_DEFAULT_SENDER")
            recipient = None
            if isinstance(default_sender, (list, tuple)):
                recipient = default_sender[1]
            elif isinstance(default_sender, str):
                recipient = default_sender
            else:
                recipient = "carecompanion@zohomail.com"

            subject_line = f"[Care Companion] Contact — {subject}"
            body = (
                f"New contact request\n\n"
                f"Name: {name}\nEmail: {email}\nPhone: {phone}\nSubject: {subject}\nReferrer: {referrer}\n\nMessage:\n{message}\n"
            )

            msg = MailMessage(subject_line, recipients=[recipient])
            msg.body = body
            try:
                msg.reply_to = email
            except Exception:
                pass
            mail.send(msg)
        except Exception:
            app.logger.exception("Failed to send contact email (non-fatal)")

        return jsonify({"success": True, "sent": True, "message": "Message sent — we will reply shortly."}), 200

    except Exception as exc:
        app.logger.exception("contact_submit handler error: %s", exc)
        return jsonify({"success": False, "error": "server_error", "detail": str(exc)}), 500












# --- Services page & contact handler ---
@app.route("/services")
def services():
    return render_template("services.html")


@app.route('/services/contact', methods=['POST'])
def services_contact():
    """
    Accepts JSON or form POST from the services contact form.
    Returns JSON: {"success": True, "sent": True} on success.
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            # fallback to form data
            data = request.form.to_dict()

        name = (data.get('name') or '').strip()
        email = (data.get('email') or '').strip()
        service = (data.get('service') or '').strip()
        role = (data.get('role') or '').strip()
        message = (data.get('message') or '').strip()

        # basic validation
        if not name or not email:
            return jsonify({"success": False, "error": "missing_name_or_email"}), 400

        # TODO: store request in DB or send email / enqueue job
        # Example: save to a simple JSON file (quick fallback)
        try:
            entry = {
                "name": name, "email": email, "service": service, "role": role, "message": message,
                "created_at": datetime.utcnow().isoformat()
            }
            # Example: append to a file (replace with DB in production)
            logfile = os.path.join(data_dir, "service_requests.jsonl")
            with open(logfile, "a", encoding="utf8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            app.logger.exception("Could not persist service request (non-fatal)")

        # Optionally send email notification to support (non-fatal)
        try:
            subject = f"New service request from {name}"
            body = f"Name: {name}\nEmail: {email}\nService: {service}\nRole: {role}\nMessage:\n{message}\n"
            msg = MailMessage(subject, recipients=[app.config.get('MAIL_DEFAULT_SENDER')[1]])
            msg.body = body
            mail.send(msg)
        except Exception:
            app.logger.exception("send email for services_contact failed (non-fatal)")

        return jsonify({"success": True, "sent": True}), 200

    except Exception as exc:
        app.logger.exception("services_contact handler error: %s", exc)
        return jsonify({"success": False, "error": "server_error", "detail": str(exc)}), 500









@app.route("/register", methods=["GET", "POST"])
def register():
    role = request.args.get("role", "caregiver")
    success = request.args.get("success") == "1"
    if request.method == "POST":
        role = request.form.get("role", role)
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        terms_ok = request.form.get("terms") == "on"

        errors = []
        if not first_name:
            errors.append("First name is required.")
        if not last_name:
            errors.append("Last name is required.")
        if not email or "@" not in email or "." not in email:
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not terms_ok:
            errors.append("You must accept the Terms to continue.")
        if User.query.filter_by(email=email).first():
            errors.append("An account with that email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            form_data = {"first_name": first_name, "last_name": last_name, "email": email, "role": role, "terms": terms_ok}
            return render_template("registration.html", role=role, form_data=form_data, success=False)

        new_user = User(first_name=first_name, last_name=last_name, email=email, role=role, verified=False)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()

        token = ts.dumps(email, salt=app.config["SECURITY_PASSWORD_SALT"])
        send_verification_email(email, token, first_name)

        flash("Registration successful - we sent a verification email. Check your inbox (and spam).", "info")
        return redirect(url_for("register", role=role, success="1"))

    return render_template("registration.html", role=role, form_data={}, success=success)


@app.route("/verify/<token>")
def verify_email(token):
    max_age_seconds = 60 * 60 * 24
    try:
        email = ts.loads(token, salt=app.config["SECURITY_PASSWORD_SALT"], max_age=max_age_seconds)
    except SignatureExpired:
        flash("The verification link has expired. Please register again.", "error")
        return redirect(url_for("register"))
    except BadSignature:
        flash("Invalid verification link.", "error")
        return redirect(url_for("register"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Unable to find an account for that verification link.", "error")
        return redirect(url_for("register"))

    if user.verified:
        flash("Your email is already verified - please log in.", "info")
        return redirect(url_for("login"))

    user.verified = True
    db.session.commit()
    flash("Email verified successfully! You can now log in.", "success")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Login handler:
      - Accepts safe 'next' and uses it after successful login (internal paths only).
      - Redirects to profile_setup ONLY when a user's profile row exists AND profile.published is truthy.
      - Otherwise redirects to profile_step1 to start onboarding.
      - HOTFIX: filters out stale HTML/banner flashes (e.g. 'Connect Now' banner) from session flashes
        immediately after successful authentication so the banner does not reappear on login.
    """
    next_target = request.args.get("next") or request.form.get("next")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", next=next_target)

        if hasattr(user, "is_deleted") and user.is_deleted:
            flash("This account has been deleted. Contact admin if you think this is a mistake.", "error")
            return render_template("login.html", next=next_target)

        if hasattr(user, "is_active") and not user.is_active:
            flash("This account is not active. Contact admin.", "error")
            return render_template("login.html", next=next_target)

        if not user.verified:
            flash("Please verify your email before logging in.", "error")
            return render_template("login.html", next=next_target)

        # Auth OK -> create session
        session["user_id"] = user.id
        session["user_name"] = user.first_name

        # --- HOTFIX: remove any leftover HTML-banner-like flashes so banner won't appear after login ---
        try:
            flashes = session.get('_flashes', [])
            if flashes:
                filtered = []
                for cat, msg in flashes:
                    drop = False
                    try:
                        if isinstance(msg, str):
                            # Drop messages that look like the banner HTML or contain the connect label.
                            if ('<a' in msg) or ('<button' in msg) or ('Connect Now' in msg) \
                               or ('profile-connect' in msg) or ('profile-connect-banner' in msg):
                                drop = True
                    except Exception:
                        # if anything odd, keep the message rather than risk losing user-visible info
                        drop = False

                    if drop:
                        app.logger.info("login(): dropped stale HTML flash for user_id=%s category=%s", user.id, cat)
                        continue
                    filtered.append((cat, msg))

                # Replace session stored flashes with filtered list
                session['_flashes'] = filtered
                session.modified = True
        except Exception:
            app.logger.exception("login(): failed to filter _flashes (non-fatal)")

        # Respect safe next if provided (only allow same-origin internal paths)
        if next_target and isinstance(next_target, str):
            try:
                if next_target.startswith("/") and not next_target.startswith("//"):
                    return redirect(next_target)
            except Exception:
                pass

        # DECISION: only redirect to profile_setup when profile exists AND profile.published is truthy
        profile = Profile.query.filter_by(user_id=user.id).first()

        # Log decision context for debugging
        app.logger.info("login decision: user_id=%s profile_exists=%s profile_published=%s session_keys=%s",
                        user.id, bool(profile), bool(profile and getattr(profile, "published", False)), list(session.keys()))

        if profile and getattr(profile, "published", False):
            flash("Logged in — welcome back!", "success")
            # suppress overlay so user lands on preview (preserves previous behavior)
            return redirect(url_for("profile_setup", suppress_overlay="1"))
        else:
            # New / unpublished profile -> ensure a clean creation flow
            flash("Logged in — continue creating your profile.", "success")
            session.pop("profile_step1", None)
            session.pop("profile_step2", None)
            session.modified = True
            return redirect(url_for("profile_step1"))

    # GET -> render login form (preserve next)
    return render_template("login.html", next=next_target)





@app.route("/logout")
def logout():
    # clear session drafts too
    session.pop("user_id", None)
    session.pop("user_name", None)
    session.pop("profile_step1", None)
    session.pop("profile_step2", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Please provide an email address.", "error")
        return redirect(url_for("forgot_password"))
    user = User.query.filter_by(email=email).first()
    if user:
        send_reset_email(user)
    flash("If that email is registered, we sent a password reset link. Check your inbox.", "success")
    return redirect(url_for("login"))


@app.route("/_preview-reset-email")
def preview_reset_email():
    demo_email = request.args.get("email", "test@example.com")
    class _U: pass
    u = _U()
    u.email = demo_email
    u.first_name = "Ada"
    token = generate_reset_token(u.email)
    reset_url = url_for("reset_password", token=token, _external=True)
    return render_template(
        "email_reset.html",
        reset_url=reset_url,
        first_name=u.first_name,
        support_url="https://your-domain.example.com/support",
        year=datetime.utcnow().year,
    )


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    email = verify_reset_token(token)
    if not email:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("login"))
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Invalid user.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        new_password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not new_password or len(new_password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)
        if new_password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token)
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash("Password updated. You can now log in.", "success")
        return redirect(url_for("login"))
    return render_template("reset_password.html", token=token)


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """
    Serve files from UPLOAD_FOLDER. Defensive: always use basename so
    a filename like '/uploads/25.jpg' or 'http://.../uploads/25.jpg' won't cause doubled paths.
    """
    # normalize to a safe basename (prevents joining with leading slashes)
    safe_name = os.path.basename(filename) if filename else filename
    # optionally ensure no path traversal (basename discourages it)
    return send_from_directory(app.config['UPLOAD_FOLDER'], safe_name)




@app.route("/profile/update_photo", methods=["POST"])
def profile_update_photo():
    """Upload only a single profile photo, normalize to JPG, persist to DB and return JSON with the new URL.

    Expects form field 'profile_photo'. Requires logged-in user (session['user_id']).
    """
    try:
        if "user_id" not in session:
            return jsonify({"status": "error", "message": "Not authenticated"}), 401

        user = db.session.get(User, session["user_id"])
        if not user:
            return jsonify({"status": "error", "message": "User not found"}), 404

        f = request.files.get("profile_photo")
        if not f or not getattr(f, "filename", ""):
            return jsonify({"status": "error", "message": "No file uploaded"}), 400

        if not allowed_file(f.filename):
            return jsonify({"status": "error", "message": "File type not allowed"}), 400

        # ensure profile row exists
        profile = Profile.query.filter_by(user_id=user.id).first()
        if profile is None:
            profile = Profile(user_id=user.id)
            db.session.add(profile)
            db.session.flush()

        # Read & normalize image using Pillow
        try:
            # Seek to start if possible
            try:
                if hasattr(f.stream, "seek"):
                    f.stream.seek(0)
            except Exception:
                pass

            img = Image.open(f.stream)
            img.load()
            img = img.convert("RGB")

            # optional: constrain to max dims (avoid huge uploads)
            MAX_DIM = (2500, 2500)
            img.thumbnail(MAX_DIM, Image.LANCZOS)

            # prepare filename and save as .jpg (normalize everything to JPEG)
            base = secure_filename(f"{user.id}_profile_{int(time.time())}")
            fname = f"{base}.jpg"
            save_path = os.path.join(app.config.get("UPLOAD_FOLDER", "uploads"), fname)

            # ensure folder exists
            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            img.save(save_path, format="JPEG", quality=85, optimize=True)
        except UnidentifiedImageError:
            return jsonify({"status": "error", "message": "Uploaded file is not a valid image"}), 400
        except Exception as exc:
            app.logger.exception("Failed to process profile photo upload: %s", exc)
            return jsonify({"status": "error", "message": "Failed to process image"}), 500

        # Remove previous file if different
        try:
            old_fn = getattr(profile, "profile_photo", None)
            if old_fn and old_fn != fname:
                old_path = os.path.join(app.config.get("UPLOAD_FOLDER", "uploads"), os.path.basename(old_fn))
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        app.logger.debug("Could not remove old profile photo: %s", old_path)
        except Exception:
            app.logger.exception("Error removing old profile photo")

        # Persist new filename to DB
        try:
            profile.profile_photo = fname
            db.session.add(profile)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to save profile photo filename to DB")
            return jsonify({"status": "error", "message": "Failed to save profile data"}), 500

        # --- REALTIME: broadcast profile update to open chat pages ---
        try:
            avatar_url = url_for("uploaded_file", filename=fname)
            broadcast_profile_update(user.id, name=f"{user.first_name} {user.last_name}", avatar_url=avatar_url)
        except Exception:
            app.logger.exception("broadcast_profile_update failed in profile_update_photo")

        # Build public URL for client
        url = url_for("uploaded_file", filename=fname)
        return jsonify({"status": "ok", "url": url, "filename": fname})
    except Exception:
        app.logger.exception("Unhandled error in profile_update_photo")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


@app.route('/profile_upload_photos', methods=['POST'])
def profile_upload_photos():
    """
    Upload one or more 'other' photos and append to profile.other_photos (comma-separated).
    Returns JSON: { status:'ok', files: [{filename,url}, ...] }
    Defensive: accepts single file or list, handles ownership check, logs extensively.
    """
    try:
        # Auth guard
        if 'user_id' not in session:
            current_app.logger.debug("profile_upload_photos: no user_id in session")
            return jsonify({'status': 'error', 'message': 'Authentication required'}), 401

        user = db.session.get(User, session['user_id']) or User.query.get(session['user_id'])
        if not user:
            current_app.logger.debug("profile_upload_photos: user not found for id %s", session.get('user_id'))
            return jsonify({'status': 'error', 'message': 'User not found'}), 404

        # Try multiple common keys and fall back to any files
        files = request.files.getlist('other_photos') or request.files.getlist('other_photos[]') or []
        if not files:
            # if nothing under expected keys, try any files (take all)
            # request.files is a MultiDict; take the FileStorage objects
            try:
                files = [v for k, v in request.files.items() if v and getattr(v, "filename", None)]
            except Exception:
                files = []

        if not files:
            current_app.logger.debug("profile_upload_photos: no files in request.files (keys: %s)", list(request.files.keys()))
            return jsonify({'status': 'error', 'message': 'No files provided'}), 400

        # Ensure profile row exists (we may not want to create persistent profile here, but your previous code does -> keep it)
        profile = Profile.query.filter_by(user_id=user.id).first()
        if not profile:
            profile = Profile(user_id=user.id)
            db.session.add(profile)
            db.session.flush()

        # Get existing persisted other_photos as basenames
        current = []
        raw = getattr(profile, "other_photos", None)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, (list, tuple)):
                    current = [os.path.basename(str(p)) for p in parsed if p and str(p).strip()]
                else:
                    current = [os.path.basename(p).strip() for p in str(raw).split(',') if p.strip()]
            except Exception:
                current = [os.path.basename(p).strip() for p in str(raw).split(',') if p.strip()]

        saved = []
        upload_folder = app.config.get('UPLOAD_FOLDER') or os.path.join(app.root_path, 'uploads')
        os.makedirs(upload_folder, exist_ok=True)

        for f in files:
            try:
                if not f or not getattr(f, "filename", ""):
                    continue

                # normalize incoming filename (secure)
                orig = secure_filename(f.filename)
                ext = os.path.splitext(orig)[1].lower()
                if not ext:
                    current_app.logger.debug("Skipping file with no extension: %s", orig)
                    continue
                if ext.lstrip('.') not in ALLOWED_EXTENSIONS:
                    current_app.logger.debug("Skipping disallowed extension: %s", ext)
                    continue

                # enforce photo count limit
                if len(current) + len(saved) >= MAX_OTHER_PHOTOS:
                    current_app.logger.info("Reached MAX_OTHER_PHOTOS: %s, skipping remaining files", MAX_OTHER_PHOTOS)
                    break

                newname = f"{user.id}_photo_{int(time.time())}_{uuid.uuid4().hex}{ext}"
                dest = os.path.join(upload_folder, newname)
                # save to disk
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                f.save(dest)

                # verify image file integrity with Pillow; if invalid delete and skip
                try:
                    im = Image.open(dest)
                    im.verify()   # verify will raise for corrupt images
                except Exception as verify_exc:
                    current_app.logger.warning("Uploaded file failed image verify and will be removed: %s (%s)", dest, verify_exc)
                    try:
                        os.remove(dest)
                    except Exception:
                        current_app.logger.exception("Failed to remove invalid file: %s", dest)
                    continue

                # success => append to lists
                current.append(newname)
                saved.append({'filename': newname, 'url': url_for('uploaded_file', filename=newname)})
                current_app.logger.debug("Saved other_photo for user %s -> %s", user.id, newname)

            except Exception as e:
                current_app.logger.exception("Exception while saving uploaded other_photo: %s", e)
                # attempt best-effort cleanup if dest exists
                try:
                    if 'dest' in locals() and os.path.exists(dest):
                        os.remove(dest)
                except Exception:
                    pass
                continue

        # persist as CSV basenames (templates expect CSV)
        try:
            profile.other_photos = ','.join(current) if current else None
            db.session.add(profile)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to persist profile.other_photos")
            return jsonify({'status': 'error', 'message': 'Failed to persist photo metadata'}), 500

        # update session draft if present (so preview immediately shows the new photos)
        try:
            s2 = session.get("profile_step2", {}) or {}
            draft_others = s2.get("other_photos") or s2.get("other_photos_csv") or None
            if draft_others:
                if isinstance(draft_others, (list, tuple)):
                    ds = [os.path.basename(str(x)) for x in draft_others if x]
                else:
                    ds = [os.path.basename(x).strip() for x in str(draft_others).split(',') if x.strip()]
                # merge new saved into existing draft list while deduping
                merged = []
                seen = set()
                for x in (ds + current):
                    if x not in seen:
                        seen.add(x)
                        merged.append(x)
                s2['other_photos'] = ','.join(merged)
                session['profile_step2'] = s2
                session.modified = True
        except Exception:
            current_app.logger.exception("Failed to update session draft other_photos")

        # broadcast update if available
        try:
            avatar_url = None
            if profile.profile_photo:
                avatar_url = url_for('uploaded_file', filename=profile.profile_photo)
            if 'broadcast_profile_update' in globals():
                broadcast_profile_update(user.id, name=f"{user.first_name} {user.last_name}", avatar_url=avatar_url)
        except Exception:
            current_app.logger.exception("broadcast_profile_update failed in profile_upload_photos")

        return jsonify({'status': 'ok', 'files': saved})
    except Exception as exc:
        current_app.logger.exception("profile_upload_photos error: %s", exc)
        return jsonify({'status': 'error', 'message': 'Server error'}), 500



@app.route('/profile_delete_photo', methods=['POST'])
def profile_delete_photo():
    """
    Delete a named photo from the user's profile (either profile_photo or other_photos).
    Expects JSON payload: { filename: "<name>" } or form data 'filename'.
    Returns JSON { status:'ok' } on success.
    """
    try:
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
        user = db.session.get(User, session['user_id']) or User.query.get(session['user_id'])
        if not user:
            return jsonify({'status': 'error', 'message': 'User not found'}), 404

        data = None
        try:
            data = request.get_json(force=True)
        except Exception:
            data = None
        filename = (data and data.get('filename')) or request.form.get('filename') or request.values.get('filename')
        if not filename:
            return jsonify({'status': 'error', 'message': 'Missing filename'}), 400

        # normalize to basename
        filename = os.path.basename(str(filename).split('?', 1)[0])

        profile = Profile.query.filter_by(user_id=user.id).first()
        if not profile:
            return jsonify({'status': 'error', 'message': 'Profile not found'}), 404

        changed = False

        # Remove if primary avatar
        if profile.profile_photo and os.path.basename(str(profile.profile_photo)) == filename:
            profile.profile_photo = None
            changed = True

        # Remove from other_photos (handle CSV or JSON or list)
        try:
            raw = getattr(profile, "other_photos", None)
            others = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, (list, tuple)):
                        others = [os.path.basename(str(p)) for p in parsed if p and str(p).strip()]
                    else:
                        others = [os.path.basename(p).strip() for p in str(raw).split(',') if p.strip()]
                except Exception:
                    others = [os.path.basename(p).strip() for p in str(raw).split(',') if p.strip()]
            new_list = [p for p in others if p != filename]
            if len(new_list) != len(others):
                profile.other_photos = ','.join(new_list) if new_list else None
                changed = True
        except Exception:
            app.logger.exception("Failed parsing profile.other_photos during delete")

        # Persist DB changes if any
        if changed:
            try:
                db.session.add(profile)
                db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception("Failed to persist profile changes on delete")
                return jsonify({'status': 'error', 'message': 'DB error'}), 500

        # Delete physical file from disk (best-effort)
        try:
            path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            app.logger.exception("failed to remove file from disk")

        # ALSO remove references from session drafts so preview doesn't resurrect the file
        try:
            s1 = session.get('profile_step1', {}) or {}
            s2 = session.get('profile_step2', {}) or {}

            modified = False
            # profile photo might be referenced in profile_step1.profile_photo
            if s1.get('profile_photo'):
                if os.path.basename(str(s1.get('profile_photo')).split('?', 1)[0]) == filename:
                    s1.pop('profile_photo', None)
                    modified = True

            # other_photos could be stored as CSV or list in draft step2
            op = s2.get('other_photos') or s2.get('other_photos_csv') or None
            if op:
                if isinstance(op, (list, tuple)):
                    new_op = [os.path.basename(str(x)) for x in op if os.path.basename(str(x)) != filename]
                    s2['other_photos'] = ','.join(new_op) if new_op else None
                    modified = True
                else:
                    arr = [os.path.basename(x).strip() for x in str(op).split(',') if x.strip()]
                    new_arr = [x for x in arr if x != filename]
                    s2['other_photos'] = ','.join(new_arr) if new_arr else None
                    modified = True

            if modified:
                session['profile_step1'] = s1
                session['profile_step2'] = s2
                session.modified = True
        except Exception:
            app.logger.exception("Failed clearing session draft references to deleted photo")

        # Broadcast to update any open public_profile / profile_setup views
        try:
            avatar_url = None
            if profile and getattr(profile, "profile_photo", None):
                avatar_url = url_for('uploaded_file', filename=profile.profile_photo)
            if 'broadcast_profile_update' in globals():
                broadcast_profile_update(user.id, name=f"{user.first_name} {user.last_name}", avatar_url=avatar_url)
            # also emit a specific "photo_deleted" socket event (room-less) so clients can react quickly
            try:
                socketio.emit("photo_deleted", {"user_id": user.id, "filename": filename})
            except Exception:
                pass
        except Exception:
            app.logger.exception("broadcast_profile_update failed in delete_photo")

        return jsonify({'status': 'ok'})
    except Exception as exc:
        app.logger.exception("profile_delete_photo error: %s", exc)
        return jsonify({'status': 'error', 'message': 'Server error'}), 500


# ---------------------------------------------------------------------------
# profile step routes (profile_step1/profile_step2/profile_setup)
# ---------------------------------------------------------------------------

@app.route("/profile-step-1", methods=["GET", "POST"])
def profile_step1():
    """
    Step 1: upload avatar, bio, marital status, age.
    Stores step-1 data in session under session['profile_step1'].
    Improved upload handling: normalize images with Pillow and save as .jpg.
    """
    if 'user_id' not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    user = db.session.get(User, session["user_id"])
    step_data = session.get('profile_step1', {}) or {}

    if request.method == "POST":
        # DEBUG printing (kept from your original)
        if app.debug:
            try:
                print("===== profile_step1 POST =====")
                print("FORM:", dict(request.form))
                print("FILES:", {k: getattr(f, 'filename', None) for k, f in request.files.items()})
                print("BEFORE session['profile_step1']:", session.get('profile_step1'))
            except Exception:
                print("profile_step1 debug print failed")

        f = request.files.get("profile_photo")
        saved_fname = step_data.get("profile_photo")

        if f and f.filename:
            # quick extension check
            if not allowed_file(f.filename):
                flash("Profile photo must be an image (png/jpg/jpeg/jfif/gif/webp).", "error")
                return redirect(url_for("profile_step1"))

            # Normalize & save upload using Pillow (prevents jfif edge-cases)
            try:
                # Ensure stream at start
                try:
                    if hasattr(f.stream, "seek"):
                        f.stream.seek(0)
                except Exception:
                    pass

                img = Image.open(f.stream)
                img.load()
                # Convert to RGB and optionally resize (thumb) - keep original dims unless large
                img = img.convert("RGB")
                # Optional: limit maximum dimensions (avoid extremely large uploads)
                MAX_DIM = (2500, 2500)
                img.thumbnail(MAX_DIM, Image.LANCZOS)

                # create safe filename and save as JPG
                base = secure_filename(f"{session['user_id']}_step1_{int(time.time())}")
                fname = f"{base}.jpg"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                # Save as JPEG with reasonable quality
                img.save(save_path, format="JPEG", quality=85, optimize=True)

                saved_fname = fname
            except UnidentifiedImageError:
                app.logger.exception("Uploaded file is not a recognizable image")
                flash("Uploaded file is not a valid image.", "error")
                return redirect(url_for("profile_step1"))
            except Exception:
                app.logger.exception("Failed to process uploaded profile photo")
                flash("Failed to save profile photo. Please try a different image.", "error")
                return redirect(url_for("profile_step1"))

        # tolerant collection of fields (accepting alternate names)
        bio = (request.form.get("bio") or "").strip()
        # accept marital_status or marital
        marital = (request.form.get("marital_status") or request.form.get("marital") or "").strip()
        # accept age or age_years or years_old
        age = (request.form.get("age") or request.form.get("age_years") or request.form.get("years_old") or "").strip()
        loc = (request.form.get("location") or "").strip()
        languages = (request.form.get("languages") or "").strip()

        session['profile_step1'] = {
            "profile_photo": saved_fname,
            "bio": bio,
            "marital_status": marital,
            "age": age,
            "location": loc,
            "languages": languages
        }
        session.modified = True

        # DEBUG: show what we saved
        if app.debug:
            try:
                print("SAVED session['profile_step1']:", session.get('profile_step1'))
            except Exception:
                print("profile_step1 save debug failed")

        flash("Step 1 saved. Proceed to the next step.", "success")
        return redirect(url_for("profile_step2"))

    return render_template("profile_step1.html", data=step_data, user=user)





# Full updated profile_step2 route (replace your existing route with this)
@app.route("/profile-step-2", methods=["GET", "POST"])
def profile_step2():
    """
    Step 2: role-specific profile details stored in session['profile_step2'].
    This handler ensures multi-value fields (checkboxes) are read with getlist()
    and stored in session as comma-separated strings (or None) to be compatible with the preview/template.
    """
    if "user_id" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    user = db.session.get(User, session["user_id"])
    step_data = session.get("profile_step2", {}) or {}

    if request.method == "POST":
        # Debug: show incoming form and lists if in debug mode
        if app.debug:
            try:
                app.logger.debug("profile_step2 POST form keys: %r", list(request.form.keys()))
                app.logger.debug("profile_step2 POST form (sample): %r", dict(request.form))
                app.logger.debug("lists: services=%r availability_days=%r care_needed=%r",
                                 request.form.getlist("services"),
                                 request.form.getlist("availability_days"),
                                 request.form.getlist("care_needed"),
                                 )
            except Exception:
                app.logger.exception("Failed to print profile_step2 debug info")

        is_draft = request.form.get("save_draft") == "1"

        def gather_caregiver_post():
            # Single-value fields
            experience_years = (request.form.get("experience_years") or "").strip()
            specialties = (request.form.get("specialties") or "").strip()
            specialties_other = (request.form.get("specialties_other") or "").strip()
            certifications = (request.form.get("certifications") or "").strip()
            salary_amount = (request.form.get("salary_amount") or "").strip()
            salary_currency = (request.form.get("salary_currency") or "").strip()
            salary_frequency = (request.form.get("salary_frequency") or "").strip()
            salary_free = (request.form.get("salary") or "").strip()
            hourly_rate_legacy = (request.form.get("hourly_rate") or "").strip()

            # Multi-value fields: MUST use getlist(); but support single hidden/csv fallback
            services_list = request.form.getlist("services") or []
            # fallback: some clients post a single hidden "services" with CSV or single token
            if not services_list:
                single_services = (request.form.get("services") or request.form.get("services_hidden") or request.form.get("services_radio") or "").strip()
                if single_services:
                    # if CSV, split; otherwise single token
                    if ',' in single_services:
                        services_list = [s.strip() for s in single_services.split(',') if s.strip()]
                    else:
                        services_list = [single_services]

            # Also include legacy checkbox names if present
            if not services_list:
                services_list = request.form.getlist("service") or request.form.getlist("service[]") or services_list

            services_other = (request.form.get("services_other") or "").strip()
            availability_days_list = request.form.getlist("availability_days") or []

            # Compose location (prefer combined hidden field)
            location_combined = (request.form.get("location") or "").strip()
            city = (request.form.get("location_city") or "").strip()
            country = (request.form.get("location_country") or "").strip()
            if not location_combined and city and country:
                location_combined = f"{city}/{country}"

            availability_start = (request.form.get("availability_start") or "").strip()
            availability_end = (request.form.get("availability_end") or "").strip()
            willing_to_travel = (request.form.get("willing_to_travel") or "").strip().lower()
            travel_radius_km = (request.form.get("travel_radius_km") or "").strip()
            preferred_age = (request.form.get("preferred_age") or "").strip()
            disability = (request.form.get("disability") or "").strip()
            disability_details = (request.form.get("disability_details") or "").strip()
            languages = (request.form.get("languages") or "").strip()

            # --- normalize services_list, expanding "other" into the typed text if provided ---
            cleaned_services = []
            for s in services_list:
                if not s:
                    continue
                s_token = str(s).strip()
                if s_token.lower().startswith("other:"):
                    # token like "other: Speech therapy"
                    v = s_token.split(":", 1)[1].strip()
                    if v:
                        cleaned_services.append(v)
                    else:
                        cleaned_services.append("Other")
                elif s_token.lower() == "other" or s_token.lower() == "others":
                    # prefer typed text, otherwise show "Other"
                    if services_other:
                        cleaned_services.append(services_other)
                    else:
                        cleaned_services.append("Other")
                else:
                    cleaned_services.append(s_token)

            # remove accidental literal None/null strings and duplicates while preserving order
            seen = set()
            deduped = []
            for x in cleaned_services:
                if not x:
                    continue
                low = str(x).strip()
                if low.lower() in ("none", "null", '"none"', '"null"'):
                    continue
                if low not in seen:
                    seen.add(low)
                    deduped.append(low)

            # store as comma-string or None when empty
            services_joined = ",".join(deduped) if deduped else None

            # availability: produce CSV string or None
            availability_joined = ",".join([d.strip() for d in availability_days_list if d and str(d).strip()]) if availability_days_list else None

            return {
                "experience_years": safe_int(experience_years) if experience_years != "" else None,
                "specialties": none_if_blank(specialties) or None,
                "specialties_other": none_if_blank(specialties_other) or None,
                "certifications": none_if_blank(certifications) or None,
                "salary_amount": none_if_blank(salary_amount) or None,
                "salary_currency": none_if_blank(salary_currency) or None,
                "salary_frequency": none_if_blank(salary_frequency) or None,
                "salary": none_if_blank(salary_free) or none_if_blank(hourly_rate_legacy) or None,
                "hourly_rate": none_if_blank(hourly_rate_legacy) if hourly_rate_legacy else None,
                "location": none_if_blank(location_combined) or None,
                "availability_start": none_if_blank(availability_start) or None,
                "availability_end": none_if_blank(availability_end) or None,
                "availability_days": availability_joined,   # store as comma-string or None
                "willing_to_travel": none_if_blank(willing_to_travel) or None,
                "travel_radius_km": safe_int(travel_radius_km),
                "services": services_joined,                 # <- cleaned, comma-string or None
                "services_other": none_if_blank(services_other) or None,
                "preferred_age": none_if_blank(preferred_age) or None,
                "disability": none_if_blank(disability) or None,
                "disability_details": none_if_blank(disability_details) or None,
                "languages": none_if_blank(languages) or None
            }

        def gather_family_post():
            """
            Read care_needed list plus typed 'other' value and canonicalize.
            Returns care_needed as comma-joined string and care_needed_other separately.
            """
            care_needed_list = request.form.getlist("care_needed") or []
            # typed "other" text (if user selected Other and typed a value)
            care_needed_other = (request.form.get("care_needed_other") or "").strip()

            number_of_dependents = (request.form.get("number_of_dependents") or "").strip()

            # NEW: read preferred caregiver age (was added to template)
            preferred_caregiver_age = (request.form.get("preferred_caregiver_age") or "").strip()

            # old/removed field (care_hours_per_week) may still be present for older clients
            care_hours_per_week = (request.form.get("care_hours_per_week") or "").strip()

            preferred_schedule = (request.form.get("preferred_schedule") or "").strip()
            preferred_schedule_start = (request.form.get("preferred_schedule_start") or "").strip()
            preferred_schedule_end = (request.form.get("preferred_schedule_end") or "").strip()

            hourly_budget = (request.form.get("hourly_budget") or "").strip()
            preferred_gender = (request.form.get("preferred_gender") or "").strip()

            # household_info: prefer hidden 'household_info', otherwise fallback to checkbox group 'household_service'
            household_info_raw = (request.form.get("household_info") or "").strip()

            # -------------------------
            # Canonicalize care_needed robustly and include typed "other" text
            # -------------------------
            chosen = []
            for item in care_needed_list:
                if not item:
                    continue
                chosen.append(item.strip())

            # If 'other' was selected, prefer the typed other text (if provided).
            if any(x.lower() == "other" for x in chosen):
                # remove literal 'other' tokens
                chosen = [x for x in chosen if x.lower() != "other"]
                if care_needed_other:
                    # use the user-typed text
                    chosen.append(care_needed_other)
                else:
                    # fallback to literal "Other"
                    chosen.append("Other")

            care_needed_joined = ",".join(chosen) if chosen else None

            # -------------------------
            # Canonicalize caregiver_services robustly (existing logic)
            # -------------------------
            chosen_raw = request.form.getlist("caregiver_services") or []
            if not chosen_raw:
                chosen_raw = request.form.getlist("caregiver_services[]") or []

            if not chosen_raw:
                single_csv = (request.form.get("caregiver_services") or request.form.get("caregiver_services_hidden") or "").strip()
                if single_csv:
                    chosen_raw = [single_csv]

            if not chosen_raw:
                # try alternate checkbox names (compat)
                chosen_raw = request.form.getlist("caregiver_services_checkbox") or chosen_raw
                chosen_raw = request.form.getlist("caregiver_services_checkbox[]") or chosen_raw

            expanded = []
            for item in chosen_raw:
                if not item:
                    continue
                for part in str(item).split(','):
                    p = part.strip()
                    if p:
                        expanded.append(p)

            seen = set()
            chosen_cs = []
            for x in expanded:
                if x not in seen:
                    seen.add(x)
                    chosen_cs.append(x)

            if not chosen_cs:
                legacy_list = request.form.getlist("household_service") or request.form.getlist("household_service[]") or []
                for item in legacy_list:
                    for part in str(item).split(','):
                        p = part.strip()
                        if p and p not in seen:
                            seen.add(p)
                            chosen_cs.append(p)

            if not chosen_cs:
                if household_info_raw:
                    for part in household_info_raw.split(','):
                        q = part.strip()
                        if q and q not in seen:
                            seen.add(q)
                            chosen_cs.append(q)

            caregiver_services_raw = ",".join(chosen_cs) if chosen_cs else None

            medical_needs = (request.form.get("medical_needs") or "").strip()
            languages = (request.form.get("languages") or "").strip()
            location_combined = (request.form.get("location") or "").strip()
            city = (request.form.get("location_city") or "").strip()
            country = (request.form.get("location_country") or "").strip()
            if not location_combined and city and country:
                location_combined = f"{city}/{country}"

            return {
                "care_needed": care_needed_joined,
                "care_needed_other": none_if_blank(care_needed_other) or None,
                "number_of_dependents": safe_int(number_of_dependents),
                "care_hours_per_week": safe_int(care_hours_per_week),
                "preferred_caregiver_age": none_if_blank(preferred_caregiver_age) or None,

                "preferred_schedule": none_if_blank(preferred_schedule) or None,
                "preferred_schedule_start": none_if_blank(preferred_schedule_start) or None,
                "preferred_schedule_end": none_if_blank(preferred_schedule_end) or None,

                "hourly_budget": none_if_blank(hourly_budget) or None,
                "preferred_gender": none_if_blank(preferred_gender) or None,
                # Ensure we return caregiver_services as canonical key (comma-string or None)
                "caregiver_services": caregiver_services_raw,
                # keep legacy household_info too for compatibility
                "household_info": none_if_blank(household_info_raw) or caregiver_services_raw,
                "medical_needs": none_if_blank(medical_needs) or None,
                "languages": none_if_blank(languages) or None,
                "location": none_if_blank(location_combined) or None
            }

        # If saving a draft -> do minimal validation and persist whatever user provided
        if is_draft:
            if user and getattr(user, "role", "caregiver") == "caregiver":
                saved = gather_caregiver_post()
            else:
                saved = gather_family_post()

            # Merge with existing session draft safely
            normalized_saved = {}
            for k, v in saved.items():
                # convert lists to CSV (none of our gather_* currently returns lists)
                if isinstance(v, list):
                    normalized_saved[k] = ",".join([str(x).strip() for x in v if str(x).strip() != ""])
                    continue

                # Protect against literal "None"/"null" strings in incoming data: convert to None
                if isinstance(v, str) and v.strip().lower() in ("none", '"none"', "null", '"null"'):
                    normalized_saved[k] = None
                    continue

                # Keep None as None; keep strings as-is
                normalized_saved[k] = v

            # merge shallowly
            session["profile_step2"] = {**(session.get("profile_step2", {}) or {}), **normalized_saved}
            session.modified = True

            if app.debug:
                app.logger.debug("Saved profile_step2 (draft) -> %r", session.get("profile_step2"))
            flash("Draft saved.", "success")
            return redirect(url_for("profile_step2"))

        # Non-draft (final submit) -> run validations then persist
        errors = []
        if user and getattr(user, "role", "caregiver") == "caregiver":
            posted = gather_caregiver_post()

            # Basic validations
            if posted["experience_years"] is None:
                errors.append("Please provide valid years of experience.")
            if not posted["specialties"]:
                errors.append("Please select a specialty.")
            # If user chose 'other' specialty require typed text (server-side)
            if posted.get("specialties") and str(posted.get("specialties")).strip().lower() == "other":
                so = (posted.get("specialties_other") or "").strip()
                if not so:
                    errors.append("Please specify your specialty in the Others field (max 21 characters).")
                elif len(so) > 21:
                    errors.append("Specialty (Others) must be at most 21 characters.")

            # salary: either structured or legacy
            salary_formatted = None
            if posted["salary_amount"] and posted["salary_currency"] and posted["salary_frequency"]:
                fm = format_money(posted["salary_amount"])
                if not fm:
                    errors.append("Please provide a valid numeric Salary amount.")
                elif posted["salary_currency"] not in CURRENCY_SYMBOLS:
                    errors.append("Please select a valid currency.")
                else:
                    salary_formatted = f"{CURRENCY_SYMBOLS[posted['salary_currency']]}{fm}/{posted['salary_frequency']}"
            else:
                if not posted["salary"]:
                    errors.append("Please provide a salary or hourly rate.")
                else:
                    salary_formatted = posted["salary"]

            if not posted["location"]:
                errors.append("Please provide a city and country for Location.")

            # availability and times
            if not posted["availability_days"]:
                errors.append("Please select at least one availability day.")
            def valid_time(s):
                try:
                    datetime.strptime(s, "%H:%M")
                    return True
                except Exception:
                    return False
            if not (posted["availability_start"] and posted["availability_end"] and
                    valid_time(posted["availability_start"]) and valid_time(posted["availability_end"])):
                errors.append("Please provide valid availability start and end times.")

            if not posted["willing_to_travel"]:
                if posted["travel_radius_km"] is None:
                    errors.append("Please provide travel preference (Yes/No) or a valid travel radius (km).")
            else:
                if posted["willing_to_travel"] not in ("yes", "no"):
                    errors.append("Invalid value for Willing to travel.")

            if not posted["services"]:
                errors.append("Please select at least one service you offer.")
            if not posted["preferred_age"]:
                errors.append("Please enter preferred care recipient age groups.")

            # disability validations: if user chose 'yes' require details; also limit length
            dis = (posted.get("disability") or "").strip().lower()
            if dis == "":
                errors.append("Please select a disability option (or None).")
            else:
                if dis in ("yes", "y", "true", "1", "on"):
                    dd = (posted.get("disability_details") or "").strip()
                    if not dd:
                        errors.append("You selected Disability = Yes — please specify (max 21 characters).")
                    elif len(dd) > 21:
                        errors.append("Disability details must be at most 21 characters.")

            if not posted["languages"]:
                errors.append("Please list languages you speak.")

            if errors:
                for e in errors:
                    flash(e, "error")
                posted_for_template = dict(posted)
                return render_template("profile_step2.html", user=user, data=normalize_for_template(posted_for_template))

            days = posted["availability_days"].split(",") if posted["availability_days"] else []
            days_str = "Mon-Sun" if len(days) == 7 else ",".join(days)
            time_str = f"{format_time_human(posted['availability_start'])}–{format_time_human(posted['availability_end'])}"
            availability_final = f"{days_str}, {time_str}"

            step_data = {
                "experience_years": posted["experience_years"],
                "specialties": posted["specialties"],
                "specialties_other": posted.get("specialties_other"),
                "certifications": posted["certifications"],
                "hourly_rate": salary_formatted,
                "salary_amount": posted["salary_amount"],
                "salary_currency": posted["salary_currency"],
                "salary_frequency": posted["salary_frequency"],
                "salary": posted["salary"],
                "location": posted["location"],
                "availability": availability_final,
                "availability_start": posted["availability_start"],
                "availability_end": posted["availability_end"],
                "availability_days": posted["availability_days"] or None,
                "willing_to_travel": posted["willing_to_travel"] or None,
                "travel_radius_km": posted["travel_radius_km"],
                "services": posted["services"],
                "services_other": posted.get("services_other"),
                "preferred_age": posted["preferred_age"],
                "disability": posted["disability"],
                "disability_details": posted.get("disability_details"),
                "languages": posted["languages"]
            }

            session["profile_step2"] = {**(session.get("profile_step2", {}) or {}), **step_data}
            session.modified = True
            if app.debug:
                app.logger.debug("Saved profile_step2 (final): %r", session.get("profile_step2"))
            flash("Step 2 saved.", "success")
            return redirect(url_for("profile_setup"))






        else:
            # Family branch final submit validations & persist
            posted = gather_family_post()
            errors = []
            if not posted["care_needed"]:
                errors.append("Please select at least one type of care needed.")
            if posted["number_of_dependents"] is None:
                errors.append("Please provide a valid number of dependents.")
            if not posted.get("preferred_caregiver_age"):
                errors.append("Please select preferred caregiver age.")
            if not posted["preferred_schedule"]:
                errors.append("Please provide preferred schedule.")
            if not posted["hourly_budget"]:
                errors.append("Please provide hourly budget.")
            if not posted["preferred_gender"]:
                errors.append("Please select preferred caregiver gender.")
            if not posted.get("caregiver_services") and not posted.get("household_info"):
                errors.append("Please provide household / caregiver services information.")
            if not posted["medical_needs"]:
                errors.append("Please describe medical needs (or none).")
            if not posted["languages"]:
                errors.append("Please list preferred languages.")
            if not posted["location"]:
                errors.append("Please enter a location (city and country).")

            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("profile_step2.html", user=user, data=normalize_for_template(posted))

            if isinstance(posted.get("caregiver_services"), list):
                posted["caregiver_services"] = ",".join([str(x).strip() for x in posted["caregiver_services"] if str(x).strip() != ""])
            if isinstance(posted.get("household_info"), list):
                posted["household_info"] = ",".join([str(x).strip() for x in posted["household_info"] if str(x).strip() != ""])

            if not posted.get("household_info") and posted.get("caregiver_services"):
                posted["household_info"] = posted.get("caregiver_services")
            if not posted.get("caregiver_services") and posted.get("household_info"):
                posted["caregiver_services"] = posted.get("household_info")

            session["profile_step2"] = {**(session.get("profile_step2", {}) or {}), **posted}
            session.modified = True
            if app.debug:
                app.logger.debug("Saved profile_step2 (family final): %r", session.get("profile_step2"))
            flash("Step 2 saved.", "success")
            return redirect(url_for("profile_setup"))

    # GET: render form with existing session draft (if any)
    return render_template("profile_step2.html", user=user, data=normalize_for_template(session.get("profile_step2", {}) or {}))


@app.route("/discard-draft", methods=["POST"])
def discard_draft():
    """Endpoint to explicitly discard profile drafts from session"""
    if "user_id" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))
    session.pop("profile_step1", None)
    session.pop("profile_step2", None)
    session.modified = True
    flash("Draft cleared.", "info")
    return redirect(url_for("profile_step1"))



@app.route("/profile-setup", methods=["GET", "POST"])
def profile_setup():
    """
    Preview & final save.
      - Visiting this page does NOT create a DB Profile automatically.
      - Only when the user POSTS (Save) do we create/update the DB Profile.
      - After successful DB save, session drafts are cleared so profile becomes permanent.
    """
    if "user_id" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    user = db.session.get(User, session["user_id"])
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    # load existing persisted profile if any (may be None)
    profile = Profile.query.filter_by(user_id=user.id).first()

    # load drafts from session for preview
    draft_step1 = session.get("profile_step1", {}) or {}
    draft_step2 = session.get("profile_step2", {}) or {}

    # ENFORCE onboarding: if user has no persisted profile AND no session drafts,
    # treat them as a new registrant and redirect to step 1. Skip this redirect when
    # explicit enable_actions is present (admin/edit flows).
    if request.method == "GET" and (not profile) and (not draft_step1) and (not draft_step2) and (not request.args.get("enable_actions")):
        app.logger.debug("Redirecting new user %s -> profile_step1 (no profile and no drafts)", getattr(user, "id", None))
        return redirect(url_for("profile_step1"))

    # --- Helper: parse & normalize profile.other_photos into a list of basenames ---
    def parse_other_photos_field(profile_obj):
        """
        Return a list of normalized filenames (basename only) extracted from profile_obj.other_photos.
        Accepts CSV string, JSON list string, list, or comma-joined string. Strips urls/paths.
        """
        out = []
        try:
            raw = None
            if not profile_obj:
                return []
            raw = getattr(profile_obj, "other_photos", None)
            if raw is None:
                return []
            # if it's already a list
            if isinstance(raw, (list, tuple)):
                candidates = list(raw)
            else:
                # try JSON (older code may have stored json.dumps(list))
                if isinstance(raw, str):
                    s = raw.strip()
                    # try JSON
                    try:
                        parsed = json.loads(s)
                        if isinstance(parsed, (list, tuple)):
                            candidates = parsed
                        else:
                            # fallback to CSV split
                            candidates = [p.strip() for p in s.split(',') if p.strip()]
                    except Exception:
                        # fallback: CSV or single string
                        candidates = [p.strip() for p in str(raw).split(',') if p.strip()]
                else:
                    # unknown type -> convert to string and split
                    candidates = [p.strip() for p in str(raw).split(',') if p.strip()]
            # normalize entries to basename (remove any leading '/uploads/' or full URL)
            for c in candidates:
                try:
                    if not c:
                        continue
                    # If it's a URL, parse it and use path basename
                    # If it contains 'uploads/' path fragment, strip everything up to and including the last uploads/ token
                    val = str(c).strip()
                    # Example patterns we want to handle:
                    #   "/uploads/filename.jpg"
                    #   "uploads/filename.jpg"
                    #   "http://host/uploads/filename.jpg"
                    #   "/var/www/app/uploads/filename.jpg"
                    #   "filename.jpg"
                    # So just get os.path.basename of any path/URL
                    # But first, if it contains '?', drop query string
                    if '?' in val:
                        val = val.split('?', 1)[0]
                    # If it looks like a URL, use urlparse to get path
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(val)
                        if parsed.scheme and parsed.path:
                            candidate_basename = os.path.basename(parsed.path)
                        else:
                            candidate_basename = os.path.basename(val)
                    except Exception:
                        candidate_basename = os.path.basename(val)
                    candidate_basename = candidate_basename.strip()
                    if candidate_basename:
                        out.append(candidate_basename)
                except Exception:
                    continue
        except Exception:
            return []
        # dedupe preserving order
        seen = set()
        dedup = []
        for x in out:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        return dedup

    # existing other_photos list for persisted profile (if profile exists)
    existing = parse_other_photos_field(profile)

    def _canon_from_request_and_draft_for_household(draft_value):
        """
        Build a canonical CSV string for household / caregiver services using:
        - draft value (string or list)
        - multiple possible form sources: caregiver_services (list), caregiver_services[] (list),
          caregiver_services_checkbox, caregiver_services_hidden (CSV), household_info,
          household_service (legacy), etc.
        Returns: comma-joined CSV string or None.
        """
        chosen_raw = []

        # 1) draft value may be a list or CSV string
        if draft_value:
            if isinstance(draft_value, list):
                chosen_raw.extend(draft_value)
            elif isinstance(draft_value, str):
                chosen_raw.append(draft_value)

        # 2) form list-style checkbox names (canonical, array, and the alternate checkbox name your template uses)
        chosen_raw.extend(request.form.getlist("caregiver_services") or [])
        chosen_raw.extend(request.form.getlist("caregiver_services[]") or [])
        chosen_raw.extend(request.form.getlist("caregiver_services_checkbox") or [])          # front-end checkbox name
        chosen_raw.extend(request.form.getlist("caregiver_services_checkbox[]") or [])       # alternate

        # 3) hidden CSV fields (single string)
        single_csv = (request.form.get("caregiver_services") or request.form.get("caregiver_services_hidden") or "").strip()
        if single_csv:
            chosen_raw.append(single_csv)

        # 4) household_info hidden field (front-end fallback)
        if request.form.get("household_info"):
            chosen_raw.append(request.form.get("household_info"))

        # 5) legacy fields
        chosen_raw.extend(request.form.getlist("household_service") or [])
        chosen_raw.extend(request.form.getlist("household_service[]") or [])

        # 6) last fallback: posted single household_service or profile existing value
        if not chosen_raw:
            if request.form.get("household_info"):
                chosen_raw.append(request.form.get("household_info"))
            elif getattr(profile, "household_info", None):
                chosen_raw.append(getattr(profile, "household_info"))

        # Expand CSV entries inside list entries, trim and dedupe preserving first-seen order
        expanded = []
        for item in chosen_raw:
            if not item:
                continue
            for part in str(item).split(','):
                p = part.strip()
                if p:
                    expanded.append(p)

        seen = set()
        chosen = []
        for x in expanded:
            if x not in seen:
                seen.add(x)
                chosen.append(x)

        return ",".join(chosen) if chosen else None

    if request.method == "POST":
        # User clicked Save & Publish -> persist profile to DB
        try:
            profile = Profile.query.filter_by(user_id=user.id).first()
            if not profile:
                profile = Profile(user_id=user.id)

            # remove existing files if requested
            to_remove = request.form.getlist("remove_existing")
            if to_remove:
                for fname in to_remove:
                    try:
                        # ensure we compare basenames (existing is a list of basenames)
                        bn = os.path.basename(str(fname))
                        if bn in existing:
                            path = os.path.join(app.config["UPLOAD_FOLDER"], bn)
                            if os.path.exists(path):
                                os.remove(path)
                            existing.remove(bn)
                    except Exception as e:
                        app.logger.warning("Failed to remove file %s: %s", fname, e)

            # profile photo upload (final save)
            f = request.files.get("profile_photo")
            if f and f.filename:
                if allowed_file(f.filename):
                    fname = secure_filename(f"{user.id}_profile_{int(time.time())}_{f.filename}")
                    save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                    f.save(save_path)
                    # remove old profile photo file (if any)
                    if profile.profile_photo:
                        try:
                            oldp = os.path.join(app.config['UPLOAD_FOLDER'], profile.profile_photo)
                            if os.path.exists(oldp):
                                os.remove(oldp)
                        except Exception:
                            pass
                    profile.profile_photo = fname
                else:
                    flash("Profile photo must be an image (png/jpg/jpeg/gif/webp).", "error")
                    return redirect(url_for("profile_setup"))

            # other photos upload
            new_files = request.files.getlist("other_photos")
            new_saved = []
            for fo in new_files:
                if fo and fo.filename:
                    if not allowed_file(fo.filename):
                        app.logger.info("Skipping invalid file type: %s", fo.filename)
                        continue
                    if len(existing) + len(new_saved) >= MAX_OTHER_PHOTOS:
                        flash(f"Maximum of {MAX_OTHER_PHOTOS} extra photos allowed. Some files were not saved.", "error")
                        break
                    fname = secure_filename(f"{user.id}_other_{int(time.time())}_{fo.filename}")
                    save_path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                    fo.save(save_path)
                    new_saved.append(fname)
            existing.extend(new_saved)

            # --- persist draft profile photo if no new upload in this request ---
            draft_step1 = session.get("profile_step1", {}) or {}
            draft_pp = draft_step1.get("profile_photo") if draft_step1 else None
            if (not getattr(profile, "profile_photo", None)) and draft_pp:
                # normalize draft_pp to a basename (strip URLs/paths) and ensure file exists in UPLOAD_FOLDER
                try:
                    candidate_basename = os.path.basename(str(draft_pp).split('?', 1)[0])
                    draft_path = os.path.join(app.config['UPLOAD_FOLDER'], candidate_basename)
                    if candidate_basename and os.path.exists(draft_path):
                        profile.profile_photo = candidate_basename
                        app.logger.debug("Persisted draft profile photo '%s' for user %s", candidate_basename, user.id)
                    else:
                        app.logger.debug("Draft profile photo '%s' not found on disk; skipping persist.", draft_pp)
                except Exception:
                    app.logger.exception("Error while persisting draft profile photo; skipping it.")

            # Use drafts first (draft_step2/draft_step1), fallback to form fields posted
            # Defensive normalization: only call .strip on string-like values
            draft_step2 = session.get("profile_step2", {}) or {}
            draft_step1 = session.get("profile_step1", {}) or {}

            bio_candidate = _first_nonempty(draft_step1.get("bio") if draft_step1 else None, request.form.get("bio", None))
            profile.bio = bio_candidate.strip() if isinstance(bio_candidate, str) else (bio_candidate or None)

            ms_candidate = _first_nonempty(
                draft_step1.get("marital_status") if draft_step1 else None,
                draft_step1.get("marital") if draft_step1 else None,
                request.form.get("marital_status", None),
                request.form.get("marital", None),
                getattr(profile, "marital_status", None)
            )
            profile.marital_status = ms_candidate or None

            age_val = None
            if draft_step1:
                for key in ("age", "age_years", "years_old"):
                    v = draft_step1.get(key)
                    if v not in (None, "", False):
                        age_val = v
                        break
            if age_val is None:
                age_val = request.form.get("age") or request.form.get("age_years") or request.form.get("years_old") or getattr(profile, "age", None)
            try:
                profile.age = int(age_val) if age_val not in (None, "", False) else None
            except Exception:
                profile.age = None

            location_candidate = _first_nonempty(
                draft_step2.get("location") if draft_step2 else None,
                request.form.get("location", None),
                draft_step1.get("location") if draft_step1 else None,
                getattr(profile, "location", None)
            )
            profile.location = (location_candidate or None)

            languages_candidate = _first_nonempty(
                draft_step2.get("languages") if draft_step2 else None,
                request.form.get("languages", None),
                draft_step1.get("languages") if draft_step1 else None,
                getattr(profile, "languages", None)
            )
            profile.languages = (languages_candidate or None)

            if user.role == "caregiver":
                # experience
                try:
                    exp = _first_nonempty(draft_step2.get("experience_years") if draft_step2 else None, request.form.get("experience_years"))
                    profile.experience_years = int(exp) if exp not in (None, "", False) else None
                except Exception:
                    profile.experience_years = None

                # salary stored in legacy hourly_rate column as formatted text
                rate_val = _first_nonempty(draft_step2.get("hourly_rate") if draft_step2 else None, request.form.get("salary", None), request.form.get("hourly_rate", None))
                profile.hourly_rate = rate_val.strip() if isinstance(rate_val, str) else (rate_val or None)

                profile.certifications = _first_nonempty(draft_step2.get("certifications") if draft_step2 else None, request.form.get("certifications", None)) or None

                try:
                    tr = _first_nonempty(draft_step2.get("travel_radius_km") if draft_step2 else None, request.form.get("travel_radius_km"))
                    profile.travel_radius_km = int(tr) if tr not in (None, "", False) else None
                except Exception:
                    profile.travel_radius_km = None

                wt = _first_nonempty(draft_step2.get("willing_to_travel") if draft_step2 else None, request.form.get("willing_to_travel", None))
                if wt:
                    wt_l = wt.strip().lower()
                    profile.willing_to_travel = wt_l if wt_l in ("yes", "no") else None
                else:
                    profile.willing_to_travel = None

                profile.background_check = _first_nonempty(draft_step2.get("background_check") if draft_step2 else None, request.form.get("background_check", None)) or None

                # --- Services & Specialties: deterministic resolution of main + other tokens ---
                def _resolve_main_and_other(draft_main, form_main, draft_other, form_other):
                    """
                    Normalize a main CSV/list and an 'other' typed text into a readable CSV string.
                    Priority:
                      - prefer draft_main (string or list) -> else form_main
                      - prefer form_other -> draft_other for the typed-other text
                      - handle tokens like 'other', 'others', 'other:something' and 'other=something'
                    Returns: None or comma-joined string.
                    """
                    # choose main source
                    main_val = None
                    if draft_main not in (None, "", False):
                        main_val = draft_main
                    elif form_main not in (None, "", False):
                        main_val = form_main

                    # typed other preferred from form then draft
                    other_val = None
                    if form_other not in (None, "", False):
                        other_val = form_other
                    elif draft_other not in (None, "", False):
                        other_val = draft_other

                    # turn main_val into tokens
                    tokens = []
                    if isinstance(main_val, list):
                        tokens = [str(t).strip() for t in main_val if str(t).strip()]
                    elif isinstance(main_val, str):
                        # treat CSV or single token
                        for part in main_val.split(','):
                            if part and part.strip():
                                tokens.append(part.strip())

                    # if no tokens but typed other exists, persist typed other
                    if not tokens and other_val:
                        return other_val.strip()

                    cleaned = []
                    for t in tokens:
                        if not t:
                            continue
                        tok = str(t).strip()
                        tl = tok.lower()
                        if tl.startswith("other:"):
                            v = tok.split(":", 1)[1].strip()
                            if v:
                                cleaned.append(v)
                            else:
                                cleaned.append("Other")
                        elif tl.startswith("other="):
                            v = tok.split("=", 1)[1].strip()
                            if v:
                                cleaned.append(v)
                            else:
                                cleaned.append("Other")
                        elif tl in ("other", "others"):
                            if other_val:
                                cleaned.append(other_val.strip())
                            else:
                                cleaned.append("Other")
                        else:
                            cleaned.append(tok)

                    # dedupe while preserving order
                    seen = set()
                    deduped = []
                    for x in cleaned:
                        key = x.lower()
                        if key not in seen:
                            seen.add(key)
                            deduped.append(x)

                    return ",".join(deduped) if deduped else None

                # Drafts and form candidates for services
                draft_services_val = draft_step2.get("services") if draft_step2 else None
                draft_services_other = draft_step2.get("services_other") if draft_step2 else None
                form_services_list = request.form.getlist("services") or []
                form_services_hidden = (request.form.get("services") or request.form.get("services_hidden") or "").strip()
                form_services_candidate = form_services_list if form_services_list else (form_services_hidden or None)

                profile.services = _resolve_main_and_other(draft_services_val, form_services_candidate, draft_services_other, request.form.get("services_other"))
                profile.services_other = _first_nonempty(draft_services_other if draft_services_other else None, request.form.get("services_other", None)) or None

                # Drafts and form candidates for specialties
                draft_specs_val = draft_step2.get("specialties") if draft_step2 else None
                draft_specs_other = draft_step2.get("specialties_other") if draft_step2 else None
                form_specs_list = request.form.getlist("specialties") or []
                form_specs_hidden = (request.form.get("specialties") or request.form.get("specialties_hidden") or "").strip()
                form_specs_candidate = form_specs_list if form_specs_list else (form_specs_hidden or None)

                profile.specialties = _resolve_main_and_other(draft_specs_val, form_specs_candidate, draft_specs_other, request.form.get("specialties_other"))
                profile.specialties_other = _first_nonempty(draft_specs_other if draft_specs_other else None, request.form.get("specialties_other", None)) or None

                # Debug log before any normalization/humanize
                app.logger.debug("About to persist caregiver values (user %s): specialties=%r specialties_other=%r services=%r services_other=%r",
                                 user.id, profile.specialties, profile.specialties_other, profile.services, profile.services_other)

                profile.preferred_age = _first_nonempty(draft_step2.get("preferred_age") if draft_step2 else None, request.form.get("preferred_age", None)) or profile.preferred_age

                profile.disability = _first_nonempty(draft_step2.get("disability") if draft_step2 else None, request.form.get("disability", None)) or None
                profile.disability_details = _first_nonempty(draft_step2.get("disability_details") if draft_step2 else None, request.form.get("disability_details", None)) or None
                profile.availability = _first_nonempty(draft_step2.get("availability") if draft_step2 else None, request.form.get("availability", None)) or None

            else:
                # family fields
                care_needed_vals = draft_step2.get("care_needed") if draft_step2 else None
                # if draft_step2 contains care_needed_other, prefer that for profile.care_needed_other
                profile.care_needed_other = _first_nonempty(draft_step2.get("care_needed_other") if draft_step2 else None, request.form.get("care_needed_other", None), getattr(profile, "care_needed_other", None)) or None

                if isinstance(care_needed_vals, str):
                    profile.care_needed = care_needed_vals if care_needed_vals.strip() != "" else None
                elif isinstance(care_needed_vals, list):
                    profile.care_needed = ",".join(care_needed_vals) if care_needed_vals else profile.care_needed
                else:
                    posted_cn = request.form.getlist("care_needed") or ([request.form.get("care_needed")] if request.form.get("care_needed") else [])
                    # If posted contains a literal 'other' and we have a typed other in request.form, replace it
                    if posted_cn and any(x and str(x).strip().lower() in ("other", "others") for x in posted_cn):
                        posted_clean = [x for x in posted_cn if x and str(x).strip().lower() not in ("other", "others")]
                        typed_other = _first_nonempty(request.form.get("care_needed_other"), draft_step2.get("care_needed_other") if draft_step2 else None)
                        if typed_other:
                            posted_clean.append(typed_other)
                        else:
                            posted_clean.append("Other")
                        profile.care_needed = ",".join([str(x).strip() for x in posted_clean if x])
                    else:
                        profile.care_needed = ",".join(posted_cn) if posted_cn else profile.care_needed

                profile.preferred_schedule = _first_nonempty(draft_step2.get("preferred_schedule") if draft_step2 else None, request.form.get("preferred_schedule", None)) or None
                profile.preferred_caregiver_age = _first_nonempty(draft_step2.get("preferred_caregiver_age") if draft_step2 else None, request.form.get("preferred_caregiver_age", None)) or None

                try:
                    nd = _first_nonempty(draft_step2.get("number_of_dependents") if draft_step2 else None, request.form.get("number_of_dependents"))
                    profile.number_of_dependents = int(nd) if nd not in (None, "", False) else None
                except Exception:
                    profile.number_of_dependents = None

                try:
                    ch = _first_nonempty(draft_step2.get("care_hours_per_week") if draft_step2 else None, request.form.get("care_hours_per_week"))
                    profile.care_hours_per_week = int(ch) if ch not in (None, "", False) else None
                except Exception:
                    profile.care_hours_per_week = None

                # Build canonical household / caregiver services CSV from draft/form/legacy/profile
                raw_household = _canon_from_request_and_draft_for_household(draft_step2.get("household_info") if draft_step2 else None)

                if isinstance(raw_household, list):
                    raw_household = ",".join([str(x).strip() for x in raw_household if str(x).strip() != ""])
                if isinstance(raw_household, str):
                    raw_household = raw_household.strip() or None

                profile.household_info = raw_household or None

                if hasattr(profile, "caregiver_services"):
                    profile.caregiver_services = raw_household or None

                profile.hourly_budget = _first_nonempty(draft_step2.get("hourly_budget") if draft_step2 else None, request.form.get("hourly_budget", None)) or None
                profile.preferred_gender = _first_nonempty(draft_step2.get("preferred_gender") if draft_step2 else None, request.form.get("preferred_gender", None)) or None
                profile.medical_needs = _first_nonempty(draft_step2.get("medical_needs") if draft_step2 else None, request.form.get("medical_needs", None)) or None

            # persist other_photos list as a comma-joined string of basenames (consistent with templates)
            try:
                profile.other_photos = ','.join(existing) if existing else None
            except Exception:
                profile.other_photos = None

            # --- Ensure typed "other" text is preferred when the main value is literal 'other' ---
            try:
                if user.role == "caregiver":
                    # specialties fallback: prefer typed-other if main is empty / literal other
                    if not profile.specialties or (isinstance(profile.specialties, str) and profile.specialties.strip().lower() in ("other", "others")):
                        typed_spec = _first_nonempty(draft_step2.get("specialties_other") if draft_step2 else None,
                                                     request.form.get("specialties_other"))
                        if typed_spec:
                            profile.specialties = typed_spec.strip()

                    # services fallback: prefer typed-other if main is empty / literal other
                    if not profile.services or (isinstance(profile.services, str) and profile.services.strip().lower() in ("other", "others")):
                        typed_svc = _first_nonempty(draft_step2.get("services_other") if draft_step2 else None,
                                                    request.form.get("services_other"))
                        if typed_svc:
                            profile.services = typed_svc.strip()
                else:
                    # family: care_needed typed-other fallback
                    if not profile.care_needed or (isinstance(profile.care_needed, str) and profile.care_needed.strip().lower() in ("other", "others")):
                        typed_cn = _first_nonempty(draft_step2.get("care_needed_other") if draft_step2 else None,
                                                   request.form.get("care_needed_other"))
                        if typed_cn:
                            profile.care_needed = typed_cn.strip()
            except Exception:
                app.logger.exception("typed-other fallback failed")

            # --------------------------
            # Normalise 'other' tokens into typed text so DB stores readable values
            # --------------------------
            # Specialties (caregiver)
            try:
                if user.role == "caregiver":
                    sp = profile.specialties
                    so = profile.specialties_other
                    if sp:
                        if isinstance(sp, str):
                            parts = [part.strip() for part in sp.split(",") if part and part.strip()]
                            # replace literal 'other' tokens with typed other (if present)
                            normalized_parts = []
                            for p in parts:
                                pl = p.strip().lower()
                                if pl in ("other", "others") and so:
                                    normalized_parts.append(so.strip())
                                else:
                                    normalized_parts.append(p)
                            profile.specialties = ",".join(normalized_parts) if normalized_parts else None
                        # if specialties is not a str, leave it unchanged
                    elif so:
                        # If no specialties token but typed other exists, persist the typed other
                        profile.specialties = so.strip()
                # Services (caregiver)
                sv = profile.services
                svo = profile.services_other
                if sv:
                    if isinstance(sv, str):
                        parts = [part.strip() for part in sv.split(",") if part and part.strip()]
                        normalized_parts = []
                        for p in parts:
                            pl = p.strip().lower()
                            if pl in ("other", "others") and svo:
                                normalized_parts.append(svo.strip())
                            else:
                                normalized_parts.append(p)
                        profile.services = ",".join(normalized_parts) if normalized_parts else None
                elif svo:
                    # If services field empty but services_other exists, persist it
                    profile.services = svo.strip()
            except Exception:
                # If normalization fails, don't block saving — keep original values
                app.logger.exception("Normalization of 'other' tokens failed.")

            # --- Humanize tokens so persisted DB values are readable (e.g. "Light housekeeping") ---
            try:
                if user.role == "caregiver":
                    # specialties: attempt to humanize, but keep original if humanize_csv returns falsy
                    if getattr(profile, "specialties", None):
                        try:
                            humanized_spec = humanize_csv(profile.specialties)
                        except Exception:
                            humanized_spec = None
                        profile.specialties = humanized_spec if humanized_spec else profile.specialties

                    # services: attempt to humanize, but keep original if humanize_csv returns falsy
                    if getattr(profile, "services", None):
                        try:
                            humanized_svc = humanize_csv(profile.services)
                        except Exception:
                            humanized_svc = None
                        profile.services = humanized_svc if humanized_svc else profile.services
                else:
                    if getattr(profile, "care_needed", None):
                        try:
                            humanized_cn = humanize_csv(profile.care_needed)
                        except Exception:
                            humanized_cn = None
                        profile.care_needed = humanized_cn if humanized_cn else profile.care_needed

                    if getattr(profile, "household_info", None):
                        try:
                            humanized_hh = humanize_csv(profile.household_info)
                        except Exception:
                            humanized_hh = None
                        profile.household_info = humanized_hh if humanized_hh else profile.household_info

                    if hasattr(profile, "caregiver_services") and getattr(profile, "caregiver_services", None):
                        try:
                            humanized_cs = humanize_csv(profile.caregiver_services)
                        except Exception:
                            humanized_cs = None
                        profile.caregiver_services = humanized_cs if humanized_cs else profile.caregiver_services
            except Exception:
                app.logger.exception("Humanize-before-save failed; continuing with original values.")

            # IMPORTANT: mark the profile published here (the one-time Publish action)
            try:
                profile.published = True
            except Exception:
                # if the DB hasn't the column (older DB) this will raise; we swallow so saving still works
                app.logger.debug("Could not set profile.published (maybe DB column missing) - continuing without setting it.")

            db.session.add(profile)
            db.session.commit()

            # --- create + emit welcome notification for this user (synchronous) ---
            try:
                # try to get user's email to send email if desired
                user_email = getattr(user, "email", None)
                # use the helper created earlier. pass our safe email wrapper to actually send the email
                # use the full welcome helper (builds DB rows + full HTML email + text alt)
                try:
                    send_welcome_notification(user)
                except Exception:
                    app.logger.exception("send_welcome_notification failed for user %s", user.id)
                
            except Exception:
                app.logger.exception("Failed to create/emit welcome notification for user %s", user.id)

            # On successful commit -> clear session drafts so the profile is now permanent
            session.pop("profile_step1", None)
            session.pop("profile_step2", None)
            session.modified = True

            flash("Profile saved!", "success")
            return redirect(url_for("profile_setup"))
            
        except Exception as exc:
            app.logger.error("Failed saving profile: %s", exc)
            flash("Failed to save profile. Please try again.", "error")
            # do not clear drafts on failure so the user can retry
            return redirect(url_for("profile_setup"))

    # GET: build preview merging draft & profile
    parsed_other_photos = existing
    preview = build_preview(user, profile, draft_step1, draft_step2, parsed_other_photos)

    # provide aliases so template references work regardless of which variable name is used
    s1 = normalize_for_template(session.get("profile_step1", {}) or {})
    s2 = normalize_for_template(session.get("profile_step2", {}) or {})

    # Defensive: sanitize literal string tokens that represent "no value"
    def sanitize_literal_none(val):
        if isinstance(val, str) and val.strip().lower() in ("none", '"none"', "null", '"null"'):
            return None
        return val

    # sanitize preview fields that might have literal 'None' strings
    for k in list(preview.keys()):
        preview[k] = sanitize_literal_none(preview[k])

    # --- compute published flag & suppress overlay param to pass to template ---
    # published_flag: True when flash indicates saved OR request arg says published=1 OR DB profile.published truthy
    flashed = get_flashed_messages()
    published_flag = (
        (('Profile saved!' in flashed)) or
        (request.args.get('published') == '1') or
        bool(profile and getattr(profile, "published", False))
    )

    # If caller explicitly asked to suppress overlay (e.g. after editing), respect that.
    suppress_publish_overlay = bool(request.args.get('suppress_overlay'))

    # enable actions when:
    #  - caller set enable_actions=1 (we will use this from profile_edit redirect),
    #  - OR the profile row has published=True in DB
    actions_enabled = bool(request.args.get('enable_actions')) or bool(profile and getattr(profile, "published", False))

    # --- EXTRA UI FLAGS: do not change existing behaviour, just expose explicit flags to template ---
    is_published = bool(profile and getattr(profile, "published", False))
    has_drafts = bool(draft_step1 or draft_step2)
    first_time_preview = (not is_published) and has_drafts
    show_save_publish = not is_published
    # disable action controls when this is a fresh preview (unpublished + drafts) unless enable_actions param present
    disable_action_controls = first_time_preview and (not bool(request.args.get('enable_actions')))

    return render_template(
        "profile_setup.html",
        user=user,
        profile=profile,
        other_photos=parsed_other_photos,
        draft_step1=draft_step1,
        draft_step2=draft_step2,
        preview=preview,
        s1=s1,
        s2=s2,
        profile_step1=s1,
        profile_step2=s2,
        published=published_flag,
        suppress_publish_overlay=suppress_publish_overlay,
        actions_enabled=actions_enabled,
        # new explicit flags your template can use
        is_published=is_published,
        has_drafts=has_drafts,
        show_save_publish=show_save_publish,
        disable_action_controls=disable_action_controls
    )






@app.route("/session-set-test")
def session_set_test():
    session["__test_marker"] = "works"
    session["profile_step2"] = {
        "specialties": "__test_specialty",
        "services": "companionship"
    }
    session.modified = True
    return "Session set. Now visit /session-get-test to inspect."

@app.route("/session-get-test")
def session_get_test():
    return jsonify(dict(session))

@app.route("/debug-session")
def debug_session():
    return jsonify(dict(session))






@app.route("/connections")
def connections():
    """
    Show opposite-role profiles: caregivers see families; families see caregivers.
    Optional: ?role=caregiver or ?role=family to override.
    """
    if "user_id" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    user = db.session.get(User, session["user_id"])
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("login"))

    # allow override by query param if desired
    override = request.args.get("role")
    if override in ("caregiver", "family"):
        target = override
    else:
        # caregivers should see families, families should see caregivers
        target = "family" if user.role == "caregiver" else "caregiver"

    # Query profiles belonging to users with the target role
    try:
        profiles = Profile.query.join(User).filter(User.role == target).all()
    except Exception:
        profiles = []

    return render_template("connections.html", user=user, profiles=profiles, target=target)


@app.route("/profile/<int:user_id>")
def view_public_profile(user_id):
    """
    Minimal public profile viewer so connections cards can link to detail pages.
    This is intentionally simple: shows the profile for the given user_id (if any).
    """
    profile = Profile.query.filter_by(user_id=user_id).first()
    if not profile:
        flash("Profile not found.", "error")
        return redirect(url_for("connections"))

    # guard: if a profile exists, grab its user as well
    user = User.query.get(user_id)
    return render_template("public_profile.html", user=user, profile=profile)


@app.route("/profile-edit", methods=["GET", "POST"])
def profile_edit():
    """
    Combined single-page editor containing profile_step1 + profile_step2 fields.

    - GET: renders template pre-filled from session drafts or DB profile.
    - POST:
        * If profile exists and profile.published == True -> persist edits directly to DB
          (this is the "post-registration edit" path).
        * Otherwise -> save edits into session['profile_step1'] and session['profile_step2']
    Image uploads are normalized using Pillow and saved as .jpg to avoid .jfif issues.
    """
    if "user_id" not in session:
        flash("Please log in to continue.", "error")
        return redirect(url_for("login"))

    user = db.session.get(User, session["user_id"])
    profile = Profile.query.filter_by(user_id=user.id).first()

    # prepare current drafts / values for the form
    data1 = normalize_for_template(session.get("profile_step1", {}) or {})
    data2 = normalize_for_template(session.get("profile_step2", {}) or {})

    if request.method == "POST":
        # Handle profile photo upload (store filename in session draft like profile_step1)
        try:
            f = request.files.get("profile_photo")
            saved_fname = data1.get("profile_photo")
            if f and f.filename:
                if not allowed_file(f.filename):
                    flash("Profile photo must be an image (png/jpg/jpeg/jfif/gif/webp).", "error")
                    return redirect(url_for("profile_edit"))

                try:
                    # reset stream if necessary
                    if hasattr(f.stream, "seek"):
                        f.stream.seek(0)
                except Exception:
                    pass

                img = Image.open(f.stream)
                img.load()
                img = img.convert("RGB")
                MAX_DIM = (2500, 2500)
                img.thumbnail(MAX_DIM, Image.LANCZOS)

                fname_base = secure_filename(f"{session['user_id']}_edit_{int(time.time())}")
                fname = f"{fname_base}.jpg"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)

                # remove old file if we will overwrite and it exists and differs
                try:
                    if saved_fname and saved_fname != fname:
                        oldp = os.path.join(app.config['UPLOAD_FOLDER'], saved_fname)
                        if os.path.exists(oldp):
                            os.remove(oldp)
                except Exception:
                    app.logger.debug("Failed to remove previous temporary photo during edit")

                img.save(save_path, format="JPEG", quality=85, optimize=True)
                saved_fname = fname
        except UnidentifiedImageError:
            app.logger.exception("Uploaded file is not a recognizable image during profile-edit")
            flash("Uploaded file is not a valid image.", "error")
            return redirect(url_for("profile_edit"))
        except Exception:
            app.logger.exception("Failed to save uploaded profile photo during profile-edit")
            flash("Failed to save profile photo. Please try again.", "error")
            return redirect(url_for("profile_edit"))

        # ---------- the rest of your original logic ----------
        # (I will leave your existing field collection + persistence logic unchanged;
        #  below I paste your original code unchanged except for using saved_fname variable)
        # STEP1 fields (gather)
        bio = (request.form.get("bio") or "").strip()
        marital = (request.form.get("marital_status") or request.form.get("marital") or "").strip()
        age = (request.form.get("age") or request.form.get("age_years") or request.form.get("years_old") or "").strip()
        loc_city = (request.form.get("location_city") or "").strip()
        loc_country = (request.form.get("location_country") or "").strip()
        loc_combined = (request.form.get("location") or "").strip()
        if not loc_combined and loc_city and loc_country:
            loc_combined = f"{loc_city}/{loc_country}"
        languages_1 = (request.form.get("languages") or request.form.get("languages_1") or "").strip()

        # STEP2 fields (some variants collected for caregiver or family)
        if user and getattr(user, "role", "caregiver") == "caregiver":
            experience_years = (request.form.get("experience_years") or "").strip()
            specialties = (request.form.get("specialties") or "").strip()
            specialties_other = (request.form.get("specialties_other") or "").strip()
            certifications = (request.form.get("certifications") or "").strip()

            salary_amount = (request.form.get("salary_amount") or "").strip()
            salary_currency = (request.form.get("salary_currency") or "").strip()
            salary_frequency = (request.form.get("salary_frequency") or "").strip()
            salary_free = (request.form.get("salary") or request.form.get("salary_hidden") or "").strip()
            hourly_rate_legacy = (request.form.get("hourly_rate") or request.form.get("hourly_rate_hidden") or "").strip()

            # services: prefer radio/typed other or hidden 'services' canonical
            services_val = ""
            services_radio = request.form.get("services_radio")
            services_other = (request.form.get("services_other") or "").strip()
            if services_radio:
                if services_radio.lower() in ("other", "others"):
                    services_val = services_other or "Other"
                else:
                    services_val = services_radio
            else:
                services_val = (request.form.get("services") or "").strip() or None

            availability_days = request.form.getlist("availability_days") or []
            availability_joined = ",".join([d for d in availability_days if d and str(d).strip()]) if availability_days else None
            availability_start = (request.form.get("availability_start") or "").strip()
            availability_end = (request.form.get("availability_end") or "").strip()

            willing_to_travel = (request.form.get("willing_to_travel") or "").strip().lower()
            travel_radius_km = (request.form.get("travel_radius_km") or "").strip()

            preferred_age = (request.form.get("preferred_age") or "").strip()
            disability = (request.form.get("disability") or "").strip()
            disability_details = (request.form.get("disability_details") or "").strip()

            languages_2 = (request.form.get("languages") or request.form.get("languages_2") or "").strip()

            try:
                exp_int = int(experience_years) if experience_years != "" else None
            except Exception:
                exp_int = None
            try:
                tr_km = int(travel_radius_km) if travel_radius_km not in (None, "", False) else None
            except Exception:
                tr_km = None
        else:
            # Family fields
            care_needed_list = request.form.getlist("care_needed") or []
            care_needed_other = (request.form.get("care_needed_other") or "").strip()
            chosen = []
            for c in care_needed_list:
                if not c: continue
                tok = c.strip()
                if tok.lower() in ("other", "others"):
                    continue
                chosen.append(tok)
            if any((c and c.strip().lower() in ("other","others")) for c in care_needed_list):
                if care_needed_other:
                    chosen.append(care_needed_other)
                else:
                    chosen.append("Other")
            care_needed_joined = ",".join(chosen) if chosen else None

            number_of_dependents = (request.form.get("number_of_dependents") or "").strip()
            try:
                num_deps = int(number_of_dependents) if number_of_dependents != "" else None
            except Exception:
                num_deps = None

            preferred_caregiver_age = (request.form.get("preferred_caregiver_age") or "").strip()
            pref_schedule_days = request.form.getlist("preferred_schedule_days") or []
            pref_schedule_joined = ",".join([d for d in pref_schedule_days if d and str(d).strip()]) if pref_schedule_days else None
            pref_start = (request.form.get("preferred_schedule_start") or "").strip()
            pref_end = (request.form.get("preferred_schedule_end") or "").strip()

            hourly_budget = (request.form.get("hourly_budget") or "").strip()
            preferred_gender = (request.form.get("preferred_gender") or "").strip()
            household_info = (request.form.get("household_info") or "").strip()
            medical_needs = (request.form.get("medical_needs") or "").strip()
            languages_3 = (request.form.get("languages") or request.form.get("languages_3") or "").strip()

        # ---------- If profile exists and is already published -> persist edits directly to DB ----------
        if profile and getattr(profile, "published", False):
            try:
                # profile photo - remove old file if replaced
                if saved_fname:
                    # remove old file if different
                    try:
                        if profile.profile_photo and profile.profile_photo != saved_fname:
                            oldp = os.path.join(app.config['UPLOAD_FOLDER'], profile.profile_photo)
                            if os.path.exists(oldp):
                                os.remove(oldp)
                    except Exception:
                        app.logger.debug("Failed to remove old profile photo during edit persist")
                    profile.profile_photo = saved_fname

                # STEP1 -> persist
                profile.bio = bio or profile.bio
                profile.marital_status = marital or profile.marital_status
                try:
                    profile.age = int(age) if (age not in (None, "", False)) else profile.age
                except Exception:
                    # keep existing on parse failure
                    pass
                profile.location = (loc_combined or profile.location)
                # languages: prefer languages_2 (caregiver) or languages_3 (family) then languages_1
                if user and getattr(user, "role", "caregiver") == "caregiver":
                    profile.languages = languages_2 or languages_1 or profile.languages
                else:
                    profile.languages = languages_3 or languages_1 or profile.languages

                # STEP2 -> persist role-specific
                if user and getattr(user, "role", "caregiver") == "caregiver":
                    profile.experience_years = exp_int if exp_int not in (None, "", False) else profile.experience_years
                    profile.specialties = specialties or profile.specialties
                    profile.specialties_other = specialties_other or profile.specialties_other
                    profile.certifications = certifications or profile.certifications

                    # salary / legacy hourly_rate
                    profile.hourly_rate = (salary_free or hourly_rate_legacy) or profile.hourly_rate

                    try:
                        profile.travel_radius_km = tr_km if tr_km not in (None, "", False) else profile.travel_radius_km
                    except Exception:
                        pass

                    profile.willing_to_travel = (willing_to_travel or profile.willing_to_travel)
                    profile.background_check = _first_nonempty(request.form.get("background_check"), profile.background_check) or profile.background_check

                    profile.preferred_age = preferred_age or profile.preferred_age
                    profile.disability = disability or profile.disability
                    profile.disability_details = disability_details or profile.disability_details

                    # availability fields
                    if availability_joined:
                        profile.availability = availability_joined if (not profile.availability or availability_joined) else profile.availability
                    if availability_start and availability_end:
                        profile.availability = f"{availability_joined or ''}{(', ' if availability_joined else '')}{availability_start}--{availability_end}" if (availability_start and availability_end) else profile.availability

                    profile.services = services_val or profile.services
                    profile.services_other = services_other or profile.services_other

                else:
                    # family
                    profile.care_needed = care_needed_joined or profile.care_needed
                    profile.care_needed_other = care_needed_other or profile.care_needed_other
                    profile.preferred_schedule = None if not pref_schedule_joined else (pref_schedule_joined + (", " + pref_start + "--" + pref_end if pref_start and pref_end else ""))
                    profile.preferred_caregiver_age = preferred_caregiver_age or profile.preferred_caregiver_age
                    profile.number_of_dependents = num_deps if num_deps not in (None, "", False) else profile.number_of_dependents
                    profile.hourly_budget = hourly_budget or profile.hourly_budget
                    profile.preferred_gender = preferred_gender or profile.preferred_gender
                    profile.household_info = household_info or profile.household_info
                    profile.medical_needs = medical_needs or profile.medical_needs
                    profile.location = loc_combined or profile.location

                # normalization, humanize, commit, etc. (kept as in your original)
                try:
                    if user and getattr(user, "role", "caregiver") == "caregiver":
                        if getattr(profile, "specialties", None):
                            try:
                                humanized_spec = humanize_csv(profile.specialties)
                            except Exception:
                                humanized_spec = None
                            profile.specialties = humanized_spec if humanized_spec else profile.specialties
                        if getattr(profile, "services", None):
                            try:
                                humanized_svc = humanize_csv(profile.services)
                            except Exception:
                                humanized_svc = None
                            profile.services = humanized_svc if humanized_svc else profile.services
                    else:
                        if getattr(profile, "care_needed", None):
                            try:
                                humanized_cn = humanize_csv(profile.care_needed)
                            except Exception:
                                humanized_cn = None
                            profile.care_needed = humanized_cn if humanized_cn else profile.care_needed
                except Exception:
                    app.logger.exception("Humanize after edit persist failed")

                db.session.add(profile)
                db.session.commit()

                session.pop("profile_step1", None)
                session.pop("profile_step2", None)
                session.modified = True

                flash("Changes saved.", "success")
                return redirect(url_for("profile_setup", enable_actions='1', suppress_overlay='1'))

            except Exception as exc:
                app.logger.exception("Failed to persist profile edits (published profile): %s", exc)
                flash("Failed to save changes. Please try again.", "error")
                return redirect(url_for("profile_edit"))

        # ---------- If NOT published (still in registration/draft flow) ----------
        # Save drafts into session (existing behavior)
        session["profile_step1"] = {
            "profile_photo": saved_fname,
            "bio": bio or None,
            "marital_status": marital or None,
            "age": age or None,
            "location": loc_combined or None,
            "location_city": loc_city or None,
            "location_country": loc_country or None,
            "languages": languages_1 or None
        }

        if user and getattr(user, "role", "caregiver") == "caregiver":
            session["profile_step2"] = {
                "experience_years": exp_int,
                "specialties": specialties or None,
                "specialties_other": specialties_other or None,
                "certifications": certifications or None,
                "salary_amount": salary_amount or None,
                "salary_currency": salary_currency or None,
                "salary_frequency": salary_frequency or None,
                "salary": salary_free or hourly_rate_legacy or None,
                "hourly_rate": hourly_rate_legacy or None,
                "location": loc_combined or None,
                "location_city": loc_city or None,
                "location_country": loc_country or None,
                "availability_start": availability_start or None,
                "availability_end": availability_end or None,
                "availability_days": availability_joined,
                "willing_to_travel": willing_to_travel or None,
                "travel_radius_km": tr_km,
                "services": services_val or None,
                "services_other": services_other or None,
                "preferred_age": preferred_age or None,
                "disability": disability or None,
                "disability_details": disability_details or None,
                "languages": languages_2 or None
            }
        else:
            session["profile_step2"] = {
                "care_needed": care_needed_joined,
                "care_needed_other": care_needed_other or None,
                "number_of_dependents": num_deps,
                "preferred_caregiver_age": preferred_caregiver_age or None,
                "preferred_schedule": None if not pref_schedule_joined else (pref_schedule_joined + (", " + pref_start + "--" + pref_end if pref_start and pref_end else "")),
                "preferred_schedule_start": pref_start or None,
                "preferred_schedule_end": pref_end or None,
                "hourly_budget": hourly_budget or None,
                "preferred_gender": preferred_gender or None,
                "household_info": household_info or None,
                "medical_needs": medical_needs or None,
                "languages": languages_3 or None,
                "location": loc_combined or None,
                "location_city": loc_city or None,
                "location_country": loc_country or None
            }

        session.modified = True
        flash("Changes saved.", "success")
        return redirect(url_for("profile_setup"))

    # GET -> render page prefilled
    return render_template(
        "profile_edit.html",
        user=user,
        profile=profile,
        data1=normalize_for_template(session.get("profile_step1", {}) or {}),
        data2=normalize_for_template(session.get("profile_step2", {}) or {})
    )



# ---------- API: search for caregivers/families (real or placeholder) ----------
import random
import re
from flask import jsonify, request, url_for, session
from sqlalchemy import func

@app.route("/api/search")
def api_search():
    """
    q = query (zip / city / country)
    - If current user is logged-in AND has a published profile -> return REAL published opposite-role profiles that match q.
    - Otherwise -> return PLACEHOLDER accounts (3-5 total) with randomized realistic names and services; chat_url -> /register
    Response JSON:
      { "type": "real"|"placeholder"|"not_found", "results": [...], "message": "..." (optional) }
    """
    q = (request.args.get("q") or "").strip()
    q_normal = q.lower().strip()

    # -----------------------------
    # City / country lists (expanded)
    # -----------------------------
    usa_cities = [
        "new york","los angeles","chicago","houston","phoenix","philadelphia","san antonio","san diego","dallas","san jose","california",
        "austin","jacksonville","fort worth","columbus","san francisco","charlotte","indianapolis","seattle","denver","washington",
        "boston","el paso","nashville","detroit","oklahoma city","portland","las vegas","memphis","louisville","baltimore","new jersey",
        "milwaukee","albuquerque","tucson","fresno","sacramento","kansas city","mesa","atlanta","omaha","colorado springs","texas",
        "raleigh","long beach","virginia beach","miami","oakland","minneapolis","tulsa","wichita","new orleans","arlington",
        "cleveland","bakersfield","aurora","anaheim","honolulu","riverside","corpus christi","lexington","stockton","henderson"
    ]

    uk_cities = [
        "london","birmingham","glasgow","liverpool","bristol","manchester","sheffield","leeds","edinburgh","leicester",
        "coventry","bradford","cardiff","belfast","newcastle","southampton","swansea","plymouth","norwich","brighton"
    ]

    nigeria_cities = [
        "lagos","abuja","kano","ibadan","port harcourt","benin city","enugu","jos","ilorin","ogbomosho","abeokuta","rivers","edo","kogi","anambra","awka"
        "warri","akure","kaduna","sokoto","maiduguri","calabar","uyo","makurdi","ikeja","ondo","owerri","awka","delta","yenagoa","bayelsa","abia","imo",
        "umuahia","adamawa","yola","akwa ibom","bauchi","benue","borno","cross river","asaba","ebonyi","abakaliki","ekiti","Nasarawa","lafia","oyo","ogun", 
        "plateau","zamfara","yobe","damaturu","taraba","jalingo","osun","osogbo","ondo","abeokuta","niger","minna","kwara","kastina","kebbi","birnin kebbi"
    ]

    ghana_cities = [
        "accra","kumasi","tamale","sekondi","takoradi","tema","koforidua","sunyani","ho","wa"
    ]

    india_cities = [
        "mumbai","delhi","bangalore","hyderabad","ahmedabad","chennai","kolkata","pune","surat","jaipur",
        "lucknow","kanpur","nagpur","indore","thane","bhopal","visakhapatnam","kochi","vadodara"
    ]

    sa_cities = ["cape town","johannesburg","durban","pretoria","port elizabeth"]
    brazil_cities = ["sao paulo","rio de janeiro","brasilia","salvador","fortaleza"]
    china_cities = ["beijing","shanghai","guangzhou","shenzhen","chengdu","chongqing"]
    europe_cities = [
        "madrid","barcelona","paris","lyon","marseille","amsterdam","rotterdam","brussels","lisbon","rome","milan",
        "berlin","munich","hamburg","vienna","budapest","prague","warsaw","stockholm","copenhagen","oslo","helsinki"
    ]
    russia_cities = ["moscow","st petersburg","novosibirsk","yekaterinburg","kazan"]

    # Representative country lists (lowercase strings)
    europe_countries = [
        "albania","andorra","armenia","austria","azerbaijan","belarus","belgium","bosnia and herzegovina","bulgaria",
        "croatia","cyprus","czech republic","denmark","estonia","finland","france","georgia","germany","greece",
        "hungary","iceland","ireland","italy","kazakhstan","latvia","liechtenstein","lithuania","luxembourg",
        "malta","moldova","monaco","montenegro","netherlands","north macedonia","norway","poland","portugal","romania",
        "russia","san marino","serbia","slovakia","slovenia","spain","sweden","switzerland","turkey","ukraine","united kingdom"
    ]

    africa_countries = [
        "algeria","angola","benin","botswana","burkina faso","burundi","cabo verde","cameroon","central african republic",
        "chad","comoros","congo","democratic republic of the congo","djibouti","egypt","equatorial guinea","eritrea","eswatini",
        "ethiopia","gabon","gambia","ghana","guinea","guinea-bissau","ivory coast","kenya","lesotho","liberia","libya",
        "madagascar","malawi","mali","mauritania","mauritius","morocco","mozambique","namibia","niger","nigeria","rwanda",
        "sao tome and principe","senegal","seychelles","sierra leone","somalia","south africa","south sudan","sudan","tanzania",
        "togo","tunisia","uganda","zambia","zimbabwe"
    ]

    # common_cities used for prefix completion & detection
    common_cities = []
    for lst in (usa_cities, uk_cities, nigeria_cities, ghana_cities, india_cities,
                sa_cities, brazil_cities, china_cities, europe_cities, russia_cities):
        common_cities.extend(lst)

    # Precompute lowercase sets for fast membership checks
    USA_SET = set([c.lower() for c in usa_cities])
    UK_SET = set([c.lower() for c in uk_cities])
    NIGERIA_SET = set([c.lower() for c in nigeria_cities])
    GHANA_SET = set([c.lower() for c in ghana_cities])
    INDIA_SET = set([c.lower() for c in india_cities])
    SA_SET = set([c.lower() for c in sa_cities])
    BRAZIL_SET = set([c.lower() for c in brazil_cities])
    CHINA_SET = set([c.lower() for c in china_cities])
    EUROPE_SET = set([c.lower() for c in europe_cities])
    RUSSIA_SET = set([c.lower() for c in russia_cities])
    EUROPE_COUNTRY_SET = set([c.lower() for c in europe_countries])
    AFRICA_COUNTRY_SET = set([c.lower() for c in africa_countries])

    # US state abbreviations (lowercase) to trim "City, NY" style
    US_STATE_ABBR = {
        "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","ia","id","il","in","ks","ky","la","ma","md",
        "me","mi","mn","mo","ms","mt","nc","nd","ne","nh","nj","nm","nv","ny","oh","ok","or","pa","ri","sc",
        "sd","tn","tx","ut","va","vt","wa","wi","wv","wy","dc"
    }

    def best_city_completion(query):
        if not query:
            return None
        qn = query.lower().replace(" ", "")
        for c in common_cities:
            if c.replace(" ", "").startswith(qn):
                return c
        return None

    # helper to build JSON from Profile row (real results)
    def result_from_profile(profile):
        try:
            u = getattr(profile, "user", None)
            if u:
                name = f"{(getattr(u,'first_name') or '').strip()} {(getattr(u,'last_name') or '').strip()}".strip()
                name = name if name else getattr(u, "email", f"user{u.id}")
                role = (getattr(u, "role", "") or "").lower()
            else:
                name = f"User {profile.user_id}"
                role = ""
            avatar = None
            if getattr(profile, "profile_photo", None):
                try:
                    avatar = url_for("uploaded_file", filename=getattr(profile, "profile_photo"))
                except Exception:
                    avatar = None
            services = getattr(profile, "services", "") or ""
            # For real/published profiles returned here, link chat to the chat_page route
            try:
                chat_url = url_for("chat_page", other_id=profile.user_id)
            except Exception:
                chat_url = f"/chat/{profile.user_id}"
            return {
                "id": profile.user_id,
                "name": name,
                "role": role,
                "location": getattr(profile, "location", "") or "",
                "services": services,
                "avatar_url": avatar,
                "profile_url": url_for("view_public_profile", user_id=profile.user_id),
                "chat_url": chat_url
            }
        except Exception:
            return None

    # detect current user published state
    current_uid = session.get("user_id")
    current_role = None
    current_published = False
    if current_uid:
        try:
            cu = db.session.get(User, current_uid)
            if cu:
                current_role = (getattr(cu, "role", "") or "").lower()
                prof = Profile.query.filter_by(user_id=cu.id).first()
                current_published = bool(prof and getattr(prof, "published", False))
        except Exception:
            current_role = None
            current_published = False

    # If searcher is a published user -> return real published opposite-role profiles that match location substring
    if current_published and current_role in ("caregiver", "family"):
        target_role = "family" if current_role == "caregiver" else "caregiver"
        try:
            qry = Profile.query.join(User).filter(User.role == target_role, Profile.published == True)
            if q_normal:
                qry = qry.filter(func.lower(Profile.location).like(f"%{q_normal}%"))
            rows = qry.limit(200).all()
            results = []
            for p in rows:
                r = result_from_profile(p)
                if r:
                    results.append(r)
            return jsonify({"type": "real", "results": results})
        except Exception:
            app.logger.exception("api_search (real) failed")
            return jsonify({"type":"real", "results": []})

    # ----------------------------
    # Otherwise produce placeholders (strict: names must match searched region)
    # ----------------------------

    def make_placeholder(name, role, location, services, avatar=None):
        return {
            "id": None,
            "name": name,
            "role": role,
            "location": location,
            "services": services,
            "avatar_url": avatar,
            "profile_url": None,
            "chat_url": url_for("register")
        }

    # ----------------------------
    # region-aware name pools (enlarged)
    # ----------------------------
    us_names = [
        ("James","Smith"),("Mary","Johnson"),("John","Williams"),("Patricia","Brown"),("Robert","Jones"),
        ("Linda","Garcia"),("Michael","Miller"),("Barbara","Davis"),("William","Rodriguez"),("Elizabeth","Martinez"),
        ("David","Hernandez"),("Jennifer","Lopez"),("Richard","Gonzalez"),("Maria","Wilson"),("Charles","Anderson"),
        ("Susan","Thomas"),("Joseph","Taylor"),("Margaret","Moore"),("Thomas","Jackson"),("Dorothy","Martin"),
        ("Christopher","Lee"),("Sarah","Perez"),("Daniel","Thompson"),("Karen","White"),("Matthew","Harris")
    ]

    uk_names = [
        ("Oliver","Smith"),("Olivia","Jones"),("Harry","Brown"),("Emily","Taylor"),("Jack","Wilson"),
        ("Amelia","Evans"),("George","Thomas"),("Isla","Roberts"),("Noah","Johnson"),("Mia","Walker")
    ]

    nigeria_names = [
        ("Chinedu","Okonkwo"),("Aisha","Bello"),("Emeka","Ibe"),("Ngozi","Ude"),("Ibrahim","Musa"),
        ("Tunde","Akinola"),("Ada","Nwankwo"),("Ifeoma","Nwankwo"),("Oluwakemi","Ojo"),("Chima","Eze"),
        ("Amaka","Nwosu"),("Michael","Ade"),("Obinna","Eze"),("Sade","Adewale"),("Femi","Ogun")
    ]

    ghana_names = [
        ("Kofi","Boateng"),("Abena","Mensah"),("Yaw","Owusu"),("Akosua","Acheampong"),("Nana","Ofori")
    ]

    india_names = [
        ("Amit","Kumar"),("Priya","Sharma"),("Rahul","Patel"),("Sneha","Reddy"),("Vikram","Singh"),
        ("Ananya","Gupta"),("Arjun","Khan"),("Sanjay","Mehta"),("Neha","Shah"),("Ravi","Nair")
    ]

    sa_names = [("Sipho","Nkosi"),("Thandi","Mbeki"),("Lunga","Dlamini")]
    brazil_names = [("João","Silva"),("Maria","Santos"),("Lucas","Souza")]
    china_names = [("Wei","Chen"),("Li","Wang"),("Xiao","Zhang")]
    spain_names = [("Juan","Garcia"),("Sofia","Martinez"),("Carlos","Lopez")]
    italy_names = [("Luca","Rossi"),("Giulia","Ferrari"),("Marco","Bianchi")]
    russia_names = [("Ivan","Ivanov"),("Anastasia","Petrova"),("Dmitri","Kuznetsov")]

    person_first_last = [
        ("Grace","Johnson"),("Fatima","Yusuf"),("Chen","Wei"),("Maria","Silva"),("Luca","Rossi"),
        ("Pedro","Santos"),("Amit","Kumar"),("Kofi","Boateng"),("Ada","Nwankwo"),("Anna","Ivanova")
    ]

    # ----------------------------
    # Helper: detect specific region or country for the query.
    # Returns a dict with keys: { "region": <code>, "label": <friendly label> }
    # region codes: 'usa','uk','nigeria','ghana','india','sa','brazil','china','spain','italy','russia','europe','africa', None
    # ----------------------------
    def detect_region_and_label(qtext):
        ql = (qtext or "").lower().strip()
        if not ql:
            return None
        # US 5-digit ZIP
        if re.fullmatch(r"\d{5}", ql):
            return {"region": "usa", "label": ql}
        # remove punctuation and trailing state abbr like ", ny" or "ny"
        cleaned = re.sub(r'[^a-z0-9\s]', ' ', ql)
        toks = [t for t in cleaned.split() if t]
        # drop trailing US state abbreviation (e.g. "portland or")
        if toks and toks[-1] in US_STATE_ABBR:
            toks = toks[:-1]
        # rejoin candidate strings (try 3,2,1 token sequences)
        for size in (3,2,1):
            if len(toks) < size:
                continue
            for i in range(len(toks)-size+1):
                cand = " ".join(toks[i:i+size])
                if cand in USA_SET:
                    return {"region": "usa", "label": cand.title()}
                if cand in NIGERIA_SET:
                    return {"region": "nigeria", "label": cand.title()}
                if cand in UK_SET:
                    return {"region": "uk", "label": cand.title()}
                if cand in GHANA_SET:
                    return {"region": "ghana", "label": cand.title()}
                if cand in INDIA_SET:
                    return {"region": "india", "label": cand.title()}
                if cand in SA_SET:
                    return {"region": "sa", "label": cand.title()}
                if cand in BRAZIL_SET:
                    return {"region": "brazil", "label": cand.title()}
                if cand in CHINA_SET:
                    return {"region": "china", "label": cand.title()}
                if cand in EUROPE_SET:
                    # if it's a known european city, try mapping to country-specific region codes for better names
                    if cand in {"madrid","barcelona","valencia","sevilla","zaragoza"}:
                        return {"region": "spain", "label": cand.title()}
                    if cand in {"rome","milan","naples","turin","palermo"}:
                        return {"region": "italy", "label": cand.title()}
                    return {"region": "europe", "label": cand.title()}
                if cand in RUSSIA_SET:
                    return {"region": "russia", "label": cand.title()}
        # exact country matches
        if ql in EUROPE_COUNTRY_SET:
            # map certain country names to more specific name pools where possible
            if "spain" in ql:
                return {"region": "spain", "label": "Spain"}
            if "italy" in ql:
                return {"region": "italy", "label": "Italy"}
            if "russia" in ql:
                return {"region": "russia", "label": "Russia"}
            if "united kingdom" in ql or ql == "uk":
                return {"region": "uk", "label": "United Kingdom"}
            return {"region": "europe", "label": ql.title()}
        if ql in AFRICA_COUNTRY_SET:
            if "nigeria" in ql:
                return {"region": "nigeria", "label": "Nigeria"}
            if "ghana" in ql:
                return {"region": "ghana", "label": "Ghana"}
            if "south africa" in ql or "southafrica" in ql:
                return {"region": "sa", "label": "South Africa"}
            return {"region": "africa", "label": ql.title()}
        # prefix completion: if the query partially matches a known city, use that (helps "new" -> "New York")
        completion = best_city_completion(qtext)
        if completion:
            # reuse earlier matching logic by calling recursively with completion
            return detect_region_and_label(completion)
        # nothing matched -> unknown region
        return None

    # Validate the query: if non-empty and not recognized, return not_found
    if q:
        region_info = detect_region_and_label(q)
        if not region_info:
            return jsonify({
                "type": "not_found",
                "results": [],
                "message": "No matching city, country, or ZIP found."
            })
    else:
        region_info = None  # empty query -> allow mixed placeholders (or you can choose default behavior)

    # ----------------------------
    # Helper to pick a pool based on region code
    # ----------------------------
    def pool_for_region(region_code):
        if region_code == "usa":
            return us_names
        if region_code == "uk":
            return uk_names
        if region_code == "nigeria":
            return nigeria_names
        if region_code == "ghana":
            return ghana_names
        if region_code == "india":
            return india_names
        if region_code == "sa":
            return sa_names
        if region_code == "brazil":
            return brazil_names
        if region_code == "china":
            return china_names
        if region_code == "spain":
            return spain_names
        if region_code == "italy":
            return italy_names
        if region_code == "russia":
            return russia_names
        if region_code == "europe":
            # use a mixed european-ish pool
            return spain_names + italy_names + person_first_last
        if region_code == "africa":
            return nigeria_names + ghana_names + person_first_last
        # fallback
        return person_first_last

    # ----------------------------
    # Unique-name generator per-request (ensures no duplicates caregiver vs family)
    # ----------------------------
    def get_unique_name(region_code, used_set):
        pool = list(pool_for_region(region_code))
        random.shuffle(pool)
        for fn, ln in pool:
            fullname = f"{fn} {ln}"
            if fullname not in used_set:
                used_set.add(fullname)
                return fn, ln
        # fallback to global pool and then synthesize unique suffix if needed
        random.shuffle(person_first_last)
        for fn, ln in person_first_last:
            fullname = f"{fn} {ln}"
            if fullname not in used_set:
                used_set.add(fullname)
                return fn, ln
        # as last resort generate a unique synthetic name
        base_fn, base_ln = random.choice(person_first_last)
        idx = 1
        while True:
            candidate = f"{base_fn}{idx} {base_ln}"
            if candidate not in used_set:
                used_set.add(candidate)
                return base_fn + str(idx), base_ln
            idx += 1

    # ----------------------------
    # Build placeholders constrained to detected region
    # ----------------------------
    example_caregiver_services = ["Light housekeeping", "Personal care", "Medication reminders", "Companionship", "Meal prep"]
    example_family_needs = ["Elder care", "Post-op care", "Childcare", "Special needs support", "Hourly care"]

    # Decide how many placeholders (5-10) but force an even total so caregivers == families
    min_total = 5
    max_total = 10
    even_choices = [n for n in range(min_total, max_total + 1) if n % 2 == 0]
    if not even_choices:
        # fallback (shouldn't happen with usual ranges)
        total_needed = max(min_total, 2)
    else:
        total_needed = random.choice(even_choices)

    cg_count = fam_count = total_needed // 2

    placeholders = []
    used_names = set()

    # friendly location string for returned placeholders
    if region_info:
        friendly_loc = region_info["label"]
        region_code = region_info["region"]
    else:
        # empty query -> generic friendly string and mixed region
        friendly_loc = "your area"
        region_code = None

    # If query is empty: allow mixed placeholders (but still avoid duplicates)
    if not q:
        # choose region for each placeholder randomly but ensure names come from that region
        for i in range(cg_count):
            # pick a random region code to bias name selection
            region_choice = random.choice(["usa","uk","nigeria","ghana","india","sa","brazil","china","spain","italy","russia"])
            fn, ln = get_unique_name(region_choice, used_names)
            name = f"{fn} {ln}"
            services = ", ".join(random.sample(example_caregiver_services, k=min(3, len(example_caregiver_services))))
            placeholders.append(make_placeholder(name, "caregiver", friendly_loc, services))
        for i in range(fam_count):
            region_choice = random.choice(["usa","uk","nigeria","ghana","india","sa","brazil","china","spain","italy","russia"])
            fn, ln = get_unique_name(region_choice, used_names)
            name = f"{fn} {ln}"
            needs = ", ".join(random.sample(example_family_needs, k=min(3, len(example_family_needs))))
            placeholders.append(make_placeholder(name, "family", friendly_loc, needs))
    else:
        # q present and validated -> produce placeholders only from that detected region
        for i in range(cg_count):
            fn, ln = get_unique_name(region_code, used_names)
            name = f"{fn} {ln}"
            services = ", ".join(random.sample(example_caregiver_services, k=min(3, len(example_caregiver_services))))
            placeholders.append(make_placeholder(name, "caregiver", friendly_loc, services))
        for i in range(fam_count):
            fn, ln = get_unique_name(region_code, used_names)
            name = f"{fn} {ln}"
            needs = ", ".join(random.sample(example_family_needs, k=min(3, len(example_family_needs))))
            placeholders.append(make_placeholder(name, "family", friendly_loc, needs))

    random.shuffle(placeholders)
    return jsonify({"type": "placeholder", "results": placeholders})






@app.route('/privacy')
@app.route('/privacy.html')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
@app.route('/terms.html')
def terms():
    return render_template('terms.html')

@app.route('/support')
@app.route('/support.html')
def support():
    return render_template('support.html')



# ------------------ ADMIN SECTION (replace this block) ------------------

# Hard-coded admin password (change this string in app.py to update)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

def admin_required():
    # simple guard: abort with 403 if not admin session
    if not session.get("is_admin"):
        abort(403)

# Use a unique uploads endpoint for admin templates to avoid collisions with app.py's uploads route
# URL path: /admin_uploads/<filename>
@app.route("/admin_uploads/<path:filename>")
def admin_serve_uploads(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_dashboard"))
        else:
            flash("Wrong admin password.", "danger")
            return redirect(url_for("admin_login"))
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    # safer redirect target: admin login page (change to your preferred landing page)
    session.pop("is_admin", None)
    flash("Logged out of admin.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin_dashboard():
    admin_required()
    # If your User model has an is_deleted field, exclude deleted users.
    q = db.session.query(User).join(Profile).filter(Profile.published == True)
    if hasattr(User, "is_deleted"):
        q = q.filter(getattr(User, "is_deleted") == False)
    users = q.order_by(User.id.desc()).all()
    return render_template("admin.html", users=users)


@app.route("/admin/view_user/<int:user_id>")
def admin_view_user(user_id):
    admin_required()
    u = User.query.get_or_404(user_id)
    return render_template("admin_view_user.html", user=u)


@app.route("/admin/delete_user/<int:user_id>", methods=["POST"])
def admin_delete_user(user_id):
    """
    Soft-delete if User has attribute 'is_deleted' -> set is_deleted=True and is_active=False.
    If the model does not have is_deleted, fallback to permanent delete (original behavior).
    Accepts JSON body: { "confirm": true }
    """
    admin_required()
    payload = request.get_json(silent=True) or {}
    if not payload.get("confirm"):
        return jsonify({"status": "error", "message": "Missing confirmation"}), 400

    u = User.query.get_or_404(user_id)

    # Prevent deleting self: only if there's a logged-in user via Flask-Login
    try:
        if current_user is not None and getattr(current_user, "is_authenticated", False):
            # current_user.id might be str or int depending on your setup; compare as ints when possible
            try:
                cur_id = int(current_user.get_id())
            except Exception:
                cur_id = getattr(current_user, "id", None)
            if cur_id is not None and cur_id == u.id:
                return jsonify({"status": "error", "message": "Cannot delete yourself"}), 400
    except Exception:
        # if current_user import or attributes fail, ignore and continue (admins managed by session only)
        pass

    # If soft-delete supported on model, use it
    if hasattr(u, "is_deleted"):
        try:
            # mark deleted / deactivate account
            setattr(u, "is_deleted", True)
            if hasattr(u, "is_active"):
                setattr(u, "is_active", False)
            db.session.add(u)
            db.session.commit()
            return jsonify({"status": "ok", "message": "User soft-deleted", "user_id": u.id}), 200
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to soft-delete user %s", user_id)
            return jsonify({"status": "error", "message": "Failed to soft-delete user"}), 500

    # fallback: permanent delete (remove files then DB row)
    try:
        if getattr(u, "profile", None):
            p = u.profile
            if getattr(p, "profile_photo", None):
                try:
                    path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(p.profile_photo))
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    app.logger.exception("Failed to remove profile photo for user %s", user_id)

            try:
                if getattr(p, "other_photos", None):
                    photos = json.loads(p.other_photos)
                    if isinstance(photos, list):
                        for fn in photos:
                            try:
                                ppath = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(fn))
                                if os.path.exists(ppath):
                                    os.remove(ppath)
                            except Exception:
                                app.logger.exception("Failed to remove other photo %s for %s", fn, user_id)
            except Exception:
                app.logger.exception("Failed to parse other_photos for user %s", user_id)

            try:
                db.session.delete(p)
            except Exception:
                app.logger.exception("Failed to delete Profile row for user %s", user_id)

        db.session.delete(u)
        db.session.commit()
        return jsonify({"status": "ok", "message": "User permanently deleted", "user_id": user_id}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to delete user %s", user_id)
        return jsonify({"status": "error", "message": "Failed to delete user"}), 500


@app.route("/admin/restore_user/<int:user_id>", methods=["POST"])
def admin_restore_user(user_id):
    """
    Restore a soft-deleted user. Requires model to have is_deleted flag.
    """
    admin_required()
    payload = request.get_json(silent=True) or {}
    if not payload.get("confirm"):
        return jsonify({"status": "error", "message": "Missing confirmation"}), 400

    u = User.query.get_or_404(user_id)
    if not hasattr(u, "is_deleted"):
        return jsonify({"status": "error", "message": "Restore not supported by model"}), 400

    try:
        setattr(u, "is_deleted", False)
        if hasattr(u, "is_active"):
            setattr(u, "is_active", True)
        db.session.add(u)
        db.session.commit()
        return jsonify({"status": "ok", "message": "User restored", "user_id": u.id}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to restore user %s", user_id)
        return jsonify({"status": "error", "message": "Failed to restore user"}), 500


@app.route("/admin/permanent_delete/<int:user_id>", methods=["POST"])
def admin_permanent_delete_user(user_id):
    """
    Hard delete the user and associated profile rows & files.
    """
    admin_required()
    payload = request.get_json(silent=True) or {}
    if not payload.get("confirm"):
        return jsonify({"status": "error", "message": "Missing confirmation"}), 400

    u = User.query.get_or_404(user_id)

    # only block if current_user is authenticated and trying to delete self
    try:
        if current_user is not None and getattr(current_user, "is_authenticated", False):
            try:
                cur_id = int(current_user.get_id())
            except Exception:
                cur_id = getattr(current_user, "id", None)
            if cur_id is not None and cur_id == u.id:
                return jsonify({"status": "error", "message": "Cannot delete yourself"}), 400
    except Exception:
        pass

    try:
        if getattr(u, "profile", None):
            p = u.profile
            if getattr(p, "profile_photo", None):
                try:
                    path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(p.profile_photo))
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    app.logger.exception("Failed to remove profile photo for user %s", user_id)

            try:
                if getattr(p, "other_photos", None):
                    photos = json.loads(p.other_photos)
                    if isinstance(photos, list):
                        for fn in photos:
                            try:
                                ppath = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(fn))
                                if os.path.exists(ppath):
                                    os.remove(ppath)
                            except Exception:
                                app.logger.exception("Failed to remove other photo %s for %s", fn, user_id)
            except Exception:
                app.logger.exception("Failed to parse other_photos for user %s", user_id)

            try:
                db.session.delete(p)
            except Exception:
                app.logger.exception("Failed to delete Profile row for user %s", user_id)

        db.session.delete(u)
        db.session.commit()
        return jsonify({"status": "ok", "message": "User permanently deleted", "user_id": user_id}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to permanently delete user %s", user_id)
        return jsonify({"status": "error", "message": "Failed to permanently delete user"}), 500


# Admin - show soft-deleted users list
@app.route("/admin/deleted_users")
def admin_deleted_users():
    admin_required()
    # Use outerjoin so we include users even if they have no Profile row
    users = (
        db.session.query(User)
        .outerjoin(Profile)
        .filter(getattr(User, "is_deleted", False) == True)
        .order_by(User.id.desc())
        .all()
    )
    return render_template("admin_deleted_users.html", users=users)


# Keep your pre-existing admin_restore_user and admin_permanent_delete_user routes (they already exist).
# If you want, make sure their endpoint names are admin_restore_user and admin_permanent_delete_user
# (the templates use those names).

# ------------------ END ADMIN SECTION ------------------


# ------------------ NOTIFICATIONS MODULE (standalone) ------------------
# Copy this whole block into app.py after your admin section (do not merge with other routes).

from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import threading

# Notification models
class Notification(db.Model):
    __tablename__ = "notification"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    sender = Column(String(120), nullable=True)         # e.g. 'admin'
    target = Column(String(120), nullable=True)         # 'all', 'caregiver', 'family', 'single:<id_or_email>'
    delivery_email = Column(Boolean, nullable=False, default=False)
    delivery_inapp = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # convenience relationship (not strictly required)
    recipients = relationship("NotificationRecipient", backref="notification", cascade="all, delete-orphan")


class NotificationRecipient(db.Model):
    __tablename__ = "notification_recipient"
    id = Column(Integer, primary_key=True)
    notification_id = Column(Integer, ForeignKey("notification.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    read_at = Column(DateTime, nullable=True)




# Helper to resolve recipients based on `target` argument
def _resolve_recipient_user_ids(target, single_user_ident=None):
    """
    target: 'all' | 'caregiver' | 'family' | 'single'
    single_user_ident: when target == 'single', can be user_id (int-like) or email string
    Returns list of user ids (ints).
    """
    try:
        qs = db.session.query(User).filter(getattr(User, "is_deleted", False) == False)
    except Exception:
        qs = db.session.query(User)

    if not target or target == "all":
        rows = qs.all()
        return [u.id for u in rows if getattr(u, "id", None) is not None]

    if target == "caregiver":
        rows = qs.filter(User.role == "caregiver").all()
        return [u.id for u in rows]

    if target == "family":
        rows = qs.filter(User.role == "family").all()
        return [u.id for u in rows]

    if target == "single":
        if not single_user_ident:
            return []
        # try interpret as int id first
        try:
            uid = int(single_user_ident)
            u = db.session.get(User, uid)
            return [u.id] if u else []
        except Exception:
            # try email
            try:
                u = db.session.query(User).filter(func.lower(User.email) == str(single_user_ident).strip().lower()).first()
                return [u.id] if u else []
            except Exception:
                return []

    return []


class PushSubscription(db.Model):
    __tablename__ = "push_subscription"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    subscription_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrowserPushSubscription(db.Model):
    __tablename__ = "browser_push_subscription"
    id = db.Column(db.Integer, primary_key=True)
    browser_key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    subscription_json = db.Column(db.Text, nullable=False)
    linked_user_ids_json = db.Column(db.Text, nullable=False, default="[]")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)






from cryptography.hazmat.primitives.serialization import load_pem_private_key

def _load_or_create_vapid_keys():
    priv_path = os.path.join(data_dir, "vapid_private.pem")
    pub_path = os.path.join(data_dir, "vapid_public.key")
    subject = os.environ.get("VAPID_SUBJECT", "mailto:carecompanion@zohomail.com")

    def _generate_and_save():
        key = ec.generate_private_key(ec.SECP256R1())

        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()
        )

        public_bytes = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint
        )
        public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")

        with open(priv_path, "wb") as f:
            f.write(private_pem)

        with open(pub_path, "w", encoding="utf-8") as f:
            f.write(public_key)

        return priv_path, public_key, subject

    # Try to load and validate existing key first
    try:
        if os.path.exists(priv_path) and os.path.exists(pub_path):
            with open(priv_path, "rb") as f:
                private_pem_bytes = f.read()

            # validate PEM before using it
            load_pem_private_key(private_pem_bytes, password=None)

            with open(pub_path, "r", encoding="utf-8") as f:
                public_key = f.read().strip()

            if public_key:
                return priv_path, public_key, subject
    except Exception as exc:
        app.logger.warning("Invalid VAPID key files found, regenerating: %s", exc)
        try:
            if os.path.exists(priv_path):
                os.remove(priv_path)
        except Exception:
            pass
        try:
            if os.path.exists(pub_path):
                os.remove(pub_path)
        except Exception:
            pass

    return _generate_and_save()


VAPID_PRIVATE_KEY_PATH, VAPID_PUBLIC_KEY, VAPID_SUBJECT = _load_or_create_vapid_keys()
app.config["VAPID_PRIVATE_KEY_PATH"] = VAPID_PRIVATE_KEY_PATH
app.config["VAPID_PUBLIC_KEY"] = VAPID_PUBLIC_KEY
app.config["VAPID_SUBJECT"] = VAPID_SUBJECT


def _get_user_avatar_url(user_id):
    """
    Return a usable avatar URL for notifications.
    Falls back to the default avatar if the user has no profile photo.
    """
    try:
        u = db.session.get(User, int(user_id))
        if u:
            prof = getattr(u, "profile", None)
            if prof and getattr(prof, "profile_photo", None):
                try:
                    return url_for("uploaded_file", filename=prof.profile_photo, _external=False)
                except Exception:
                    pass
    except Exception:
        pass

    return url_for("static", filename="default-avatar.png", _external=False)


# ----------------- real-time chat + push notifications (replace this whole block) -----------------

# Track currently connected users so we do not send both socket popup + web push to the same online user
ONLINE_USER_SOCKET_COUNTS = {}
ONLINE_USER_LOCK = threading.Lock()

def _set_user_online(user_id):
    try:
        uid = int(user_id)
    except Exception:
        return
    with ONLINE_USER_LOCK:
        ONLINE_USER_SOCKET_COUNTS[uid] = ONLINE_USER_SOCKET_COUNTS.get(uid, 0) + 1

def _set_user_offline(user_id):
    try:
        uid = int(user_id)
    except Exception:
        return
    with ONLINE_USER_LOCK:
        if uid not in ONLINE_USER_SOCKET_COUNTS:
            return
        ONLINE_USER_SOCKET_COUNTS[uid] = max(0, ONLINE_USER_SOCKET_COUNTS.get(uid, 0) - 1)
        if ONLINE_USER_SOCKET_COUNTS[uid] <= 0:
            ONLINE_USER_SOCKET_COUNTS.pop(uid, None)

def _user_is_online(user_id):
    try:
        uid = int(user_id)
    except Exception:
        return False
    with ONLINE_USER_LOCK:
        return ONLINE_USER_SOCKET_COUNTS.get(uid, 0) > 0


def maybe_prompt_push_for_user(user_id):
    """
    Show the push-permission prompt only once per user,
    and only after their first successful message send.
    """
    try:
        user = db.session.get(User, int(user_id))
        if not user:
            return False

        if getattr(user, "push_prompt_shown_at", None) is not None:
            return False

        user.push_prompt_shown_at = datetime.utcnow()
        db.session.add(user)
        db.session.commit()

        socketio.emit(
            "show_push_prompt",
            {"user_id": int(user.id)},
            room=f"user_{int(user.id)}"
        )
        return True

    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.exception("maybe_prompt_push_for_user failed")
        return False


def _load_json_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


def notify_chat_recipient(sender_id, recipient_id, conversation_id, message_obj):
    """
    Send:
      1) a live Socket.IO event to the recipient's open page
      2) a Web Push notification to all subscribed devices/browsers for that recipient
    """

    app.logger.warning(
        "DEBUG: notify_chat_recipient() called | sender=%s recipient=%s conversation=%s",
        sender_id,
        recipient_id,
        conversation_id,
    )

    sender = db.session.get(User, int(sender_id))
    recipient = db.session.get(User, int(recipient_id))

    sender_name = (
        f"{getattr(sender, 'first_name', '')} {getattr(sender, 'last_name', '')}".strip()
        if sender else "Someone"
    )

    sender_avatar = _get_user_avatar_url(sender_id)
    recipient_email = (getattr(recipient, "email", None) or "").strip()

    text = (message_obj.get("text") or "").strip()
    if not text:
        text = "Photo" if message_obj.get("attachments") else "New message"

    title = f"New message from {sender_name}"
    preview = text[:120]

    chat_url = url_for("chat_page", other_id=int(sender_id), _external=False)

    push_payload = {
        "title": title,
        "body": preview,
        "url": chat_url,
        "conversation_id": conversation_id,
        "sender_id": int(sender_id),
        "recipient_id": int(recipient_id),
        "recipient_email": recipient_email,
        "message_id": message_obj.get("id"),
        "tag": f"chat_{conversation_id}_{message_obj.get('id')}",
        "requireInteraction": True,
        "avatar_url": sender_avatar,
        "icon": sender_avatar,
        "badge": sender_avatar,
    }

    try:
        socketio.emit(
            "message_received",
            {
                "user_id": int(recipient_id),
                "sender_id": int(sender_id),
                "recipient_id": int(recipient_id),
                "recipient_email": recipient_email,
                "conversation_id": conversation_id,
                "message_id": message_obj.get("id"),
                "title": title,
                "message": preview,
                "url": chat_url,
                "avatar_url": sender_avatar,
            },
            room=f"user_{int(recipient_id)}",
        )

        app.logger.warning(
            "DEBUG: Socket.IO event emitted successfully."
        )

    except Exception:
        app.logger.exception("Failed to emit message_received event")

    app.logger.warning(
        "DEBUG: About to call _send_push_to_user()"
    )

    try:
        result = _send_push_to_user(recipient_id, push_payload)

        app.logger.warning(
            "DEBUG: _send_push_to_user() returned: %s",
            result,
        )

    except Exception:
        app.logger.exception("Failed to send push chat notification")

def _send_push_to_user(user_id, payload):
    """
    Send a Web Push notification to every saved subscription for one user.

    Supports:
      - PushSubscription rows tied directly to user_id
      - BrowserPushSubscription rows linked to that user_id
    """

    app.logger.warning(
        "DEBUG: _send_push_to_user() STARTED for user=%s",
        user_id,
    )

    if webpush is None:
        app.logger.warning("DEBUG: webpush is None!")
        raise RuntimeError("pywebpush is not installed or could not be imported.")

    uid = int(user_id)
    payload = dict(payload or {})

    subs = []

    try:
        subs.extend(PushSubscription.query.filter_by(user_id=uid).all())
    except Exception:
        app.logger.exception("DEBUG: Failed reading PushSubscription table")

    try:
        browser_rows = BrowserPushSubscription.query.all()

        for row in browser_rows:
            linked_ids = _load_json_list(row.linked_user_ids_json)
            linked_ids = [int(x) for x in linked_ids if str(x).strip() != ""]

            if uid in linked_ids:
                subs.append(row)

    except Exception:
        app.logger.exception("DEBUG: Failed reading BrowserPushSubscription table")

    seen_endpoints = set()
    unique_subs = []

    for sub in subs:
        endpoint = getattr(sub, "endpoint", None)

        if not endpoint or endpoint in seen_endpoints:
            continue

        seen_endpoints.add(endpoint)
        unique_subs.append(sub)

    app.logger.warning(
        "DEBUG: Found %s unique subscription(s) for user %s",
        len(unique_subs),
        uid,
    )

    if not unique_subs:
        app.logger.warning("No push subscriptions found for user %s", uid)
        return {
            "ok": False,
            "sent": 0,
            "failed": 0,
            "reason": "no_subscription",
        }

    sent = 0
    failed = 0

    for sub in unique_subs:

        app.logger.warning(
            "DEBUG: Processing endpoint: %s",
            getattr(sub, "endpoint", None),
        )

        try:
            subscription_info = json.loads(sub.subscription_json)

            app.logger.warning("DEBUG: Calling webpush()")

            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=app.config["VAPID_PRIVATE_KEY_PATH"],
                vapid_claims={
                    "sub": app.config["VAPID_SUBJECT"]
                },
            )

            sent += 1

            app.logger.warning(
                "DEBUG: webpush() SUCCESS"
            )

        except Exception as exc:

            failed += 1

            app.logger.exception(
                "Push send failed for user %s endpoint %s: %s",
                uid,
                getattr(sub, "endpoint", None),
                exc,
            )

            try:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)

                if status in (404, 410):
                    app.logger.warning(
                        "DEBUG: Removing expired subscription."
                    )

                    db.session.delete(sub)
                    db.session.commit()

            except Exception:
                db.session.rollback()

    result = {
        "ok": sent > 0,
        "sent": sent,
        "failed": failed,
    }

    app.logger.warning(
        "DEBUG: _send_push_to_user() FINISHED -> %s",
        result,
    )

    return result

@app.route("/api/push/consent", methods=["POST"])
def api_push_consent():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()

    user = db.session.get(User, me["id"])
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    try:
        if action == "accept":
            user.push_notifications_enabled = True
            user.push_notifications_rejected_at = None
            if user.push_prompt_shown_at is None:
                user.push_prompt_shown_at = datetime.utcnow()

        elif action == "reject":
            user.push_notifications_enabled = False
            user.push_notifications_rejected_at = datetime.utcnow()
            if user.push_prompt_shown_at is None:
                user.push_prompt_shown_at = datetime.utcnow()

        else:
            return jsonify({"error": "invalid_action"}), 400

        db.session.add(user)
        db.session.commit()
        return jsonify({"ok": True})

    except Exception:
        db.session.rollback()
        app.logger.exception("api_push_consent failed")
        return jsonify({"ok": False}), 500








import re
from html import unescape
from datetime import datetime
import threading

def send_welcome_notification(user):
    """
    Create a one-off welcome notification (in-app + email) for a single user,
    then send a multipart email (text + html). This version guarantees the
    plain-text alternative contains the full copy (not just the short title).
    """
    if not user or not getattr(user, "email", None):
        app.logger.debug("send_welcome_notification: no user or no email; skipping")
        return

    try:
        # Title (short) stored in DB; keep as before for consistency
        title = "Welcome to Care Companion"
        # Full message paragraphs used for both text alternative and HTML body
        p1 = (f"Hi {getattr(user, 'first_name', 'there')}, welcome aboard 👋 — "
              "thanks for completing your profile. Your listing is now published and visible to families and caregivers.")
        p2 = ("You can now browse connections, receive requests, and respond to matches. "
              "We recommend visiting your profile to confirm availability and contact details so requests reach you quickly.")

        # DB message (keep reasonably short but informative)
        db_message = f"{p1}"

        # --- DB: create Notification + recipient row (unchanged behaviour) ---
        notif = None
        try:
            notif = Notification(
                title=title,
                message=db_message,
                sender="system",
                target=f"single:{user.id}",
                delivery_email=True,
                delivery_inapp=True
            )
            db.session.add(notif)
            db.session.flush()  # notif.id available

            nr = NotificationRecipient(notification_id=notif.id, user_id=int(user.id), is_read=False)
            db.session.add(nr)
            db.session.commit()
        except Exception:
            # If DB fails, rollback but continue to attempt to send email (best-effort)
            try:
                db.session.rollback()
            except Exception:
                pass
            app.logger.exception("send_welcome_notification: failed to create DB records")

        # SocketIO realtime emit (best-effort)
        try:
            socketio.emit("notification_sent", {
                "notification_id": getattr(notif, "id", None),
                "title": title,
                "message": db_message,
                "user_id": user.id,
                "delivery_inapp": True,
                "delivery_email": True
            })
        except Exception:
            app.logger.exception("send_welcome_notification: socketio emit failed")

        # --- Build URLs in a way that works inside or outside a request context ---
        try:
            home_url = url_for('home', _external=True)
        except Exception:
            base = app.config.get('BASE_URL') or app.config.get('SERVER_NAME') or 'localhost:5000'
            scheme = app.config.get('PREFERRED_URL_SCHEME', 'http')
            if base.startswith('http://') or base.startswith('https://'):
                home_url = base.rstrip('/') + '/'
            else:
                home_url = f"{scheme}://{base.rstrip('/')}/"

        try:
            profile_url = url_for('profile_setup', _external=True)
        except Exception:
            profile_url = home_url

        # Render HTML template (your email_welcome.html)
        try:
            html_body = render_template(
                "email_welcome.html",
                first_name=(user.first_name or ""),
                profile_url=profile_url,
                home_url=home_url,
                current_year=datetime.utcnow().year
            )
            template_rendered = True
        except Exception:
            app.logger.exception("send_welcome_notification: render_template failed for email_welcome.html")
            html_body = f"<h3>{title}</h3><p>{p1}</p><p>{p2}</p>"
            template_rendered = False

        # Build a full text alternative that mirrors the HTML content
        text_lines = [
            "Welcome to Care Companion — your profile is live",
            "",
            p1,
            "",
            p2,
            "",
            f"Open: {home_url or profile_url or ''}",
            "",
            "Need help? Reply to this email or visit our support page.",
            "",
            "Care Companion",
            "Helping families & caregivers connect safely.",
            f"© {datetime.utcnow().year}"
        ]
        text_body = "\n".join(text_lines)

        # Final subject and send via your safe wrapper (ensures multipart/alternative)
        subj = "Welcome to Care Companion"
        sent_ok = _safe_send_email(subject=subj, recipients=[user.email], text_body=text_body, html_body=html_body)

        # Extra logging for easier debugging
        try:
            app.logger.info("WELCOME EMAIL: to=%s html_len=%s text_len=%s rendered=%s send_ok=%s",
                            user.email,
                            len(html_body or ""),
                            len(text_body or ""),
                            bool(template_rendered),
                            bool(sent_ok))
        except Exception:
            # avoid logging blowups
            pass

        # If _safe_send_email failed, attempt background send (best-effort)
        if not sent_ok:
            try:
                _send_email_async(subj, [user.email], html_body, text_body=text_body)
                app.logger.info("WELCOME EMAIL: queued async fallback for %s", user.email)
            except Exception:
                app.logger.exception("send_welcome_notification: both safe send and async fallback failed")

    except Exception:
        app.logger.exception("send_welcome_notification: uncaught exception")


def _html_to_text(html: str) -> str:
    """
    Robust HTML -> plain-text converter for email text alternative.
    Removes head/style/script/comments, converts block tags to newlines,
    strips remaining tags, unescapes entities and collapses whitespace.
    """
    if not html:
        return ""
    try:
        s = str(html)

        # remove HTML comments
        s = re.sub(r'<!--.*?-->', '', s, flags=re.S)

        # remove <head> ... </head>
        s = re.sub(r'<head[\s\S]*?>[\s\S]*?</head>', '', s, flags=re.I)

        # remove style/script blocks and contents
        s = re.sub(r'<style[\s\S]*?>[\s\S]*?</style>', '', s, flags=re.I)
        s = re.sub(r'<script[\s\S]*?>[\s\S]*?</script>', '', s, flags=re.I)

        # Replace end-block tags with double newline to keep paragraphs
        s = re.sub(r'</(p|div|section|article|header|footer|h[1-6])\s*>', '\n\n', s, flags=re.I)

        # Replace <br> with newline
        s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)

        # Remove remaining tags
        s = re.sub(r'<[^>]+>', '', s)

        # Unescape HTML entities
        s = unescape(s)

        # Trim, collapse multiple blank lines to max two
        lines = [ln.strip() for ln in s.splitlines()]
        s = '\n'.join(lines)
        s = re.sub(r'\n{3,}', '\n\n', s)
        s = s.strip()

        return s
    except Exception:
        try:
            return unescape(re.sub(r'<[^>]+>', '', html)).strip()
        except Exception:
            return ""


def _safe_send_email(subject, recipients, text_body=None, html_body=None):
    """
    Send synchronously inside app context (ensures proper multipart/alternative).
    Returns True on success.
    """
    try:
        with app.app_context():
            # prefer explicit text_body if provided
            if text_body and isinstance(text_body, str) and text_body.strip():
                tb = text_body
            else:
                tb = _html_to_text(html_body or "")

            if not tb:
                tb = subject + "\n\n" + (html_body or "")

            msg = MailMessage(subject=subject, recipients=list(recipients))
            msg.body = tb
            if html_body:
                msg.html = html_body
            mail.send(msg)
        return True
    except Exception:
        app.logger.exception("Notification email send failed (sync safe wrapper).")
        return False


def _send_email_async(subject, recipients, html_body=None, text_body=None):
    """
    Background send ensuring text + html parts exist.
    """
    def _send():
        try:
            if not recipients:
                return
            with app.app_context():
                if text_body and isinstance(text_body, str) and text_body.strip():
                    tb_local = text_body
                else:
                    tb_local = _html_to_text(html_body or "")
                if not tb_local:
                    tb_local = subject + "\n\n" + (html_body or "")
                msg = MailMessage(subject=subject, recipients=list(recipients))
                msg.body = tb_local
                if html_body:
                    msg.html = html_body
                mail.send(msg)
        except Exception:
            app.logger.exception("Notification email send failed (async).")

    try:
        thr = threading.Thread(target=_send, daemon=True)
        thr.start()
    except Exception:
        # sync fallback
        try:
            with app.app_context():
                tb_local = text_body if (text_body and text_body.strip()) else _html_to_text(html_body or "")
                if not tb_local:
                    tb_local = subject + "\n\n" + (html_body or "")
                msg = MailMessage(subject=subject, recipients=list(recipients))
                msg.body = tb_local
                if html_body:
                    msg.html = html_body
                mail.send(msg)
        except Exception:
            app.logger.exception("Notification email send failed (sync fallback).")


# ----------------- PERSONALIZED BULK EMAIL SENDER HELPER -----------------
def _send_personalized_emails(subject, template_name, common_ctx, recipient_user_ids, throttle_delay=0.06):
    """
    Send one email per recipient (personalized HTML + text fallback).
    - subject: email subject string
    - template_name: Jinja template filename (e.g. 'email_notification.html')
    - common_ctx: dict of context keys passed to template (profile_url, logo_url, title, message, ...)
    - recipient_user_ids: iterable of user ids (ints)
    - throttle_delay: seconds to pause between sends to avoid bursting SMTP (small)
    Returns number_sent (int).
    """
    sent = 0
    # make a list to compute len reliably
    ids = list(recipient_user_ids or [])
    for uid in ids:
        try:
            u = db.session.get(User, int(uid))
            if not u or not getattr(u, "email", None):
                continue
            recipient_email = u.email

            # Build per-recipient context
            ctx = dict(common_ctx or {})
            ctx.update({
                "first_name": (getattr(u, "first_name", "") or ""),
                "recipient_email": recipient_email
            })

            # Render HTML for this recipient (fallback to a tiny fragment if rendering fails)
            try:
                per_html = render_template(template_name, **ctx)
            except Exception:
                title = ctx.get("title") or ""
                message = ctx.get("message") or ""
                per_html = f"<h3>{title}</h3><p>{message}</p>"

            # Build a simple personalized text fallback
            text_lines = []
            if ctx.get("title"):
                text_lines.append(ctx["title"])
                text_lines.append("")
            if ctx.get("first_name"):
                text_lines.append(f"Hi {ctx['first_name']},")
                text_lines.append("")
            text_lines.append(ctx.get("message", ""))
            text_lines.append("")
            if ctx.get("profile_url"):
                text_lines.append(f"Open: {ctx['profile_url']}")
            text_body = "\n".join([ln for ln in text_lines if ln is not None])

            # Use async wrapper (non-blocking) if available
            try:
                _send_email_async(subject, [recipient_email], per_html, text_body=text_body)
                sent += 1
            except Exception:
                # Last-resort sync send
                try:
                    _safe_send_email(subject, [recipient_email], text_body=text_body, html_body=per_html)
                    sent += 1
                except Exception:
                    app.logger.exception("Failed sending notification email to %s", recipient_email)

            # Throttle a little (avoid bursting)
            try:
                if throttle_delay and throttle_delay > 0:
                    time.sleep(throttle_delay)
            except Exception:
                pass

        except Exception:
            app.logger.exception("Error while attempting to send notification email to user id %s", uid)
            continue

    app.logger.info("_send_personalized_emails: attempted=%s sent=%s", len(ids), sent)
    return int(sent)
# ----------------- end helper -----------------


# --- Add this route near your notifications helpers in app.py ---
from flask import request, jsonify
import traceback

# ----------------- create_notification_and_emit (replacement) -----------------
def create_notification_and_emit(
    user_id,
    title,
    message,
    delivery_email=1,
    delivery_inapp=1,
    send_email_fn=None,   # kept for compatibility but unused
    user_email=None
):
    """
    Create a normal notification for a single user, store it in the DB,
    emit a Socket.IO event, and optionally send email.

    Use this for system/admin notifications.
    Do NOT use this for chat messages if you want chat to update the message icon instead of the bell.
    """
    try:
        now = datetime.utcnow()

        # 1) Insert notification row using ORM
        notif = Notification(
            title=title,
            message=message,
            sender="system",
            target=f"single:{int(user_id)}",
            delivery_email=bool(delivery_email),
            delivery_inapp=bool(delivery_inapp),
            created_at=now,
        )
        db.session.add(notif)
        db.session.flush()  # gives notif.id

        # 2) Insert recipient row
        nr = NotificationRecipient(
            notification_id=notif.id,
            user_id=int(user_id),
            is_read=False,
            created_at=now,
        )
        db.session.add(nr)
        db.session.commit()

        # 3) Emit real-time socket event
        try:
            socketio.emit(
                "notification_sent",
                {
                    "user_id": int(user_id),
                    "title": title,
                    "message": message,
                    "notification_id": int(notif.id),
                },
                room=f"user_{int(user_id)}",
            )
        except Exception:
            app.logger.exception("socketio emit failed for individual user")

        # 4) EMAIL DELIVERY — per-recipient sending
        if delivery_email:
            try:
                try:
                    profile_url = url_for("profile_setup", _external=True)
                except Exception:
                    profile_url = None

                try:
                    logo_url = url_for("static", filename="images/caregiver_logo.png", _external=True)
                except Exception:
                    logo_url = None

                common_ctx = {
                    "title": title,
                    "message": message,
                    "profile_url": profile_url,
                    "logo_url": logo_url,
                    "current_year": datetime.utcnow().year,
                }

                _send_personalized_emails(
                    subject=f"[Care Companion] {title}",
                    template_name="email_notification.html",
                    common_ctx=common_ctx,
                    recipient_user_ids=[int(user_id)],
                    throttle_delay=0.02,
                )
            except Exception:
                app.logger.exception("Notification email failed (create_notification_and_emit)")

        return True

    except Exception:
        app.logger.exception("create_notification_and_emit failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return False
# ----------------- end replacement -----------------




# ----------------- admin_notifications route (replacement) -----------------
@app.route("/admin/notifications", methods=["GET", "POST"])
def admin_notifications():
    admin_required()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        message = (request.form.get("message") or "").strip()
        target = (request.form.get("target") or "all")
        delivery = (request.form.get("delivery") or "inapp")  # 'inapp' | 'email' | 'both'
        single_user_ident = (request.form.get("single_user") or "").strip()

        if not title or not message:
            flash("Title and message are required.", "error")
            return redirect(url_for("admin_notifications"))

        delivery_email = delivery in ("email", "both")
        delivery_inapp = delivery in ("inapp", "both")

        try:
            # create notification record
            notif = Notification(
                title=title,
                message=message,
                sender="admin",
                target=target,
                delivery_email=delivery_email,
                delivery_inapp=delivery_inapp
            )
            db.session.add(notif)
            db.session.flush()  # ensure notif.id available

            # compute recipients
            recipient_user_ids = _resolve_recipient_user_ids(target if target != "single" else "single", single_user_ident)

            # create recipient rows for in-app and collect emails
            email_recipients = []
            for uid in recipient_user_ids:
                try:
                    nr = NotificationRecipient(notification_id=notif.id, user_id=int(uid), is_read=False)
                    db.session.add(nr)
                    if delivery_email:
                        u = db.session.get(User, uid)
                        if u and getattr(u, "email", None):
                            email_recipients.append(u.email)
                except Exception:
                    app.logger.exception("Failed to add notification recipient %s", uid)
                    continue

            db.session.commit()

            # Real-time push
            try:
                socketio.emit("notification_sent", {"notification_id": notif.id, "title": notif.title, "message": notif.message})
            except Exception:
                app.logger.exception("socketio emit failed for notification_sent")

            # send emails (personalized, one-per-user)
            if delivery_email and recipient_user_ids:
                try:
                    try:
                        profile_url = url_for('profile_setup', _external=True)
                    except Exception:
                        profile_url = None
                    try:
                        logo_url = url_for('static', filename='images/caregiver_logo.png', _external=True)
                    except Exception:
                        logo_url = None

                    common_ctx = {
                        "title": title,
                        "message": message,
                        "profile_url": profile_url,
                        "logo_url": logo_url,
                        "current_year": datetime.utcnow().year
                    }

                    _send_personalized_emails(
                        subject=f"[Care Companion] {title}",
                        template_name="email_notification.html",
                        common_ctx=common_ctx,
                        recipient_user_ids=recipient_user_ids,
                        throttle_delay=0.06
                    )
                except Exception:
                    app.logger.exception("Notification email failed (admin_notifications)")

            flash("Notification sent.", "success")
            return redirect(url_for("admin_notifications"))
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to create/send notification")
            flash("Failed to send notification.", "error")
            return redirect(url_for("admin_notifications"))

    # GET: show minimal admin form
    return render_template("admin_notifications.html")
# ----------------- end admin_notifications replacement -----------------


@app.route("/admin/notifications/panel", methods=["GET", "POST"])
def notif_admin_panel():
    # Require notifications admin session; redirect to login page if not present.
    if not session.get("notif_admin"):
        # preserve the target path so the login can return here
        return redirect(url_for("notif_admin_login", next=request.path))

    # --- below is your full existing panel logic (unchanged behaviour) ---
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        message = (request.form.get("message") or "").strip()
        target = (request.form.get("target") or "all")
        delivery = (request.form.get("delivery") or "inapp")
        single_user_ident = (request.form.get("single_user") or "").strip()

        if not title or not message:
            flash("Title and message are required.", "error")
            return redirect(url_for("notif_admin_panel"))

        delivery_email = delivery in ("email", "both")
        delivery_inapp = delivery in ("inapp", "both")

        try:
            # 1) Insert Notification
            notif = Notification(
                title=title,
                message=message,
                sender="admin",
                target=target,
                delivery_email=delivery_email,
                delivery_inapp=delivery_inapp
            )
            db.session.add(notif)
            db.session.flush()  # get notif.id

            # 2) Resolve recipients
            recipient_user_ids = _resolve_recipient_user_ids(
                target if target != "single" else "single",
                single_user_ident
            )

            email_recipients = []
            for uid in recipient_user_ids:
                try:
                    nr = NotificationRecipient(
                        notification_id=notif.id,
                        user_id=int(uid),
                        is_read=False
                    )
                    db.session.add(nr)

                    if delivery_email:
                        u = db.session.get(User, uid)
                        if u and getattr(u, "email", None):
                            email_recipients.append(u.email)

                except Exception:
                    app.logger.exception("Failed to add notification recipient %s", uid)
                    continue

            db.session.commit()

            # 3) Socket.io push
            try:
                socketio.emit(
                    "notification_sent",
                    {
                        "notification_id": notif.id,
                        "title": notif.title,
                        "message": notif.message
                    }
                )
            except Exception:
                app.logger.exception("socketio emit failed (notif_admin_panel)")

            # 4) Send EMAIL (per-user)
            if delivery_email and recipient_user_ids:
                try:
                    try:
                        profile_url = url_for("profile_setup", _external=True)
                    except Exception:
                        profile_url = None

                    try:
                        home_url = url_for("home", _external=True)
                    except Exception:
                        base = (
                            app.config.get("BASE_URL")
                            or app.config.get("SERVER_NAME")
                            or "localhost:5000"
                        )
                        scheme = app.config.get("PREFERRED_URL_SCHEME", "http")
                        if base.startswith('http://') or base.startswith('https://'):
                            home_url = base.rstrip("/") + "/"
                        else:
                            home_url = f"{scheme}://{base.rstrip('/')}/"

                    try:
                        logo_url = url_for("static", filename="images/caregiver_logo.png", _external=True)
                    except Exception:
                        logo_url = None

                    common_ctx = {
                        "title": title,
                        "message": message,
                        "profile_url": profile_url,
                        "home_url": home_url,
                        "logo_url": logo_url,
                        "current_year": datetime.utcnow().year
                    }

                    # _send_personalized_emails is the helper that sends one email per user
                    _send_personalized_emails(
                        subject=f"[Care Companion] {title}",
                        template_name="email_notification.html",
                        common_ctx=common_ctx,
                        recipient_user_ids=recipient_user_ids,
                        throttle_delay=0.06
                    )

                except Exception:
                    app.logger.exception("Failed to send notification email (notif_admin_panel)")

            flash("Notification sent.", "success")
            return redirect(url_for("notif_admin_panel"))

        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to create/send notification (notif_admin_panel)")
            flash("Failed to send notification.", "error")
            return redirect(url_for("notif_admin_panel"))

    # GET
    return render_template("notifications_admin_panel.html")






# API: unread count for current user
@app.route("/api/notifications/unread_count")
def api_notifications_unread_count():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        cnt = db.session.query(NotificationRecipient).filter_by(user_id=me["id"], is_read=False).count()
        return jsonify({"unread": int(cnt)})
    except Exception:
        app.logger.exception("Failed to get unread notifications count")
        return jsonify({"unread": 0})


# API: list notifications for current user (latest first)
@app.route("/api/notifications")
def api_notifications_list():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        rows = (
            db.session.query(Notification, NotificationRecipient)
            .join(NotificationRecipient, Notification.id == NotificationRecipient.notification_id)
            .filter(NotificationRecipient.user_id == me["id"])
            .order_by(Notification.created_at.desc())
            .limit(200)
            .all()
        )
        out = []
        for n, nr in rows:
            out.append({
                "id": n.id,
                "title": n.title,
                "message": n.message,
                "sender": n.sender,
                "delivery_email": bool(n.delivery_email),
                "delivery_inapp": bool(n.delivery_inapp),
                "created_at": (n.created_at.isoformat() + "Z") if n.created_at else None,
                "is_read": bool(nr.is_read),
                "recipient_id": nr.id
            })
        return jsonify({"notifications": out})
    except Exception:
        app.logger.exception("Failed to list notifications")
        return jsonify({"notifications": []})


# API: mark one or many notification recipient rows as read
@app.route("/api/notifications/mark_read", methods=["POST"])
def api_notifications_mark_read():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        payload = request.get_json(silent=True) or {}
        # accept recipient_ids list OR single notification_id to mark for this user
        recip_ids = payload.get("recipient_ids") or payload.get("recipient_id")
        now = datetime.utcnow()
        updated = 0
        if recip_ids:
            if not isinstance(recip_ids, list):
                recip_ids = [recip_ids]
            for rid in recip_ids:
                try:
                    nr = NotificationRecipient.query.get(int(rid))
                    if nr and nr.user_id == me["id"] and not nr.is_read:
                        nr.is_read = True
                        nr.read_at = now
                        db.session.add(nr)
                        updated += 1
                except Exception:
                    continue
        else:
            # accept notification_id -> translate to recipient row for this user
            nid = payload.get("notification_id")
            if nid:
                nr = NotificationRecipient.query.filter_by(notification_id=nid, user_id=me["id"]).first()
                if nr and not nr.is_read:
                    nr.is_read = True
                    nr.read_at = now
                    db.session.add(nr)
                    updated = 1
        if updated > 0:
            db.session.commit()
            return jsonify({"status": "ok", "updated": updated})
        return jsonify({"status": "ok", "updated": 0})
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to mark notifications read")
        return jsonify({"status": "error", "message": "Server error"}), 500


# User-facing notifications page
@app.route("/notifications")
def notifications_page():
    me = get_current_user()
    if not me:
        flash("Please log in to view notifications.", "error")
        return redirect(url_for("login", next=url_for("notifications_page")))
    # template will call /api/notifications to populate list via JS for snappy UX
    return render_template("notifications.html", user=me)



# ---------- Standalone notifications-admin (keeps existing /admin routes untouched) ----------
# Add this after your notifications module. It uses session['notif_admin'] to avoid
# touching your existing session['is_admin'] admin.

NOTIF_ADMIN_PASSWORD = os.environ.get("NOTIF_ADMIN_PASSWORD")

def notif_admin_required():
    if not session.get("notif_admin"):
        abort(403)

@app.route("/admin/notifications/login", methods=["GET", "POST"])
def notif_admin_login():
    """
    Standalone login for the notifications admin (path: /admin/notifications/login).
    Accepts an optional `next` query param (or hidden form field) to return the admin
    to the requested panel after successful login.
    """
    # accept next from querystring or posted form
    next_target = request.args.get("next") or request.form.get("next")

    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if pw == NOTIF_ADMIN_PASSWORD:
            session["notif_admin"] = True
            flash("Notifications admin login successful.", "success")
            # Safe redirect: only allow local/internal paths (start with '/')
            if next_target and isinstance(next_target, str) and next_target.startswith("/") and not next_target.startswith("//"):
                return redirect(next_target)
            return redirect(url_for("notif_admin_panel"))
        flash("Wrong admin password.", "danger")
        # Keep next param so retry stays consistent
        return redirect(url_for("notif_admin_login", next=next_target))

    # GET -> render login form (pass next back to template)
    return render_template("notifications_admin_login.html", next=next_target)


@app.route("/admin/notifications/logout")
def notif_admin_logout():
    session.pop("notif_admin", None)
    flash("Logged out of notifications admin.", "info")
    return redirect(url_for("notif_admin_login"))

@app.route("/admin/users/<int:user_id>/suspend", methods=["POST"])
def admin_suspend_user(user_id):
    if not session.get("is_admin"):
        abort(403)

    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    if getattr(user, "is_deleted", False):
        return jsonify({"status": "error", "message": "Deleted users cannot be suspended."}), 400

    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip() or "Violation of the website terms."

    try:
        user.is_active = False
        db.session.add(user)
        db.session.commit()

        try:
            send_suspension_email(user, reason)
        except Exception:
            app.logger.exception("Suspension email failed")

        return jsonify({
            "status": "ok",
            "user_id": user.id,
            "is_active": user.is_active,
            "message": "User suspended."
        }), 200

    except Exception as exc:
        db.session.rollback()
        app.logger.exception("admin_suspend_user failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/admin/users/<int:user_id>/unsuspend", methods=["POST"])
def admin_unsuspend_user(user_id):
    if not session.get("is_admin"):
        abort(403)

    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    try:
        user.is_active = True
        db.session.add(user)
        db.session.commit()

        try:
            send_unsuspension_email(user)
        except Exception:
            app.logger.exception("Unsuspension email failed")

        return jsonify({
            "status": "ok",
            "user_id": user.id,
            "is_active": user.is_active,
            "message": "User unsuspended."
        }), 200

    except Exception as exc:
        db.session.rollback()
        app.logger.exception("admin_unsuspend_user failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.before_request
def block_suspended_users():
    uid = session.get("user_id")
    if not uid:
        return
    user = db.session.get(User, uid)
    if user and not user.is_active and request.endpoint not in ("login", "logout", "static"):
        session.clear()
        return redirect(url_for("login"))



@app.route("/api/notifications/mark_all_read", methods=["POST"])
def api_notifications_mark_all_read():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401
    try:
        now = datetime.utcnow()
        rows = NotificationRecipient.query.filter_by(user_id=me["id"], is_read=False).all()
        updated = 0
        for r in rows:
            r.is_read = True
            r.read_at = now
            db.session.add(r)
            updated += 1
        if updated:
            db.session.commit()
        return jsonify({"status":"ok","updated":updated})
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to mark all notifications read")
        return jsonify({"status":"error"}), 500





@app.route("/api/notifications/delete", methods=["POST"])
def api_notifications_delete():
    """
    Delete notification_recipient rows for the signed-in user.
    Accepts JSON payload:
      { "recipient_ids": [<id>, ...] }  OR
      { "notification_id": <notification_id> }
    Returns: { status: "ok", deleted: <n> } on success
    """
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    data = request.get_json(force=True, silent=True) or {}
    rec_ids = data.get("recipient_ids") or []
    notification_id = data.get("notification_id")

    uid = int(me["id"])

    # normalize rec_ids -> ints
    try:
        rec_ids = [int(x) for x in (rec_ids or []) if str(x).strip() != ""]
    except Exception:
        rec_ids = []

    deleted = 0
    try:
        if rec_ids:
            # delete recipient rows that belong to this user
            placeholders = ",".join(str(int(x)) for x in rec_ids)
            if placeholders:
                sql = text(f"DELETE FROM notification_recipient WHERE id IN ({placeholders}) AND user_id = :uid")
                res = db.session.execute(sql, {"uid": uid})
                db.session.commit()
                deleted = res.rowcount if getattr(res, "rowcount", None) is not None else 0

        elif notification_id:
            try:
                nid = int(notification_id)
            except Exception:
                return jsonify({"error": "invalid notification_id"}), 400
            sql = text("DELETE FROM notification_recipient WHERE notification_id = :nid AND user_id = :uid")
            res = db.session.execute(sql, {"nid": nid, "uid": uid})
            db.session.commit()
            deleted = res.rowcount if getattr(res, "rowcount", None) is not None else 0

        else:
            return jsonify({"error": "no recipient_ids or notification_id provided"}), 400

        app.logger.info("User %s deleted %s notification_recipient rows", uid, deleted)
        return jsonify({"status": "ok", "deleted": int(deleted)})
    except Exception as exc:
        app.logger.exception("Failed deleting notification_recipient rows: %s", exc)
        db.session.rollback()
        return jsonify({"error": "delete_failed"}), 500


# alias endpoint for compatibility with clients that call /remove
@app.route("/api/notifications/remove", methods=["POST"])
def api_notifications_remove():
    return api_notifications_delete()


@app.route("/api/notifications/<int:notification_id>", methods=["DELETE"])
def api_notifications_delete_by_nid(notification_id):
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401
    uid = int(me["id"])
    try:
        sql = text("DELETE FROM notification_recipient WHERE notification_id = :nid AND user_id = :uid")
        res = db.session.execute(sql, {"nid": int(notification_id), "uid": uid})
        db.session.commit()
        deleted = res.rowcount if getattr(res, "rowcount", None) is not None else 0
        app.logger.info("User %s deleted %s rows for notification_id %s", uid, deleted, notification_id)
        return jsonify({"status": "ok", "deleted": int(deleted)})
    except Exception as exc:
        app.logger.exception("Failed deleting notification_recipient rows by notification_id: %s", exc)
        db.session.rollback()
        return jsonify({"error": "delete_failed"}), 500


@app.route("/api/push/vapid_public_key", methods=["GET"])
def api_push_vapid_public_key():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401
    return jsonify({"publicKey": app.config.get("VAPID_PUBLIC_KEY", "")})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    data = request.get_json(silent=True) or {}
    subscription = data.get("subscription") or {}
    endpoint = (subscription.get("endpoint") or "").strip()
    browser_key = (data.get("browser_key") or endpoint or "").strip()

    if not endpoint:
        return jsonify({"error": "missing_subscription"}), 400
    if not browser_key:
        return jsonify({"error": "missing_browser_key"}), 400

    uid = int(me["id"])
    subscription_json = json.dumps(subscription)

    try:
        # Keep ONE browser record, but link it to MANY user ids over time.
        browser_row = (
            BrowserPushSubscription.query
            .filter(
                (BrowserPushSubscription.browser_key == browser_key) |
                (BrowserPushSubscription.endpoint == endpoint)
            )
            .first()
        )

        if browser_row:
            linked_ids = _load_json_list(browser_row.linked_user_ids_json)
            linked_ids = [int(x) for x in linked_ids if str(x).strip() != ""]
            if uid not in linked_ids:
                linked_ids.append(uid)

            browser_row.browser_key = browser_key
            browser_row.endpoint = endpoint
            browser_row.subscription_json = subscription_json
            browser_row.linked_user_ids_json = json.dumps(sorted(set(linked_ids)))
            browser_row.updated_at = datetime.utcnow()
            db.session.add(browser_row)
        else:
            db.session.add(BrowserPushSubscription(
                browser_key=browser_key,
                endpoint=endpoint,
                subscription_json=subscription_json,
                linked_user_ids_json=json.dumps([uid]),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))

        # Keep the direct user subscription for the current account too.
        # If the same endpoint was previously tied to another user, remove that old direct row first.
        try:
            db.session.execute(
                text("DELETE FROM push_subscription WHERE endpoint = :endpoint"),
                {"endpoint": endpoint}
            )
        except Exception:
            pass

        db.session.add(PushSubscription(
            user_id=uid,
            endpoint=endpoint,
            subscription_json=subscription_json,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))

        db.session.commit()
        return jsonify({"ok": True})

    except IntegrityError as exc:
        db.session.rollback()
        tb = traceback.format_exc()
        app.logger.exception("push subscribe integrity error")
        return jsonify({
            "ok": False,
            "error": str(exc),
            "type": "IntegrityError",
            "traceback": tb
        }), 500

    except Exception as exc:
        db.session.rollback()
        tb = traceback.format_exc()
        app.logger.exception("push subscribe failed")
        return jsonify({
            "ok": False,
            "error": str(exc),
            "type": type(exc).__name__,
            "traceback": tb
        }), 500


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    browser_key = (data.get("browser_key") or "").strip()

    if not endpoint and not browser_key:
        return jsonify({"error": "missing_endpoint_or_browser_key"}), 400

    uid = int(me["id"])

    try:
        # Remove only the current user's direct subscription row(s)
        try:
            db.session.execute(
                text("DELETE FROM push_subscription WHERE user_id = :uid OR endpoint = :endpoint"),
                {"uid": uid, "endpoint": endpoint}
            )
        except Exception:
            pass

        # Unlink this user from the browser-wide subscription, but keep other users linked
        try:
            q = BrowserPushSubscription.query
            if browser_key:
                browser_row = q.filter(BrowserPushSubscription.browser_key == browser_key).first()
            else:
                browser_row = q.filter(BrowserPushSubscription.endpoint == endpoint).first()

            if browser_row:
                linked_ids = _load_json_list(browser_row.linked_user_ids_json)
                linked_ids = [int(x) for x in linked_ids if str(x).strip() != "" and int(x) != uid]

                if linked_ids:
                    browser_row.linked_user_ids_json = json.dumps(sorted(set(linked_ids)))
                    browser_row.updated_at = datetime.utcnow()
                    db.session.add(browser_row)
                else:
                    db.session.delete(browser_row)
        except Exception:
            pass

        db.session.commit()
        return jsonify({"ok": True})

    except Exception as exc:
        db.session.rollback()
        app.logger.exception("push unsubscribe failed: %s", exc)
        return jsonify({"ok": False, "error": "server_error"}), 500


@app.route("/api/push/debug", methods=["GET"])
def api_push_debug():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    uid = int(me["id"])

    try:
        direct_subs = PushSubscription.query.filter_by(user_id=uid).all()
    except Exception:
        direct_subs = []

    try:
        browser_rows = BrowserPushSubscription.query.all()
        linked_browser_rows = []
        for row in browser_rows:
            try:
                linked_ids = _load_json_list(row.linked_user_ids_json)
                linked_ids = [int(x) for x in linked_ids if str(x).strip() != ""]
                if uid in linked_ids:
                    linked_browser_rows.append(row)
            except Exception:
                continue
    except Exception:
        linked_browser_rows = []

    return jsonify({
        "ok": True,
        "user_id": uid,
        "subscription_count": len(direct_subs),
        "browser_subscription_count": len(linked_browser_rows),
        "endpoints": [s.endpoint[-40:] for s in direct_subs],
        "browser_endpoints": [s.endpoint[-40:] for s in linked_browser_rows],
    })


@app.route("/sw.js")
def sw_js():
    js = r"""
self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
  event.respondWith(fetch(event.request));
});

self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'New message', body: 'You received a new message.' };
  }

  const title = data.title || 'New message';
  const avatar = data.avatar_url || data.icon || '/static/default-avatar.png';

  const options = {
    body: data.body || 'You received a new message.',
    icon: avatar,
    badge: data.badge || '/static/default-avatar.png',
    tag: data.tag || ('chat_' + String(data.message_id || Date.now())),
    renotify: true,
    requireInteraction: true,
    data: {
      url: data.url || '/notifications',
      conversation_id: data.conversation_id || null,
      sender_id: data.sender_id || null,
      recipient_id: data.recipient_id || null,
      recipient_email: data.recipient_email || null,
      message_id: data.message_id || null,
      avatar_url: avatar
    }
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();

  event.waitUntil((async () => {
    const data = event.notification.data || {};
    const chatUrl = data.url || '/notifications';

    const loginUrl =
      '/login?next=' + encodeURIComponent(chatUrl) +
      (data.recipient_email ? '&email=' + encodeURIComponent(data.recipient_email) : '');

    let targetUrl = loginUrl;

    try {
      const meRes = await fetch('/api/me', {
        credentials: 'include',
        cache: 'no-store'
      });

      if (meRes.ok) {
        const me = await meRes.json().catch(() => null);
        if (me && data.recipient_id && Number(me.id) === Number(data.recipient_id)) {
          targetUrl = chatUrl;
        }
      }
    } catch (e) {
      targetUrl = loginUrl;
    }

    if (clients.openWindow) {
      await clients.openWindow(targetUrl);
    }
  })());
});
"""
    response = current_app.response_class(js, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response

@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    payload = {
        "title": "Care Companion test",
        "body": "If you see this popup, push notifications are working.",
        "url": url_for("notifications_page"),
        "recipient_id": me["id"],
        "message_id": int(time.time() * 1000),
        "tag": f"test_{me['id']}_{int(time.time())}",
        "requireInteraction": True,
        "avatar_url": _get_user_avatar_url(me["id"]),
        "icon": _get_user_avatar_url(me["id"]),
        "badge": _get_user_avatar_url(me["id"]),
    }

    result = _send_push_to_user(me["id"], payload)

    return jsonify({
        "ok": True,
        **result
    })
# ----------------- end notification block -----------------


# Avoid endpoint collisions in development by giving an explicit endpoint name
# while keeping the public URL the same.
if 'api_notify_staff_v2' in app.view_functions:
    try:
        app.logger.warning("Replacing existing 'api_notify_staff_v2' endpoint with updated handler.")
    except Exception:
        pass
    app.view_functions.pop('api_notify_staff_v2')

@app.route('/api/notify_staff', methods=['POST'], endpoint='api_notify_staff_v2')
def api_notify_staff_v2():
    """
    Accepts JSON: { chat_ref, visitor_message, visitor_agent_hint, timestamp, link }
    Sends email to configured staff addresses and returns JSON result.

    NOTE: The public URL remains /api/notify_staff so no changes are needed
    on the client (footer) side.
    """
    try:
        data = request.get_json(silent=True) or {}
        chat_ref = data.get('chat_ref') or str(uuid.uuid4())
        visitor_message = data.get('visitor_message', '(no message)')
        # prefer any explicit link from the client; otherwise direct to site root with chat_ref
        link = data.get('link') or (request.host_url.rstrip('/') + '/?chat_ref=' + chat_ref)

        # ensure the visitor message is stored immediately so agents can load the transcript
        chat_storage.setdefault(chat_ref, []).append({
            'who': 'user', 'text': visitor_message, 'ts': datetime.utcnow().isoformat()
        })

        # DEBUG LOG: record that notify_staff stored the message (remove later if desired)
        try:
            app.logger.info("notify_staff stored chat_ref=%s messages_count=%d visitor_preview=%s",
                            chat_ref, len(chat_storage.get(chat_ref, [])), (visitor_message or "")[:120])
        except Exception:
            pass

        # notify any connected sockets in the chat room (best-effort)
        try:
            socketio.emit('new_message', {'who': 'user', 'text': visitor_message}, room=chat_ref)
        except Exception:
            app.logger.exception("emit failed while saving visitor message in api_notify_staff_v2")

        subject = f"Live chat waiting — ref {chat_ref}"
        # simple html + text body (keeps it brief)
        html_body = render_template('email_chat_notification.html', chat_ref=chat_ref,
                                    visitor_message=visitor_message, link=link, timestamp=datetime.utcnow())
        # fallback text
        text_body = f"Live chat waiting (ref {chat_ref})\n\nMessage: {visitor_message}\n\nOpen: {link}\n\n-- Care Companion"

        recipients = ['charleyomeneki@gmail.com', 'carecompanion@zohomail.com']

        # Use async sender helper to avoid blocking; returns quickly
        try:
            _send_email_async(subject, recipients, html_body, text_body=text_body)
            return jsonify({"ok": True, "sent": True}), 200
        except Exception:
            sent_ok = _safe_send_email(subject, recipients, text_body=text_body, html_body=html_body)
            return jsonify({"ok": bool(sent_ok), "sent": bool(sent_ok)}), (200 if sent_ok else 500)

    except Exception as e:
        app.logger.exception("api_notify_staff_v2 failed: %s", e)
        return jsonify({"ok": False, "error": "Internal server error", "trace": traceback.format_exc()[:1000]}), 500



# Simple in-memory chat store (dev only; not persisted across restarts)
chat_storage = {}  # { chat_ref: [ { who:'user'|'agent'|'bot', text: '...', ts: <iso> }, ... ] }

from flask_socketio import join_room, emit





@socketio.on('livechat_join')
def handle_livechat_join(data):
    try:
        ref = data.get('chat_ref')
        if not ref:
            return

        join_room(ref)

        app.logger.info(
            "LIVECHAT JOIN: SID=%s ROOM=%s",
            request.sid,
            ref
        )

    except Exception:
        app.logger.exception("handle_livechat_join failed")


# When visitor sends a message via socket, server should include chat_ref
@socketio.on('visitor_message')
def handle_visitor_message(data):
    """
    data = { chat_ref: 'uuid', text: 'message' }
    """
    try:
        ref = data.get('chat_ref')
        text = data.get('text', '')
        client_msg_id = data.get('client_msg_id') 
        if not ref:
            return
        chat_storage.setdefault(ref, []).append({'who':'user', 'text': text, 'ts': datetime.utcnow().isoformat()})
        # forward message to room (agents)
        emit(
            'new_message',
            {
                'who': 'user',
                'text': text,
                'client_msg_id': client_msg_id,
                'ts': datetime.utcnow().isoformat()
            },
            room=ref
        )        
    except Exception:
        app.logger.exception("handle_visitor_message failed")


@socketio.on('visitor_typing')
def handle_visitor_typing(data):
    try:
        app.logger.info("=== VISITOR_TYPING RECEIVED === %s", data)

        ref = data.get('chat_ref')
        if not ref:
            app.logger.warning("visitor_typing: Missing chat_ref")
            return

        app.logger.info("Broadcasting visitor_typing to room: %s", ref)

        emit(
            'visitor_typing',
            {'chat_ref': ref},
            room=ref,
            include_self=False
        )

        app.logger.info("visitor_typing broadcast complete")

    except Exception:
        app.logger.exception("handle_visitor_typing failed")


@socketio.on('visitor_stop_typing')
def handle_visitor_stop_typing(data):
    try:
        app.logger.info("=== VISITOR_STOP_TYPING RECEIVED === %s", data)

        ref = data.get('chat_ref')
        if not ref:
            app.logger.warning("visitor_stop_typing: Missing chat_ref")
            return

        emit(
            'visitor_stop_typing',
            {'chat_ref': ref},
            room=ref,
            include_self=False
        )

        app.logger.info("visitor_stop_typing broadcast complete")

    except Exception:
        app.logger.exception("handle_visitor_stop_typing failed")


@socketio.on('agent_typing')
def handle_agent_typing(data):
    try:
        app.logger.info("=== AGENT_TYPING RECEIVED === %s", data)

        ref = data.get('chat_ref')
        if not ref:
            app.logger.warning("agent_typing: Missing chat_ref")
            return

        app.logger.info("Broadcasting agent_typing to room: %s", ref)

        emit(
            'agent_typing',
            {'chat_ref': ref},
            room=ref,
            include_self=False
        )

        app.logger.info("agent_typing broadcast complete")

    except Exception:
        app.logger.exception("handle_agent_typing failed")


@socketio.on('agent_stop_typing')
def handle_agent_stop_typing(data):
    try:
        app.logger.info("=== AGENT_STOP_TYPING RECEIVED === %s", data)

        ref = data.get('chat_ref')
        if not ref:
            app.logger.warning("agent_stop_typing: Missing chat_ref")
            return

        emit(
            'agent_stop_typing',
            {'chat_ref': ref},
            room=ref,
            include_self=False
        )

        app.logger.info("agent_stop_typing broadcast complete")

    except Exception:
        app.logger.exception("handle_agent_stop_typing failed")



# Agent joins room
@socketio.on('agent_join')
def handle_agent_join(data):
    try:
        ref = data.get('chat_ref')
        if not ref:
            return

        join_room(ref)

        chat_storage.setdefault(ref, []).append({
            'who': 'agent',
            'text': 'Agent joined room',
            'ts': datetime.utcnow().isoformat()
        })

        # Notify everyone that the agent joined
        emit(
            'agent_joined',
            {'chat_ref': ref},
            room=ref
        )

        # Also show a chat message
        emit(
            'new_message',
            {
                'who': 'agent',
                'text': 'An agent has joined the chat.'
            },
            room=ref
        )

    except Exception:
        app.logger.exception("handle_agent_join failed")

# Agent sends message
@socketio.on('agent_message')
def handle_agent_message(data):
    try:
        ref = data.get('chat_ref')
        text = data.get('text', '')
        if not ref:
            return
        chat_storage.setdefault(ref, []).append({'who':'agent', 'text': text, 'ts': datetime.utcnow().isoformat()})
        emit('new_message', {'who':'agent', 'text': text}, room=ref)
    except Exception:
        app.logger.exception("handle_agent_message failed")

# Transcript endpoint used by client to load previous messages
@app.route('/api/chat_transcript', methods=['GET'])
def api_chat_transcript():
    ref = request.args.get('ref') or request.args.get('chat_ref')
    try:
        app.logger.info("chat_transcript requested ref=%s from %s", ref, request.remote_addr)
    except Exception:
        pass

    if not ref:
        return jsonify({"ok": False, "messages": []}), 400
    msgs = chat_storage.get(ref, [])
    try:
        app.logger.info("chat_transcript returning %d messages for ref=%s", len(msgs), ref)
    except Exception:
        pass
    return jsonify({"ok": True, "messages": msgs}), 200

# Temporary debug endpoint (remove when done)
@app.route('/debug/chat_storage', methods=['GET'])
def debug_chat_storage():
    # usage: /debug/chat_storage?ref=<chat_ref>
    ref = request.args.get('ref')
    msgs = chat_storage.get(ref, [])
    return jsonify({"ok": True, "ref": ref, "count": len(msgs), "messages": msgs}), 200

@app.route('/api/chat_message', methods=['POST'])
def api_chat_message():
    """
    Accepts JSON: { chat_ref, text, who='user' }
    Stores message into chat_storage and emits to room (so agents can receive it).
    """
    try:
        data = request.get_json(silent=True) or {}
        ref = data.get('chat_ref')
        text = data.get('text', '') or ''
        who = data.get('who', 'user')
        if not ref:
            return jsonify({"ok": False, "error": "missing chat_ref"}), 400

        # store in memory
        chat_storage.setdefault(ref, []).append({
            'who': who,
            'text': text,
            'ts': datetime.utcnow().isoformat()
        })

        # broadcast to any connected sockets in the room (best-effort)
        try:
            socketio.emit('new_message', {'who': who, 'text': text}, room=ref)
        except Exception:
            app.logger.exception("emit failed in /api/chat_message")

        return jsonify({"ok": True}), 200
    except Exception:
        app.logger.exception("api_chat_message failed")
        return jsonify({"ok": False, "error": "internal error"}), 500


@app.route('/api/end_chat', methods=['POST'])
def api_end_chat():
    try:
        data = request.get_json(silent=True) or {}

        ref = data.get('chat_ref')
        ended_by = data.get('ended_by', 'user')

        if not ref:
            return jsonify({"ok": False}), 400

        chat_storage.setdefault(ref, []).append({
            'who': 'system',
            'text': f'{ended_by.capitalize()} ended the chat.',
            'ts': datetime.utcnow().isoformat()
        })

        return jsonify({"ok": True})

    except Exception:
        app.logger.exception("api_end_chat failed")
        return jsonify({"ok": False}), 500

# ---------------- Live chat page route + small socket join handler ----------------
from markupsafe import Markup


@app.route('/live-chat')
def live_chat():
    """
    Renders the modern live chat page.
    Optional query params:
      - chat_ref: to load an existing chat (visitor or agent)
      - agent=1 : mark this page as an agent view (opens panel ready to send)
    """
    chat_ref = request.args.get('chat_ref') or ''
    is_agent = request.args.get('agent') in ('1', 'true', 'True')
    # Pass these into the template so client JS can join the right room
    return render_template('live_chat.html', chat_ref=chat_ref, is_agent=is_agent)

# allow sockets to join a room for visitor or other clients
@socketio.on('join_live_chat')
def handle_join_room(data):
    """
    client should send { chat_ref: 'xxx' } to be added to the room so they receive 'new_message' emits.
    Works for both visitor and agent.
    """
    try:
        ref = data.get('chat_ref') if isinstance(data, dict) else None
        if not ref:
            return
        join_room(ref)
        # Optionally notify room participants that someone joined (system message)
        emit('new_message', {'who': 'system', 'text': 'A participant joined the chat.'}, room=ref)
    except Exception:
        app.logger.exception("handle_join_room failed")
# -------------------------------------------------------------------------------

@socketio.on('end_chat')
def handle_end_chat(data):
    try:
        ref = data.get('chat_ref')
        ended_by = data.get('ended_by', 'user')

        if not ref:
            return

        # Save the event in the transcript
        chat_storage.setdefault(ref, []).append({
            'who': 'system',
            'text': f'{ended_by.capitalize()} ended the chat.',
            'ts': datetime.utcnow().isoformat()
        })

        # Notify everyone in the room (visitor and agent)
        emit(
            'chat_ended',
            {
                'chat_ref': ref,
                'ended_by': ended_by
            },
            room=ref
        )

    except Exception:
        app.logger.exception("handle_end_chat failed")



@socketio.on("connect")
def socket_connect():
    try:
        me = get_current_user()

        if me:
            join_room(f"user_{me['id']}")
            _set_user_online(me["id"])
            app.logger.info(
                "Authenticated socket connected: user_%s",
                me["id"]
            )
        else:
            # Allow anonymous live-chat visitors.
            app.logger.info(
                "Anonymous live-chat socket connected: %s",
                request.sid
            )

        return True

    except Exception:
        app.logger.exception("socket_connect failed")
        return False

@socketio.on("disconnect")
def socket_disconnect():
    try:
        uid = session.get("user_id")
        if uid:
            _set_user_offline(uid)
    except Exception:
        pass





from collections import deque
from threading import Lock

PUSH_DEBUG_LOG = deque(maxlen=200)
PUSH_DEBUG_LOCK = Lock()

@app.route("/api/push/log", methods=["POST"])
def api_push_log():
    me = get_current_user()
    payload = request.get_json(silent=True) or {}

    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "user_id": me["id"] if me else None,
        "origin": request.headers.get("Origin"),
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "payload": payload,
    }

    with PUSH_DEBUG_LOCK:
        PUSH_DEBUG_LOG.append(entry)

    return jsonify({"ok": True})

@app.route("/api/push/logs")
def api_push_logs():
    me = get_current_user()
    if not me:
        return jsonify({"error": "unauthenticated"}), 401

    with PUSH_DEBUG_LOCK:
        logs = list(PUSH_DEBUG_LOG)[-50:]

    return jsonify({"ok": True, "logs": logs})


if __name__ == "__main__":
    # Use socketio.run to start the Socket.IO server
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

 


