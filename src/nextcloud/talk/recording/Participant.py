#
# SPDX-FileCopyrightText: 2023 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""
Module to join a call with a browser.
"""

import hashlib
import hmac
import json
import re
import threading
from datetime import datetime
from secrets import token_urlsafe
from shutil import disk_usage
from time import sleep

import websocket
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeDriver
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.webdriver import WebDriver as FirefoxDriver
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from .Config import config


class BiDiLogsHelper:
    """
    Helper class to get browser logs using the BiDi protocol.

    A new thread is started by each object to receive the logs, so they can be
    printed in real time even if the main thread is waiting for some script to
    finish.
    """

    def __init__(self, driver, parentLogger):
        if not 'webSocketUrl' in driver.capabilities:
            raise Exception('webSocketUrl not found in capabilities')

        self._logger = parentLogger.getChild('BiDiLogsHelper')

        self.realtimeLogsEnabled = False
        self.pendingLogs = []
        self.logsLock = threading.Lock()

        # Web socket connection is rejected by Firefox with "Bad request" if
        # "Origin" header is present; logs show:
        # "The handshake request has incorrect Origin header".
        self.websocket = websocket.create_connection(driver.capabilities['webSocketUrl'], suppress_origin=True)

        self.websocket.send(json.dumps({
            'id': 1,
            'method': 'session.subscribe',
            'params': {
                'events': ['log.entryAdded'],
            },
        }))

        self.initialLogsLock = threading.Lock()
        # pylint: disable=consider-using-with
        self.initialLogsLock.acquire()

        self.loggingThread = threading.Thread(target=self._processLogEvents, daemon=True)
        self.loggingThread.start()

        # Do not return until the existing logs were fetched, except if it is
        # taking too long.
        # pylint: disable=consider-using-with
        self.initialLogsLock.acquire(timeout=10)

    def __del__(self):
        if self.websocket:
            self.websocket.close()

        if self.loggingThread:
            self.loggingThread.join()

    def _messageFromEvent(self, event):
        if not 'params' in event:
            return '???'

        method = ''
        if 'method' in event['params']:
            method = event['params']['method']
        elif 'level' in event['params']:
            method = event['params']['level'] if event['params']['level'] != 'warning' else 'warn'

        text = ''
        if 'text' in event['params']:
            text = event['params']['text']

        time = '??:??:??'
        if 'timestamp' in event['params']:
            timestamp = event['params']['timestamp']

            # JavaScript timestamps are millisecond based, Python timestamps
            # are second based.
            time = datetime.fromtimestamp(timestamp / 1000).strftime('%H:%M:%S')

        methodShort = '?'
        if method == 'error':
            methodShort = 'E'
        elif method == 'warn':
            methodShort = 'W'
        elif method == 'log':
            methodShort = 'L'
        elif method == 'info':
            methodShort = 'I'
        elif method == 'debug':
            methodShort = 'D'

        return time + ' ' + methodShort + ' ' + text

    def _processLogEvents(self):
        while True:
            try:
                event = json.loads(self.websocket.recv())
            except:
                self._logger.debug('BiDi WebSocket closed')
                return

            if 'id' in event and event['id'] == 1:
                self.initialLogsLock.release()
                continue

            if not 'method' in event or event['method'] != 'log.entryAdded':
                continue

            message = self._messageFromEvent(event)

            with self.logsLock:
                if self.realtimeLogsEnabled:
                    self._logger.debug(message)
                else:
                    self.pendingLogs.append(message)

    def clearLogs(self):
        """
        Clears, without printing, the logs received while realtime logs were not
        enabled.
        """

        with self.logsLock:
            self.pendingLogs = []

    def printLogs(self):
        """
        Prints the logs received while realtime logs were not enabled.

        The logs are cleared after printing them.
        """

        with self.logsLock:
            for log in self.pendingLogs:
                self._logger.debug(log)

            self.pendingLogs = []

    def setRealtimeLogsEnabled(self, realtimeLogsEnabled):
        """
        Enable or disable realtime logs.

        If logs are received while realtime logs are not enabled they can be
        printed using "printLogs()".
        """

        with self.logsLock:
            self.realtimeLogsEnabled = realtimeLogsEnabled


class SeleniumHelper:
    """
    Helper class to start a browser and execute scripts in it using WebDriver.

    The browser is expected to be available in the local system.
    """

    def __init__(self, parentLogger, acceptInsecureCerts):
        self._parentLogger = parentLogger
        self._logger = parentLogger.getChild('SeleniumHelper')

        self._acceptInsecureCerts = acceptInsecureCerts

        self.driver = None
        self.bidiLogsHelper = None

    def __del__(self):
        if self.driver:
            # The session must be explicitly quit to remove the temporary files
            # created in "/tmp".
            self.driver.quit()

    def startChrome(self, width, height, env, driverPath, browserPath):
        """
        Starts a Chrome instance.

        Will use Chromium if Google Chrome is not installed.

        :param width: the width of the browser window.
        :param height: the height of the browser window.
        :param env: the environment variables, including the display to start
                    the browser in.
        :param driverPath: the path to override the default chromedriver.
        :param browserPath: the path to override the default Google Chrome or
                            Chromium executable.
        """

        options = ChromeOptions()

        options.set_capability('acceptInsecureCerts', self._acceptInsecureCerts)

        # "webSocketUrl" is needed for BiDi.
        options.set_capability('webSocketUrl', True)

        options.add_argument('--use-fake-ui-for-media-stream')

        # Allow to play media without user interaction.
        options.add_argument('--autoplay-policy=no-user-gesture-required')

        options.add_argument('--kiosk')
        options.add_argument(f'--window-size={width},{height}')
        options.add_argument('--disable-infobars')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        if disk_usage('/dev/shm').free < 2147483648:
            self._logger.info('Less than 2 GiB available in "/dev/shm", usage disabled')
            options.add_argument("--disable-dev-shm-usage")

        if disk_usage('/tmp').free < 134217728:
            self._logger.warning('Less than 128 MiB available in "/tmp", strange failures may occur')

        service = ChromeService(
            env=env,
            executable_path=driverPath,
        )

        if browserPath:
            options.binary_location = browserPath

        self.driver = ChromeDriver(
            options=options,
            service=service,
        )

        self.bidiLogsHelper = BiDiLogsHelper(self.driver, self._parentLogger)

    def startFirefox(self, width, height, env, driverPath, browserPath):
        """
        Starts a Firefox instance.

        :param width: the width of the browser window.
        :param height: the height of the browser window.
        :param env: the environment variables, including the display to start
                    the browser in.
        :param driverPath: the path to override the default geckodriver.
        :param browserPath: the path to override the default Firefox executable.
        """

        options = FirefoxOptions()

        options.set_capability('acceptInsecureCerts', self._acceptInsecureCerts)

        # "webSocketUrl" is needed for BiDi; this should be set already by
        # default, but just in case.
        options.set_capability('webSocketUrl', True)
        # In Firefox < 101 BiDi protocol was not enabled by default, although it
        # works fine for getting the logs with Firefox 99, so it is explicitly
        # enabled.
        # https://bugzilla.mozilla.org/show_bug.cgi?id=1753997
        options.set_preference('remote.active-protocols', 3)

        options.set_preference('media.navigator.permission.disabled', True)

        # Allow to play media without user interaction.
        options.set_preference('media.autoplay.default', 0)

        options.add_argument('--kiosk')
        options.add_argument(f'--width={width}')
        options.add_argument(f'--height={height}')

        if disk_usage('/tmp').free < 134217728:
            self._logger.warning('Less than 128 MiB available in "/tmp", strange failures may occur')

        service = FirefoxService(
            env=env,
            executable_path=driverPath,
        )

        if browserPath:
            options.binary_location = browserPath

        self.driver = FirefoxDriver(
            options=options,
            service=service,
        )

        self.bidiLogsHelper = BiDiLogsHelper(self.driver, self._parentLogger)

    def clearLogs(self):
        """
        Clears browser logs not printed yet.

        This does not affect the logs in the browser itself, only the ones
        received by the SeleniumHelper.
        """

        if self.bidiLogsHelper:
            self.bidiLogsHelper.clearLogs()
            return

        self.driver.get_log('browser')

    def printLogs(self):
        """
        Prints browser logs received since last print.

        These logs do not include realtime logs, as they are printed as soon as
        they are received.
        """

        if self.bidiLogsHelper:
            self.bidiLogsHelper.printLogs()
            return

        for log in self.driver.get_log('browser'):
            self._logger.debug(log['message'])

    def printConsoleLog(self):
        msgs = []
        for log in self.driver.get_log('browser'):
            msg = log['message']
            self._logger.debug(msg)
            msgs.append(msg)
        return msgs

    def execute(self, script):
        """
        Executes the given script.

        If the script contains asynchronous code "executeAsync()" should be used
        instead to properly wait until the asynchronous code finished before
        returning.

        Technically Chrome (unlike Firefox) works as expected with something
        like "execute('await someFunctionCall(); await anotherFunctionCall()'",
        but "executeAsync" has to be used instead for something like
        "someFunctionReturningAPromise().then(() => { more code })").

        If realtime logs are available logs are printed as soon as they are
        received. Otherwise they will be printed once the script has finished.

        The value returned by the script will be in turn returned by this
        function; the type will be respected and adjusted as needed (so a
        JavaScript string is returned as a Python string, but a JavaScript
        object is returned as a Python dict). If nothing is returned by the
        script None will be returned.

        :return: the value returned by the script, or None
        """

        # Real time logs are enabled while the command is being executed.
        if self.bidiLogsHelper:
            self.printLogs()
            self.bidiLogsHelper.setRealtimeLogsEnabled(True)

        result = self.driver.execute_script(script)

        if self.bidiLogsHelper:
            # Give it some time to receive the last real time logs before
            # disabling them again.
            sleep(0.5)

            self.bidiLogsHelper.setRealtimeLogsEnabled(False)

        self.printLogs()

        return result

    def executeAsync(self, script):
        """
        Executes the given script asynchronously.

        This function should be used to execute JavaScript code that needs to
        wait for a promise to be fulfilled, either explicitly or through "await"
        calls.

        The script needs to explicitly signal that the execution has finished by
        calling "returnResolve()" (with or without a parameter). If
        "returnResolve()" is not called (no matter if with or without a
        parameter) the function will automatically return once all the root
        statements of the script were executed (which works as expected if using
        "await" calls, but not if the script includes something like
        "someFunctionReturningAPromise().then(() => { more code })"; in that
        case the script should be written as
        "someFunctionReturningAPromise().then(() => { more code; returnResolve() })").

        Similarly, exceptions thrown by a root statement (including "await"
        calls) will be propagated to the Python function. However, this does not
        work if the script includes something like
        "someFunctionReturningAPromise().catch(exception => { more code; throw exception })";
        in that case the script should be written as
        "someFunctionReturningAPromise().catch(exception => { more code; returnReject(exception) })".

        If realtime logs are available logs are printed as soon as they are
        received. Otherwise they will be printed once the script has finished.

        The value returned by the script will be in turn returned by this
        function; the type will be respected and adjusted as needed (so a
        JavaScript string is returned as a Python string, but a JavaScript
        object is returned as a Python dict). If nothing is returned by the
        script None will be returned.

        Note that the value returned by the script must be explicitly specified
        by calling "returnResolve(XXX)"; it is not possible to use "return XXX".

        :return: the value returned by the script, or None
        """

        # Real time logs are enabled while the command is being executed.
        if self.bidiLogsHelper:
            self.printLogs()
            self.bidiLogsHelper.setRealtimeLogsEnabled(True)

        # Add an explicit return point at the end of the script if none is
        # given.
        if re.search('returnResolve\\(.*\\)', script) is None:
            script += '; returnResolve()'

        # await is not valid in the root context in Firefox, so the script to be
        # executed needs to be wrapped in an async function.
        # Asynchronous scripts need to explicitly signal that they are finished
        # by invoking the callback injected as the last argument with a promise
        # and resolving or rejecting the promise.
        # https://w3c.github.io/webdriver/#dfn-execute-async-script
        script = 'promise = new Promise(async(returnResolve, returnReject) => { try { ' + script + ' } catch (exception) { returnReject(exception) } }); arguments[arguments.length - 1](promise)'

        result = self.driver.execute_async_script(script)

        if self.bidiLogsHelper:
            # Give it some time to receive the last real time logs before
            # disabling them again.
            sleep(0.5)

            self.bidiLogsHelper.setRealtimeLogsEnabled(False)

        self.printLogs()

        return result


class Participant():
    """
    Wrapper for a real participant in Talk.

    This wrapper exposes functions to use a real participant in a browser.
    """

    def __init__(self, browser, nextcloudUrl, width, height, env, driverPath, browserPath, parentLogger):
        """
        Starts a real participant in the given Nextcloud URL using the given
        browser.

        :param browser: currently only "firefox" is supported.
        :param nextcloudUrl: the URL of the Nextcloud instance to start the real
            participant in.
        :param width: the width of the browser window.
        :param height: the height of the browser window.
        :param env: the environment variables, including the display to start
                    the browser in.
        :param driverPath: the path to override the default Selenium driver.
        :param browserPath: the path to override the default browser executable.
        :param parentLogger: the parent logger to get a child from.
        """

        # URL should not contain a trailing '/', as that could lead to a double
        # '/' which may prevent Talk UI from loading as expected.
        self.nextcloudUrl = nextcloudUrl.rstrip('/')

        acceptInsecureCerts = config.getBackendSkipVerify(self.nextcloudUrl)

        self.seleniumHelper = SeleniumHelper(parentLogger, acceptInsecureCerts)
        self._logger = parentLogger
        if browser == 'chrome':
            self.seleniumHelper.startChrome(width, height, env, driverPath, browserPath)
        elif browser == 'firefox':
            self.seleniumHelper.startFirefox(width, height, env, driverPath, browserPath)
        else:
            raise Exception('Invalid browser: ' + browser)

    def joinCall(self, token):
        """
        Joins the call in the room with the given token.

        The participant will join as an internal client of the signaling server.

        :param token: the token of the room to join.
        """

        self.seleniumHelper.driver.get(self.nextcloudUrl + '/index.php/call/' + token + '/recording')

        secret = config.getBackendSecret(self.nextcloudUrl)
        if secret is None:
            raise Exception(f"No configured backend secret for {self.nextcloudUrl}")

        random = token_urlsafe(64)
        hmacValue = hmac.new(secret.encode(), random.encode(), hashlib.sha256)

        # If there are several signaling servers configured in Nextcloud the
        # signaling settings can change between different calls, so they need to
        # be got just once. The scripts are executed in their own scope, so
        # values have to be stored in the window object to be able to use them
        # later in another script.
        settings = self.seleniumHelper.executeAsync(f'''
            window.signalingSettings = await OCA.Talk.signalingGetSettingsForRecording('{token}', '{random}', '{hmacValue.hexdigest()}')
            returnResolve(window.signalingSettings)
        ''')

        secret = config.getSignalingSecret(settings['server'])
        if secret is None:
            raise Exception(f"No configured signaling secret for {settings['server']}")

        random = token_urlsafe(64)
        hmacValue = hmac.new(secret.encode(), random.encode(), hashlib.sha256)

        self.seleniumHelper.executeAsync(f'''
            await OCA.Talk.signalingJoinCallForRecording(
                '{token}',
                window.signalingSettings,
                {{
                    random: '{random}',
                    token: '{hmacValue.hexdigest()}',
                    backend: '{self.nextcloudUrl}',
                }}
            )
        ''')

    def disconnect(self):
        """
        Disconnects from the signaling server.
        """

        self.seleniumHelper.execute('''
            OCA.Talk.signalingKill()
        ''')

    def startMonitoringSpeaking(self):
        """
        Starts monitoring who is speaking in the call using direct Nextcloud Talk internal handlers.
        Optimized for speaker mode (no microphone).
        """

        js_code = '''
        (function() {
            // Инициализируем массивы для событий
            if (!window.speakingEvents) {
                window.speakingEvents = [];
            }
            window.lastProcessedEventIndex = 0;

            // ============================================
            // ОСНОВНОЙ МЕТОД ДЛЯ SPEAKER РЕЖИМА
            // ============================================

            // 1. Мониторинг через WebSocket (основной источник в speaker режиме)
            function setupWebSocketInterceptor() {
                if (window.WebSocket && !window._websocketIntercepted) {
                    window._websocketIntercepted = true;
                    const OriginalWebSocket = window.WebSocket;

                    window.WebSocket = function(...args) {
                        const ws = new OriginalWebSocket(...args);
                        const url = args[0];

                        ws.addEventListener('message', function(event) {
                            try {
                                const data = JSON.parse(event.data);

                                // Логируем все сообщения для отладки
                                if (data.type === 'message' || data.type === 'event') {
                                    console.log('[WS]', data.type, data);
                                }

                                // Проверяем наличие speaking данных
                                if (data.type === 'speaking') {
                                    window.speakingEvents.push({
                                        timestamp: Date.now(),
                                        type: 'websocket_speaking',
                                        data: data,
                                        source: 'websocket'
                                    });
                                    console.log('[SPEAKING] WebSocket speaking event:', data);
                                }

                                // Проверяем users-changed события (содержат speaking статус)
                                if (data.type === 'users-changed' || (data.data && data.data.users)) {
                                    const users = data.data?.users || data.users || [];
                                    const speakingUsers = users.filter(u => u.speaking === true);
                                    if (speakingUsers.length > 0) {
                                        window.speakingEvents.push({
                                            timestamp: Date.now(),
                                            type: 'websocket_users_changed',
                                            speakingUsers: speakingUsers,
                                            allUsers: users,
                                            source: 'websocket'
                                        });
                                        console.log('[SPEAKING] Users changed with speakers:', speakingUsers);
                                    }
                                }
                            } catch(e) {}
                        });

                        return ws;
                    };

                    window.WebSocket.prototype = OriginalWebSocket.prototype;
                    console.log('[SPEAKING MONITOR] WebSocket interceptor installed for speaker mode');
                    return true;
                }
                return false;
            }

            // 2. Мониторинг через store polling (fallback)
            function setupStorePolling() {
                if (!window._storePollingInstalled) {
                    window._storePollingInstalled = true;

                    let lastSpeakingState = {};

                    window.storePollingInterval = setInterval(() => {
                        const store = window.globalThis?.store;
                        if (store && store.state?.participantsStore) {
                            const speaking = store.state.participantsStore.speaking || {};
                            const attendees = store.state.participantsStore.attendees || {};
                            const token = window.location.pathname.match(/\\/call\\/([^\\/?#]+)/)?.[1];

                            const currentSpeaking = {};
                            for (const [id, info] of Object.entries(speaking)) {
                                if (info.speaking === true) {
                                    const attendee = attendees[token]?.[id] || {};
                                    currentSpeaking[id] = {
                                        id: id,
                                        name: attendee.displayName || attendee.actorId || 'Unknown',
                                        speaking: info.speaking,
                                        totalTimeMs: info.totalCountedTime || 0,
                                        timestamp: info.lastTimestamp
                                    };
                                }
                            }

                            const currentJson = JSON.stringify(currentSpeaking);
                            const lastJson = JSON.stringify(lastSpeakingState);

                            if (currentJson !== lastJson && Object.keys(currentSpeaking).length > 0) {
                                window.speakingEvents.push({
                                    timestamp: Date.now(),
                                    type: 'store_polling',
                                    speakingState: currentSpeaking,
                                    previousState: lastSpeakingState,
                                    source: 'store'
                                });
                                console.log('[SPEAKING] Store polling detected speakers:', Object.keys(currentSpeaking));
                                lastSpeakingState = currentSpeaking;
                            } else if (Object.keys(currentSpeaking).length === 0 && Object.keys(lastSpeakingState).length > 0) {
                                // Все замолчали
                                window.speakingEvents.push({
                                    timestamp: Date.now(),
                                    type: 'store_polling',
                                    speakingState: {},
                                    previousState: lastSpeakingState,
                                    source: 'store'
                                });
                                lastSpeakingState = {};
                            }
                        }
                    }, 500);

                    console.log('[SPEAKING MONITOR] Store polling installed');
                    return true;
                }
                return false;
            }

            // 3. Мониторинг через DOM (визуальные индикаторы)
            function setupDOMObserver() {
                if (!window._domObserverInstalled) {
                    window._domObserverInstalled = true;

                    let lastSpeakers = [];

                    window.domPollingInterval = setInterval(() => {
                        // Ищем говорящих через CSS классы
                        const currentSpeakers = [];

                        // Метод 1: через класс 'speaking'
                        document.querySelectorAll('.speaking, [class*="speaking-true"], [data-speaking="true"]').forEach(el => {
                            const nameEl = el.querySelector('.video-name, .participant-name, [class*="name"]');
                            if (nameEl) {
                                currentSpeakers.push(nameEl.textContent.trim());
                            }
                        });

                        // Метод 2: через границы видео (стиль)
                        document.querySelectorAll('.video, .video-container').forEach(video => {
                            const borderColor = window.getComputedStyle(video).borderColor;
                            if (borderColor === 'rgb(0, 160, 210)' || borderColor === '#00a0d2') {
                                const nameEl = video.querySelector('.video-name, .participant-name');
                                if (nameEl) {
                                    const name = nameEl.textContent.trim();
                                    if (!currentSpeakers.includes(name)) {
                                        currentSpeakers.push(name);
                                    }
                                }
                            }
                        });

                        if (JSON.stringify(currentSpeakers) !== JSON.stringify(lastSpeakers) && currentSpeakers.length > 0) {
                            window.speakingEvents.push({
                                timestamp: Date.now(),
                                type: 'dom_speaking',
                                speakers: currentSpeakers,
                                previous: lastSpeakers,
                                source: 'dom'
                            });
                            console.log('[SPEAKING] DOM detected speakers:', currentSpeakers);
                            lastSpeakers = currentSpeakers;
                        }
                    }, 300);

                    console.log('[SPEAKING MONITOR] DOM observer installed');
                    return true;
                }
                return false;
            }

            // 4. Прямой доступ к signaling (если доступен)
            function setupSignalingAccess() {
                if (window.OCA?.Talk?.signaling) {
                    console.log('[SPEAKING] OCA.Talk.signaling available');

                    // Сохраняем оригинальный обработчик
                    const signaling = window.OCA.Talk.signaling;

                    if (signaling._speakingMonitored) return;
                    signaling._speakingMonitored = true;

                    // Перехватываем метод обработки сообщений
                    const originalOnMessage = signaling._onMessage;
                    if (originalOnMessage) {
                        signaling._onMessage = function(message) {
                            if (message.type === 'speaking' || (message.data && message.data.speaking)) {
                                window.speakingEvents.push({
                                    timestamp: Date.now(),
                                    type: 'signaling_direct',
                                    message: message,
                                    source: 'signaling'
                                });
                            }
                            return originalOnMessage.call(this, message);
                        };
                    }
                }
            }

            // 5. Периодическая проверка активных участников
            function setupParticipantsPolling() {
                if (!window._participantsPollingInstalled) {
                    window._participantsPollingInstalled = true;

                    window.participantsPollingInterval = setInterval(() => {
                        const store = window.globalThis?.store;
                        if (store && store.state?.participantsStore) {
                            const inCall = store.state.participantsStore.inCall || {};
                            const token = window.location.pathname.match(/\\/call\\/([^\\/?#]+)/)?.[1];
                            const roomInCall = inCall[token] || {};

                            // Проверяем, есть ли кто-то в звонке
                            const participantsInCall = Object.keys(roomInCall).filter(sid => roomInCall[sid] !== 0);

                            if (participantsInCall.length > 0 && !window._hasLoggedParticipants) {
                                window._hasLoggedParticipants = true;
                                window.speakingEvents.push({
                                    timestamp: Date.now(),
                                    type: 'participants_in_call',
                                    count: participantsInCall.length,
                                    sessions: participantsInCall,
                                    source: 'store'
                                });
                                console.log('[SPEAKING] Participants in call:', participantsInCall.length);
                            }
                        }
                    }, 2000);
                }
            }

            // ============================================
            // ЗАПУСК
            // ============================================

            function installAll() {
                console.log('[SPEAKING MONITOR] Installing interceptors for speaker mode...');

                setupWebSocketInterceptor();
                setupStorePolling();
                setupDOMObserver();
                setupSignalingAccess();
                setupParticipantsPolling();

                window.speakingMonitorReady = true;

                // Выводим отладочную информацию
                console.log('[SPEAKING MONITOR] Store available:', !!window.globalThis?.store);
                console.log('[SPEAKING MONITOR] WebSocket available:', !!window.WebSocket);
                console.log('[SPEAKING MONITOR] OCA.Talk available:', !!window.OCA?.Talk);
            }

            installAll();
            setTimeout(installAll, 2000);
            setTimeout(installAll, 5000);

            return true;
        })();
        '''

        self.seleniumHelper.execute(js_code)

        def get_all_events():
            """Returns all captured events."""
            events_json = self.seleniumHelper.execute('''
                return window.speakingEvents || [];
            ''')
            return events_json if events_json else []

        def get_new_events():
            """Returns only new events since last call."""
            events_json = self.seleniumHelper.execute('''
                const events = window.speakingEvents || [];
                const lastIndex = window.lastProcessedEventIndex || 0;
                const newEvents = events.slice(lastIndex);
                window.lastProcessedEventIndex = events.length;
                return newEvents;
            ''')
            return events_json if events_json else []

        def stop_monitoring():
            """Stops the monitoring."""
            self.seleniumHelper.execute('''
                if (window.storePollingInterval) clearInterval(window.storePollingInterval);
                if (window.domPollingInterval) clearInterval(window.domPollingInterval);
                if (window.participantsPollingInterval) clearInterval(window.participantsPollingInterval);
                console.log('[SPEAKING MONITOR] Stopped');
            ''')
            return get_all_events()

        def get_debug_info():
            """Returns debug information."""
            debug_info = self.seleniumHelper.execute('''
                return {
                    storeAvailable: !!window.globalThis?.store,
                    webSocketIntercepted: !!window._websocketIntercepted,
                    speakingEventsCount: window.speakingEvents?.length || 0,
                    storeSpeakingState: window.globalThis?.store?.state?.participantsStore?.speaking || {},
                    inCallState: window.globalThis?.store?.state?.participantsStore?.inCall || {},
                    ocaTalkAvailable: !!window.OCA?.Talk,
                    url: window.location.href
                };
            ''')
            return debug_info

        def force_check():
            """Force check current speaking status from store."""
            status = self.seleniumHelper.execute('''
                const store = window.globalThis?.store;
                if (!store) return [];

                const speaking = store.state?.participantsStore?.speaking || {};
                const attendees = store.state?.participantsStore?.attendees || {};
                const inCall = store.state?.participantsStore?.inCall || {};
                const token = window.location.pathname.match(/\\/call\\/([^\\/?#]+)/)?.[1];

                const result = [];

                // Проверяем всех участников в звонке
                const roomInCall = inCall[token] || {};
                const participantsInCall = Object.keys(roomInCall);

                for (const [id, info] of Object.entries(speaking)) {
                    if (info.speaking === true) {
                        const attendee = attendees[token]?.[id] || {};
                        result.push({
                            attendeeId: id,
                            name: attendee.displayName || attendee.actorId || 'Unknown',
                            speaking: info.speaking,
                            totalTimeMs: info.totalCountedTime || 0,
                            totalTimeSec: ((info.totalCountedTime || 0) / 1000).toFixed(1)
                        });
                    }
                }

                return {
                    speakers: result,
                    participantsInCall: participantsInCall.length,
                    speakingCount: Object.keys(speaking).length
                };
            ''')
            return status

        def debug_speaker_mode(self):
            """Отладка speaker режима"""

            result = self.seleniumHelper.execute('''
                const store = window.globalThis?.store;
                const participantsStore = store?.state?.participantsStore;

                const token = window.location.pathname.match(/\\/call\\/([^\\/?#]+)/)?.[1];

                return {
                    url: window.location.href,
                    token: token,
                    storeAvailable: !!store,
                    participantsStoreAvailable: !!participantsStore,
                    inCall: participantsStore?.inCall?.[token] || {},
                    speaking: participantsStore?.speaking || {},
                    attendees: Object.keys(participantsStore?.attendees?.[token] || {}).length,
                    webSocketIntercepted: !!window._websocketIntercepted,
                    speakingEventsCount: window.speakingEvents?.length || 0
                };
            ''')

            self._logger.info(f"Speaker mode debug: {result}")
            return result

        return {
            'get_all': get_all_events,
            'get_new': get_new_events,
            'stop': stop_monitoring,
            'debug': get_debug_info,
            'debug_speaker': debug_speaker_mode,
            'force_check': force_check
        }
