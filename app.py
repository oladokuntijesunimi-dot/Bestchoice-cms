import os
import io
import mimetypes
import secrets
import string
from datetime import datetime

from flask import (
    Flask, render_template, redirect, url_for, request, flash, send_file, abort, jsonify, session
)
from flask_login import (
    login_user, logout_user, login_required, current_user
)
from dotenv import load_dotenv

from extensions import db, login_manager, socketio
from models import (
    User, Loan, ElectionSettings, GiftSettings, Position, Candidate, Vote, GiftPreference,
    LoanType, LoanTypeOption, LoanGuarantor, generate_membership_code
)
from utils import (
    save_upload, delete_upload, get_upload_bytes, build_member_profile_pdf,
    build_members_excel, build_members_csv_minimal, build_gift_preferences_excel, parse_bulk_import_file, send_email,
    build_member_brief_pdf, build_members_brief_pdf_combined,
    ALLOWED_RECEIPT_EXTENSIONS
)

load_dotenv()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "cms.db"))
    # Render/Heroku-style URLs sometimes start with postgres:// ; SQLAlchemy needs postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.config["MAX_UPLOAD_MB"] = float(os.environ.get("MAX_UPLOAD_MB", 5))
    os.makedirs(app.instance_path, exist_ok=True)

    app.config["DEFAULT_MEMBER_PASSWORD"] = os.environ.get("DEFAULT_MEMBER_PASSWORD", "Molete2026")

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please log in to continue."
    socketio.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    with app.app_context():
        db.create_all()
        _seed_admin(app)
        _seed_loan_types()
        if not ElectionSettings.query.first():
            db.session.add(ElectionSettings(status="Closed"))
            db.session.commit()
        if not GiftSettings.query.first():
            db.session.add(GiftSettings(is_active=False))
            db.session.commit()

    register_routes(app)
    return app


def _seed_loan_types():
    if LoanType.query.first():
        return

    personal = LoanType(
        name="Personal Loan",
        clause="For general welfare or productive use. Maximum: 2x your total savings balance.",
        basis="savings_multiple", multiplier=2.0, is_active=True, sort_order=1,
    )
    personal.options = [
        LoanTypeOption(tenure_months=12, interest_rate=10.0),
        LoanTypeOption(tenure_months=18, interest_rate=15.0),
    ]

    emergency = LoanType(
        name="Emergency Loan",
        clause="For urgent needs. Maximum: your total savings balance. Flat 5% interest, up to 6 months.",
        basis="savings_multiple", multiplier=1.0, is_active=True, sort_order=2,
    )
    emergency.options = [LoanTypeOption(tenure_months=6, interest_rate=5.0)]

    telephone = LoanType(
        name="Telephone Loan",
        clause="Special loan capped at \u20a6150,000. Flat 10% interest per annum, up to 12 months.",
        basis="fixed", fixed_max_amount=150000, is_active=True, sort_order=3,
    )
    telephone.options = [LoanTypeOption(tenure_months=12, interest_rate=10.0)]

    household = LoanType(
        name="Household Loan",
        clause="Offered exclusively in December each year. Maximum amount is set annually by the "
               "Executive Committee. Flat 10% interest per annum, up to 12 months.",
        basis="fixed", fixed_max_amount=0, is_active=False, is_seasonal=True, sort_order=4,
    )
    household.options = [LoanTypeOption(tenure_months=12, interest_rate=10.0)]

    seasonal = LoanType(
        name="Commodity & Seasonal Loan",
        clause="Short-cycle facility for specific occasions (e.g. Christmas, Ileya, Itunu-Awe). "
               "Maximum amount and terms are set by the Executive Committee before each launch period.",
        basis="fixed", fixed_max_amount=0, is_active=False, is_seasonal=True, sort_order=5,
    )
    seasonal.options = [LoanTypeOption(tenure_months=3, interest_rate=5.0)]

    db.session.add_all([personal, emergency, telephone, household, seasonal])
    db.session.commit()


def _seed_admin(app):
    admin_username = os.environ.get("ADMIN_USERNAME", "admin").strip()
    admin_phone = os.environ.get("ADMIN_PHONE")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if not admin_password:
        return

    existing = User.query.filter(
        (User.username == admin_username) |
        ((User.account_number == admin_phone) if admin_phone else False)
    ).first()
    if existing:
        # Backfill username if this admin was seeded before this field existed.
        updated = False
        if not existing.username and admin_username:
            existing.username = admin_username
            updated = True
        # If an admin phone/account was provided in the env, keep the record in sync.
        if admin_phone and existing.account_number != admin_phone:
            existing.account_number = admin_phone
            updated = True
        # If ADMIN_PASSWORD is provided, update the stored hash when it differs.
        if admin_password and not existing.check_password(admin_password):
            existing.set_password(admin_password)
            updated = True
        if updated:
            db.session.commit()
        return

    admin = User(
        username=admin_username,
        full_name=os.environ.get("ADMIN_NAME", "Administrator"),
        account_number=admin_phone or f"admin-{admin_username}",
        role="Admin",
        account_status="Active",
        is_profile_complete=True,
    )
    admin.set_password(admin_password)
    db.session.add(admin)
    db.session.commit()


def _name_taken(full_name, exclude_id=None):
    query = User.query.filter(db.func.lower(User.full_name) == full_name.strip().lower())
    if exclude_id:
        query = query.filter(User.id != exclude_id)
    return query.first() is not None


def register_routes(app):

    # ---------- before_request: force profile completion ----------
    @app.before_request
    def enforce_profile_completion():
        if not current_user.is_authenticated or current_user.is_admin:
            return
        exempt_endpoints = {"complete_profile", "logout", "static", "secure_file"}
        if request.endpoint in exempt_endpoints:
            return
        if not current_user.is_profile_complete:
            return redirect(url_for("complete_profile"))

    # ---------- public ----------
    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("admin_dashboard") if current_user.is_admin else url_for("member_dashboard"))
        return render_template("login.html")

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            account_number = request.form.get("account_number", "").strip()
            phone_number = request.form.get("phone_number", "").strip()
            work_position = request.form.get("work_position", "").strip()
            email = request.form.get("email", "").strip()

            if not all([full_name, account_number, phone_number, work_position, email]):
                flash("Full name, account number, phone number, work position, and email are all required.", "error")
                return render_template("signup.html")
            if "@" not in email or "." not in email.split("@")[-1]:
                flash("Please enter a valid email address.", "error")
                return render_template("signup.html")
            if User.query.filter_by(account_number=account_number).first():
                flash("An account with this account number already exists.", "error")
                return render_template("signup.html")
            if _name_taken(full_name):
                flash("An account with this exact full name already exists. Since members log in by "
                      "name, please include a middle name or distinguishing detail, or contact the admin.", "error")
                return render_template("signup.html")

            try:
                passport_path = save_upload(request.files.get("passport"), "passports")
                signature_path = save_upload(request.files.get("signature"), "signatures")
                nin_path = save_upload(request.files.get("nin"), "nin_documents", allowed_extensions=ALLOWED_RECEIPT_EXTENSIONS)
            except ValueError as e:
                flash(str(e), "error")
                return render_template("signup.html")

            if not passport_path or not signature_path:
                flash("Passport photograph and digital signature are both required.", "error")
                return render_template("signup.html")
            if not nin_path:
                flash("A photo or scan of your NIN (National Identification Number) document is required.", "error")
                return render_template("signup.html")

            user = User(
                full_name=full_name,
                account_number=account_number,
                phone_number=phone_number,
                work_position=work_position,
                email=email,
                passport_path=passport_path,
                signature_path=signature_path,
                nin_path=nin_path,
                role="Member",
                account_status="Pending",
                is_profile_complete=False,
            )
            user.set_password(current_app_default_password())
            db.session.add(user)
            db.session.commit()
            flash("Registration submitted! An admin will review and approve your account. "
                  "You'll receive your Membership Code and a default password by email once approved.", "success")
            return redirect(url_for("login"))

        return render_template("signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("admin_dashboard") if current_user.is_admin else url_for("member_dashboard"))
        if request.method == "POST":
            identifier = request.form.get("identifier", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter(
                (User.username == identifier) |
                (db.func.lower(User.membership_code) == identifier.lower())
            ).first()

            if not user or not user.check_password(password):
                flash("Invalid credentials.", "error")
                return render_template("login.html")
            if user.account_status == "Pending":
                flash("Your account is still pending admin approval.", "error")
                return render_template("login.html")
            if user.account_status == "Rejected":
                flash("Your registration was not approved. Please contact the admin.", "error")
                return render_template("login.html")

            login_user(user)
            return redirect(url_for("admin_dashboard") if user.is_admin else url_for("member_dashboard"))
        return render_template("login.html")

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if current_user.is_authenticated:
            return redirect(url_for("admin_dashboard") if current_user.is_admin else url_for("member_dashboard"))

        if request.method == "POST":
            form_token = request.form.get("forgot_password_token")
            saved_token = session.pop("forgot_password_token", None)
            if not form_token or form_token != saved_token:
                flash("This password reset request was already processed. If you still need help, try again.", "success")
                return redirect(url_for("login"))

            email = request.form.get("email", "").strip()
            if not email:
                flash("Please enter your email address.", "error")
                token = generate_forgot_password_token()
                session["forgot_password_token"] = token
                return render_template("forgot_password.html", forgot_password_token=token)

            user = User.query.filter(db.func.lower(User.email) == email.lower()).first()
            if user and user.email:
                temp_password = generate_temporary_password()
                user.set_password(temp_password)
                db.session.commit()
                email_sent = send_email(
                    to_email=user.email,
                    subject="Password reset for Best Choice Cooperative",
                    body=(
                        f"Hello {user.full_name},\n\n"
                        f"Your password has been reset. Use the temporary password below to log in:\n\n"
                        f"{temp_password}\n\n"
                        "Please change your password after logging in.\n\n"
                        "Best Choice Cooperative"
                    )
                )
                if email_sent:
                    flash("A password reset email has been sent if that address exists.", "success")
                else:
                    flash("Password reset completed, but we could not send email. Please contact the admin.", "error")
            else:
                flash("If that address is registered, a password reset email has been sent.", "success")
            return redirect(url_for("login"))

        session["forgot_password_token"] = generate_forgot_password_token()
        return render_template("forgot_password.html", forgot_password_token=session["forgot_password_token"])

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/complete-profile", methods=["GET", "POST"])
    @login_required
    def complete_profile():
        if current_user.is_profile_complete:
            return redirect(url_for("member_dashboard"))

        needs_work_position = not current_user.work_position
        needs_passport = not current_user.passport_path
        needs_signature = not current_user.signature_path
        needs_nin = not current_user.nin_path
        needs_office_number = not current_user.office_number
        needs_marital_status = not current_user.marital_status
        needs_sex = not current_user.sex
        needs_employer = not current_user.employer
        needs_bankers_branch = not current_user.bankers_branch
        needs_application_fee = current_user.application_fee is None
        needs_starting_contribution = current_user.starting_contribution is None
        needs_deduction_due_date = not current_user.deduction_due_date
        needs_preferred_deduction_month_year = not current_user.preferred_deduction_month_year
        needs_next_of_kin = not current_user.next_of_kin_name
        template_kwargs = dict(
            needs_work_position=needs_work_position,
            needs_passport=needs_passport,
            needs_signature=needs_signature,
            needs_nin=needs_nin,
            needs_office_number=needs_office_number,
            needs_email=not current_user.email,
            needs_marital_status=needs_marital_status,
            needs_sex=needs_sex,
            needs_employer=needs_employer,
            needs_bankers_branch=needs_bankers_branch,
            needs_application_fee=needs_application_fee,
            needs_starting_contribution=needs_starting_contribution,
            needs_deduction_due_date=needs_deduction_due_date,
            needs_preferred_deduction_month_year=needs_preferred_deduction_month_year,
            needs_next_of_kin=needs_next_of_kin,
        )

        if request.method == "POST":
            new_password = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")

            if needs_work_position:
                work_position = request.form.get("work_position", "").strip()
                if not work_position:
                    flash("Work position is required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.work_position = work_position

            if needs_office_number:
                office_number = request.form.get("office_number", "").strip()
                if not office_number:
                    flash("Office number is required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.office_number = office_number

            if needs_marital_status:
                marital_status = request.form.get("marital_status", "").strip()
                if marital_status not in ("Single", "Married", "Divorced", "Widowed"):
                    flash("Please select a valid marital status.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.marital_status = marital_status

            if needs_sex:
                sex = request.form.get("sex", "").strip()
                if sex not in ("Male", "Female"):
                    flash("Please select a valid sex.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.sex = sex

            if needs_employer:
                employer = request.form.get("employer", "").strip()
                if not employer:
                    flash("Employer is required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.employer = employer

            if needs_bankers_branch:
                bankers_branch = request.form.get("bankers_branch", "").strip()
                if not bankers_branch:
                    flash("Bankers/Branch is required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.bankers_branch = bankers_branch

            if needs_application_fee:
                raw = request.form.get("application_fee", "").strip()
                try:
                    application_fee = float(raw)
                    if application_fee < 0:
                        raise ValueError
                except ValueError:
                    flash("Please enter a valid application & development fee amount.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.application_fee = application_fee

            if needs_starting_contribution:
                raw = request.form.get("starting_contribution", "").strip()
                try:
                    starting_contribution = float(raw)
                    if starting_contribution < 0:
                        raise ValueError
                except ValueError:
                    flash("Please enter a valid starting contribution amount.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.starting_contribution = starting_contribution
                current_user.savings_balance = (current_user.savings_balance or 0.0) + starting_contribution

            if needs_deduction_due_date:
                deduction_due_date = request.form.get("deduction_due_date", "").strip()
                if not deduction_due_date:
                    flash("Deduction due date is required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.deduction_due_date = deduction_due_date

            if needs_preferred_deduction_month_year:
                preferred = request.form.get("preferred_deduction_month_year", "").strip()
                if not preferred:
                    flash("Preferred month of deduction and year is required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.preferred_deduction_month_year = preferred

            if needs_next_of_kin:
                nok_name = request.form.get("next_of_kin_name", "").strip()
                nok_address = request.form.get("next_of_kin_address", "").strip()
                nok_phone = request.form.get("next_of_kin_phone", "").strip()
                if not nok_name or not nok_address or not nok_phone:
                    flash("Next of Kin name, address, and phone are all required.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.next_of_kin_name = nok_name
                current_user.next_of_kin_address = nok_address
                current_user.next_of_kin_phone = nok_phone

            if not current_user.email:
                email = request.form.get("email", "").strip() or None
                if email and ("@" not in email or "." not in email.split("@")[-1]):
                    flash("Please enter a valid email address, or leave it blank.", "error")
                    return render_template("complete_profile.html", **template_kwargs)
                current_user.email = email

            if new_password != confirm or len(new_password) < 6:
                flash("Passwords must match and be at least 6 characters.", "error")
                return render_template("complete_profile.html", **template_kwargs)

            if needs_passport or needs_signature or needs_nin:
                try:
                    if needs_passport:
                        passport_path = save_upload(request.files.get("passport"), "passports")
                        if not passport_path:
                            flash("Passport photograph is required.", "error")
                            return render_template("complete_profile.html", **template_kwargs)
                        current_user.passport_path = passport_path
                    if needs_signature:
                        signature_path = save_upload(request.files.get("signature"), "signatures")
                        if not signature_path:
                            flash("Digital signature is required.", "error")
                            return render_template("complete_profile.html", **template_kwargs)
                        current_user.signature_path = signature_path
                    if needs_nin:
                        nin_path = save_upload(request.files.get("nin"), "nin_documents", allowed_extensions=ALLOWED_RECEIPT_EXTENSIONS)
                        if not nin_path:
                            flash("A photo or scan of your NIN (National Identification Number) document is required.", "error")
                            return render_template("complete_profile.html", **template_kwargs)
                        current_user.nin_path = nin_path
                except ValueError as e:
                    flash(str(e), "error")
                    return render_template("complete_profile.html", **template_kwargs)

            current_user.set_password(new_password)
            current_user.is_profile_complete = True
            db.session.commit()
            flash("Profile completed successfully!", "success")
            return redirect(url_for("member_dashboard"))

        return render_template("complete_profile.html", **template_kwargs)

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not current_user.check_password(current_pw):
                flash("Current password is incorrect.", "error")
            elif len(new_pw) < 6 or new_pw != confirm_pw:
                flash("New passwords must match and be at least 6 characters.", "error")
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash("Password changed successfully.", "success")
                return redirect(url_for("admin_dashboard") if current_user.is_admin else url_for("member_dashboard"))
        return render_template("change_password.html")

    # ---------- secure file serving ----------
    @app.route("/secure-file/<int:user_id>/<kind>")
    @login_required
    def secure_file(user_id, kind):
        if kind not in ("passport", "signature", "nin"):
            abort(404)
        if not current_user.is_admin and current_user.id != user_id:
            abort(403)
        user = db.session.get(User, user_id)
        if not user:
            abort(404)
        rel_path = {
            "passport": user.passport_path,
            "signature": user.signature_path,
            "nin": user.nin_path,
        }[kind]
        if not rel_path:
            abort(404)
        file_bytes = get_upload_bytes(rel_path)
        if file_bytes is None:
            abort(404)
        mimetype = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
        return send_file(io.BytesIO(file_bytes), mimetype=mimetype,
                          download_name=rel_path.rsplit("/", 1)[-1])

    @app.route("/secure-file/loan-receipt/<int:loan_id>")
    @login_required
    def loan_receipt_file(loan_id):
        loan = db.session.get(Loan, loan_id)
        if not loan:
            abort(404)
        if not current_user.is_admin and current_user.id != loan.user_id:
            abort(403)
        if not loan.receipt_path:
            abort(404)
        file_bytes = get_upload_bytes(loan.receipt_path)
        if file_bytes is None:
            abort(404)
        mimetype = mimetypes.guess_type(loan.receipt_path)[0] or "application/octet-stream"
        return send_file(io.BytesIO(file_bytes), mimetype=mimetype,
                          download_name=loan.receipt_path.rsplit("/", 1)[-1])

    # =====================================================================
    # MEMBER ROUTES
    # =====================================================================
    @app.route("/member/dashboard")
    @login_required
    def member_dashboard():
        if current_user.is_admin:
            return redirect(url_for("admin_dashboard"))
        loans = Loan.query.filter_by(user_id=current_user.id).order_by(Loan.created_at.desc()).all()
        election = ElectionSettings.query.first()
        positions = Position.query.all()
        my_votes = {v.position_id for v in Vote.query.filter_by(user_id=current_user.id).all()}
        gift = GiftPreference.query.filter_by(user_id=current_user.id).first()
        gift_settings = GiftSettings.query.first()
        loan_types = LoanType.query.filter_by(is_active=True).order_by(LoanType.sort_order).all()
        guarantor_requests = LoanGuarantor.query.filter_by(
            guarantor_id=current_user.id, status="Pending"
        ).order_by(LoanGuarantor.id.desc()).all()
        return render_template(
            "member/dashboard.html",
            loans=loans, election=election, positions=positions,
            my_votes=my_votes, gift=gift, gift_settings=gift_settings, loan_types=loan_types,
            guarantor_requests=guarantor_requests
        )

    @app.route("/member/loan/apply", methods=["POST"])
    @login_required
    def apply_loan():
        if current_user.is_admin:
            abort(403)
        try:
            amount = float(request.form.get("amount", 0))
        except ValueError:
            amount = 0
        loan_type_id = request.form.get("loan_type_id")
        option_id = request.form.get("option_id")

        loan_type = db.session.get(LoanType, int(loan_type_id)) if loan_type_id else None
        option = db.session.get(LoanTypeOption, int(option_id)) if option_id else None

        if not loan_type or not loan_type.is_active:
            flash("Please select a valid, currently available loan type.", "error")
            return redirect(url_for("member_dashboard"))
        if not option or option.loan_type_id != loan_type.id:
            flash("Please select a valid tenure/interest option for this loan type.", "error")
            return redirect(url_for("member_dashboard"))
        if amount <= 0:
            flash("Please provide a valid amount.", "error")
            return redirect(url_for("member_dashboard"))

        # The official loan form requires exactly two guarantors, each vouching for a stated amount.
        guarantor_entries = []
        for i in (1, 2):
            code = request.form.get(f"guarantor_code_{i}", "").strip()
            raw_amount = request.form.get(f"guarantor_amount_{i}", "").strip()
            if not code or not raw_amount:
                flash("Both guarantors and their guaranteed amounts are required.", "error")
                return redirect(url_for("member_dashboard"))
            try:
                g_amount = float(raw_amount)
            except ValueError:
                flash("Guarantor amounts must be numbers.", "error")
                return redirect(url_for("member_dashboard"))
            if g_amount <= 0:
                flash("Guarantor amounts must be greater than zero.", "error")
                return redirect(url_for("member_dashboard"))

            guarantor = User.query.filter_by(membership_code=code).first()
            if not guarantor or guarantor.role != "Member" or guarantor.account_status != "Active":
                flash(f"Guarantor {i} not found. Please enter a valid, active member's membership code.", "error")
                return redirect(url_for("member_dashboard"))
            if guarantor.id == current_user.id:
                flash("You cannot be your own guarantor.", "error")
                return redirect(url_for("member_dashboard"))

            guarantor_entries.append((guarantor, g_amount))

        if guarantor_entries[0][0].id == guarantor_entries[1][0].id:
            flash("Please provide two different guarantors.", "error")
            return redirect(url_for("member_dashboard"))

        total_guaranteed = guarantor_entries[0][1] + guarantor_entries[1][1]
        if abs(total_guaranteed - amount) > 0.01:
            flash(
                f"The two guaranteed amounts (\u20a6{total_guaranteed:,.2f}) must add up to the "
                f"loan amount (\u20a6{amount:,.2f}).", "error"
            )
            return redirect(url_for("member_dashboard"))

        max_amount = loan_type.max_amount_for(current_user)
        if max_amount <= 0:
            flash(f"{loan_type.name} amounts haven't been set by the admin yet. Please check back later.", "error")
            return redirect(url_for("member_dashboard"))
        if amount > max_amount:
            flash(f"Amount exceeds the maximum of \u20a6{max_amount:,.2f} allowed for {loan_type.name}.", "error")
            return redirect(url_for("member_dashboard"))

        # A photo/scan of the signed physical loan application form receipt is compulsory.
        receipt_file = request.files.get("application_receipt")
        if not receipt_file or receipt_file.filename == "":
            flash("Please upload a photo or scan of your signed loan application form receipt.", "error")
            return redirect(url_for("member_dashboard"))
        try:
            receipt_path = save_upload(receipt_file, "loan_receipts", allowed_extensions=ALLOWED_RECEIPT_EXTENSIONS)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("member_dashboard"))
        if not receipt_path:
            flash("Please upload a photo or scan of your signed loan application form receipt.", "error")
            return redirect(url_for("member_dashboard"))

        loan = Loan(
            user_id=current_user.id,
            loan_type_id=loan_type.id,
            loan_type_name=loan_type.name,
            amount=amount,
            tenure_months=option.tenure_months,
            interest_rate=option.interest_rate,
            receipt_path=receipt_path,
        )
        db.session.add(loan)
        db.session.flush()
        for guarantor, g_amount in guarantor_entries:
            db.session.add(LoanGuarantor(loan_id=loan.id, guarantor_id=guarantor.id, amount_guaranteed=g_amount))
        db.session.commit()

        for guarantor, g_amount in guarantor_entries:
            if guarantor.email:
                send_email(
                    to_email=guarantor.email,
                    subject="You've been named as a loan guarantor",
                    body=(
                        f"Dear {guarantor.full_name},\n\n"
                        f"{current_user.full_name} (Membership Code: {current_user.membership_code}) has "
                        f"named you as a guarantor for \u20a6{g_amount:,.2f} of their "
                        f"\u20a6{amount:,.2f} {loan_type.name} loan application with MOLETE (IBADAN) BEST "
                        f"CHOICE MULTIPURPOSE CO-OPERATIVE SOCIETY LTD.\n\n"
                        f"Please log in to your member portal and check the \"Guarantor Requests\" tab "
                        f"to accept or decline.\n\n"
                        f"Regards,\nBest Choice Multipurpose Cooperative Society"
                    ),
                )

        names = " and ".join(g.full_name for g, _ in guarantor_entries)
        flash(f"Loan application submitted. {names} have been notified as your guarantors and "
              f"both need to accept before this can be approved.", "success")
        return redirect(url_for("member_dashboard"))

    @app.route("/member/vote", methods=["POST"])
    @login_required
    def cast_vote():
        if current_user.is_admin:
            abort(403)
        election = ElectionSettings.query.first()
        if not election or not election.is_active:
            flash("Voting is not currently active.", "error")
            return redirect(url_for("member_dashboard"))

        position_id = request.form.get("position_id")
        candidate_id = request.form.get("candidate_id")
        if not position_id or not candidate_id:
            flash("Please select a candidate.", "error")
            return redirect(url_for("member_dashboard"))

        already = Vote.query.filter_by(user_id=current_user.id, position_id=position_id).first()
        if already:
            flash("You have already voted for this position.", "error")
            return redirect(url_for("member_dashboard"))

        vote = Vote(user_id=current_user.id, position_id=position_id, candidate_id=candidate_id)
        db.session.add(vote)
        db.session.commit()

        _emit_vote_update(position_id)
        flash("Vote cast successfully.", "success")
        return redirect(url_for("member_dashboard"))

    @app.route("/member/gift-preference", methods=["POST"])
    @login_required
    def set_gift_preference():
        if current_user.is_admin:
            abort(403)
        gift_settings = GiftSettings.query.first()
        if not gift_settings or not gift_settings.is_active:
            flash("Gift preference selection is not currently open.", "error")
            return redirect(url_for("member_dashboard"))
        preference = request.form.get("preference_type")
        if preference not in ("Physical", "Monetized"):
            flash("Please select a valid preference.", "error")
            return redirect(url_for("member_dashboard"))

        existing = GiftPreference.query.filter_by(user_id=current_user.id).first()
        if existing:
            existing.preference_type = preference
        else:
            db.session.add(GiftPreference(user_id=current_user.id, preference_type=preference))
        db.session.commit()
        flash("Gift preference saved.", "success")
        return redirect(url_for("member_dashboard"))

    @app.route("/member/guarantor/<int:slot_id>/respond", methods=["POST"])
    @login_required
    def respond_guarantor_request(slot_id):
        if current_user.is_admin:
            abort(403)
        slot = db.session.get(LoanGuarantor, slot_id) or abort(404)
        if slot.guarantor_id != current_user.id:
            abort(403)
        if slot.status != "Pending":
            flash("This guarantee request has already been responded to.", "error")
            return redirect(url_for("member_dashboard"))

        action = request.form.get("action")
        loan = slot.loan
        if action == "accept":
            slot.status = "Accepted"
            flash(f"You've accepted to guarantee \u20a6{slot.amount_guaranteed:,.2f} of "
                  f"{loan.member.full_name}'s loan.", "success")
        elif action == "decline":
            slot.status = "Declined"
            loan.status = "Declined"
            loan.admin_condition = f"Guarantor ({current_user.full_name}) declined to guarantee this loan."
            loan.decided_at = datetime.utcnow()
            flash("You've declined this guarantee request.", "success")
        else:
            flash("Invalid action.", "error")
            return redirect(url_for("member_dashboard"))

        slot.responded_at = datetime.utcnow()
        db.session.commit()
        return redirect(url_for("member_dashboard"))

    # =====================================================================
    # ADMIN ROUTES
    # =====================================================================
    def _admin_only():
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)

    @app.route("/admin/dashboard")
    @login_required
    def admin_dashboard():
        _admin_only()
        pending_members = User.query.filter_by(role="Member", account_status="Pending").all()
        active_members = User.query.filter_by(role="Member").filter(User.account_status != "Pending").order_by(User.full_name).all()
        search_q = request.args.get("q", "").strip()
        if search_q:
            like = f"%{search_q}%"
            active_members = [m for m in active_members if
                               search_q.lower() in (m.full_name or "").lower()
                               or search_q.lower() in (m.membership_code or "").lower()
                               or search_q.lower() in (m.account_number or "")]

        pending_loans = Loan.query.filter_by(status="Pending").order_by(Loan.created_at.desc()).all()
        decided_loans = Loan.query.filter(Loan.status != "Pending").order_by(Loan.created_at.desc()).limit(50).all()

        election = ElectionSettings.query.first()
        positions = Position.query.all()

        gift_rows = db.session.query(User, GiftPreference).join(
            GiftPreference, GiftPreference.user_id == User.id
        ).all()
        gift_settings = GiftSettings.query.first()

        loan_types = LoanType.query.order_by(LoanType.sort_order).all()

        return render_template(
            "admin/dashboard.html",
            pending_members=pending_members,
            active_members=active_members,
            pending_loans=pending_loans,
            decided_loans=decided_loans,
            election=election,
            positions=positions,
            gift_rows=gift_rows,
            gift_settings=gift_settings,
            search_q=search_q,
            loan_types=loan_types,
        )

    # ---- pending signup approval ----
    @app.route("/admin/member/<int:user_id>/approve", methods=["POST"])
    @login_required
    def approve_member(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        if user.account_status != "Pending":
            flash("This member is not pending approval.", "error")
            return redirect(url_for("admin_dashboard"))

        code = generate_membership_code()
        user.membership_code = code
        user.account_status = "Active"
        db.session.commit()

        email_sent = False
        if user.email:
            email_sent = send_email(
                to_email=user.email,
                subject="Your Molete Best Choice Cooperative membership has been approved",
                body=(
                    f"Dear {user.full_name},\n\n"
                    f"Congratulations — your membership application with MOLETE (IBADAN) BEST CHOICE "
                    f"MULTIPURPOSE CO-OPERATIVE SOCIETY LTD has been approved.\n\n"
                    f"Your Membership Code is: {code}\n"
                    f"Your default password is: {current_app_default_password()}\n\n"
                    f"Please log in to your member portal using your Membership Code and "
                    f"this default password. You'll be asked to set your own new password when you log in.\n\n"
                    f"Regards,\nBest Choice Multipurpose Cooperative Society"
                ),
            )

        flash(
            f"Approved! Membership Code for {user.full_name}: {code} (default password: "
            f"{current_app_default_password()})"
            + (f" — an email notification was sent to {user.email}."
               if email_sent else
               f" — please relay this code and password to them directly (in person, phone call, etc.)"
               + (" (email notification failed to send).")),
            "success"
        )
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/member/<int:user_id>/reject", methods=["POST"])
    @login_required
    def reject_member(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        user.account_status = "Rejected"
        db.session.commit()
        flash(f"{user.full_name}'s registration was rejected.", "success")
        return redirect(url_for("admin_dashboard"))

    # ---- add a single existing member manually ----
    @app.route("/admin/members/add", methods=["POST"])
    @login_required
    def add_member_manual():
        _admin_only()
        full_name = request.form.get("full_name", "").strip()
        membership_code = request.form.get("membership_code", "").strip()
        account_number = request.form.get("account_number", "").strip()
        phone_number = request.form.get("phone_number", "").strip() or None
        work_position = request.form.get("work_position", "").strip()

        if not full_name or not membership_code or not account_number:
            flash("Full name, membership code, and account number are required.", "error")
            return redirect(url_for("admin_dashboard"))

        if User.query.filter(
            (User.membership_code == membership_code) | (User.account_number == account_number)
        ).first():
            flash("A member with this membership code or account number already exists.", "error")
            return redirect(url_for("admin_dashboard"))
        if _name_taken(full_name):
            flash("A member with this exact full name already exists. Since members log in by name, "
                  "please add a middle name or distinguishing detail to tell them apart.", "error")
            return redirect(url_for("admin_dashboard"))

        user = User(
            full_name=full_name,
            membership_code=membership_code,
            account_number=account_number,
            phone_number=phone_number,
            work_position=work_position or None,
            role="Member",
            account_status="Active",
            is_profile_complete=False,
        )
        user.set_password(current_app_default_password())
        db.session.add(user)
        db.session.commit()
        flash(f"{full_name} added. They'll log in with code {membership_code} and the default "
              f"password, then be asked to complete their profile (including email and other details).", "success")
        return redirect(url_for("admin_dashboard"))

    # ---- bulk import existing members ----
    @app.route("/admin/members/bulk-import", methods=["POST"])
    @login_required
    def bulk_import_members():
        _admin_only()
        file = request.files.get("import_file")
        if not file or file.filename == "":
            flash("Please choose a CSV or Excel file.", "error")
            return redirect(url_for("admin_dashboard"))
        try:
            records = parse_bulk_import_file(file)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("admin_dashboard"))

        # Hash the shared default password once — hashing is deliberately slow (scrypt/pbkdf2),
        # so re-hashing it per row is what makes large imports crawl.
        temp_user = User()
        temp_user.set_password(current_app_default_password())
        default_password_hash = temp_user.password_hash

        # Pull existing codes/account numbers/names into memory once, instead of querying per row.
        existing_codes = {c for (c,) in db.session.query(User.membership_code).filter(User.membership_code.isnot(None))}
        existing_account_numbers = {a for (a,) in db.session.query(User.account_number)}
        existing_names = {n.lower() for (n,) in db.session.query(User.full_name)}

        created, skipped = 0, 0
        new_users = []
        for rec in records:
            code = rec["membership_code"]
            account_number = rec["account_number"]
            name = rec["full_name"]
            name_lower = name.lower()

            if not code or not account_number or not name:
                skipped += 1
                continue
            if code in existing_codes or account_number in existing_account_numbers or name_lower in existing_names:
                skipped += 1
                continue

            user = User(
                full_name=name,
                membership_code=code,
                account_number=account_number,
                phone_number=rec.get("phone_number") or None,
                work_position=rec.get("work_position") or None,
                role="Member",
                account_status="Active",
                is_profile_complete=False,
                password_hash=default_password_hash,
            )
            new_users.append(user)
            existing_codes.add(code)
            existing_account_numbers.add(account_number)
            existing_names.add(name_lower)
            created += 1

        db.session.bulk_save_objects(new_users)
        db.session.commit()
        flash(f"Bulk import complete: {created} member(s) created, {skipped} skipped (duplicates/invalid).", "success")
        return redirect(url_for("admin_dashboard"))

    # ---- members directory: edit / delete / pdf ----
    @app.route("/admin/member/<int:user_id>/edit", methods=["POST"])
    @login_required
    def edit_member(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        new_name = request.form.get("full_name", user.full_name).strip()
        if new_name.lower() != user.full_name.lower() and _name_taken(new_name, exclude_id=user.id):
            flash("Another member already has this exact full name. Since members log in by name, "
                  "please use a distinguishing variant.", "error")
            return redirect(url_for("admin_dashboard"))
        user.full_name = new_name
        user.account_number = request.form.get("account_number", user.account_number).strip()
        user.phone_number = request.form.get("phone_number", user.phone_number or "").strip() or None
        user.office_number = request.form.get("office_number", user.office_number or "").strip() or None
        user.work_position = request.form.get("work_position", user.work_position)
        new_email = request.form.get("email", "").strip()
        if new_email and ("@" not in new_email or "." not in new_email.split("@")[-1]):
            flash("Email left unchanged: please enter a valid address or leave it blank.", "error")
        else:
            user.email = new_email or None
        savings_raw = request.form.get("savings_balance")
        if savings_raw not in (None, ""):
            try:
                user.savings_balance = max(0.0, float(savings_raw))
            except ValueError:
                flash("Savings balance must be a number; it was left unchanged.", "error")
        db.session.commit()
        flash("Member updated.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/member/<int:user_id>/delete", methods=["POST"])
    @login_required
    def delete_member(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        if user.is_admin:
            flash("Cannot delete an admin account.", "error")
            return redirect(url_for("admin_dashboard"))
        active_guarantor_loans = [
            gs.loan for gs in user.guarantor_slots
            if gs.loan and gs.loan.status in ("Pending", "Approved")
        ]
        if active_guarantor_loans:
            names = ", ".join(sorted({l.member.full_name for l in active_guarantor_loans}))
            flash(
                f"Cannot delete {user.full_name}: they are a guarantor on an active loan "
                f"application for {names}. Resolve or delete that loan first.", "error"
            )
            return redirect(url_for("admin_dashboard"))
        delete_upload(user.passport_path)
        delete_upload(user.signature_path)
        db.session.delete(user)
        db.session.commit()
        flash("Member deleted.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/member/<int:user_id>/reset-password", methods=["POST"])
    @login_required
    def admin_reset_member_password(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        if user.is_admin:
            flash("Cannot reset an admin account password here.", "error")
            return redirect(url_for("admin_dashboard"))

        default_password = current_app_default_password()
        user.set_password(default_password)
        db.session.commit()

        email_sent = False
        if user.email:
            email_sent = send_email(
                to_email=user.email,
                subject="Your Best Choice Cooperative password was reset",
                body=(
                    f"Hello {user.full_name},\n\n"
                    "An administrator has reset your password. Your new password is:\n\n"
                    f"{default_password}\n\n"
                    "Please log in and change it immediately.\n\n"
                    "Best Choice Cooperative"
                )
            )

        if user.email and email_sent:
            flash(f"Password reset successfully and emailed to {user.email}.", "success")
        elif user.email and not email_sent:
            flash("Password was reset, but email delivery failed. Share the default password with the member manually.", "error")
        else:
            flash(f"Password reset to the default password ({default_password}). This member has no email configured.", "success")

        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/member/<int:user_id>/pdf")
    @login_required
    def member_pdf(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        buf = build_member_profile_pdf(user)
        return send_file(
            buf, mimetype="application/pdf", as_attachment=True,
            download_name=f"{user.membership_code or user.id}_profile.pdf"
        )

    @app.route("/admin/member/<int:user_id>/brief-pdf")
    @login_required
    def member_brief_pdf(user_id):
        _admin_only()
        user = db.session.get(User, user_id) or abort(404)
        buf = build_member_brief_pdf(user)
        return send_file(
            buf, mimetype="application/pdf", as_attachment=True,
            download_name=f"{user.membership_code or user.id}_brief.pdf"
        )

    @app.route("/admin/members/brief-pdf")
    @login_required
    def members_brief_pdf_combined():
        _admin_only()
        members = User.query.filter_by(role="Member").all()
        buf = build_members_brief_pdf_combined(members)
        return send_file(
            buf, mimetype="application/pdf", as_attachment=True,
            download_name="members_brief.pdf"
        )

    @app.route("/admin/members/export")
    @login_required
    def export_members():
        _admin_only()
        members = User.query.filter_by(role="Member").all()
        brief_url_func = lambda u: url_for("member_brief_pdf", user_id=u.id, _external=True)
        buf = build_members_excel(members, brief_pdf_url_func=brief_url_func)
        return send_file(
            buf, as_attachment=True, download_name="members.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    @app.route("/admin/members/export-csv")
    @login_required
    def export_members_csv():
        _admin_only()
        members = User.query.filter_by(role="Member").all()
        buf = build_members_csv_minimal(members)
        return send_file(
            buf, as_attachment=True, download_name="members.csv", mimetype="text/csv"
        )

    # ---- loans ----
    @app.route("/admin/loan/<int:loan_id>/approve", methods=["POST"])
    @login_required
    def approve_loan(loan_id):
        _admin_only()
        loan = db.session.get(Loan, loan_id) or abort(404)
        if not loan.all_guarantors_accepted:
            statuses = ", ".join(f"{g.guarantor.full_name}: {g.status}" for g in loan.guarantors)
            flash(f"Cannot approve: both guarantors must accept first ({statuses}).", "error")
            return redirect(url_for("admin_dashboard"))
        loan.status = "Approved"
        loan.decided_at = datetime.utcnow()
        db.session.commit()
        flash("Loan approved.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/loan/<int:loan_id>/decline", methods=["POST"])
    @login_required
    def decline_loan(loan_id):
        _admin_only()
        loan = db.session.get(Loan, loan_id) or abort(404)
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("A reason is required to decline a loan.", "error")
            return redirect(url_for("admin_dashboard"))
        loan.status = "Declined"
        loan.admin_condition = reason
        loan.decided_at = datetime.utcnow()
        db.session.commit()
        flash("Loan declined.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/loan/<int:loan_id>/delete", methods=["POST"])
    @login_required
    def delete_loan(loan_id):
        _admin_only()
        loan = db.session.get(Loan, loan_id) or abort(404)
        delete_upload(loan.receipt_path)
        db.session.delete(loan)
        db.session.commit()
        flash("Loan application deleted.", "success")
        return redirect(url_for("admin_dashboard"))

    # ---- loan type management ----
    @app.route("/admin/loan-type/<int:loan_type_id>/edit", methods=["POST"])
    @login_required
    def edit_loan_type(loan_type_id):
        _admin_only()
        lt = db.session.get(LoanType, loan_type_id) or abort(404)
        lt.clause = request.form.get("clause", lt.clause).strip()
        lt.season_label = request.form.get("season_label", "").strip() or None

        if lt.basis == "savings_multiple":
            try:
                lt.multiplier = float(request.form.get("multiplier", lt.multiplier or 0))
            except ValueError:
                flash("Multiplier must be a number.", "error")
                return redirect(url_for("admin_dashboard"))
        else:
            try:
                lt.fixed_max_amount = float(request.form.get("fixed_max_amount", lt.fixed_max_amount or 0))
            except ValueError:
                flash("Maximum amount must be a number.", "error")
                return redirect(url_for("admin_dashboard"))

        db.session.commit()
        flash(f"{lt.name} updated.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/loan-type/<int:loan_type_id>/toggle", methods=["POST"])
    @login_required
    def toggle_loan_type(loan_type_id):
        _admin_only()
        lt = db.session.get(LoanType, loan_type_id) or abort(404)
        lt.is_active = not lt.is_active
        db.session.commit()
        flash(f"{lt.name} is now {'OPEN' if lt.is_active else 'CLOSED'} for applications.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/loan-type/<int:loan_type_id>/option/add", methods=["POST"])
    @login_required
    def add_loan_type_option(loan_type_id):
        _admin_only()
        lt = db.session.get(LoanType, loan_type_id) or abort(404)
        try:
            tenure = int(request.form.get("tenure_months"))
            rate = float(request.form.get("interest_rate"))
        except (TypeError, ValueError):
            flash("Tenure and interest rate must be valid numbers.", "error")
            return redirect(url_for("admin_dashboard"))
        db.session.add(LoanTypeOption(loan_type_id=lt.id, tenure_months=tenure, interest_rate=rate))
        db.session.commit()
        flash(f"Added a {tenure}-month / {rate}% option to {lt.name}.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/loan-type-option/<int:option_id>/delete", methods=["POST"])
    @login_required
    def delete_loan_type_option(option_id):
        _admin_only()
        option = db.session.get(LoanTypeOption, option_id) or abort(404)
        if len(option.loan_type.options) <= 1:
            flash("A loan type must keep at least one tenure/interest option.", "error")
            return redirect(url_for("admin_dashboard"))
        db.session.delete(option)
        db.session.commit()
        flash("Option removed.", "success")
        return redirect(url_for("admin_dashboard"))

    # ---- voting management ----
    @app.route("/admin/election/position/add", methods=["POST"])
    @login_required
    def add_position():
        _admin_only()
        name = request.form.get("position_name", "").strip()
        candidate_names = [c.strip() for c in request.form.get("candidate_names", "").split(",") if c.strip()]
        if not name or not candidate_names:
            flash("Provide a position name and at least one candidate.", "error")
            return redirect(url_for("admin_dashboard"))
        if Position.query.filter_by(name=name).first():
            flash("A position with this name already exists.", "error")
            return redirect(url_for("admin_dashboard"))
        position = Position(name=name)
        db.session.add(position)
        db.session.flush()
        for cname in candidate_names:
            db.session.add(Candidate(position_id=position.id, name=cname))
        db.session.commit()
        flash(f"Position '{name}' created with {len(candidate_names)} candidate(s).", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/election/position/<int:position_id>/delete", methods=["POST"])
    @login_required
    def delete_position(position_id):
        _admin_only()
        position = db.session.get(Position, position_id) or abort(404)
        db.session.delete(position)
        db.session.commit()
        flash("Position deleted.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/election/set-status", methods=["POST"])
    @login_required
    def set_election_status():
        _admin_only()
        new_status = request.form.get("status")
        if new_status not in ("Open", "Paused", "Closed"):
            flash("Invalid voting status.", "error")
            return redirect(url_for("admin_dashboard"))
        election = ElectionSettings.query.first()
        election.status = new_status
        db.session.commit()
        flash(f"Voting is now {new_status.upper()}.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/election/results")
    @login_required
    def election_results():
        _admin_only()
        results = {}
        for position in Position.query.all():
            results[position.name] = [
                {"candidate": c.name, "votes": len(c.votes)} for c in position.candidates
            ]
        return jsonify(results)

    @app.route("/admin/gift-preference/toggle", methods=["POST"])
    @login_required
    def toggle_gift_preference():
        _admin_only()
        gift_settings = GiftSettings.query.first()
        gift_settings.is_active = not gift_settings.is_active
        db.session.commit()
        flash(f"Gift preference selection is now {'OPEN' if gift_settings.is_active else 'CLOSED'} to members.", "success")
        return redirect(url_for("admin_dashboard"))

    # ---- gift preferences export ----
    @app.route("/admin/gift-preferences/export")
    @login_required
    def export_gift_preferences():
        _admin_only()
        rows = db.session.query(User, GiftPreference).join(
            GiftPreference, GiftPreference.user_id == User.id
        ).all()
        buf = build_gift_preferences_excel(rows)
        return send_file(
            buf, as_attachment=True, download_name="gift_preferences.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    def current_app_default_password():
        from flask import current_app
        return current_app.config["DEFAULT_MEMBER_PASSWORD"]

    def generate_forgot_password_token():
        return secrets.token_urlsafe(24)

    def generate_temporary_password(length=12):
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))


def _emit_vote_update(position_id):
    position = db.session.get(Position, int(position_id))
    if not position:
        return
    data = {
        "position": position.name,
        "results": [{"candidate": c.name, "votes": len(c.votes)} for c in position.candidates],
    }
    socketio.emit("vote_update", data)


app = create_app()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)
