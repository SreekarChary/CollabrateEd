import os
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, g
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from models import db, User, Project, ProjectMember, Task, Note, Message
from datetime import datetime
from werkzeug.utils import secure_filename
from sqlalchemy.orm import joinedload # <--- FIX: ADDED THIS IMPORT AND USED BELOW

# Ensure instance folder exists
instance_path = os.path.join(os.path.dirname(__file__), 'instance')
os.makedirs(instance_path, exist_ok=True)

# Build absolute path to database (safe for OneDrive)
db_path = os.path.abspath(os.path.join(instance_path, 'database.db')).replace('\\', '/')

template_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'templates')
app = Flask(__name__, template_folder=template_dir)

app.secret_key = "supersecretkey"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db.init_app(app)
CORS(app)
socketio = SocketIO(app)

# --- FIX: Load user before every request to populate g.user for base.html ---
@app.before_request
def load_logged_in_user():
    """Loads the logged-in user from the database and sets it on the Flask 'g' object."""
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        # Load user object from the database for use in templates (e.g., base.html)
        g.user = User.query.get(user_id)
# ----------------------------------------------------------------------------


def create_tables():
    with app.app_context():
        db.create_all()

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="Username already exists")
        user = User(username=username, password=password)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:
            session["user_id"] = user.id
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = User.query.get(session["user_id"])
    user_projects = ProjectMember.query.filter_by(user_id=user.id).all()
    projects = [Project.query.get(p.project_id) for p in user_projects]
    total_users = User.query.count()

    personal_projects = []
    team_projects = []
    all_project_ids = [p.id for p in projects]
    
    # New: Fetch user's tasks and pending tasks
    # Fetch all tasks from projects the user is a member of (Team & Personal)
    all_tasks = Task.query.filter(Task.project_id.in_(all_project_ids)).order_by(Task.due_date).all()
    
    # Filter for pending tasks assigned to the user
    # FIX: Use joinedload to eagerly fetch the Project, Assignee, and Submitter User objects
    pending_tasks = Task.query.options(
        joinedload(Task.project), 
        joinedload(Task.assignee),
        joinedload(Task.submitter_user)
    ).filter(
        Task.project_id.in_(all_project_ids),
        Task.submitted == False,
        Task.assigned_to == user.id # assuming tasks created in a team project are assigned to the creator by default
    ).order_by(Task.due_date).all()

    for p in projects:
        member_count = ProjectMember.query.filter_by(project_id=p.id).count()
        if p.is_team: # Use is_team flag instead of member count comparison
            team_projects.append(p)
        elif p.owner_id == user.id:
            personal_projects.append(p)

    team_project_ids = [p.id for p in team_projects]
    messages = Message.query.filter(Message.project_id.in_(team_project_ids)).order_by(Message.timestamp.desc()).limit(20).all()
    joined_ids = [p.project_id for p in user_projects]
    joinable_projects = Project.query.filter(~Project.id.in_(joined_ids)).all()

    return render_template("dashboard.html", user=user,
                           personal_projects=personal_projects,
                           team_projects=team_projects,
                           messages=messages,
                           joinable_projects=joinable_projects,
                           pending_tasks=pending_tasks) # Passed pending tasks

@app.route("/projects/<int:project_id>/join", methods=["POST"])
def join_project(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=session["user_id"]).first()
    if not existing:
        db.session.add(ProjectMember(project_id=project_id, user_id=session["user_id"]))
        db.session.commit()
    return redirect(url_for("dashboard"))

@app.route("/profile")
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = User.query.get(session["user_id"])
    memberships = ProjectMember.query.filter_by(user_id=user.id).all()
    projects = [Project.query.get(m.project_id) for m in memberships]
    return render_template("profile.html", user=user, projects=projects)

@app.route("/projects/<int:project_id>")
def view_project(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    project = Project.query.get(project_id)
    if not project or project.is_team:
        return "Access denied", 403
    if project.owner_id != session["user_id"]:
        return "Access denied", 403
    notes = Note.query.filter_by(project_id=project_id, user_id=session["user_id"]).all()
    return render_template("project.html", project=project, notes=notes)

@app.route("/projects/<int:project_id>/upload", methods=["POST"])
def upload_personal_note(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    project = Project.query.get(project_id)
    if not project or project.is_team or project.owner_id != session["user_id"]:
        return "Access denied", 403
    # Note: File input name must be 'file' to match templates/project.html fix
    file = request.files.get("file")
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        note = Note(user_id=session["user_id"], project_id=project_id, filename=filename)
        db.session.add(note)
        db.session.commit()
    return redirect(url_for("view_project", project_id=project_id))

@app.route("/team-projects", methods=["GET", "POST"])
def team_projects():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form["name"].strip()
        if name:
            project = Project(name=name, owner_id=session["user_id"], is_team=True)
            db.session.add(project)
            db.session.commit()
            all_users = User.query.all()
            for user in all_users:
                member = ProjectMember(project_id=project.id, user_id=user.id)
                db.session.add(member)
            db.session.commit()
    user_id = session["user_id"]
    member_project_ids = [pm.project_id for pm in ProjectMember.query.filter_by(user_id=user_id).all()]
    team_projects = Project.query.filter(Project.id.in_(member_project_ids), Project.is_team == True).all()
    for p in team_projects:
        p.tasks = Task.query.filter_by(project_id=p.id).all()
        p.notes = Note.query.filter_by(project_id=p.id).all()
    return render_template("team_projects.html", team_projects=team_projects)

@app.route("/team-projects/<int:project_id>/upload", methods=["POST"])
def upload_team_note(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    project = Project.query.get(project_id)
    is_member = ProjectMember.query.filter_by(project_id=project_id, user_id=session["user_id"]).first()
    if not project or not project.is_team or not is_member:
        return "Access denied", 403
    file = request.files.get("file")
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        note = Note(user_id=session["user_id"], project_id=project_id, filename=filename)
        db.session.add(note)
        db.session.commit()
    return redirect(url_for("team_projects"))

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/tasks/<int:project_id>", methods=["POST"])
def add_task(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    project = Project.query.get(project_id)
    if not project:
        return "Project not found", 404

    title = request.form["title"].strip()
    due_date_str = request.form.get("due_date")

    # Convert to Python date object or None
    due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date() if due_date_str else None

    if title:
        task = Task(
            title=title,
            project_id=project_id,
            assigned_to=session["user_id"],
            due_date=due_date
        )
        db.session.add(task)
        db.session.commit()

    return redirect(url_for("team_projects"))

@app.route("/submit-task/<int:task_id>", methods=["POST"])
def submit_task(task_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    task = Task.query.get(task_id)
    if not task:
        return "Task not found", 404

    task.submitted = True
    task.submitted_at = datetime.utcnow()
    task.submitted_by = session["user_id"]
    db.session.commit()

    return redirect(url_for("team_projects"))


@app.route("/chat/<int:project_id>", methods=["GET", "POST"])
def chat(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    project = Project.query.get(project_id)
    if not project:
        return "Project not found", 404

    is_member = ProjectMember.query.filter_by(project_id=project_id, user_id=session["user_id"]).first()
    if not is_member:
        return "Access denied", 403

    if request.method == "POST":
        text = request.form["text"].strip()
        if text:
            msg = Message(sender_id=session["user_id"], project_id=project_id, text=text)
            db.session.add(msg)
            db.session.commit()
        return redirect(url_for("chat", project_id=project_id))

    messages = Message.query.filter_by(project_id=project_id).order_by(Message.timestamp.desc()).limit(50).all()

    # Assign color classes to each user
    color_classes = ["text-primary", "text-success", "text-danger", "text-warning", "text-info"]
    user_colors = {}
    for msg in messages:
        uid = msg.sender_id
        if uid not in user_colors:
            # Note: This logic must be replicated in the handle_send_message function for real-time updates
            user_colors[uid] = color_classes[uid % len(color_classes)]

    return render_template("chat.html", project=project, messages=messages, user_colors=user_colors)

@app.route("/projects/<int:project_id>/invite", methods=["POST"])
def invite_member(project_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    username = request.form["username"]
    user = User.query.filter_by(username=username).first()
    if user:
        existing = ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first()
        if not existing:
            member = ProjectMember(project_id=project_id, user_id=user.id)
            db.session.add(member)
            db.session.commit()
    return redirect(url_for("view_project", project_id=project_id))

# 🌟 FIX: Real-time chat handler corrected to include username and color class
@socketio.on('send_message')
def handle_send_message(data):
    msg = Message(sender_id=data['sender_id'], project_id=data['project_id'], text=data['text'])
    db.session.add(msg)
    db.session.commit()
    
    # Fetch user and calculate color class for real-time display
    user = User.query.get(msg.sender_id)
    color_classes = ["text-primary", "text-success", "text-danger", "text-warning", "text-info"]
    color_class = color_classes[user.id % len(color_classes)] 

    emit('new_message', {
        'sender_id': msg.sender_id,
        'username': user.username,  # ✅ Added username
        'color_class': color_class, # ✅ Added color class
        'text': msg.text,
        'timestamp': msg.timestamp.strftime('%H:%M')
    }, broadcast=True)

# Real-time task handler (Not used in provided templates, but kept for completeness)
@socketio.on('add_task')
def handle_add_task(data):
    task = Task(title=data['title'], project_id=data['project_id'], assigned_to=data['assigned_to'])
    db.session.add(task)
    db.session.commit()
    emit('new_task', {
        'title': task.title,
        'assigned_to': task.assigned_to
    }, broadcast=True)

# Final block to run the app
if __name__ == "__main__":
    create_tables()
    socketio.run(app, debug=True)