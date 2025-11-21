from app import app, db
from models import User

users = [
    {"username": "alice", "password": "alice123"},
    {"username": "bob", "password": "bob123"},
    {"username": "charlie", "password": "charlie123"}
]

with app.app_context():
    db.create_all()
    for u in users:
        existing = User.query.filter_by(username=u["username"]).first()
        if not existing:
            user = User(username=u["username"], password=u["password"])
            db.session.add(user)
    db.session.commit()
    print("✅ Users created successfully")