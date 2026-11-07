import time


class PipelineContext:
    def __init__(self, state, flow_id: str, verbose: bool = False):
        self.state = state
        self.flow_id = flow_id
        self.verbose = verbose
        self.receipt_sync_hook = None

    def log(self, msg: str, level: str = "info", category: str = "pipeline") -> None:
        self.state.log(msg, level, category=category, flow_id=self.flow_id)

    def debug(self, msg: str, category: str = "pipeline") -> None:
        if self.verbose:
            self.log(msg, level="debug", category=category)

    def set_status(self, stage: str, msg: str, **kwargs) -> None:
        self.state.set_status(stage, msg, **kwargs)

    def is_stop_requested(self) -> bool:
        return self.state.is_stop_requested()

    def sync_receipts(self) -> None:
        if self.receipt_sync_hook and not self.is_stop_requested():
            self.receipt_sync_hook()

    def wait_retry(self, secs: int) -> bool:
        if self.is_stop_requested():
            self.set_status("stopped", "已停止")
            return True
        self.set_status("running", "WAITING_RETRY", progress_item=f"Retry in {secs}s")
        for _ in range(secs):
            if self.is_stop_requested():
                self.set_status("stopped", "已停止")
                return True
            self.sync_receipts()
            time.sleep(1)
        return False
