#!/usr/bin/env python
"""The multi-user notebook application"""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import getpass
import io
import logging
import os
from subprocess import Popen

try:
    raw_input
except NameError:
    # py3
    raw_input = input

from jinja2 import Environment, FileSystemLoader

import tornado.httpserver
import tornado.options
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.log import LogFormatter
from tornado import gen, web

from IPython.utils.traitlets import (
    Unicode, Integer, Dict, TraitError, List, Bool, Any,
    DottedObjectName, Set,
)
from IPython.config import Application, catch_config_error
from IPython.utils.importstring import import_item

here = os.path.dirname(__file__)

from . import handlers, apihandlers

from . import orm
from ._data import DATA_FILES_PATH
from .utils import url_path_join, random_hex, TimeoutError

# classes for config
from .auth import Authenticator, PAMAuthenticator
from .spawner import Spawner, LocalProcessSpawner

aliases = {
    'log-level': 'Application.log_level',
    'f': 'JupyterHubApp.config_file',
    'config': 'JupyterHubApp.config_file',
    'y': 'JupyterHubApp.answer_yes',
    'ssl-key': 'JupyterHubApp.ssl_key',
    'ssl-cert': 'JupyterHubApp.ssl_cert',
    'ip': 'JupyterHubApp.ip',
    'port': 'JupyterHubApp.port',
    'db': 'JupyterHubApp.db_url',
    'pid-file': 'JupyterHubApp.pid_file',
}

flags = {
    'debug': ({'Application' : {'log_level' : logging.DEBUG}},
        "set log level to logging.DEBUG (maximize logging output)"),
    'generate-config': ({'JupyterHubApp': {'generate_config' : True}},
        "generate default config file")
}


class JupyterHubApp(Application):
    """An Application for starting a Multi-User Jupyter Notebook server."""
    
    description = """Start a multi-user Jupyter Notebook server
    
    Spawns a configurable-http-proxy and multi-user Hub,
    which authenticates users and spawns single-user Notebook servers
    on behalf of users.
    """
    
    examples = """
    
    generate default config file:
    
        jupyterhub --generate-config -f myconfig.py
    
    spawn the server on 10.0.1.2:443 with https:
    
        jupyterhub --ip 10.0.1.2 --port 443 --ssl-key my_ssl.key --ssl-cert my_ssl.cert
    """
    
    aliases = Dict(aliases)
    flags = Dict(flags)
    
    classes = List([
        Spawner,
        LocalProcessSpawner,
        Authenticator,
        PAMAuthenticator,
    ])
    
    config_file = Unicode('jupyter_hub_config.py', config=True,
        help="The config file to load",
    )
    generate_config = Bool(False, config=True,
        help="Generate default config file",
    )
    answer_yes = Bool(False, config=True,
        help="Answer yes to any questions (e.g. confirm overwrite)"
    )
    pid_file = Unicode('', config=True,
        help="""File to write PID
        Useful for daemonizing jupyterhub.
        """
    )
    proxy_check_interval = Integer(int(1e4), config=True,
        help="Interval (in ms) at which to check if the proxy is running."
    )
    
    data_files_path = Unicode(DATA_FILES_PATH, config=True,
        help="The location of jupyter data files (e.g. /usr/local/share/jupyter)"
    )
    
    ssl_key = Unicode('', config=True,
        help="""Path to SSL key file for the public facing interface of the proxy
        
        Use with ssl_cert
        """
    )
    ssl_cert = Unicode('', config=True,
        help="""Path to SSL certificate file for the public facing interface of the proxy
        
        Use with ssl_key
        """
    )
    ip = Unicode('', config=True,
        help="The public facing ip of the proxy"
    )
    port = Integer(8000, config=True,
        help="The public facing port of the proxy"
    )
    base_url = Unicode('/', config=True,
        help="The base URL of the entire application"
    )
    
    jinja_environment_options = Dict(config=True,
        help="Supply extra arguments that will be passed to Jinja environment."
    )
    
    proxy_cmd = Unicode('configurable-http-proxy', config=True,
        help="""The command to start the http proxy.
        
        Only override if configurable-http-proxy is not on your PATH
        """
    )
    proxy_auth_token = Unicode(config=True,
        help="The Proxy Auth token"
    )
    def _proxy_auth_token_default(self):
        return orm.new_token()
    
    proxy_api_ip = Unicode('localhost', config=True,
        help="The ip for the proxy API handlers"
    )
    proxy_api_port = Integer(config=True,
        help="The port for the proxy API handlers"
    )
    def _proxy_api_port_default(self):
        return self.port + 1
    
    hub_port = Integer(8081, config=True,
        help="The port for this process"
    )
    hub_ip = Unicode('localhost', config=True,
        help="The ip for this process"
    )
    
    hub_prefix = Unicode('/hub/', config=True,
        help="The prefix for the hub server. Must not be '/'"
    )
    def _hub_prefix_default(self):
        return url_path_join(self.base_url, '/hub/')
    
    def _hub_prefix_changed(self, name, old, new):
        if new == '/':
            raise TraitError("'/' is not a valid hub prefix")
        newnew = new
        if not new.startswith('/'):
            newnew = '/' + new
        if not newnew.endswith('/'):
            newnew = newnew + '/'
        if not newnew.startswith(self.base_url):
            newnew = url_path_join(self.base_url, newnew)
        if newnew != new:
            self.hub_prefix = newnew
    
    cookie_secret = Unicode(config=True,
        help="""The cookie secret to use to encrypt cookies.
        Loaded from the JPY_COOKIE_SECRET env variable by default.
        """
    )
    def _cookie_secret_default(self):
        return os.environ.get('JPY_COOKIE_SECRET', random_hex(64))
    
    authenticator = DottedObjectName("jupyterhub.auth.PAMAuthenticator", config=True,
        help="""Class for authenticating users.
        
        This should be a class with the following form:
        
        - constructor takes one kwarg: `config`, the IPython config object.
        
        - is a tornado.gen.coroutine
        - returns username on success, None on failure
        - takes two arguments: (handler, data),
          where `handler` is the calling web.RequestHandler,
          and `data` is the POST form data from the login page.
        """
    )
    # class for spawning single-user servers
    spawner_class = DottedObjectName("jupyterhub.spawner.LocalProcessSpawner", config=True,
        help="""The class to use for spawning single-user servers.
        
        Should be a subclass of Spawner.
        """
    )
    
    db_url = Unicode('sqlite:///:memory:', config=True)
    debug_db = Bool(False)
    db = Any()
    
    admin_users = Set({getpass.getuser()}, config=True,
        help="""list of usernames of admin users

        If unspecified, only the user that launches the server will be admin.
        """
    )
    tornado_settings = Dict(config=True)
    
    handlers = List()
    
    _log_formatter_cls = LogFormatter
    
    def _log_level_default(self):
        return logging.INFO
    
    def _log_datefmt_default(self):
        """Exclude date from default date format"""
        return "%H:%M:%S"
    
    def _log_format_default(self):
        """override default log format to include time"""
        return u"%(color)s[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s]%(end_color)s %(message)s"
    
    def _log_format_changed(self, name, old, new):
        """Change the log formatter when log_format is set."""
        # FIXME: IPython < 3 compat
        _log_handler = self.log.handlers[0]
        _log_formatter = self._log_formatter_cls(fmt=new, datefmt=self.log_datefmt)
        _log_handler.setFormatter(_log_formatter)
    
    def init_logging(self):
        # This prevents double log messages because tornado use a root logger that
        # self.log is a child of. The logging module dipatches log messages to a log
        # and all of its ancenstors until propagate is set to False.
        self.log.propagate = False
        
        # hook up tornado 3's loggers to our app handlers
        logger = logging.getLogger('tornado')
        logger.propagate = True
        logger.parent = self.log
        logger.setLevel(self.log.level)
        # FIXME: IPython < 3 compat
        self._log_format_changed('', '', self.log_format)
    
    def init_ports(self):
        if self.hub_port == self.port:
            raise TraitError("The hub and proxy cannot both listen on port %i" % self.port)
        if self.hub_port == self.proxy_api_port:
            raise TraitError("The hub and proxy API cannot both listen on port %i" % self.hub_port)
        if self.proxy_api_port == self.port:
            raise TraitError("The proxy's public and API ports cannot both be %i" % self.port)
    
    @staticmethod
    def add_url_prefix(prefix, handlers):
        """add a url prefix to handlers"""
        for i, tup in enumerate(handlers):
            lis = list(tup)
            lis[0] = url_path_join(prefix, tup[0])
            handlers[i] = tuple(lis)
        return handlers
    
    def init_handlers(self):
        h = []
        h.extend(handlers.default_handlers)
        h.extend(apihandlers.default_handlers)

        self.handlers = self.add_url_prefix(self.hub_prefix, h)


        # some extra handlers, outside hub_prefix
        self.handlers.extend([
            (r"%s" % self.hub_prefix.rstrip('/'), web.RedirectHandler,
                {
                    "url": self.hub_prefix,
                    "permanent": False,
                }
            ),
            (r"(?!%s).*" % self.hub_prefix, handlers.PrefixRedirectHandler),
            (r'(.*)', handlers.Template404),
        ])
    
    def init_db(self):
        # TODO: load state from db for resume
        # TODO: if not resuming, clear existing db contents
        self.db = orm.new_session(self.db_url, echo=self.debug_db)
        for name in self.admin_users:
            user = orm.User(name=name, admin=True)
            self.db.add(user)
        self.db.commit()
    
    def init_hub(self):
        """Load the Hub config into the database"""
        self.hub = orm.Hub(
            server=orm.Server(
                ip=self.hub_ip,
                port=self.hub_port,
                base_url=self.hub_prefix,
                cookie_secret=self.cookie_secret,
                cookie_name='jupyter-hub-token',
            )
        )
        self.db.add(self.hub)
        self.db.commit()
    
    def init_proxy(self):
        """Load the Proxy config into the database"""
        self.proxy = orm.Proxy(
            public_server=orm.Server(
                ip=self.ip,
                port=self.port,
            ),
            api_server=orm.Server(
                ip=self.proxy_api_ip,
                port=self.proxy_api_port,
                base_url='/api/routes/'
            ),
            auth_token = orm.new_token(),
        )
        self.db.add(self.proxy)
        self.db.commit()
    
    @gen.coroutine
    def start_proxy(self):
        """Actually start the configurable-http-proxy"""
        env = os.environ.copy()
        env['CONFIGPROXY_AUTH_TOKEN'] = self.proxy.auth_token
        cmd = [self.proxy_cmd,
            '--ip', self.proxy.public_server.ip,
            '--port', str(self.proxy.public_server.port),
            '--api-ip', self.proxy.api_server.ip,
            '--api-port', str(self.proxy.api_server.port),
            '--default-target', self.hub.server.host,
        ]
        if self.log_level == logging.DEBUG:
            cmd.extend(['--log-level', 'debug'])
        if self.ssl_key:
            cmd.extend(['--ssl-key', self.ssl_key])
        if self.ssl_cert:
            cmd.extend(['--ssl-cert', self.ssl_cert])
        self.log.info("Starting proxy: %s", cmd)
        self.proxy_process = Popen(cmd, env=env)
        def _check():
            status = self.proxy_process.poll()
            if status is not None:
                e = RuntimeError("Proxy failed to start with exit code %i" % status)
                # py2-compatible `raise e from None`
                e.__cause__ = None
                raise e
        
        for server in (self.proxy.public_server, self.proxy.api_server):
            for i in range(10):
                _check()
                try:
                    yield server.wait_up(1)
                except TimeoutError:
                    continue
                else:
                    break
            yield server.wait_up(1)
        self.log.debug("Proxy started and appears to be up")
    
    @gen.coroutine
    def check_proxy(self):
        if self.proxy_process.poll() is None:
            return
        self.log.error("Proxy stopped with exit code %i", self.proxy_process.poll())
        yield self.start_proxy()
        self.log.info("Setting up routes on new proxy")
        yield self.proxy.add_all_users()
        self.log.info("New proxy back up, and good to go")
    
    def init_tornado_settings(self):
        """Set up the tornado settings dict."""
        base_url = self.base_url
        template_path = os.path.join(self.data_files_path, 'templates'),
        jinja_env = Environment(
            loader=FileSystemLoader(template_path),
            **self.jinja_environment_options
        )
        
        settings = dict(
            config=self.config,
            log=self.log,
            db=self.db,
            proxy=self.proxy,
            hub=self.hub,
            admin_users=self.admin_users,
            authenticator=import_item(self.authenticator)(config=self.config),
            spawner_class=import_item(self.spawner_class),
            base_url=base_url,
            cookie_secret=self.cookie_secret,
            login_url=url_path_join(self.hub.server.base_url, 'login'),
            static_path=os.path.join(self.data_files_path, 'static'),
            static_url_prefix=url_path_join(self.hub.server.base_url, 'static/'),
            template_path=template_path,
            jinja2_env=jinja_env,
        )
        # allow configured settings to have priority
        settings.update(self.tornado_settings)
        self.tornado_settings = settings
    
    def init_tornado_application(self):
        """Instantiate the tornado Application object"""
        self.tornado_application = web.Application(self.handlers, **self.tornado_settings)
    
    def write_pid_file(self):
        pid = os.getpid()
        if self.pid_file:
            self.log.debug("Writing PID %i to %s", pid, self.pid_file)
            with io.open(self.pid_file, 'w') as f:
                f.write(u'%i' % pid)
    
    @catch_config_error
    def initialize(self, *args, **kwargs):
        super(JupyterHubApp, self).initialize(*args, **kwargs)
        if self.generate_config:
            return
        self.load_config_file(self.config_file)
        self.write_pid_file()
        self.init_logging()
        self.init_ports()
        self.init_db()
        self.init_hub()
        self.init_proxy()
        self.init_handlers()
        self.init_tornado_settings()
        self.init_tornado_application()
    
    @gen.coroutine
    def cleanup(self):
        """Shutdown our various subprocesses and cleanup runtime files."""
        self.log.info("Cleaning up single-user servers...")
        # request (async) process termination
        futures = []
        for user in self.db.query(orm.User):
            if user.spawner is not None:
                futures.append(user.spawner.stop())
        
        # clean up proxy while SUS are shutting down
        self.log.info("Cleaning up proxy[%i]..." % self.proxy_process.pid)
        self.proxy_process.terminate()
        
        # wait for the requests to stop finish:
        for f in futures:
            yield f
        
        if self.pid_file and os.path.exists(self.pid_file):
            self.log.info("Cleaning up PID file %s", self.pid_file)
            os.remove(self.pid_file)
        
        # finally stop the loop once we are all cleaned up
        self.log.info("...done")
    
    def write_config_file(self):
        if os.path.exists(self.config_file) and not self.answer_yes:
            answer = ''
            def ask():
                prompt = "Overwrite %s with default config? [y/N]" % self.config_file
                try:
                    return raw_input(prompt).lower() or 'n'
                except KeyboardInterrupt:
                    print('') # empty line
                    return 'n'
            answer = ask()
            while not answer.startswith(('y', 'n')):
                print("Please answer 'yes' or 'no'")
                answer = ask()
            if answer.startswith('n'):
                return
        
        config_text = self.generate_config_file()
        print("Writing default config to: %s" % self.config_file)
        with io.open(self.config_file, encoding='utf8', mode='w') as f:
            f.write(config_text)
    
    def start(self):
        """Start the whole thing"""
        if self.generate_config:
            self.write_config_file()
            return
        
        # start the proxy
        try:
            IOLoop().run_sync(self.start_proxy)
        except Exception as e:
            self.log.critical("Failed to start proxy", exc_info=True)
            return
        
        loop = IOLoop.current()
        
        pc = PeriodicCallback(self.check_proxy, self.proxy_check_interval)
        pc.start()
        
        # start the webserver
        http_server = tornado.httpserver.HTTPServer(self.tornado_application)
        http_server.listen(self.hub_port)
        
        try:
            loop.start()
        except KeyboardInterrupt:
            print("\nInterrupted")
        finally:
            # run the cleanup step (in a new loop, because the interrupted one is unclean)
            IOLoop().run_sync(self.cleanup)

main = JupyterHubApp.launch_instance

if __name__ == "__main__":
    main()
