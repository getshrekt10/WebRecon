import threading

import requests
import queue
import time
import json
import urllib3
import datetime

from typing import Any, Dict
from pathlib import Path
from functools import lru_cache
from abc import abstractmethod
from .utils import *

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ScanManager(object):
    _SCAN_COLOR = OutputColors.White
    _SUPPORTS_CACHE = False  # overwrite for each scanner
    _WRITE_RESULTS = False  # overwrite for each scanner
    _CACHE_MUTEX = threading.RLock()
    _SHOULD_ABORT = False
    _RUN_ID = str()

    def __new__(cls, *args, **kwargs):
        if not ScanManager._RUN_ID:
            ScanManager._RUN_ID = generate_runid()
        return object.__new__(cls)

    def __init__(self, scheme, target_hostname, target_url, *args, **kwargs):
        if kwargs.get("disable_cache", False):
            self.__class__._SUPPORTS_CACHE = False
        self.target_hostname = target_hostname
        self.target_url = target_url
        self.scheme = scheme

        self.wordlist_path = kwargs.get("wordlist_path", None)  # if None... scanner does not use a wordlist

        self.results_path = kwargs.get("results_path")
        self.results_path_full = self._setup_results_path() if self.results_path else None

        self._output_manager = self._output_manager_setup()
        if self._output_manager.is_key_in_status(self._get_scanner_name(), OutputStatusKeys.ResultsPath):
            self._log_status(OutputStatusKeys.ResultsPath, self.truncate_str(self.results_path_full))

        self._use_prev_cache = False
        self._cache_dict: dict = self._load_cache_if_exists()
        if not self._use_prev_cache and self._WRITE_RESULTS:
            self._remove_old_results()
        if self._output_manager.is_key_in_status(self._get_scanner_name(), OutputStatusKeys.UsingCache):  # WebRecon cls doesn't have this
            self._log_status(OutputStatusKeys.UsingCache, OutputValues.BoolTrue if self._use_prev_cache else OutputValues.BoolFalse)

        self._current_progress_mutex = threading.RLock()
        self._current_progress_perc = int()
        self._count_multiplier = 1  # for content extensions, etc... purely visual to avoid messing cache

    def _output_manager_setup(self) -> OutputManager:
        om = OutputManager()
        keys = self._define_status_output()
        if keys:
            om.insert_output(self._get_scanner_name(), OutputType.Status, keys)
        om.insert_output(ScannerDefaultParams.ProgLogName, OutputType.Lines)
        return om

    @lru_cache()
    def _get_scanner_name(self, include_ansi=True) -> str:
        return f"{self.__class__._SCAN_COLOR if include_ansi else ''}{self.__class__.__name__}"

    def _setup_results_path(self) -> str:
        Path(self._get_results_directory()).mkdir(parents=True, exist_ok=True)  # recursively make directories
        full_path = self._get_results_fullpath()
        return full_path

    def _log_line(self, log_name, line: str):
        self._output_manager.update_lines(log_name, f"{datetime.datetime.now().strftime('%H:%M:%S')}" + line)

    def _log_status(self, lkey: str, lval: Any, refresh_output=True):
        self._output_manager.update_status(self._get_scanner_name(), lkey, lval, refresh_output)

    def _log_exception(self, exc_text, abort: bool):
        self._log_line(ScannerDefaultParams.ProgLogName, f" {self.__class__.__name__} exception - {exc_text},"
                                                         f" aborting - {abort}")

    def _log_progress(self, prog_text):
        self._log_line(ScannerDefaultParams.ProgLogName, f" {self.__class__.__name__} {prog_text}")

    def _save_results(self, results: str, mode="a"):
        if self._WRITE_RESULTS:
            with ScanManager._CACHE_MUTEX:
                path = self._get_results_fullpath()
                with open(path, mode) as res_file:
                    res_file.write(results)
                self._update_cache_results()

    def _update_cache_results(self):
        if self._supports_cache() and self._WRITE_RESULTS:
            with ScanManager._CACHE_MUTEX:
                self._cache_dict["results_filehash"] = get_filehash(self._get_results_fullpath())
                with open(self._get_cache_fullpath(), "r") as cf:
                    cache_json = json.load(cf)
                cache_json["scanners"][self._get_scanner_name(include_ansi=False)] = self._cache_dict
                with open(self._get_cache_fullpath(), "w") as cf:
                    json.dump(cache_json, cf)

    def _define_status_output(self) -> Dict[str, Any]:
        status = dict()
        status[OutputStatusKeys.State] = OutputValues.StateSetup
        status[OutputStatusKeys.UsingCache] = OutputValues.EmptyStatusVal

        return status

    def _load_cache_if_exists(self) -> dict:
        try:
            if self._supports_cache() and self._WRITE_RESULTS:
                Path(self._get_cache_directory()).mkdir(parents=True, exist_ok=True)
                with ScanManager._CACHE_MUTEX:
                    cache_path = Path(self._get_cache_fullpath())
                    if cache_path.exists():
                        with cache_path.open('r') as cf:
                            cache_json = json.load(cf)
                            scan_cache = cache_json["scanners"].get(self._get_scanner_name(include_ansi=False))
                            if scan_cache:
                                results_filehash = scan_cache.get("results_filehash", "")
                                wordlist_filehash = scan_cache.get("wordlist_filehash", "")
                                run_id = scan_cache.get("run_id", "")
                                if results_filehash == get_filehash(self._get_results_fullpath()) and \
                                        wordlist_filehash == get_filehash(os.path.join(self.wordlist_path)) and \
                                        time.time() - scan_cache.get("timestamp", 0) < CacheDefaultParams.CacheMaxAge and \
                                        run_id != ScanManager._RUN_ID:
                                    self._use_prev_cache = True
                                    scan_cache["run_id"] = ScanManager._RUN_ID
                                    self._log_progress(f"loading up old cache...")
                                    return scan_cache
                    else:  # create file
                        with open(cache_path, mode='w') as cf:
                            self._log_progress(f"no cache file found, creating a new one...")
                            json.dump(self._init_cache_file_dict(self.target_url), cf)
        except Exception as exc:
            pass  # failed to load cache
        return self._init_cache_scanner_dict()

    def _remove_old_results(self):
        if self._WRITE_RESULTS:
            if os.path.isfile(self.results_path_full):
                os.remove(self.results_path_full)

    def _supports_cache(self) -> bool:
        return self.__class__._SUPPORTS_CACHE

    def _get_results_filename(self) -> str:
        return f"{self.__class__.__name__}.txt"

    def _get_cache_filename(self) -> str:
        return f"cache_{self._format_name_for_path(self.target_hostname)}.json"

    @staticmethod
    def _init_cache_file_dict(target_url: str) -> dict:
        return {
            "target_url": target_url,
            "scanners": dict()
        }

    def _init_cache_scanner_dict(self) -> dict:
        return {
            "wordlist_filehash": get_filehash(self.wordlist_path),
            "results_filehash": "",
            "finished": 0,
            "run_id": ScanManager._RUN_ID,
            "timestamp": time.time()
        }

    @lru_cache
    def _get_results_directory(self, *args, **kwargs) -> str:
        path = os.path.join(self.results_path,
                            self._format_name_for_path(self.target_hostname),
                            self._format_name_for_path(self.target_url))

        return path

    @lru_cache
    def _get_cache_directory(self) -> str:
        return ScannerDefaultParams.DefaultCacheDirectory

    def _get_results_fullpath(self) -> str:
        return os.path.join(self._get_results_directory(), self._get_results_filename())

    def _get_cache_fullpath(self) -> str:
        return os.path.join(self._get_cache_directory(), self._get_cache_filename())

    def _clear_cache_file(self):
        if self._supports_cache():
            with ScanManager._CACHE_MUTEX:
                cache_path = self._get_cache_fullpath()
                if os.path.exists(cache_path):
                    os.remove(cache_path)

    @lru_cache(maxsize=5)
    def generate_url_base_path(self, dnsname: str) -> str:
        return f"{self.scheme}://{dnsname}.{self.target_hostname}" if \
            dnsname is not None else f"{self.scheme}://{self.target_hostname}"

    @lru_cache(maxsize=5)
    def _format_name_for_path(self, name: str) -> str:
        return name.replace(f'{self.scheme}://', '').replace('.', '_')

    def _update_progress_status(self, finished_c, total_c, current: str, force_update=False):
        with self._current_progress_mutex:
            progress = (100 * finished_c) // total_c
            with ScanManager._CACHE_MUTEX:
                self._cache_dict["finished"] = finished_c
            if progress % OutputProgBarParams.ProgBarIntvl == 0 and progress > self._current_progress_perc or \
                    force_update:
                print_prog_mod = OutputProgBarParams.ProgressMod
                prog_count = progress // print_prog_mod
                prog_str = f"[{('#' * prog_count).ljust(OutputProgBarParams.ProgressMax, '-')}]"
                self._log_status(OutputStatusKeys.Progress, prog_str, refresh_output=False)
                self._current_progress_perc = progress
                force_update = True  # update current also
            left = self._count_multiplier * (total_c - finished_c)
            if finished_c % OutputProgBarParams.ProgLeftIntvl == 0 or left == 0 or force_update:
                self._log_status(OutputStatusKeys.Current, current, refresh_output=False)
                self._log_status(OutputStatusKeys.Left,
                                 f"{self._count_multiplier * total_c - self._count_multiplier * finished_c} "
                                 f"out of {self._count_multiplier * total_c}")

    def abort_scan(self, reason=None):
        ScanManager._SHOULD_ABORT = True
        self._log_status(OutputStatusKeys.State, OutputValues.StateFail)
        if reason:
            self._log_exception(reason, ScanManager._SHOULD_ABORT)
        os.kill(os.getpid(), 9)

    @staticmethod
    def truncate_str(text: str) -> str:
        return f"...{text[-OutputDefaultParams.StrTruncLimit:]}" if \
            len(text) > OutputDefaultParams.StrTruncLimit else text


class Scanner(ScanManager):
    SCAN_NICKNAME = None  # overwrite for each scan individually

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.wordlist_path:
            self.words_queue: queue.Queue = self.load_words()

        self._total_count = self.words_queue.qsize() if self.wordlist_path else 0
        self._finished_count = 0
        self._success_count = 0
        self._count_mutex = threading.RLock()

        self.request_cooldown = kwargs.get("request_cooldown")
        self.thread_count = kwargs.get("thread_count")
        self.request_timeout = kwargs.get("request_timeout")

        self._default_headers = dict()  # for rotating user agents
        self._session: Union[requests.Session, None] = None
        self._setup_session()

    def load_words(self) -> queue.Queue:
        try:
            with open(self.wordlist_path, 'r') as wl:
                words = queue.Queue()
                for word in wl.readlines()[max(self._cache_dict.get("finished", 0) - 1, 0):]:
                    words.put(word.strip("\n"))
            return words
        except Exception as exc:
            self._log_exception(exc, True)
            raise InvalidPathLoad("wordlist", self.wordlist_path)

    def _update_count(self, current, success=False):
        with self._count_mutex:
            self._finished_count += 1
            self._update_progress_status(self._finished_count, self._total_count, current)
            if success:
                self._success_count += 1
                self._log_status(OutputStatusKeys.Found, self._success_count)

    def start_scanner(self) -> Any:
        try:
            self._log_progress("starting...")
            self._log_status(OutputStatusKeys.State, OutputValues.StateSetup)
            scan_results = self._start_scanner()
            self._log_status(OutputStatusKeys.State, OutputValues.StateComplete)
            self._log_progress("status finished")
            return scan_results
        except Exception as exc:
            ScanManager._SHOULD_ABORT = True
            self._log_status(OutputStatusKeys.State, OutputValues.StateFail)
            self._log_exception(exc, ScanManager._SHOULD_ABORT)
            os.kill(os.getpid(), 9)

    @abstractmethod
    def _start_scanner(self) -> Any:
        ...

    def _setup_session(self):
        if self.scheme not in ScannerDefaultParams.AcceptedSchemes:
            raise UnsupportedScheme(self.scheme)

        if not self.target_url:
            raise MissingTargetURL

        self._default_headers.clear()
        self._default_headers['User-Agent'] = get_random_useragent()

        self._session = requests.Session()

    def _make_request(self, method: str, url: str, headers=None, timeout=None, **kwargs):
        if not headers:
            headers = dict()
        headers.update(self._default_headers)

        res = self._session.request(method=method, url=url, headers=headers, timeout=timeout or self.request_timeout,
                                    verify=False, **kwargs)

        if res.status_code == ScannerDefaultParams.LimitRateSCode:
            self._log_exception("too many requests", abort=False)
            time.sleep(NetworkDefaultParams.TooManyReqSleep)

        return res

    def _sleep_after_request(self):
        if self.request_cooldown:
            time.sleep(self.request_cooldown)


if __name__ == "__main__":
    pass
