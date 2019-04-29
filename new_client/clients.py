import json
import logging
import socket
from functools import partial
from http.client import RemoteDisconnected
from typing import List, Callable

import certifi
from urllib3 import PoolManager
from urllib3.connection import HTTPConnection
from urllib3.exceptions import MaxRetryError, ReadTimeoutError, ProtocolError

from new_client.node import NodesContainer
from steep.consts import CONDENSER_API
from steepbase.base_client import BaseClient
from steepbase.exceptions import RPCErrorRecoverable, RPCError

logger = logging.getLogger(__name__)


class HttpClient(BaseClient):
    """ Simple Steem JSON-HTTP-RPC API

    This class serves as an abstraction layer for easy use of the Steem API.

    Args:
      nodes (list): A list of Steem HTTP RPC nodes to connect to.

    .. code-block:: python

       from steem.http_client import HttpClient

       rpc = HttpClient(['https://steemd-node1.com',
       'https://steemd-node2.com'])

    any call available to that port can be issued using the instance
    via the syntax ``rpc.call('command', *parameters)``.

    Example:

    .. code-block:: python

       rpc.call(
           'get_followers',
           'furion', 'abit', 'blog', 10,
           api='follow_api'
       )

    """

    success_codes = {200, 301, 302, 303, 307, 308}
    ban_codes = {403, 429}
    unavailability_codes = {502, 503, 504}

    retry_exceptions = (
        MaxRetryError,
        ConnectionResetError,
        ReadTimeoutError,
        RemoteDisconnected,
        ProtocolError,
        RPCErrorRecoverable,
        json.decoder.JSONDecodeError,
    )

    def __init__(self, nodes: List[str], **kwargs):
        """Build pool manager and nodes iterator."""
        super().__init__()

        # self.return_with_args = kwargs.get('return_with_args', False)
        # self.re_raise = kwargs.get('re_raise', True)
        # self.max_workers = kwargs.get('max_workers', None)

        self.pool_manager: PoolManager = self._build_pool_manager(**kwargs)
        self.nodes: NodesContainer = NodesContainer(nodes_urls=nodes)
        self.request: Callable = partial(self.pool_manager.urlopen, 'POST')
        # self._next_node()

        log_level = kwargs.get('log_level', logging.INFO)
        logger.setLevel(log_level)

    @property
    def hostname(self) -> str:
        """Returns hostname of current node."""
        return self.nodes.cur_node.hostname

    def call(self, api_method: str, *args, **kwargs):
        """ Call a remote procedure in steemd.

        Warnings:
            This command will auto-retry in case of node failure, as well as handle
            node fail-over, unless we are broadcasting a transaction.
            In latter case, the exception is **re-raised**.
        """
        for _ in self.nodes.lap():
            print(self.hostname)
            try:
                set_default_api = True
                if 'set_default_api' in kwargs:
                    set_default_api = kwargs['set_default_api']
                    kwargs.pop('set_default_api')

                body_kwargs = kwargs.copy()

                if set_default_api and self.nodes.cur_node.use_condenser_api:
                    body_kwargs['api'] = CONDENSER_API

                body = self.json_rpc_body(api_method, *args, **body_kwargs)
                response = self.request(self.nodes.cur_node.url, body=body)

                if response.status not in self.success_codes:
                    if response.status in self.ban_codes:
                        self.nodes.cur_node.set_banned()
                    elif response.status in self.unavailability_codes:
                        self.nodes.cur_node.set_unavailable()

                    raise RPCErrorRecoverable(
                        'non-200 response: {status} from {host}'.format(
                            status=response.status,
                            host=self.hostname,
                        )
                    )

                result = json.loads(response.data.decode('utf-8'))
                assert result, 'result entirely blank'

                if 'error' in result:
                    # legacy (pre-appbase) nodes always return err code 1
                    legacy = result['error']['code'] == 1
                    detail = result['error']['message']

                    # some errors have no data key (db lock error)
                    if 'data' not in result['error']:
                        error = 'error'
                    # some errors have no name key (jussi errors)
                    elif 'name' not in result['error']['data']:
                        error = 'unspecified error'
                    else:
                        error = result['error']['data']['name']

                    if legacy:
                        detail = ":".join(detail.split("\n")[0:2])
                        if self.nodes.cur_node.use_condenser_api:
                            self.nodes.cur_node.use_condenser_api = False
                            logger.error('Downgrade-retry %s', self.hostname)
                            continue

                    detail = '{err} from {host} ({detail}) in {method}'.format(
                        err=error,
                        host=self.hostname,
                        detail=detail,
                        method=api_method,
                    )

                    if self._is_error_recoverable(result['error']):
                        raise RPCErrorRecoverable(detail)
                    else:
                        raise RPCError(detail)

                return result['result']

            except self.retry_exceptions as e:
                logger.error(
                    'Failed to call API - {exception}: {details}'.format(
                        exception=e.__class__.__name__,
                        details=e,
                    )
                )

                self.nodes.next_node()

            except Exception as e:
                logger.error(
                    'Unexpected exception - {exception}: {details}'.format(
                        exception=e.__class__.__name__,
                        details=e,
                    ),
                    extra={'err': e, 'request': self.request},
                )

                self.nodes.next_node()

    def _is_error_recoverable(self, error: dict) -> bool:
        """Checks if the error is recoverable."""
        assert 'message' in error, "missing error msg key: {}".format(error)
        assert 'code' in error, "missing error code key: {}".format(error)
        message = error['message']
        code = error['code']

        # common steemd error
        # {"code"=>-32003, "message"=>"Unable to acquire database lock"}
        if message == 'Unable to acquire database lock':
            return True

        # rare steemd error
        # {"code"=>-32000, "message"=>"Unknown exception", "data"=>"0 exception: unspecified\nUnknown Exception\n[...]"}
        if message == 'Unknown exception':
            return True

        # generic jussi error
        # {'code': -32603, 'message': 'Internal Error', 'data': {'error_id': 'c7a15140-f306-4727-acbd-b5e8f3717e9b',
        #         'request': {'amzn_trace_id': 'Root=1-5ad4cb9f-9bc86fbca98d9a180850fb80', 'jussi_request_id': None}}}
        if message == 'Internal Error' and code == -32603:
            return True

        return False

    def _build_pool_manager(self, **kwargs) -> PoolManager:
        """Builds Pool manager according giver kwargs."""
        num_pools = kwargs.get('num_pools', 10)
        maxsize = kwargs.get('maxsize', 10)
        timeout = kwargs.get('timeout', 20)
        retries = kwargs.get('retries', 5)
        pool_block = kwargs.get('pool_block', False)
        tcp_keepalive = kwargs.get('tcp_keepalive', True)

        if tcp_keepalive:
            socket_options = HTTPConnection.default_socket_options + \
                             [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1), ]
        else:
            socket_options = HTTPConnection.default_socket_options

        return PoolManager(
            num_pools=num_pools,
            maxsize=maxsize,
            block=pool_block,
            timeout=timeout,
            retries=retries,
            socket_options=socket_options,
            headers={'Content-Type': 'application/json'},
            cert_reqs='CERT_REQUIRED',
            ca_certs=certifi.where(),
        )


nodes = [
    'https://api.steemit.com',
    'https://api.steemitdev.com',
    'https://api.steemitstage.com',
    'https://api.steem.house',
    'https://appbasetest.timcliff.com',
    'https://appbase.buildteam.io',
    'https://gtg.steem.house:8090',
    'https://rpc.curiesteem.com',
    'https://rpc.steemviz.com',
    'https://steemd.minnowsupportproject.org',
    'https://steemd.privex.io',
    'https://rpc.usesteem.com',
]


x = HttpClient(nodes)
for _ in range(3):
    print('result', x.call('follow_api.get_reblogged_by', 'steemit', 'firstpost', set_default_api=False))


# for node in nodes:
#     x = HttpClient([node])
#     config = x.call('get_config', api='database_api')
#     if config is not None:
#         print(node, config['STEEM_BLOCKCHAIN_HARDFORK_VERSION'], config['STEEM_BLOCKCHAIN_VERSION'])
