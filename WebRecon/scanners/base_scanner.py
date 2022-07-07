import requests
import os
import queue
import time

from typing import Any, Dict, Union
from pathlib import Path
from functools import lru_cache
from abc import abstractmethod
from .utils import *
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


#   --------------------------------------------------------------------------------------------------------------------
#
#   Base Scanner
#
#   Notes
#       *
#
#   Mitigation
#       *
#
#   --------------------------------------------------------------------------------------------------------------------


class ScanManager:
    _DEF_OUTPUT_DIRECTORY = "results"
    _ACCEPTED_SCHEMES = ["http", "https"]
    _ERROR_LOG_NAME = f"{OutputColors.Red}error_log{OutputColors.White}"  # TODO to default values?
    _SCAN_COLOR = OutputColors.White

    def __init__(self, scheme, target_hostname, target_url, *args, **kwargs):
        self.target_hostname = target_hostname
        self.target_url = target_url
        self.scheme = scheme

        self.results_path = kwargs.get("results_path", f'{self._DEF_OUTPUT_DIRECTORY}')
        self.results_path_full = self._setup_results_path()

        self._current_progress_perc = int()
        self._output_manager = None
        self._output_manager_setup()

    def _output_manager_setup(self):
        self._output_manager = OutputManager()
        keys = self._define_status_output()
        if keys:
            self._output_manager.insert_output(self._get_scanner_name(), OutputType.Status, keys)
        self._output_manager.insert_output(self._ERROR_LOG_NAME, OutputType.Lines)

    @lru_cache()
    def _get_scanner_name(self) -> str:
        return f"{self.__class__._SCAN_COLOR}{self.__class__.__name__}"

    def _setup_results_path(self) -> str:
        Path(self._get_results_directory()).mkdir(parents=True, exist_ok=True)  # recursively make directories
        full_path = os.path.join(self._get_results_directory(), self._get_results_filename())
        if os.path.isfile(full_path):
            os.remove(full_path)  # remove old files
        return full_path

    def _log_line(self, log_name, line: str):
        # TODO with colors based on type of message
        # TODO not each line individually
        # TODO mutex into op manager
        self._output_manager.update_lines(log_name, line)
                # print(f"[{self.target_hostname}] {( + ' ').ljust(20, '-')}> {line}")

    def _log_status(self, lkey: str, lval: Any):
        # TODO with colors based on type of message
        # TODO mutex into op manager
        self._output_manager.update_status(self._get_scanner_name(), lkey, lval)
                # print(f"[{self.target_hostname}] {( + ' ').ljust(20, '-')}> {line}")

    def _log_exception(self, exc_text, abort: bool):
        self._log_line(self._ERROR_LOG_NAME, f" {self.__class__.__name__} exception - {exc_text}, aborting - {abort}")

    def _save_results(self, results: str):
        path = os.path.join(self._get_results_directory(), self._get_results_filename())
        with open(path, "a") as res_file:
            res_file.write(f"{results}")

    def _get_results_filename(self, *args, **kwargs) -> str:
        return f"{self.__class__.__name__}.txt"

    @abstractmethod
    def _define_status_output(self) -> Dict[str, Any]:
        ...

    @lru_cache
    def _get_results_directory(self, *args, **kwargs) -> str:
        path = os.path.join(self.results_path,
                            self._format_name_for_path(self.target_hostname),
                            self._format_name_for_path(self.target_url))

        return path

    @lru_cache(maxsize=5)
    def generate_url_base_path(self, dnsname: str) -> str:
        return f"{self.scheme}://{dnsname}.{self.target_hostname}"

    @lru_cache(maxsize=5)
    def _format_name_for_path(self, name: str) -> str:
        return name.replace(f'{self.scheme}://', '').replace('.', '_')

    def _update_progress_status(self, finished_c, total_c):
        progress = (100 * finished_c) // total_c
        self._log_status(OutputStatusKeys.Left, f"{total_c - finished_c} out of {total_c}")

        if progress % ScannerDefaultParams.ProgBarIntvl == 0 and progress > self._current_progress_perc:
            print_prog_mod = 5  # TODO params
            print_prog_count = progress // print_prog_mod  # TODO params
            print_prog_max = (100 // print_prog_mod)  # TODO params
            prog_str = f"[{('#' * print_prog_count).ljust(print_prog_max - print_prog_count, '-')}]"
            self._log_status(OutputStatusKeys.Progress, prog_str)
            self._current_progress_perc = progress


class Scanner(ScanManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.wordlist_path = kwargs.get("wordlist_path", getattr(WordlistDefaultPath, self.__class__.__name__, None))  # TODO argparse
        if self.wordlist_path:
            self.words_queue: queue.Queue = self.load_words()

        self.request_cooldown = kwargs.get("request_cooldown", NetworkDefaultParams.RequestCooldown)
        self.thread_count = kwargs.get("thread_count", ScannerDefaultParams.ThreadCount)
        self.request_timeout = kwargs.get("request_timeout", NetworkDefaultParams.RequestTimeout)

        self._default_headers = dict()  # for rotating user agents
        self._session: Union[requests.Session, None] = None
        self._setup_session()

        self._session_refresh_interval = kwargs.get("session_refresh_interval",
                                                    NetworkDefaultParams.SessionRefreshInterval)
        self._session_refresh_count = 0

    def load_words(self) -> queue.Queue:
        with open(self.wordlist_path, "r") as wl:
            words = queue.Queue()
            for word in wl.readlines():
                words.put(word.rstrip("\n"))
        return words

    def start_scanner(self) -> Any:
        try:
            self._log_status(OutputStatusKeys.State, OutputValues.StateSetup)
            scan_results = self._start_scanner()
            self._log_status(OutputStatusKeys.State, OutputValues.StateComplete)
            return scan_results
        except Exception as exc:
            self._log_exception(exc, True)  # TODO try to have our own exceptions

    @abstractmethod
    def _start_scanner(self) -> Any:
        ...

    @abstractmethod
    def _define_status_output(self) -> Dict[str, Any]:
        ...

    def _setup_session(self):
        if self.scheme not in self._ACCEPTED_SCHEMES:
            raise Exception(f"Missing / unsupported url scheme, should be one of: {', '.join(self._ACCEPTED_SCHEMES)}")
            # TODO exceptions class?

        if not self.target_url:
            raise Exception("Missing target url")  # TODO exceptions class?

        self._default_headers.clear()
        self._default_headers['User-Agent'] = get_random_useragent()

        self._session = requests.Session()

    def _make_request(self, method: str, url: str, headers=None, **kwargs):
        if not self._session_refresh_count % self._session_refresh_interval:
            self._setup_session()
        if not headers:
            headers = dict()
        headers.update(self._default_headers)

        res = self._session.request(method=method, url=url, headers=headers, timeout=self.request_timeout, **kwargs)

        if res.status_code == 429:  # to default values?
            time.sleep(NetworkDefaultParams.TooManyReqSleep)

        return res


if __name__ == "__main__":
    pass
