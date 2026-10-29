import asyncio
import tempfile
import unittest

from backend.tests._support import NoNetworkMixin, load_app, request


class WebSendReceiptTests(NoNetworkMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_app(self.temp.name, telegram=False, brain='desktop')
        self.auth = {'Authorization': 'Bearer test-relay-secret'}
        self.rid = '11111111-aaaa-4aaa-8aaa-111111111111'

    async def test_canonical_sqlite_history_and_sse_keep_correlation_and_attachments(self):
        queue = asyncio.Queue()
        self.module.app_subs.add(queue)
        self.addCleanup(self.module.app_subs.discard, queue)
        attachment = {'url': '/uploads/synthetic.png', 'kind': 'image', 'mime': 'image/png'}
        response = await request(self.module, 'POST', '/app/send', headers=self.auth,
                                 json={'text': '  same text  ', 'session_id': 'a',
                                       'client_request_id': self.rid, 'attachments': [attachment]})
        self.assertEqual(response.status_code, 200)
        frame = await queue.get()
        self.assertEqual(frame['meta']['client_request_id'], self.rid)
        self.assertEqual(frame['meta']['api_session'], 'a')
        self.assertEqual(frame['text'], 'same text')
        history = await request(self.module, 'GET', '/app/history?session_id=a', headers=self.auth)
        row, = history.json()['messages']
        self.assertEqual(row['id'], response.json()['id'])
        self.assertEqual(row['meta']['attachments'], [attachment])
        self.assertEqual(row['meta']['client_request_id'], self.rid)
        other = await request(self.module, 'GET', '/app/history?session_id=b', headers=self.auth)
        self.assertEqual(other.json()['messages'], [])
        with self.module.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    async def test_invalid_request_ids_do_not_persist_or_broadcast(self):
        for value in [None, True, 1, {}, '', self.rid.upper(), 'x'*10000,
                      '11111111-1111-1111-8111-111111111111', self.rid+'\n']:
            with self.subTest(value=str(value)[:60]):
                response = await request(self.module, 'POST', '/app/send', headers=self.auth,
                                         json={'text': 'synthetic', 'session_id': 'a', 'client_request_id': value})
                self.assertEqual(response.status_code, 400)
        with self.module.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    async def test_old_clients_and_authentication_remain_compatible(self):
        denied = await request(self.module, 'POST', '/app/send', json={'text': 'synthetic', 'client_request_id': self.rid})
        self.assertEqual(denied.status_code, 401)
        sent = await request(self.module, 'POST', '/app/send', headers=self.auth, json={'text': 'old client', 'session_id': 'a'})
        self.assertEqual(sent.status_code, 200)
        history = await request(self.module, 'GET', '/app/history?session_id=a', headers=self.auth)
        self.assertNotIn('client_request_id', history.json()['messages'][0]['meta'])
