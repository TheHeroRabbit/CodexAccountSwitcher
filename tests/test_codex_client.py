import unittest

from codex_client import (
    CODEX_AUMID,
    CodexClientError,
    ProcessInfo,
    StoreCodexClient,
    is_codex_store_process,
)

CODEX_PROCESS = ProcessInfo(
    1200,
    r"C:\Program Files\WindowsApps\OpenAI.Codex_26.721.4979.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe",
)


class FakeBackend:
    def __init__(
        self,
        running=True,
        close_removes=True,
        terminate_removes=True,
        activate_adds=True,
        activation_error=None,
    ) -> None:
        self.processes = [CODEX_PROCESS] if running else []
        self.close_removes = close_removes
        self.terminate_removes = terminate_removes
        self.activate_adds = activate_adds
        self.activation_error = activation_error
        self.close_calls = []
        self.terminate_calls = []
        self.activation_calls = []
        self.clock = 0.0

    def list_processes(self):
        return list(self.processes)

    def request_close(self, process_ids):
        self.close_calls.append(list(process_ids))
        if self.close_removes:
            self.processes = []
        return len(process_ids)

    def terminate_processes(self, process_ids):
        process_ids = list(process_ids)
        self.terminate_calls.append(process_ids)
        if self.terminate_removes:
            self.processes = []
        return len(process_ids) if self.terminate_removes else 0

    def activate_application(self, app_user_model_id):
        self.activation_calls.append(app_user_model_id)
        if self.activation_error is not None:
            raise self.activation_error
        if self.activate_adds:
            self.processes = [ProcessInfo(2400, CODEX_PROCESS.image_path)]
        return 2400

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds


class StoreCodexClientTests(unittest.TestCase):
    def make_client(self, backend):
        return StoreCodexClient(
            backend=backend,
            graceful_timeout=0,
            force_timeout=0,
            launch_timeout=0,
            poll_interval=0.01,
        )

    def test_process_match_is_limited_to_codex_store_package(self) -> None:
        self.assertTrue(is_codex_store_process(CODEX_PROCESS.image_path))
        self.assertTrue(
            is_codex_store_process(
                r"D:/WindowsApps/OpenAI.Codex_99.0_x64__publisher/app/resources/codex.exe"
            )
        )
        self.assertFalse(is_codex_store_process(r"D:\Tools\Codex账号切换器.exe"))
        self.assertFalse(is_codex_store_process(r"C:\Program Files\ChatGPT\ChatGPT.exe"))

    def test_running_client_closes_gracefully_and_restarts(self) -> None:
        backend = FakeBackend(running=True, close_removes=True)

        result = self.make_client(backend).restart()

        self.assertTrue(result.was_running)
        self.assertFalse(result.forced_close)
        self.assertEqual(result.launched_pid, 2400)
        self.assertEqual(backend.close_calls, [[1200]])
        self.assertEqual(backend.terminate_calls, [])
        self.assertEqual(backend.activation_calls, [CODEX_AUMID])

    def test_stopped_client_is_started_without_close_request(self) -> None:
        backend = FakeBackend(running=False)

        result = self.make_client(backend).restart()

        self.assertFalse(result.was_running)
        self.assertFalse(result.forced_close)
        self.assertEqual(backend.close_calls, [])
        self.assertEqual(backend.activation_calls, [CODEX_AUMID])

    def test_unresponsive_client_uses_scoped_force_close(self) -> None:
        backend = FakeBackend(running=True, close_removes=False, terminate_removes=True)

        result = self.make_client(backend).restart()

        self.assertTrue(result.forced_close)
        self.assertEqual(backend.terminate_calls, [[1200]])
        self.assertEqual(backend.activation_calls, [CODEX_AUMID])

    def test_restart_stops_when_client_cannot_be_closed(self) -> None:
        backend = FakeBackend(
            running=True,
            close_removes=False,
            terminate_removes=False,
        )

        with self.assertRaisesRegex(CodexClientError, "无法关闭 Codex"):
            self.make_client(backend).restart()

        self.assertEqual(backend.activation_calls, [])

    def test_activation_error_is_reported(self) -> None:
        backend = FakeBackend(
            running=False,
            activation_error=CodexClientError("未检测到 Microsoft Store 版 Codex。"),
        )

        with self.assertRaisesRegex(CodexClientError, "Microsoft Store"):
            self.make_client(backend).restart()

    def test_launch_must_be_confirmed_by_process_detection(self) -> None:
        backend = FakeBackend(running=False, activate_adds=False)

        with self.assertRaisesRegex(CodexClientError, "未能启动 Codex"):
            self.make_client(backend).restart()


if __name__ == "__main__":
    unittest.main()
