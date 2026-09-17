import tempfile
import unittest
from pathlib import Path
from flask_migrate import upgrade
from sqlalchemy import inspect, text
from app import create_app
from app.config import Config
from app.extensions import db

class MigrationTest(unittest.TestCase):
    def test_upgrade_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            class TestConfig(Config):
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + (Path(folder)/"migration.db").as_posix()
            app = create_app(TestConfig)
            with app.app_context():
                try:
                    upgrade(revision="066cf68194cc")
                    db.session.execute(text("INSERT INTO customers (id,email) VALUES (1,'preserved@example.com')"))
                    db.session.commit()
                    upgrade()
                    self.assertEqual(db.session.execute(text("SELECT email FROM customers WHERE id=1")).scalar(), "preserved@example.com")
                    self.assertIn("pickup_address", {c["name"] for c in inspect(db.engine).get_columns("exchange_requests")})
                    self.assertIn("notifications", inspect(db.engine).get_table_names())
                finally:
                    db.session.remove()
                    db.engine.dispose()
