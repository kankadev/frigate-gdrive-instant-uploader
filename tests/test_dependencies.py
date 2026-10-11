"""Dependency compatibility checks using only synthetic data and loopback HTTP."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import requests
from dotenv import load_dotenv, set_key
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from src.frigate_api import fetch_event, EventNotFoundError
from src.mattermost_handler import send_mattermost_notification


class DependencyTests(unittest.TestCase):
    def test_zip_extraction_does_not_reuse_predictable_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'fixture.zip'
            existing = Path(directory) / 'certificate.pem'
            existing.write_bytes(b'untrusted preexisting data')
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('certificate.pem', b'legitimate fixture')
            with patch.object(tempfile, 'tempdir', directory):
                extracted = Path(requests.utils.extract_zipped_paths(str(archive / 'certificate.pem')))
            self.assertNotEqual(extracted, existing)
            self.assertEqual(extracted.read_bytes(), b'legitimate fixture')
            self.assertEqual(existing.read_bytes(), b'untrusted preexisting data')

    def test_dotenv_environment_precedence_and_expansion(self):
        with patch.dict(os.environ, {'EXISTING': 'container'}, clear=True):
            load_dotenv(stream=io.StringIO('EXISTING=file\nDERIVED=${EXISTING}/suffix\nEMPTY=\nQUOTED="two words"\n'))
            self.assertEqual(os.environ['EXISTING'], 'container')
            self.assertEqual(os.environ['DERIVED'], 'container/suffix')
            self.assertEqual(os.environ['EMPTY'], '')
            self.assertEqual(os.environ['QUOTED'], 'two words')

    @unittest.skipUnless(os.name != 'nt', 'Linux deployment symlinks')
    def test_dotenv_write_preserves_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            link = Path(directory) / '.env'
            target.write_text('SAFE=original\n')
            link.symlink_to(target)
            set_key(link, 'ADDED', 'value')
            self.assertEqual(target.read_text(), 'SAFE=original\n')
            self.assertFalse(link.is_symlink())

    def test_netrc_credentials_do_not_follow_url_userinfo(self):
        with tempfile.TemporaryDirectory() as directory:
            netrc = Path(directory) / 'netrc'
            netrc.write_text('machine trusted.example login synthetic password dummy\n')
            with patch.dict(os.environ, {'NETRC': str(netrc)}):
                self.assertEqual(requests.utils.get_netrc_auth('https://trusted.example/'), ('synthetic', 'dummy'))
                self.assertIsNone(requests.utils.get_netrc_auth('https://trusted.example:443@other.example/'))

    def test_http_json_stream_retry_and_webhook_payload(self):
        state = {'attempts': 0, 'posts': []}
        payload = b'synthetic-video-bytes' * 8192

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/api/events/known':
                    self.reply(200, b'{"id":"known"}')
                elif self.path == '/stream':
                    state['attempts'] += 1
                    self.reply(503 if state['attempts'] == 1 else 200,
                               b'retry' if state['attempts'] == 1 else payload)
                else:
                    self.reply(404, b'not found')

            def do_POST(self):
                state['posts'].append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.reply(200, b'ok')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual(fetch_event(url, 'known')['id'], 'known')
            with self.assertRaises(EventNotFoundError):
                fetch_event(url, 'missing')
            with requests.Session() as session:
                session.mount('http://', HTTPAdapter(max_retries=Retry(total=1, status_forcelist=[503])))
                with session.get(url + '/stream', stream=True, timeout=(2, 2)) as response:
                    response.raise_for_status()
                    self.assertEqual(b''.join(response.iter_content(4096)), payload)
            self.assertEqual(state['attempts'], 2)
            self.assertTrue(send_mattermost_notification('English title', 'English body', webhook_url=url))
            self.assertEqual(state['posts'][0]['attachments'][0]['text'], 'English body')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
