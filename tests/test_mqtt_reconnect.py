"""Local protocol probe: broker absent at boot, then recovery and reconnect."""
import socket
import threading
import time
import unittest
import paho.mqtt.client as mqtt


class MQTTRecovery(unittest.TestCase):
    def test_initial_failure_and_later_disconnect_recover(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
        connected = threading.Event()
        subscribed = threading.Event()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = lambda c,u,f,r,p: (connected.set(), c.subscribe('test/events'))
        client.on_subscribe = lambda *args: subscribed.set()
        client.reconnect_delay_set(1,2)
        client.connect_async('127.0.0.1',port,5)
        thread = threading.Thread(target=lambda: client.loop_forever(retry_first_connection=True),daemon=True)
        thread.start()
        try:
            time.sleep(1.2)
            self.assertTrue(thread.is_alive())
            self.assertFalse(connected.is_set())
            listener.listen(2)
            listener.settimeout(8)
            for _ in range(2):
                conn,_ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    data = conn.recv(4096)
                    self.assertEqual(data[0]>>4,1)
                    conn.sendall(bytes([0x20,2,0,0]))
                    self.assertTrue(connected.wait(3))
                    data = conn.recv(4096)
                    self.assertEqual(data[0]>>4,8)
                    conn.sendall(bytes([0x90,3,data[2],data[3],0]))
                    self.assertTrue(subscribed.wait(3))
                connected.clear()
                subscribed.clear()
            self.assertTrue(thread.is_alive())
        finally:
            client.disconnect()
            listener.close()
            thread.join(8)


if __name__=='__main__':
    unittest.main()
