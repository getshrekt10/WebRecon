import copy
import urllib.parse
import pprint

from scanners.utils import arg_parser
from typing import Tuple, List, Type
from scanners import *
from tld import get_tld, get_tld_names

#   --------------------------------------------------------------------------------------------------------------------
#
#   Perform a full web reconnaissance report
#
#   * Scan for content brute
#   * Test 403 with different bypass methods
#   * Nmap for open ports and services
#
#         self.recon_results = {
#             "domain_name": {
#                 "ContentScanner": {
#                     "code": [results],
#                     "code2": [results],
#                     "bypass": {
#                         "url": {
#                             "code": ["curl cmd"],
#                             "code2": ["curl_cmd"]
#                         },
#                         "url2": {
#                             "code": ["curl cmd"],
#                             "code2": ["curl_cmd"]
#                         }
#                     }
#                 }
#                 "NmapScanner": {
#                   ```nmap scan report format```
#               }
#             },
#         }
#
#
#   --------------------------------------------------------------------------------------------------------------------


class WebRecon(ScanManager):
    _SCAN_COLOR = OutputColors.Orange

    _SCANNAME_TO_METHOD_MAP = {
        ScannerNames.DnsScan: None,
        ScannerNames.ContentScan: ContentScanner,
        ScannerNames.BypassScan: None,
        ScannerNames.NmapScan: NmapScanner
    }

    def __init__(self,
                 target_url: str,
                 scans: List[str],
                 results_path: str,
                 *args, **kwargs):
        get_tld_names()

        self._all_scans = scans
        self._scans = self._parse_scan_list(scans)  # only the ones we call using `_do_scan()`


        self.scheme, self.subdomain, self.target_hostname = self._parse_target_url(target_url)
        self._default_general_scanner_args = {
            "scheme": self.scheme,
            "target_hostname": self.target_hostname,
            "results_path": results_path,
        }

        self._default_custom_scanner_args = self._setup_custom_scanner_args(**kwargs)

        self.domains = queue.Queue()
        self.dns_recursion = ScannerNames.DnsScan in self._all_scans

        self.recon_results = dict()

        super().__init__(target_url=self.generate_url_base_path(self.subdomain),
                         *args, **kwargs, **self._default_general_scanner_args)

    def _setup_custom_scanner_args(self, **kwargs) -> Dict[str, dict]:
        default_custom_scanner_args = dict()
        default_custom_scanner_args = {s_name.value:
                                           {"wordlist_path": kwargs.get("wordlist_paths", dict()).get(s_name.value, None)}
                                       for s_name in ScannerNames}
        print(default_custom_scanner_args)

        default_custom_scanner_args[ScannerNames.ContentScan]["do_bypass"] = ScannerNames.BypassScan in self._all_scans

        default_custom_scanner_args[ScannerNames.NmapScan]["cmdline_args"] = kwargs.get("nmap_cmdline")
        default_custom_scanner_args[ScannerNames.NmapScan]["ports"] = kwargs.get("nmap_ports")

        return default_custom_scanner_args

    def _generate_scanner_args(self, scan_name: str) -> dict:
        args = copy.deepcopy(self._default_general_scanner_args)
        args.update(self._default_custom_scanner_args.get(scan_name, dict()))
        return args

    def _parse_scan_list(self, scan_list: List[str]) -> List[Type[Scanner]]:
        scans = list()
        for scan_name in scan_list:
            if scan_name not in self._SCANNAME_TO_METHOD_MAP:
                raise Exception("Bad scanner name")  # TODO exceptions
            scanner = self._SCANNAME_TO_METHOD_MAP.get(scan_name)
            if scanner:
                scans.append(scanner)
        return scans

    def _parse_target_url(self, target_url: str) -> Tuple[str, str, str]:
        parsed_target = urllib.parse.urlparse(target_url)
        scheme = parsed_target.scheme
        netloc = parsed_target.netloc
        sub = netloc.split(".")[0] if self._contains_subdomain(target_url) else ScannerDefaultParams.DefaultSubdomain
        hostname = netloc.split(".", 1)[-1] if self._contains_subdomain(target_url) else netloc

        return scheme, sub, hostname

    def _start_scans_for_target(self, target: str) -> List[threading.Thread]:
        scanner_threads = list()
        for scanner in self._scans:
            scanner_name = scanner.__name__
            t = threading.Thread(target=self._do_scan(scanner, scanner_name, target))
            t.start()
            scanner_threads.append(t)
        return scanner_threads

    def start_recon(self):
        try:
            domains = self._setup_targets()
            domains_count = domains.qsize()
            self._log_status(OutputStatusKeys.State, OutputValues.StateRunning)

            total_count = 0
            while not domains.empty():
                target = domains.get()
                if total_count and target == self.target_url:
                    continue
                self._update_progress_status(total_count, domains_count, target)
                self.recon_results[target] = dict()
                try:
                    scanner_threads = self._start_scans_for_target(target)
                    for t in scanner_threads:
                        t.join()
                except Exception as exc:
                    self._log_exception(f"target {target} exception {exc}", False)
                finally:
                    total_count += 1
                    results_str = pprint.pformat(self.recon_results,
                                                 compact=PPrintDefaultParams.Compact, width=PPrintDefaultParams.Width)
                    self._save_results(results_str, mode='w')

            self._update_progress_status(total_count, domains_count, OutputValues.EmptyStatusVal)
            self._log_status(OutputStatusKeys.State, OutputValues.StateComplete)

            if CacheDefaultParams.ClearWhenFinished:
                self._clear_cache_file()

        except Exception as exc:
            self._log_status(OutputStatusKeys.State, OutputValues.StateFail)
            self._log_exception(exc, True)

    def _setup_targets(self) -> queue.Queue:
        domains = queue.Queue()
        domains.put(self.target_url)
        if self.dns_recursion:
            subdomain_scanner.DNSScanner(target_url=self.target_hostname, domains_queue=self.domains,
                                         **self._generate_scanner_args(DNSScanner.SCAN_NICKNAME)).start_scanner()
        return domains

    def _do_scan(self, scanner_cls: Type[Scanner], scanner_name: str, target: str):
        self.recon_results[target][scanner_name] = dict()
        scanner = scanner_cls(target_url=target,
                              **self._generate_scanner_args(scanner_cls.SCAN_NICKNAME))
        results = scanner.start_scanner()
        if results:
            self.recon_results[target][scanner_name].update(results)

    def _get_results_filename(self, *args, **kwargs) -> str:
        return "results_summary.txt"

    @lru_cache
    def _get_results_directory(self, *args, **kwargs) -> str:
        path = os.path.join(self.results_path,
                            self._format_name_for_path(self.target_hostname))
        return path

    @staticmethod
    def _contains_subdomain(target_url: str):
        return len(target_url.replace(f'.{get_tld(target_url)}', '').split('.')) > 1

    def _define_status_output(self) -> Dict[str, Any]:
        status = dict()
        status[OutputStatusKeys.State] = OutputValues.StateSetup
        status[OutputStatusKeys.Current] = OutputValues.EmptyStatusVal
        status[OutputStatusKeys.Progress] = OutputValues.EmptyStatusVal
        status[OutputStatusKeys.ResultsPath] = self.results_path_full
        status[OutputStatusKeys.Left] = OutputValues.EmptyStatusVal

        return status

    @lru_cache()
    def _get_scanner_name(self, *args, **kwargs) -> str:
        return str()


if __name__ == "__main__":
    parser = arg_parser.get_argument_parser()
    arguments = parser.parse_args()

    WebRecon(target_url=arguments.target_url,
             scans=arg_parser.parse_scan_list(arguments),
             wordlist_paths=arg_parser.parse_wordlist_list(arguments),
             results_path=arguments.results_path,
             nmap_cmdline=ArgParserArgName.NmapCmdlineargs,
             nmap_ports=ArgParserArgName.NmapPorts).start_recon()
