import re
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db

# Matches the cooperative's existing membership code format, e.g. BC08210002
MEMBERSHIP_CODE_PATTERN = re.compile(r"^BC(\d{8})$")
MEMBERSHIP_CODE_PREFIX = "BC"
MEMBERSHIP_CODE_DIGITS = 8
# Fallback starting point if the database has no existing BC-numbered codes yet
# (e.g. before the historical member register has been bulk-imported).
MEMBERSHIP_CODE_FALLBACK_START = 8210001


def generate_membership_code():
    """
    Generate the next membership code in the cooperative's existing orderly sequence
    (BC + 8-digit number, e.g. BC08210002, BC08210003, ...) rather than a random one.
    Continues from whatever the highest existing BC-numbered code in the database is.
    """
    highest = MEMBERSHIP_CODE_FALLBACK_START
    for (code,) in db.session.query(User.membership_code).filter(User.membership_code.isnot(None)):
        match = MEMBERSHIP_CODE_PATTERN.match(code or "")
        if match:
            highest = max(highest, int(match.group(1)))

    next_number = highest + 1
    candidate = f"{MEMBERSHIP_CODE_PREFIX}{next_number:0{MEMBERSHIP_CODE_DIGITS}d}"
    # Skip past any that somehow already exist (e.g. gaps filled in manually), staying orderly.
    while User.query.filter_by(membership_code=candidate).first():
        next_number += 1
        candidate = f"{MEMBERSHIP_CODE_PREFIX}{next_number:0{MEMBERSHIP_CODE_DIGITS}d}"
    return candidate


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=True)  # admin's fixed login name (e.g. "admin")
    membership_code = db.Column(db.String(50), unique=True, nullable=True)
    full_name = db.Column(db.String(150), nullable=False)
    account_number = db.Column(db.String(20), unique=True, nullable=False)
    phone_number = db.Column(db.String(20), nullable=True)  # contact only — not unique, not used for login
    office_number = db.Column(db.String(50), nullable=True)  # office/work phone extension
    email = db.Column(db.String(150), nullable=True)
    work_position = db.Column(db.String(150), nullable=True)
    savings_balance = db.Column(db.Float, default=0.0, nullable=False)
    passport_path = db.Column(db.String(255), nullable=True)
    signature_path = db.Column(db.String(255), nullable=True)
    nin_path = db.Column(db.String(255), nullable=True)  # scanned/photographed NIN slip or card

    # Remaining fields from the official Membership Application Form
    marital_status = db.Column(db.String(20), nullable=True)  # Single / Married / Divorced / Widowed
    sex = db.Column(db.String(10), nullable=True)  # Male / Female
    employer = db.Column(db.String(150), nullable=True)
    bankers_branch = db.Column(db.String(150), nullable=True)
    application_fee = db.Column(db.Float, nullable=True)
    starting_contribution = db.Column(db.Float, nullable=True)
    deduction_due_date = db.Column(db.String(50), nullable=True)
    preferred_deduction_month_year = db.Column(db.String(50), nullable=True)
    next_of_kin_name = db.Column(db.String(150), nullable=True)
    next_of_kin_address = db.Column(db.String(255), nullable=True)
    next_of_kin_phone = db.Column(db.String(20), nullable=True)

    password_hash = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), default="Member", nullable=False)  # Member / Admin
    account_status = db.Column(db.String(20), default="Pending", nullable=False)  # Pending / Active / Rejected
    is_profile_complete = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    loans = db.relationship(
        "Loan", back_populates="member", lazy=True, cascade="all, delete-orphan",
        foreign_keys="Loan.user_id"
    )
    guarantor_slots = db.relationship(
        "LoanGuarantor", back_populates="guarantor", lazy=True,
        foreign_keys="LoanGuarantor.guarantor_id",
        cascade="all, delete-orphan"
    )
    votes = db.relationship("Vote", backref="member", lazy=True, cascade="all, delete-orphan")
    gift_preference = db.relationship(
        "GiftPreference", backref="member", uselist=False, cascade="all, delete-orphan"
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_admin(self):
        return self.role == "Admin"


class LoanType(db.Model):
    """A loan product offered by the cooperative, with its own eligibility clause."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    clause = db.Column(db.Text, nullable=False)  # human-readable rules shown to members
    # How the max loanable amount is determined:
    #   "savings_multiple" -> savings_balance * multiplier
    #   "fixed"             -> fixed_max_amount (admin-editable, e.g. seasonal/annual caps)
    basis = db.Column(db.String(20), nullable=False, default="fixed")
    multiplier = db.Column(db.Float, nullable=True)       # used when basis == savings_multiple
    fixed_max_amount = db.Column(db.Float, nullable=True)  # used when basis == fixed
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_seasonal = db.Column(db.Boolean, default=False, nullable=False)
    season_label = db.Column(db.String(150), nullable=True)  # e.g. "Christmas 2026"
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    options = db.relationship(
        "LoanTypeOption", backref="loan_type", lazy=True,
        cascade="all, delete-orphan", order_by="LoanTypeOption.tenure_months"
    )

    def max_amount_for(self, user):
        if self.basis == "savings_multiple":
            return (self.multiplier or 0) * (user.savings_balance or 0)
        return self.fixed_max_amount or 0


class LoanTypeOption(db.Model):
    """A tenure/interest-rate pairing available under a given loan type."""

    id = db.Column(db.Integer, primary_key=True)
    loan_type_id = db.Column(db.Integer, db.ForeignKey("loan_type.id"), nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    interest_rate = db.Column(db.Float, nullable=False)  # flat rate, percent


class Loan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    loan_type_id = db.Column(db.Integer, db.ForeignKey("loan_type.id"), nullable=True)
    loan_type_name = db.Column(db.String(100), nullable=False)  # snapshot, survives type edits/deletes
    amount = db.Column(db.Float, nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    interest_rate = db.Column(db.Float, nullable=False)  # snapshot at time of application
    status = db.Column(db.String(20), default="Pending", nullable=False)  # Pending / Approved / Declined
    admin_condition = db.Column(db.Text, nullable=True)
    # Scanned/photographed copy of the signed physical loan application form. Nullable at the DB
    # level (so this doesn't break an already-deployed database that predates this field), but
    # enforced as required by the /member/loan/apply route itself.
    receipt_path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime, nullable=True)

    member = db.relationship("User", back_populates="loans", foreign_keys=[user_id])
    guarantors = db.relationship(
        "LoanGuarantor", back_populates="loan", lazy=True,
        cascade="all, delete-orphan", order_by="LoanGuarantor.id"
    )

    @property
    def total_repayment(self):
        return round(self.amount + (self.amount * self.interest_rate / 100), 2)

    @property
    def all_guarantors_accepted(self):
        return len(self.guarantors) > 0 and all(g.status == "Accepted" for g in self.guarantors)

    @property
    def any_guarantor_declined(self):
        return any(g.status == "Declined" for g in self.guarantors)


class LoanGuarantor(db.Model):
    """One of the two guarantors required on the official loan application form,
    each vouching for a specific portion of the loan amount."""

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loan.id"), nullable=False)
    guarantor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount_guaranteed = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="Pending", nullable=False)  # Pending / Accepted / Declined
    responded_at = db.Column(db.DateTime, nullable=True)

    loan = db.relationship("Loan", back_populates="guarantors")
    guarantor = db.relationship("User", back_populates="guarantor_slots", foreign_keys=[guarantor_id])


class ElectionSettings(db.Model):
    """Singleton row controlling voting status: Open, Paused, or Closed. Admin-controlled only."""

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(10), default="Closed", nullable=False)  # Open / Paused / Closed

    @property
    def is_active(self):
        """True only when voting is actually Open (not Paused or Closed)."""
        return self.status == "Open"


class GiftSettings(db.Model):
    """Singleton row controlling whether gift preference selection is currently open to members."""

    id = db.Column(db.Integer, primary_key=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)


class Position(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    candidates = db.relationship(
        "Candidate", backref="position", lazy=True, cascade="all, delete-orphan"
    )


class Candidate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    position_id = db.Column(db.Integer, db.ForeignKey("position.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    votes = db.relationship("Vote", backref="candidate", lazy=True, cascade="all, delete-orphan")


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    position_id = db.Column(db.Integer, db.ForeignKey("position.id"), nullable=False)
    candidate_id = db.Column(db.Integer, db.ForeignKey("candidate.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "position_id", name="_user_position_uc"),)


class GiftPreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    preference_type = db.Column(db.String(50), nullable=False)  # Physical / Monetized
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
