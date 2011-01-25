# Copyright (C) 2007-2010 Samuel Abels.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""
An abstract base class for all protocols.
"""
import re
import sys
import os
from drivers             import driver_map, isdriver
from OsGuesser           import OsGuesser
from Exception           import TransportException, \
                                InvalidCommandException, \
                                LoginFailure, \
                                TimeoutException, \
                                DriverReplacedException, \
                                ExpectCancelledException
from Exscript.util.crypt import otp
from Exscript.util.event import Event
from Exscript.util.cast  import to_regexs

_skey_re = re.compile(r'(?:s\/key|otp-md4) (\d+) (\S+)')

class Transport(object):
    """
    This is the base class for all protocols; it defines the common portions 
    of the API.

    The goal of all transport classes is to provide an interface that
    is unified accross protocols, such that the adapters may be used
    interchangably without changing any other code.

    In order to achieve this, the main challenge are the differences
    arising from the authentication methods that are used.
    The reason is that many devices may support the following variety
    authentification/authorization methods:

    1. Protocol level authentification, such as SSH's built-in
       authentication.

            - p1: password only
            - p2: username
            - p3: username + password
            - p4: username + key
            - p5: username + key + password

    2. App level authentification, such that the authentification may
       happen long after a connection is already accepted.
       This type of authentication is normally used in combination with
       Telnet, but some SSH hosts also do this (users have reported
       devices from Enterasys). These devices may also combine
       protocol-level authentification with app-level authentification.
       The following types of app-level authentication exist:

            - a1: password only
            - a2: username
            - a3: username + password

    3. App level authorization: In order to implement the AAA protocol,
       some devices ask for two separate app-level logins, whereas the
       first serves to authenticate the user, and the second serves to
       authorize him.
       App-level authorization may support the same methods as app-level
       authentification:

            - A1: password only
            - A2: username
            - A3: username + password

    We are assuming that the following methods are used:

        - Telnet:

          - p1 - p5: never
          - a1 - a3: optional
          - A1 - A3: optional

        - SSH:

          - p1 - p5: optional
          - a1 - a3: optional
          - A1 - A3: optional

    To achieve authentication method compatibility accross different
    protocols, we must hide all this complexity behind one single API
    call, and figure out which ones are supported.

    As a use-case, our goal is that the following code will always work,
    regardless of which combination of authentication methods a device
    supports::

            key = PrivateKey.from_file('~/.ssh/id_rsa', 'my_key_password')

            # The user account to use for protocol level authentification.
            # The key defaults to None, in which case key authentication is
            # not attempted.
            account = Account(name     = 'myuser',
                              password = 'mypassword',
                              key      = key)

            # The account to use for app-level authentification.
            # password2 defaults to password.
            app_account = Account(name      = 'myuser',
                                  password  = 'my_app_password',
                                  password2 = 'my_app_password2')

            # app_account defaults to account.
            conn.login(account, app_account = None, flush = True)

    Another important consideration is that once the login is complete, the
    device must be in a clearly defined state, i.e. we need to
    have processed the data that was retrieved from the connected host.

    More precisely, the buffer that contains the incoming data must be in
    a state such that the following call to expect_prompt() will either
    always work, or always fail.

    So what can we hide behind the login() call? Assuming we always try every
    method::

        if app_account is None:
            app_account = account
        # Protocol level authentification.
        conn.authenticate(account, flush = False)
        # App-level authentification.
        conn.app_authenticate(app_account, flush = False)
        # App-level authorization.
        conn.app_authorize(app_account, flush = False)

    The code could produce the following result::

        Telnet:
            conn.authenticate -> NOP
            conn.app_authenticate
                -> waits for username or password prompt, authenticates,
                   returns after a CLI prompt was seen.
            conn.app_authorize
                -> calls driver.enable(), waits for username or password prompt,
                   authorizes, returns after a CLI prompt was seen.

        SSH:
            conn.authenticate -> authenticates using user/key/password
            conn.app_authenticate -> like Telnet
            conn.app_authorize -> like Telnet

    We can see the following:

        - authenticate() must not wait for a prompt, because else
          app_authenticate() has no way of knowing whether an app-level
          login is even necessary.

        - app_authenticate() must check the buffer first, to see if
          authentication has already succeeded. In the case that
          app_authenticate() is not necessary (i.e. the buffer contains a
          CLI prompt), it just returns.

          app_authenticate() must NOT eat the prompt from the buffer, because
          else the result may be inconsistent with devices that do not do
          any authentication; i.e., when app_authenticate() is not called.

        - Since the prompt must still be contained in the buffer,
          conn.driver.app_authorize() needs to eat it before it sends the
          command for starting the authorization procedure.

          This has a drawback - if a user attempts to call app_authenticate()
          at a time where there is no prompt in the buffer, it would fail.
          But this is actually good - the user is supposed to call send() in
          such cases, because else a CLI prompt would be expected, while the
          device responds with a username or password prompt.

          However, app_authorize() must never eat the CLI prompt that follows.

        - Once all logins are processed, it makes sense to eat the prompt
          depending on the wait parameter. Wait should default to True,
          because it's better that the connection stalls waiting forever,
          than to risk that an error is not immediately discovered due to
          timing issues (this is a race condition that I'm not going to
          detail here).
    """

    def __init__(self, **kwargs):
        """
        Constructor.
        The following events are provided:

          - data_received_event: A packet was received from the connected host.
          - otp_requested_event: The connected host requested a
          one-time-password to be entered.

        @type  kwargs: dict
        @param kwargs: The following arguments are supported:
          - driver: passed to set_driver().
          - stdout: Where to write the device response. Defaults to os.devnull.
          - stderr: Where to write debug info. Defaults to stderr.
          - debug: An integer between 0 (no debugging) and 5 (very verbose 
          debugging) that specifies the amount of debug info sent to the 
          terminal. The default value is 0.
          - timeout: See set_timeout(). The default value is 30.
          - logfile: A file into which a log of the conversation with the 
          device is dumped.
        """
        self.data_received_event   = Event()
        self.otp_requested_event   = Event()
        self.os_guesser            = OsGuesser(self)
        self.auto_driver           = driver_map[self.guess_os()]
        self.authenticated         = False
        self.app_authenticated     = False
        self.app_authorized        = False
        self.manual_user_re        = None
        self.manual_password_re    = None
        self.manual_prompt_re      = None
        self.manual_error_re       = None
        self.manual_login_error_re = None
        self.driver_replaced       = False
        self.manual_driver         = kwargs.get('driver')
        self.host                  = kwargs.get('host',    None)
        self.last_account          = kwargs.get('account', None)
        self.stdout                = kwargs.get('stdout')
        self.stderr                = kwargs.get('stderr',  sys.stderr)
        self.debug                 = kwargs.get('debug',   0)
        self.timeout               = kwargs.get('timeout', 30)
        self.logfile               = kwargs.get('logfile', None)
        self.log                   = None
        self.response              = None
        if not self.stdout:
            self.stdout = open(os.devnull, 'w')
        if self.logfile is not None:
            self.log = open(kwargs['logfile'], 'a')

    def __copy__(self):
        """
        Overwritten to return the very same object instead of copying the
        stream, because copying a network connection is impossible.

        @rtype:  Transport
        @return: self
        """
        return self

    def __deepcopy__(self, memo):
        """
        Overwritten to return the very same object instead of copying the
        stream, because copying a network connection is impossible.

        @type  memo: object
        @param memo: Please refer to Python's standard library documentation.
        @rtype:  Transport
        @return: self
        """
        return self

    def _driver_replaced_notify(self, old, new):
        self.driver_replaced = True
        self.cancel_expect()
        msg = 'Transport: driver replaced: %s -> %s' % (old.name, new.name)
        self._dbg(1, msg)

    def _receive_cb(self, data, **kwargs):
        data = data.replace(chr(13) + chr(0), '')
        text = data.replace('\r', '')
        self.stdout.write(text)
        self.stdout.flush()
        if self.log is not None:
            self.log.write(text)
        old_driver = self.get_driver()
        self.os_guesser.data_received(data)
        os               = self.guess_os()
        self.auto_driver = driver_map[os]
        new_driver       = self.get_driver()
        if old_driver != new_driver:
            self._driver_replaced_notify(old_driver, new_driver)
        self.data_received_event(data)
        return data

    def is_dummy(self):
        """
        Returns True if the adapter implements a virtual device, i.e.
        it isn't an actual network connection.

        @rtype:  Boolean
        @return: True for dummy adapters, False for network adapters.
        """
        return False

    def _otp_cb(self, seq, seed):
        self.otp_requested_event(seq, seed)

    def _dbg(self, level, msg):
        if self.debug < level:
            return
        self.stderr.write(self.get_driver().name + ': ' + msg + '\n')

    def set_driver(self, driver = None):
        """
        Defines the driver that is used to recognize prompts and implement
        behavior depending on the remote system.
        The driver argument may be an subclass of protocols.drivers.Driver,
        a known driver name (string), or None.
        If the driver argument is None, the adapter automatically chooses
        a driver using the the guess_os() function.

        @type  driver: Driver|str
        @param driver: The pattern that, when matched, causes an error.
        """
        if driver is None:
            self.manual_driver = None
        elif isinstance(driver, str):
            if driver not in driver_map:
                raise TypeError('no such driver:' + repr(driver))
            self.manual_driver = driver_map[driver]
        elif isdriver(driver):
            self.manual_driver = driver
        else:
            raise TypeError('unsupported argument type:' + type(driver))

    def get_driver(self):
        """
        Returns the currently used driver.

        @rtype:  Driver
        @return: A regular expression.
        """
        if self.manual_driver:
            return self.manual_driver
        return self.auto_driver

    def autoinit(self):
        """
        Make the remote host more script-friendly by automatically executing
        one or more commands on it.
        The commands executed depend on the currently used driver.
        For example, the driver for Cisco IOS would execute the
        following commands::

            term len 0
            term width 0
        """
        self.get_driver().init_terminal(self)

    def set_username_prompt(self, regex = None):
        """
        Defines a pattern that is used to monitor the response of the
        connected host for a username prompt.

        @type  regex: RegEx
        @param regex: The pattern that, when matched, causes an error.
        """
        if regex is None:
            self.manual_user_re = regex
        else:
            self.manual_user_re = to_regexs(regex)

    def get_username_prompt(self):
        """
        Returns the regular expression that is used to monitor the response
        of the connected host for a username prompt.

        @rtype:  regex
        @return: A regular expression.
        """
        if self.manual_user_re:
            return self.manual_user_re
        return self.get_driver().user_re

    def set_password_prompt(self, regex = None):
        """
        Defines a pattern that is used to monitor the response of the
        connected host for a password prompt.

        @type  regex: RegEx
        @param regex: The pattern that, when matched, causes an error.
        """
        if regex is None:
            self.manual_password_re = regex
        else:
            self.manual_password_re = to_regexs(regex)

    def get_password_prompt(self):
        """
        Returns the regular expression that is used to monitor the response
        of the connected host for a username prompt.

        @rtype:  regex
        @return: A regular expression.
        """
        if self.manual_password_re:
            return self.manual_password_re
        return self.get_driver().password_re

    def set_prompt(self, prompt = None):
        """
        Defines a pattern that is waited for when calling the expect_prompt() 
        method.
        If the set_prompt() method is not called, or if it is called with the 
        prompt argument set to None, a default prompt is used that should 
        work with many devices running Unix, IOS, IOS-XR, or Junos and others.

        @type  prompt: RegEx
        @param prompt: The pattern that matches the prompt of the remote host.
        """
        if prompt is None:
            self.manual_prompt_re = prompt
        else:
            self.manual_prompt_re = to_regexs(prompt)

    def get_prompt(self):
        """
        Returns the regular expressions that is matched against the host
        response when calling the expect_prompt() method.

        @rtype:  list(re.RegexObject)
        @return: A list of regular expression objects.
        """
        if self.manual_prompt_re:
            return self.manual_prompt_re
        return self.get_driver().prompt_re

    def set_error_prompt(self, error = None):
        """
        Defines a pattern that is used to monitor the response of the
        connected host. If the pattern matches (any time the expect() or
        expect_prompt() methods are used), an error is raised.

        @type  error: RegEx
        @param error: The pattern that, when matched, causes an error.
        """
        if error is None:
            self.manual_error_re = error
        else:
            self.manual_error_re = to_regexs(error)

    def get_error_prompt(self):
        """
        Returns the regular expression that is used to monitor the response
        of the connected host for errors.

        @rtype:  regex
        @return: A regular expression.
        """
        if self.manual_error_re:
            return self.manual_error_re
        return self.get_driver().error_re

    def set_login_error_prompt(self, error = None):
        """
        Defines a pattern that is used to monitor the response of the
        connected host during the authentication procedure.
        If the pattern matches an error is raised.

        @type  error: RegEx
        @param error: The pattern that, when matched, causes an error.
        """
        if error is None:
            self.manual_login_error_re = error
        else:
            self.manual_login_error_re = to_regexs(error)

    def get_login_error_prompt(self):
        """
        Returns the regular expression that is used to monitor the response
        of the connected host for login errors; this is only used during
        the login procedure, i.e. authenticate() or authorize().

        @rtype:  regex
        @return: A regular expression.
        """
        if self.manual_login_error_re:
            return self.manual_login_error_re
        return self.get_driver().login_error_re

    def set_timeout(self, timeout):
        """
        Defines the maximum time that the adapter waits before a call to 
        expect() or expect_prompt() fails.

        @type  timeout: int
        @param timeout: The maximum time in seconds.
        """
        self.timeout = int(timeout)

    def get_timeout(self):
        """
        Returns the current timeout in seconds.

        @rtype:  int
        @return: The timeout in seconds.
        """
        return self.timeout

    def connect(self, hostname = None, port = None):
        """
        Opens the connection to the remote host or IP address.

        @type  hostname: string
        @param hostname: The remote host or IP address.
        @type  port: int
        @param port: The remote TCP port number.
        """
        if hostname is not None:
            self.host = hostname
        return self._connect_hook(self.host, port)

    def _get_account(self, account):
        if account is None:
            account = self.last_account
        if account is None:
            raise TypeError('An account is required')
        self.last_account = account
        return account

    def login(self, account = None, app_account = None, flush = True):
        """
        Log into the connected host using the best method available.
        If an account is not given, default to the account that was
        used during the last call to login(). If a previous call was not
        made, use the account that was passed to the constructor. If that
        also fails, raise a TypeError.

        The app_account is passed to app_authenticate. If app_account is
        not given, default to the value of the account argument.

        @type  account: Account
        @param account: The account for protocol level authentification.
        @type  app_account: Account
        @param app_account: The account for app level authentification.
        @type  flush: bool
        @param flush: Whether to flush the last prompt from the buffer.
        """
        account = self._get_account(account)
        if app_account is None:
            app_account = account

        self.authenticate(account, flush = False)
        self.app_authenticate(app_account, flush = False)
        self.auto_app_authorize(app_account, flush = flush)

    def _protocol_authenticate(self, user, password):
        pass

    def _protocol_authenticate_by_key(self, user, key):
        pass

    def authenticate(self, account = None, flush = True):
        """
        Low-level API to perform protocol-level authentification on protocols
        that support it.

        @note In most cases, you want to use the login() method instead, as
           it automatically chooses the best login method for each protocol.

        @type  account: Account
        @param account: An account object, like login().
        @type  flush: bool
        @param flush: Whether to expect/flush the first prompt.
        """
        account  = self._get_account(account)
        user     = account.get_name()
        password = account.get_password()
        key      = account.get_key()
        if key is None:
            self._dbg(1, "Attempting to authenticate %s." % user)
            self._protocol_authenticate(user, password)
        else:
            self._dbg(1, "Authenticate %s with key." % user)
            self._protocol_authenticate_by_key(user, key)
        self.authenticated = True
        if flush:
            self.expect_prompt()

    def is_authenticated(self):
        """
        Returns True if the protocol-level authentication procedure was
        completed, False otherwise.

        @rtype:  bool
        @return: Whether the authentication was completed.
        """
        return self.authenticated

    def _app_authenticate(self, user, password, flush = True):
        while True:
            # Wait for any prompt. Once a match is found, we need to be able
            # to find out which type of prompt was matched, so we build a
            # structure to allow for mapping the match index back to the
            # prompt type.
            prompts = (('login-error', self.get_login_error_prompt()),
                       ('username',    self.get_username_prompt()),
                       ('skey',        [_skey_re]),
                       ('password',    self.get_password_prompt()),
                       ('cli',         self.get_prompt()))
            prompt_map  = []
            prompt_list = []
            for section, sectionprompts in prompts:
                for prompt in sectionprompts:
                    prompt_map.append((section, prompt))
                    prompt_list.append(prompt)

            # Wait for the prompt.
            try:
                index, match = self._waitfor(prompt_list)
            except TimeoutException:
                if self.response is None:
                    self.response = ''
                msg = "Buffer: %s" % repr(self.response)
                raise TimeoutException(msg)
            except DriverReplacedException:
                # Driver replaced, retry.
                self._dbg(1, 'Transport.app_authenticate(): driver replaced')
                continue
            except ExpectCancelledException:
                self._dbg(1, 'Transport.app_authenticate(): expect cancelled')
                raise
            except EOFError:
                self._dbg(1, 'Transport.app_authenticate(): EOF')
                raise

            # Login error detected.
            section, prompt = prompt_map[index]
            if section == 'login-error':
                raise LoginFailure("Login failed")

            # User name prompt.
            elif section == 'username':
                self._dbg(1, "Username prompt %s received." % index)
                self.expect(prompt) # consume the prompt from the buffer
                self.send(user + '\r')
                continue

            # s/key prompt.
            elif section == 'skey':
                self._dbg(1, "S/Key prompt received.")
                self.expect(prompt) # consume the prompt from the buffer
                seq  = int(match.group(1))
                seed = match.group(2)
                self._otp_cb(seq, seed)
                self._dbg(2, "Seq: %s, Seed: %s" % (seq, seed))
                phrase = otp(password, seed, seq)

                # A password prompt is now required.
                self.expect(self.get_password_prompt())
                self.send(phrase + '\r')
                self._dbg(1, "Password sent.")
                continue

            # Cleartext password prompt.
            elif section == 'password':
                self._dbg(1, "Cleartext password prompt received.")
                self.expect(prompt) # consume the prompt from the buffer
                self.send(password + '\r')
                continue

            # Shell prompt.
            elif section == 'cli':
                self._dbg(1, 'Shell prompt received.')
                break

            else:
                assert False # No such section

        if flush:
            self.expect_prompt()

    def app_authenticate(self, account = None, flush = True):
        """
        Attempt to perform application-level authentication. Application
        level authentication is needed on devices where the username and
        password are requested from the user after the connection was
        already accepted by the remote device.

        The difference between app-level authentication and protocol-level
        authentication is that in the latter case, the prompting is handled
        by the client, whereas app-level authentication is handled by the
        remote device.

        App-level authentication comes in a large variety of forms, and
        while this method tries hard to support them all, there is no
        guarantee that it will always work.

        We attempt to smartly recognize the user and password prompts;
        for a list of supported operating systems please check the
        Exscript.protocols.drivers module.

        Returns upon finding the first command line prompt. Depending
        on whether the flush argument is True, it also removes the
        prompt from the incoming buffer.

        @type  account: Account
        @param account: An account object, like login().
        @type  flush: bool
        @param flush: Whether to flush the last prompt from the buffer.
        """
        account  = self._get_account(account)
        user     = account.get_name()
        password = account.get_password()
        self._dbg(1, "Attempting to app-authenticate %s." % user)
        self._app_authenticate(user, password, flush)
        self.app_authenticated = True

    def is_app_authenticated(self):
        """
        Returns True if the application-level authentication procedure was
        completed, False otherwise.

        @rtype:  bool
        @return: Whether the authentication was completed.
        """
        return self.app_authenticated

    def app_authorize(self, account = None, flush = True):
        """
        Like app_authenticate(), but uses the authorization password
        of the account.

        For the difference between authentication and authorization 
        please google for AAA.

        @type  account: Account
        @param account: An account object, like login().
        @type  flush: bool
        @param flush: Whether to flush the last prompt from the buffer.
        """
        account  = self._get_account(account)
        user     = account.get_name()
        password = account.get_authorization_password()
        if password is None:
            password = account.get_password()
        self._dbg(1, "Attempting to app-authorize %s." % user)
        self._app_authenticate(user, password, flush)
        self.app_authorized = True

    def auto_app_authorize(self, account = None, flush = True):
        """
        Like authorize(), but instead of just waiting for a user or
        password prompt, it automatically initiates the authorization
        procedure by sending a driver-specific command.

        In the case of devices that understand AAA, that means sending
        a command to the device. For example, on routers running Cisco
        IOS, this command executes the 'enable' command before expecting
        the password.

        In the case of a device that is not recognized to support AAA, this
        method does nothing.

        @type  password: string
        @param password: The plain password.
        @type  flush: bool
        @param flush: Whether to flush the last prompt from the buffer.
        """
        account = self._get_account(account)
        self._dbg(1, 'Calling driver.auto_authorize().')
        self.get_driver().auto_authorize(self, account, flush)

    def is_app_authorized(self):
        """
        Returns True if the application-level authorization procedure was
        completed, False otherwise.

        @rtype:  bool
        @return: Whether the authorization was completed.
        """
        return self.app_authorized

    def send(self, data):
        """
        Sends the given data to the remote host.
        Returns without waiting for a response.

        @type  data: string
        @param data: The data that is sent to the remote host.
        @rtype:  Boolean
        @return: True on success, False otherwise.
        """
        raise NotImplementedError()

    def execute(self, command):
        """
        Sends the given data to the remote host (with a newline appended) 
        and waits for a prompt in the response. The prompt attempts to use 
        a sane default that works with many devices running Unix, IOS, 
        IOS-XR, or Junos and others. If that fails, a custom prompt may 
        also be defined using the set_prompt() method.
        This method also modifies the value of the response (self.response) 
        attribute, for details please see the documentation of the 
        expect() method.

        @type  command: string
        @param command: The data that is sent to the remote host.
        """
        self.send(command + '\r')
        return self.expect_prompt()

    def _waitfor(self, prompt):
        result = self._domatch(to_regexs(prompt), False)
        self.os_guesser.response_received()
        return result

    def waitfor(self, prompt):
        """
        Monitors the data received from the remote host and waits until 
        the response matches the given prompt.
        Once a match has been found, the buffer containing incoming data
        is NOT changed. In other words, consecutive calls to this function
        will always work, e.g.::

            conn.waitfor('myprompt>')
            conn.waitfor('myprompt>')
            conn.waitfor('myprompt>')

        will always work. Hence in most cases, you probably want to use
        expect() instead.

        This method also stores the received data in the response 
        attribute (self.response).

        Returns the index of the regular expression that matched.

        Raises TimeoutException if the timeout was reached.
        Raises ExpectCancelledException if cancel_expect() was called
        through a callback.

        @type  prompt: str|re.RegexObject|list(str|re.RegexObject)
        @param prompt: One or more regular expressions.
        @rtype:  int, re.MatchObject
        @return: The index of the regular expression that matched,
          and the match object.
        """
        while True:
            try:
                result = self._waitfor(prompt)
            except DriverReplacedException:
                continue # retry
            return result

    def _expect(self, prompt):
        result = self._domatch(to_regexs(prompt), True)
        self.os_guesser.response_received()
        return result

    def expect(self, prompt):
        """
        Like waitfor(), but also removes the matched string from the buffer
        containing the incoming data. In other words, the following may not
        alway complete::

            conn.expect('myprompt>')
            conn.expect('myprompt>') # timeout

        Returns the index of the regular expression that matched.

        Raises TimeoutException if the timeout was reached.
        Raises ExpectCancelledException if cancel_expect() was called
        through a callback.

        @type  prompt: str|re.RegexObject|list(str|re.RegexObject)
        @param prompt: One or more regular expressions.
        @rtype:  int, re.MatchObject
        @return: The index of the regular expression that matched,
          and the match object.
        """
        while True:
            try:
                result = self._expect(prompt)
            except DriverReplacedException:
                continue # retry
            return result

    def expect_prompt(self):
        """
        Monitors the data received from the remote host and waits for a 
        prompt in the response. The prompt attempts to use 
        a sane default that works with many devices running Unix, IOS, 
        IOS-XR, or Junos and others. If that fails, a custom prompt may 
        also be defined using the set_prompt() method.
        This method also stores the received data in the response 
        attribute (self.response).
        """
        self.expect(self.get_prompt())

        # We skip the first line because it contains the echo of the command
        # sent.
        self._dbg(5, "Checking %s for errors" % repr(self.response))
        for line in self.response.split('\n')[1:]:
            match = None
            for prompt in self.get_error_prompt():
                if prompt.match(line):
                    break
            if match is None:
                continue
            error = repr(prompt.pattern)
            self._dbg(5, "error prompt (%s) matches %s" % (error, repr(line)))
            raise InvalidCommandException('Device said:\n' + self.response)

    def cancel_expect(self):
        """
        Cancel the current call to expect() as soon as control returns
        to the protocol adapter. This method may be used in callbacks to
        the events emitted by this class, e.g. Transport.data_received_event.
        """
        raise NotImplementedError()

    def close(self, force = False):
        """
        Closes the connection with the remote host.
        """
        raise NotImplementedError()

    def get_host(self):
        """
        Returns the name or address of the currently connected host.

        @rtype:  string
        @return: A name or an address.
        """
        return self.host

    def guess_os(self):
        """
        Returns an identifer that specifies the operating system that is 
        running on the remote host. This OS is obtained by watching the 
        response of the remote host, such as any messages retrieved during 
        the login procedure.

        The OS is also a wild guess that often depends on volatile 
        information, so there is no guarantee that this will always work.

        @rtype:  string
        @return: A string to help identify the remote operating system.
        """
        return self.os_guesser.get('os')
