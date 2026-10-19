"""Offline SDK-loop regressions; injected clients never connect to a provider."""
from concurrent.futures import Future
from queue import Queue
import socket
import threading
import time

import pytest

from ml.intraday.market_input_stream import DatabentoMarketStreams


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline stream tests cannot open network connections")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class CallbackLoop:
    """The SDK dispatches every dataset's callbacks on one shared loop."""
    def __init__(self):
        self.jobs = Queue()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while (job := self.jobs.get()) is not None:
            callback, future = job
            try:
                future.set_result(callback())
            except BaseException as exc:
                future.set_exception(exc)

    def submit(self, callback):
        future = Future()
        self.jobs.put((callback, future))
        return future

    def close(self):
        self.jobs.put(None)
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


class Client:
    def add_callback(self, callback, error):
        self.callback = callback

    def add_reconnect_callback(self, *args):
        pass

    def subscribe(self, **request):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def block_for_close(self, **kwargs):
        pass

    def terminate(self):
        pass


def test_previous_dataset_callback_cannot_deadlock_next_subscription(tmp_path):
    loop, enter_next = CallbackLoop(), threading.Event()
    made, callback_completed, blocked_subscriptions = [], [], []

    class SharedLoopClient(Client):
        def __init__(self):
            self.number = len(made)
            made.append(self)

        def subscribe(self, **request):
            if self.number == 1:
                enter_next.set()
                try:
                    loop.submit(lambda: None).result(timeout=1)
                except TimeoutError:
                    blocked_subscriptions.append(request["dataset"])
                    raise

        def start(self):
            if self.number == 0:
                def dispatch():
                    assert enter_next.wait(2)
                    self.callback({"rtype": 255})
                    callback_completed.append(True)
                loop.submit(dispatch)

    stream = DatabentoMarketStreams(tmp_path, ["AAPL"], ["ES.c.0"],
        client_factory=lambda **kwargs: SharedLoopClient(),
        datasets=("EQUS.MINI", "GLBX.MDP3"))
    try:
        statuses = stream.start()
        assert not blocked_subscriptions
        assert statuses == {"EQUS.MINI": "starting", "GLBX.MDP3": "starting"}
        assert callback_completed == [True]
    finally:
        stream.close()
        loop.close()


def test_snapshot_connection_check_allows_sdk_callback(tmp_path):
    loop = CallbackLoop()

    class ConnectedClient(Client):
        def is_connected(self):
            return loop.submit(lambda: (self.callback({"rtype": 255}), True)[1]).result(timeout=1)

    stream = DatabentoMarketStreams(tmp_path, ["AAPL"], [],
        client_factory=lambda **kwargs: ConnectedClient(), datasets=("EQUS.MINI",))
    try:
        stream.start()
        stream.snapshot()
        assert stream._status["EQUS.MINI"] == "starting"
    finally:
        stream.close()
        loop.close()


def test_close_during_subscription_prevents_late_start_and_retains_no_client(tmp_path):
    subscribed, release, finished = threading.Event(), threading.Event(), threading.Event()
    activity = []

    class SlowClient(Client):
        def subscribe(self, **request):
            subscribed.set()
            assert release.wait(3)

        def start(self):
            activity.append("start")

        def terminate(self):
            activity.append("terminate")

    stream = DatabentoMarketStreams(tmp_path, ["AAPL"], [],
        client_factory=lambda **kwargs: SlowClient(), datasets=("EQUS.MINI",))
    starter = threading.Thread(target=stream.start, daemon=True)
    closer = threading.Thread(target=lambda: (stream.close(), finished.set()), daemon=True)
    starter.start()
    try:
        assert subscribed.wait(2)
        closer.start()
        deadline = time.monotonic() + 1
        while not stream._closed and time.monotonic() < deadline:
            time.sleep(.001)
        assert stream._closed
    finally:
        release.set()
        starter.join(timeout=2)
        if closer.ident is not None:
            closer.join(timeout=2)
        stream.close()
    assert not starter.is_alive() and not closer.is_alive() and finished.is_set()
    assert activity == ["terminate"]
    assert not stream._clients and not stream._opening


def test_rejection_callback_during_start_reopens_only_remaining_schemas(tmp_path):
    loop, reopened = CallbackLoop(), threading.Event()
    made = []

    class RejectingClient(Client):
        def __init__(self):
            self.schemas, self.closed = [], 0
            self.number = len(made)
            made.append(self)

        def subscribe(self, **request):
            self.schemas.append(request["schema"])
            if self.number == 0 and request["schema"] == "mbp-1":
                loop.submit(lambda: self.callback({"rtype": 21,
                    "err": "not authorized for mbp-1 schema"})).result(timeout=1)

        def start(self):
            if self.number:
                reopened.set()

        def block_for_close(self, **kwargs):
            self.closed += 1

    stream = DatabentoMarketStreams(tmp_path, ["AAPL"], [],
        client_factory=lambda **kwargs: RejectingClient(), datasets=("EQUS.MINI",))
    try:
        stream.start()
        assert reopened.wait(2)
        assert len(made) == 2
        assert made[0].closed == 1
        assert made[1].schemas == ["definition", "ohlcv-1m"]
        assert stream._clients["EQUS.MINI"] is made[1]
    finally:
        stream.close()
        loop.close()


def test_snapshot_during_connection_setup_does_not_reopen_starting_client(tmp_path):
    subscribed, release = threading.Event(), threading.Event()
    made = []

    class ConnectingClient(Client):
        def __init__(self):
            made.append(self)

        def subscribe(self, **request):
            subscribed.set()
            assert release.wait(3)

        def is_connected(self):
            return False

    stream = DatabentoMarketStreams(tmp_path, ["AAPL"], [],
        client_factory=lambda **kwargs: ConnectingClient(), datasets=("EQUS.MINI",))
    starter = threading.Thread(target=stream.start, daemon=True)
    starter.start()
    try:
        assert subscribed.wait(2)
        stream.snapshot()
        assert not stream._recovering
        assert len(made) == 1
    finally:
        release.set()
        starter.join(timeout=2)
        stream.close()
    assert not starter.is_alive()
