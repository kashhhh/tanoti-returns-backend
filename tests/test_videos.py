import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import test_workflows as fixtures
from app.extensions import db
from app.models import Order, OrderVideo, ReturnRequest, ExchangeRequest, ReturnReason, ExchangeReason, RefundMode


def mp4(size=128):
    return b'\x00\x00\x00\x18ftypisom' + b'\x00' * (size - 12)


class VideoTests(unittest.TestCase):
    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.video_dir = tempfile.TemporaryDirectory()
        self.app.config.update(VIDEOS_STORAGE_DIR=self.video_dir.name, VIDEO_MIN_FREE_MB=0)
        self.order_id = Order.query.one().id

    def tearDown(self):
        fixtures.WorkflowTests.tearDown(self)
        self.video_dir.cleanup()

    def upload(self, body=None, headers=None, **values):
        data = dict(order_id=str(self.order_id), kind='packing', upload_id=str(uuid.uuid4()),
                    video=(io.BytesIO(body if body is not None else mp4()), 'recording.mp4'))
        data.update(values)
        return self.client.post('/api/admin/videos', data=data, headers=self.admin_headers if headers is None else headers)

    def test_admin_access_and_range_playback(self):
        result = self.upload()
        self.assertEqual(result.status_code, 201, result.json)
        url = result.json['video']['url']
        for headers in ({}, self.customer_headers):
            for route in ('/api/admin/videos/orders', f'/api/admin/videos/orders/{self.order_id}', url):
                self.assertEqual(self.client.get(route, headers=headers).status_code, 401)
            self.assertEqual(self.upload(headers=headers).status_code, 401)
        self.assertEqual(self.upload(headers={'Cookie': self.admin_headers['Cookie']}).status_code, 403)
        response = self.client.get(url, headers={**self.admin_headers, 'Range': 'bytes=0-11'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, mp4()[:12])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        response.close()

    def test_stages_and_wrong_order_rejected(self):
        common = dict(order_item_id=self.item_id, customer_id=Order.query.one().customer_id)
        db.session.add(ReturnRequest(return_number='RET-VIDEO', reason=ReturnReason.SIZE_ISSUE,
            refund_mode=RefundMode.ACCOUNT, refund_amount=999, net_refund_amount=999, **common))
        db.session.add(ExchangeRequest(exchange_number='EXC-VIDEO', reason=ExchangeReason.SIZE_TOO_SMALL,
            requested_size='M', **common))
        db.session.commit()
        for kind, number in [('packing', ''), ('return_opening', 'RET-VIDEO'), ('return_opening', 'EXC-VIDEO'), ('exchange_packing', 'EXC-VIDEO')]:
            response = self.upload(kind=kind, request_number=number)
            self.assertEqual(response.status_code, 201, response.json)
        result = self.client.get(f'/api/admin/videos/orders/{self.order_id}', headers=self.admin_headers).json
        self.assertEqual(len(result['videos']), 4)
        self.assertEqual(len(result['requests']), 2)
        cards = self.client.get('/api/admin/requests', headers=self.admin_headers).json['requests']
        self.assertEqual(len(cards), 2)
        for card in cards:
            self.assertEqual(card['packing_video_count'], 2)
            self.assertEqual({v['kind'] for v in card['packing_videos']}, {'packing', 'exchange_packing'})
            for video in card['packing_videos']:
                response = self.client.get(video['url'] + '?download=1', headers=self.admin_headers)
                self.assertEqual(response.status_code, 200)
                self.assertIn('attachment', response.headers['Content-Disposition'])
                response.close()
                self.assertEqual(self.client.get(video['url'] + '?download=1', headers=self.customer_headers).status_code, 401)
        self.assertEqual(self.client.get('/api/admin/videos/orders?q=%23100', headers=self.admin_headers).json['orders'][0]['id'], self.order_id)
        for kind, number in [('exchange_packing', 'RET-VIDEO'), ('return_opening', 'RET-MISSING'), ('packing', 'RET-VIDEO'), ('other', '')]:
            self.assertEqual(self.upload(kind=kind, request_number=number).status_code, 400)
        source = Order.query.one()
        other = Order(shopify_order_id='200', order_number='200', customer_id=source.customer_id, payment_method=source.payment_method)
        db.session.add(other); db.session.commit()
        self.assertEqual(self.upload(order_id=str(other.id), kind='return_opening', request_number='RET-VIDEO').status_code, 400)
        orders = self.client.get('/api/admin/videos/orders', headers=self.admin_headers).json['orders']
        counts = {order['id']: order['packing_video_count'] for order in orders}
        self.assertEqual(counts[self.order_id], 2)
        self.assertEqual(counts[other.id], 0)

    def test_retry_and_conflict(self):
        upload_id = str(uuid.uuid4())
        first = self.upload(upload_id=upload_id)
        self.app.config['MAX_VIDEO_STORAGE_MB'] = 0
        second = self.upload(upload_id=upload_id)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json['video']['id'], second.json['video']['id'])
        self.assertEqual(OrderVideo.query.count(), 1)
        self.assertEqual(self.upload(body=mp4(129), upload_id=upload_id).status_code, 409)
        self.assertEqual(len(list(Path(self.video_dir.name).iterdir())), 1)

    def test_upload_limits_and_failure_cleanup(self):
        self.assertEqual(self.upload(body=b'not a video' * 10).status_code, 400)
        self.assertEqual(self.upload(body=mp4(20)).status_code, 400)
        self.app.config['MAX_VIDEO_UPLOAD_MB'] = 1
        self.assertEqual(self.upload(body=mp4(1024 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.upload(body=mp4(3 * 1024 * 1024)).status_code, 413)
        self.app.config['MAX_VIDEO_STORAGE_MB'] = 0
        self.assertEqual(self.upload().status_code, 507)
        self.app.config['MAX_VIDEO_STORAGE_MB'] = 10
        with patch('app.admin.videos.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            self.assertEqual(self.upload().status_code, 507)
        with patch.object(db.session, 'commit', side_effect=RuntimeError('test failure')):
            self.assertEqual(self.upload().status_code, 503)
        self.assertEqual(OrderVideo.query.count(), 0)
        self.assertEqual(list(Path(self.video_dir.name).iterdir()), [])
        self.assertEqual(self.upload(body=b'\x1a\x45\xdf\xa3webm' + b'x' * 100).status_code, 201)

    def test_customer_limit_unchanged(self):
        self.app.config['MAX_CONTENT_LENGTH'] = 50
        self.assertEqual(self.upload().status_code, 201)
        response = self.client.post(f'/api/customer/order-items/{self.item_id}/return',
            data={'photo_front': (io.BytesIO(b'x' * 1000), 'photo.jpg')}, headers=self.customer_headers)
        self.assertEqual(response.status_code, 413)
