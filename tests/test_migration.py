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
                    db.session.execute(text("INSERT INTO orders (id,shopify_order_id,order_number,customer_id,payment_method) VALUES (1,'100','100',1,'PREPAID')"))
                    db.session.execute(text("INSERT INTO order_items (id,order_id,shopify_line_item_id,product_title,price,quantity) VALUES (1,1,'1','Two shirts',999,2)"))
                    db.session.execute(text("INSERT INTO return_requests (return_number,order_item_id,customer_id,reason,refund_mode,refund_amount,net_refund_amount,status) VALUES ('RET-OLD',1,1,'SIZE_ISSUE','ACCOUNT',999,999,'REJECTED')"))
                    db.session.commit()
                    upgrade()
                    self.assertEqual(db.session.execute(text("SELECT email FROM customers WHERE id=1")).scalar(), "preserved@example.com")
                    self.assertIn("pickup_address", {c["name"] for c in inspect(db.engine).get_columns("exchange_requests")})
                    self.assertIn("refund_paid_at", {c["name"] for c in inspect(db.engine).get_columns("return_requests")})
                    self.assertIn("delivered_at", {c["name"] for c in inspect(db.engine).get_columns("exchange_requests")})
                    for table in ("return_requests", "exchange_requests"):
                        columns = {c["name"]: c for c in inspect(db.engine).get_columns(table)}
                        self.assertTrue(columns["customer_note"]["nullable"])
                    units = db.session.execute(text("SELECT id,unit_number,quantity,line_quantity,request_claimed FROM order_items ORDER BY unit_number")).all()
                    self.assertEqual(len(units),2)
                    self.assertEqual(tuple(units[0]),(1,1,1,2,True))
                    self.assertEqual(tuple(units[1])[1:],(2,1,2,False))
                    self.assertEqual(db.session.execute(text("SELECT order_item_id FROM return_requests WHERE return_number='RET-OLD'")).scalar(),1)
                    self.assertIn("notifications", inspect(db.engine).get_table_names())
                finally:
                    db.session.remove()
                    db.engine.dispose()
